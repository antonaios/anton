"""Routing lane-matrix endpoint (#llm-routing-postjune15 G4 -- Mission B).

A read-only surface for the dashboard's task-class tiering view: the FULL
(task_type x sensitivity) -> lane grid that ``shared.routing.pick_lane`` already
enforces, computed live from the function itself (no hardcoded mirror -> it
cannot drift from the dispatcher). SURFACE-ONLY: this exposes the existing
routing, it does NOT change it -- the ``_TASK_CLASS_DEFAULT_PROVIDER`` bias map
is untouched (D4 = surface-only).

Loopback-only, same pattern as ``skills_providers`` / ``sensitivity_overrides``.

Two §B divergences the grid makes visible (these are the TRUTH, not bugs):

  * light public/internal (triage / classification / news-bulk) -> ``claude-cli-haiku``
    (cheap CLOUD Haiku), NOT local; ``prefer_local`` is the per-skill downgrade lever.
  * ``minimax`` is the lane for generic-format public/internal work but is UNWIRED
    (no production skill routes to it yet).

The matrix is TIER-dependent (confidential routes to cloud only under
``AGENTIC_PLAN_TIER=enterprise``); ``tier`` reports which tier the grid was
computed for.
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
from typing import Annotated, Literal, Optional, get_args

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StringConstraints

from routines.api import rate_limit
from routines.api.deps import RUNS_DIR
from routines.shared import audit
from routines.shared.routing import (
    Lane,
    Sensitivity,
    TaskType,
    lane_to_model,
    pick_lane,
    plan_tier,
    plan_tier_state,
    set_plan_tier,
)
from routines.skills._runtime.llm_call_counter import current_run_id

log = logging.getLogger(__name__)

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "testclient"})


def _loopback_only(request: Request) -> None:
    client_host = request.client.host if request.client else None
    if client_host not in _LOOPBACK_HOSTS:
        log.warning("routing endpoint refused non-loopback connection from %r", client_host)
        raise HTTPException(
            status_code=403,
            detail=(
                "routing endpoints are loopback-only; refusing "
                f"connection from {client_host!r}"
            ),
        )


router = APIRouter(
    prefix="/api/routing",
    tags=["routing"],
    dependencies=[Depends(_loopback_only)],
)


class LaneInfo(BaseModel):
    lane: str
    provider: str          # ollama | claude | codex | minimax
    model: str             # qwen3:14b | opus | gpt-5 | ...
    local: bool            # True iff the lane runs on the local Ollama box


class MatrixRow(BaseModel):
    task_type: str
    # sensitivity -> the lane that (this task_type, that sensitivity) resolves to.
    cells: dict[str, LaneInfo]


class LaneMatrixResponse(BaseModel):
    # The plan tier the grid was computed for (confidential routes to cloud only
    # under enterprise). From AGENTIC_PLAN_TIER (default "bridge").
    tier: str
    task_types: list[str]
    sensitivities: list[str]
    matrix: list[MatrixRow]
    # The full lane legend (every lane -> provider/model/local) so the UI can
    # render a key without re-deriving lane_to_model.
    lanes: dict[str, LaneInfo]


def _lane_info(lane: str) -> LaneInfo:
    provider, model = lane_to_model(lane)  # type: ignore[arg-type]
    return LaneInfo(lane=lane, provider=provider, model=model, local=(provider == "ollama"))


@router.get("/lane-matrix", response_model=LaneMatrixResponse)
def get_lane_matrix() -> LaneMatrixResponse:
    """The live (task_type x sensitivity) -> lane grid from ``pick_lane``.

    Computed by calling ``pick_lane`` for every (task_type, sensitivity) pair, so
    it is the SAME routing the dispatcher uses -- a single source, drift-proof.
    The ``_TASK_CLASS_DEFAULT_PROVIDER`` provider-bias layer is NOT applied here
    (it is a Tier-2 provider nudge, not the LANE); this surface is the lane
    tiering only.
    """
    task_types = list(get_args(TaskType))
    sensitivities = list(get_args(Sensitivity))

    matrix = [
        MatrixRow(
            task_type=tt,
            cells={s: _lane_info(pick_lane(tt, s)) for s in sensitivities},  # type: ignore[arg-type]
        )
        for tt in task_types
    ]
    lanes = {lane: _lane_info(lane) for lane in get_args(Lane)}

    return LaneMatrixResponse(
        tier=plan_tier(),
        task_types=task_types,
        sensitivities=sensitivities,
        matrix=matrix,
        lanes=lanes,
    )


# ────────────────────────────────────────────────────────────────────────────
# Plan-tier control (#plan-tier-toggle) — guarded RUNTIME flip of the plan tier
# ────────────────────────────────────────────────────────────────────────────
#
# A confidentiality-boundary control: lifting to ``enterprise`` routes
# CONFIDENTIAL material to cloud Claude (MNPI stays local — that needs the
# separate P5 attestation, even at enterprise). Guards mirror the
# sensitivity-override endpoint:
#   * loopback-only (the router dependency above),
#   * a single-use confirmation nonce (CSRF F-8 — a headless cross-origin POST
#     can mint a nonce via /challenge but cannot READ it, so cannot supply one),
#   * an explicit ``acknowledge_cloud_routing`` flag required to lift to cloud,
#   * an audit row (who / from→to) on every change.
# ``set_plan_tier`` flips ``os.environ`` live (effective at once, no restart) and
# persists to routines/state/plan_tier.json (survives a restart).

_NONCE_TTL_SECONDS = 120.0
_MAX_LIVE_NONCES = 256
_nonces: dict[str, float] = {}          # nonce → expiry (monotonic seconds)
_nonce_lock = threading.Lock()


def _prune_nonces_locked(now: float) -> None:
    for n, exp in list(_nonces.items()):
        if exp < now:
            del _nonces[n]
    if len(_nonces) > _MAX_LIVE_NONCES:
        for n, _exp in sorted(_nonces.items(), key=lambda kv: kv[1])[
            : len(_nonces) - _MAX_LIVE_NONCES
        ]:
            del _nonces[n]


def _issue_nonce(now: Optional[float] = None) -> str:
    now = time.monotonic() if now is None else now
    nonce = secrets.token_urlsafe(32)
    with _nonce_lock:
        _prune_nonces_locked(now)
        _nonces[nonce] = now + _NONCE_TTL_SECONDS
        _prune_nonces_locked(now)
    return nonce


def _consume_nonce(nonce: str, now: Optional[float] = None) -> bool:
    now = time.monotonic() if now is None else now
    if not nonce:
        return False
    with _nonce_lock:
        _prune_nonces_locked(now)
        exp = _nonces.pop(nonce, None)   # pop → single-use even if not expired
    return exp is not None and exp >= now


def _reset_nonces_for_tests() -> None:
    with _nonce_lock:
        _nonces.clear()


class ChallengeResponse(BaseModel):
    confirmation_nonce: str
    expires_in_seconds: float


class PlanTierStateResponse(BaseModel):
    tier: str                       # the LIVE tier (bridge | enterprise)
    source: str                     # 'operator' (persisted UI flip) | 'env-default'
    set_by: Optional[str] = None
    set_at: Optional[str] = None


class SetPlanTierRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tier: Literal["bridge", "enterprise"]
    # strip_whitespace server-side so a whitespace-only identity (which the UI
    # would trim) can't slip past as a "present" audit actor.
    set_by: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=64),
    ] = Field(..., description="Operator identity — audit-trail record.")
    # StrictBool: a sensitive acknowledgement must be a real boolean, not a
    # coerced "yes" / 1 / "true" string.
    acknowledge_cloud_routing: StrictBool = Field(
        False,
        description=(
            "Required true when lifting to 'enterprise': acknowledges that "
            "CONFIDENTIAL material will then route to cloud Claude."
        ),
    )
    confirmation_nonce: str = Field(
        ..., min_length=1, max_length=128,
        description="Single-use nonce from POST /api/routing/plan-tier/challenge.",
    )


@router.get("/plan-tier", response_model=PlanTierStateResponse)
def get_plan_tier() -> PlanTierStateResponse:
    """The live plan tier + provenance (who set it / when, or env-default)."""
    return PlanTierStateResponse(**plan_tier_state())


@router.post("/plan-tier/challenge", response_model=ChallengeResponse, status_code=201)
def plan_tier_challenge() -> ChallengeResponse:
    """Mint a single-use confirmation nonce for the next plan-tier flip (F-8)."""
    if not rate_limit.allow(
        "routing-plan-tier:challenge", capacity=5, refill_per_sec=1.0,
    ):
        raise HTTPException(
            status_code=429,
            detail="too many challenge requests — slow down (nonce-pool DoS guard)",
        )
    return ChallengeResponse(
        confirmation_nonce=_issue_nonce(),
        expires_in_seconds=_NONCE_TTL_SECONDS,
    )


@router.post("/plan-tier", response_model=PlanTierStateResponse)
def set_plan_tier_endpoint(req: SetPlanTierRequest) -> PlanTierStateResponse:
    """Flip the live plan tier (and persist it). Guarded: a single-use nonce, and
    — for the lift to cloud — an explicit acknowledgement. Audited.

    403 on a missing/expired nonce; 422 when lifting to enterprise without
    ``acknowledge_cloud_routing``.
    """
    if not _consume_nonce(req.confirmation_nonce):
        raise HTTPException(
            status_code=403,
            detail="invalid or expired confirmation nonce — re-confirm and retry",
        )
    if req.tier == "enterprise" and not req.acknowledge_cloud_routing:
        raise HTTPException(
            status_code=422,
            detail=(
                "lifting to 'enterprise' routes confidential material to cloud — "
                "acknowledge_cloud_routing must be true"
            ),
        )

    prev = plan_tier()
    set_plan_tier(req.tier, set_by=req.set_by)   # live (os.environ) + persisted
    log.warning(
        "plan tier changed: %s -> %s by %r (ack_cloud=%s)",
        prev, req.tier, req.set_by, req.acknowledge_cloud_routing,
    )
    try:
        audit.write(
            routine="routing.plan-tier",
            run_id=current_run_id() or audit.new_run_id(),
            status="ok",
            audit_dir=RUNS_DIR,
            inputs={
                "tier": req.tier,
                "set_by": req.set_by,
                "acknowledge_cloud_routing": req.acknowledge_cloud_routing,
            },
            outputs={"from": prev, "to": req.tier},
        )
    except Exception as e:  # noqa: BLE001 — never fail the flip on an audit miss
        # Surface as error: an unaudited confidentiality-boundary flip is exactly
        # what an operator would want flagged (the pre-flip log.warning above is
        # the compensating record so the change is never fully untraced).
        log.error("plan-tier audit write FAILED (flip %s applied unaudited): %s", req.tier, e)

    return PlanTierStateResponse(**plan_tier_state())
