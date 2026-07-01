"""Budget gating endpoints — #57.

  * GET    /api/budgets                         — list policies + spend
  * POST   /api/budgets                         — create/update a policy
  * GET    /api/budgets/incidents               — list incidents
  * POST   /api/budgets/incidents/{id}/ack      — acknowledge an incident

Loopback-only (Invariant 10, mirror of #25 credentials). The bridge is
expected to bind to 127.0.0.1 — if it ever lands on 0.0.0.0 (debug or
misconfiguration) the budget surface remains LAN-inaccessible.

Required ``comment`` on POST .../ack is the Paperclip pattern: every
governance decision leaves an audit-trail footprint. Empty / whitespace-
only comments return 422.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from routines.api.deps import ROUTINES_REPO
from routines.budgets import (
    BudgetPolicy,
    ScopeRef,
    delete_policy,
    list_policies,
    upsert_policy,
)
from routines.budgets.gate import (
    _applicable_scopes,
    _current_spend_by_scope,
    current_tokens_by_scope,
)
from routines.budgets.incidents import (
    AckAction,
    Incident,
    IncidentAlreadyAcknowledged,
    IncidentNotFound,
    acknowledge,
    force_clear,
    list_all_incidents,
    list_open_incidents,
)
from routines.budgets.policy import scope_id
from routines.shared import audit

log = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────────────
# Loopback-only guard (mirror of credentials.py — Invariant 10)
# ────────────────────────────────────────────────────────────────────────────


_LOOPBACK_HOSTS = frozenset({
    "127.0.0.1",
    "::1",
    "localhost",
    "testclient",  # Starlette ASGI in-process
})


def _loopback_only(request: Request) -> None:
    client_host = request.client.host if request.client else None
    if client_host not in _LOOPBACK_HOSTS:
        log.warning(
            "budgets endpoint refused non-loopback connection from %r",
            client_host,
        )
        raise HTTPException(
            status_code=403,
            detail=(
                f"budgets endpoints are loopback-only; refusing "
                f"connection from {client_host!r}"
            ),
        )


router = APIRouter(dependencies=[Depends(_loopback_only)])


# ────────────────────────────────────────────────────────────────────────────
# Request / response models
# ────────────────────────────────────────────────────────────────────────────


class ScopeRefDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["global", "provider", "workspace", "workspace_provider"]
    a: Optional[str] = None
    b: Optional[str] = None


class BudgetPolicyIn(BaseModel):
    """Inbound payload for POST /api/budgets."""

    model_config = ConfigDict(extra="forbid")

    scope: ScopeRefDTO
    cap_usd: float = Field(..., ge=0.0)
    cap_tokens: Optional[int] = Field(None, ge=0)
    period: Literal["monthly_utc"] = "monthly_utc"
    warn_pct: float = Field(80.0, ge=0.0, le=100.0)
    hard_pct: float = Field(100.0, ge=0.0, le=100.0)

    @model_validator(mode="after")
    def _reject_meaningless_zero_usd_cap(self) -> "BudgetPolicyIn":
        # F-10 (HR S-8): the gate treats ``cap_usd<=0`` as "never USD-block"
        # (the intended semantics for a token-only policy). So a bare
        # ``cap_usd=0`` policy is not a cap at all — it's a no-op row that, if
        # it OVERWRITES a real USD cap at the same scope, silently disables the
        # #57 spend gate. A token-only policy is legitimate, but ONLY when it
        # actually carries a positive token cap (the dashboard's Token Budget
        # tab always sends ``cap_usd:0`` together with ``cap_tokens:N``). Refuse
        # the degenerate zero-USD / zero-token combination; to remove a USD cap
        # the operator must DELETE the policy (explicit), not zero it.
        if self.cap_usd <= 0 and not (self.cap_tokens and self.cap_tokens > 0):
            raise ValueError(
                "cap_usd must be > 0 (a zero USD cap disables the spend gate "
                "for this scope). For a token-only budget set cap_tokens > 0; "
                "to remove a cap, DELETE the policy."
            )
        return self


class BudgetPolicyDTO(BaseModel):
    scope: ScopeRefDTO
    cap_usd: float
    cap_tokens: Optional[int] = None
    period: Literal["monthly_utc"]
    warn_pct: float
    hard_pct: float
    created: str
    last_modified: str
    current_spend_usd: float
    current_pct: float
    current_tokens: int = 0
    current_token_pct: Optional[float] = None
    incident_id: Optional[str] = None


class ListBudgetsResponse(BaseModel):
    policies: list[BudgetPolicyDTO]
    window: dict[str, str]


class IncidentDTO(BaseModel):
    id: str
    scope: ScopeRefDTO
    opened_at: str
    period_start: str
    current_pct: float
    hard_pct: float
    cap_usd: float
    current_spend_usd: float
    status: Literal["open", "acknowledged_raised", "acknowledged_paused"]
    ack_at: Optional[str] = None
    # ``ack_action`` may also surface ``force_clear`` once the leave_paused
    # escape hatch was used (see /api/budgets/incidents/{id}/force-clear).
    ack_action: Optional[Literal["raise_cap", "leave_paused", "force_clear"]] = None
    ack_new_cap_usd: Optional[float] = None
    ack_comment: Optional[str] = None


class ListIncidentsResponse(BaseModel):
    incidents: list[IncidentDTO]


class AckRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["raise_cap", "leave_paused"]
    comment: str
    new_cap_usd: Optional[float] = Field(None, ge=0.0)

    @field_validator("comment")
    @classmethod
    def _comment_nonempty(cls, v: str) -> str:
        # Required-comment is the Paperclip audit invariant. Empty or
        # whitespace-only fails 422 here so the route doesn't have to
        # repeat the check.
        if not v or not v.strip():
            raise ValueError("comment is required and must not be blank")
        return v


class ForceClearRequest(BaseModel):
    """Body for POST /api/budgets/incidents/{id}/force-clear.

    Only ``comment`` — no action choice, since the only thing force-clear
    does is end a ``leave_paused`` state without changing the cap.
    """

    model_config = ConfigDict(extra="forbid")

    comment: str

    @field_validator("comment")
    @classmethod
    def _comment_nonempty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("comment is required and must not be blank")
        return v


# ────────────────────────────────────────────────────────────────────────────
# Routes
# ────────────────────────────────────────────────────────────────────────────


@router.get("/budgets", response_model=ListBudgetsResponse)
def list_budgets_endpoint() -> ListBudgetsResponse:
    """List active policies + current spend per scope.

    Spend is aggregated against ``llm_calls.jsonl`` in the current monthly
    UTC window — same window the gate uses, so the dashboard's display
    matches what the gate would block on.
    """
    policies = list_policies()
    now = datetime.now(timezone.utc)

    # Materialise scope set for spend + token aggregation in one file scan each.
    scopes = [p.scope for p in policies]
    spend = _current_spend_by_scope(scopes, now=now) if scopes else {}
    tokens = current_tokens_by_scope(scopes, now=now) if scopes else {}

    # Map open incidents by scope so the dashboard can deep-link.
    open_by_scope = {inc.scope.id(): inc for inc in list_open_incidents()}

    dtos: list[BudgetPolicyDTO] = []
    for p in policies:
        sid = scope_id(p.scope)
        s = spend.get(sid, 0.0)
        pct = (s / p.cap_usd * 100.0) if p.cap_usd > 0 else 0.0
        t = tokens.get(sid, 0)
        tpct = (t / p.cap_tokens * 100.0) if p.cap_tokens else None
        dtos.append(BudgetPolicyDTO(
            scope=ScopeRefDTO(kind=p.scope.kind, a=p.scope.a, b=p.scope.b),
            cap_usd=p.cap_usd,
            cap_tokens=p.cap_tokens,
            period=p.period,
            warn_pct=p.warn_pct,
            hard_pct=p.hard_pct,
            created=p.created.isoformat(timespec="seconds"),
            last_modified=p.last_modified.isoformat(timespec="seconds"),
            current_spend_usd=round(s, 6),
            current_pct=round(pct, 4),
            current_tokens=t,
            current_token_pct=round(tpct, 4) if tpct is not None else None,
            incident_id=open_by_scope[sid].id if sid in open_by_scope else None,
        ))

    from routines.budgets.policy import monthly_utc_window
    month_start, until = monthly_utc_window(now)
    return ListBudgetsResponse(
        policies=dtos,
        window={
            "since": month_start.isoformat(timespec="seconds"),
            "until": until.isoformat(timespec="seconds"),
        },
    )


@router.post("/budgets", response_model=BudgetPolicyDTO, status_code=201)
def create_budget_endpoint(payload: BudgetPolicyIn) -> BudgetPolicyDTO:
    """Create or update a policy (upsert by scope id)."""
    run_id = audit.new_run_id()
    t0 = time.monotonic()

    try:
        scope = ScopeRef(kind=payload.scope.kind, a=payload.scope.a, b=payload.scope.b)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e

    now = datetime.now(timezone.utc)
    try:
        policy = BudgetPolicy(
            scope=scope,
            period=payload.period,
            warn_pct=payload.warn_pct,
            hard_pct=payload.hard_pct,
            cap_usd=payload.cap_usd,
            cap_tokens=payload.cap_tokens,
            created=now,
            last_modified=now,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e

    stored = upsert_policy(policy)

    audit.write_structured(
        actor={"type": "user", "id": "operator"},
        entity_type="budget",
        entity_id=scope_id(scope),
        action="upsert",
        routine="budgets.upsert",
        run_id=run_id,
        status="ok",
        audit_dir=ROUTINES_REPO / "runs",
        inputs={
            "scope": scope_id(scope),
            "cap_usd": payload.cap_usd,
            "cap_tokens": payload.cap_tokens,
            "warn_pct": payload.warn_pct,
            "hard_pct": payload.hard_pct,
        },
        duration_ms=int((time.monotonic() - t0) * 1000),
    )

    # Compute spend + tokens at the freshly-stored scope so the response
    # carries consistent dashboard fields (no separate fetch needed by clients).
    spend = _current_spend_by_scope([scope], now=now).get(scope_id(scope), 0.0)
    pct = (spend / stored.cap_usd * 100.0) if stored.cap_usd > 0 else 0.0
    tok = current_tokens_by_scope([scope], now=now).get(scope_id(scope), 0)
    tpct = (tok / stored.cap_tokens * 100.0) if stored.cap_tokens else None
    open_inc = next(
        (i for i in list_open_incidents() if scope_id(i.scope) == scope_id(scope)),
        None,
    )
    return BudgetPolicyDTO(
        scope=ScopeRefDTO(kind=stored.scope.kind, a=stored.scope.a, b=stored.scope.b),
        cap_usd=stored.cap_usd,
        cap_tokens=stored.cap_tokens,
        period=stored.period,
        warn_pct=stored.warn_pct,
        hard_pct=stored.hard_pct,
        created=stored.created.isoformat(timespec="seconds"),
        last_modified=stored.last_modified.isoformat(timespec="seconds"),
        current_spend_usd=round(spend, 6),
        current_pct=round(pct, 4),
        current_tokens=tok,
        current_token_pct=round(tpct, 4) if tpct is not None else None,
        incident_id=open_inc.id if open_inc else None,
    )


@router.delete("/budgets", status_code=204)
def delete_budget_endpoint(
    kind: Literal["global", "provider", "workspace", "workspace_provider"],
    a: Optional[str] = None,
    b: Optional[str] = None,
) -> None:
    """Remove a policy by scope. 404 if not found."""
    try:
        scope = ScopeRef(kind=kind, a=a, b=b)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    if not delete_policy(scope):
        raise HTTPException(
            status_code=404,
            detail=f"no policy at scope {scope_id(scope)!r}",
        )
    audit.write_structured(
        actor={"type": "user", "id": "operator"},
        entity_type="budget",
        entity_id=scope_id(scope),
        action="delete",
        routine="budgets.delete",
        run_id=audit.new_run_id(),
        status="ok",
        audit_dir=ROUTINES_REPO / "runs",
        inputs={"scope": scope_id(scope)},
    )


@router.get("/budgets/incidents", response_model=ListIncidentsResponse)
def list_incidents_endpoint(
    include_acknowledged: bool = False,
) -> ListIncidentsResponse:
    """List incidents. By default returns only open + paused (the ones
    that block the gate). Pass ``?include_acknowledged=1`` for history."""
    rows = list_all_incidents(limit=200) if include_acknowledged else list_open_incidents()
    return ListIncidentsResponse(
        incidents=[_incident_to_dto(i) for i in rows],
    )


@router.post(
    "/budgets/incidents/{incident_id}/ack",
    response_model=IncidentDTO,
)
def ack_incident_endpoint(incident_id: str, payload: AckRequest) -> IncidentDTO:
    """Ack an open incident.

      * action='raise_cap' requires ``new_cap_usd`` strictly greater than
        the policy's current cap (422 otherwise).
      * action='leave_paused' ignores ``new_cap_usd``.
      * Empty / whitespace-only ``comment`` is 422 (validator).
    """
    run_id = audit.new_run_id()
    t0 = time.monotonic()

    if payload.action == "raise_cap" and payload.new_cap_usd is None:
        raise HTTPException(
            status_code=422,
            detail="action='raise_cap' requires new_cap_usd",
        )

    try:
        inc = acknowledge(
            incident_id,
            action=payload.action,
            comment=payload.comment,
            new_cap_usd=payload.new_cap_usd,
        )
    except IncidentNotFound as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except IncidentAlreadyAcknowledged as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e

    audit.write_structured(
        actor={"type": "user", "id": "operator"},
        entity_type="budget",
        entity_id=incident_id,
        action="ack",
        routine="budgets.ack",
        run_id=run_id,
        status="ok",
        audit_dir=ROUTINES_REPO / "runs",
        inputs={
            "incident_id": incident_id,
            "action": payload.action,
            "new_cap_usd": payload.new_cap_usd,
        },
        duration_ms=int((time.monotonic() - t0) * 1000),
    )

    return _incident_to_dto(inc)


@router.post(
    "/budgets/incidents/{incident_id}/force-clear",
    response_model=IncidentDTO,
)
def force_clear_incident_endpoint(
    incident_id: str, payload: ForceClearRequest,
) -> IncidentDTO:
    """Mid-period escape from a ``leave_paused`` incident.

    Operator originally picked ``leave_paused`` (kept the brake on);
    after investigation they want to flow again without changing the
    cap. This endpoint marks the incident resolved WITHOUT touching
    the policy. If spend is still over cap, the gate will mint a NEW
    incident immediately on the next call — that's by design (no
    quiet bypass; you can only un-pause).

      * 404 if the incident doesn't exist
      * 409 if the incident isn't ``acknowledged_paused`` (use
        ``/ack`` for fresh open rows; raised rows are already done)
      * 422 if comment is blank
    """
    run_id = audit.new_run_id()
    t0 = time.monotonic()
    try:
        inc = force_clear(incident_id, comment=payload.comment)
    except IncidentNotFound as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except IncidentAlreadyAcknowledged as e:
        raise HTTPException(status_code=409, detail=str(e)) from e

    audit.write_structured(
        actor={"type": "user", "id": "operator"},
        entity_type="budget",
        entity_id=incident_id,
        action="force_clear",
        routine="budgets.force_clear",
        run_id=run_id,
        status="ok",
        audit_dir=ROUTINES_REPO / "runs",
        inputs={"incident_id": incident_id},
        duration_ms=int((time.monotonic() - t0) * 1000),
    )
    return _incident_to_dto(inc)


# ────────────────────────────────────────────────────────────────────────────
# DTO helper
# ────────────────────────────────────────────────────────────────────────────


def _incident_to_dto(inc: Incident) -> IncidentDTO:
    return IncidentDTO(
        id=inc.id,
        scope=ScopeRefDTO(kind=inc.scope.kind, a=inc.scope.a, b=inc.scope.b),
        opened_at=inc.opened_at.isoformat(timespec="seconds"),
        period_start=inc.period_start.isoformat(timespec="seconds"),
        current_pct=inc.current_pct,
        hard_pct=inc.hard_pct,
        cap_usd=inc.cap_usd,
        current_spend_usd=inc.current_spend_usd,
        status=inc.status,
        ack_at=inc.ack_at.isoformat(timespec="seconds") if inc.ack_at else None,
        ack_action=inc.ack_action,  # type: ignore[arg-type]
        ack_new_cap_usd=inc.ack_new_cap_usd,
        ack_comment=inc.ack_comment,
    )
