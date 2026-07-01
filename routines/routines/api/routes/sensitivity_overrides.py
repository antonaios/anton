"""Operator sensitivity-override endpoints (#llm-routing-override).

Three endpoints, all loopback-only (same pattern as ``budgets.py``):
  * POST   /api/sensitivity/overrides       — open a new window
  * GET    /api/sensitivity/overrides       — list active windows
  * POST   /api/sensitivity/overrides/{id}/close — close early

The OVERRIDE bypasses the sensitivity refusal only; #57 budget gate +
per-workspace policy still fire normally on top. See
``LLM-ROUTING-2026-06-02.md`` §5.
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
from datetime import datetime, timezone
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from routines.api import rate_limit
from routines.sensitivity_overrides import (
    DEFAULT_DURATION_SECONDS,
    MAX_DURATION_SECONDS,
    UNTIL_CLOSED_DURATION,
    Override,
    OverrideNotFound,
    OverrideRefused,
    close_override,
    list_active_overrides,
    open_override,
)

log = logging.getLogger(__name__)

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "testclient"})


# ────────────────────────────────────────────────────────────────────────────
# F-8 (HR S-9) — operator-confirmation nonce (defense-in-depth on top of F-1)
# ────────────────────────────────────────────────────────────────────────────
#
# Opening a sensitivity-override window is the one operator action that can lift
# a confidential workspace's lane to cloud (the lbo-intake-agent path). The F-1
# Origin/Host CSRF middleware already closes the cross-origin reach; this adds a
# second wall: ``POST /overrides`` requires a single-use nonce minted by
# ``POST /overrides/challenge``. A headless CSRF request CAN call the challenge
# endpoint, but CANNOT READ its response (the cross-origin response is opaque
# under the same-origin policy — the bridge sets no permissive CORS for an
# attacker origin), so it can't learn the nonce and the open is refused. The
# legitimate same-origin dashboard reads the nonce and includes it.

_NONCE_TTL_SECONDS = 120.0
# Hard cap on live nonces. The /challenge endpoint is CSRF-callable by design,
# so an unbounded store would let a flood grow memory to ~request_rate×TTL
# (codex-5.5 F-8 r1). The operator opens ONE override at a time; 256 is far
# more than any legitimate burst, and the oldest are evicted past it.
_MAX_LIVE_NONCES = 256
_nonces: dict[str, float] = {}          # nonce → expiry (monotonic seconds)
_nonce_lock = threading.Lock()


def _prune_nonces_locked(now: float) -> None:
    for n, exp in list(_nonces.items()):
        if exp < now:
            del _nonces[n]
    # Bound memory even when nothing has expired yet: evict the oldest-expiring
    # entries down to the cap (a flood of /challenge can't grow the store).
    if len(_nonces) > _MAX_LIVE_NONCES:
        for n, _exp in sorted(_nonces.items(), key=lambda kv: kv[1])[
            : len(_nonces) - _MAX_LIVE_NONCES
        ]:
            del _nonces[n]


def _issue_nonce(now: Optional[float] = None) -> str:
    """Mint a single-use, TTL'd confirmation nonce."""
    now = time.monotonic() if now is None else now
    nonce = secrets.token_urlsafe(32)
    with _nonce_lock:
        _prune_nonces_locked(now)
        _nonces[nonce] = now + _NONCE_TTL_SECONDS
        # Prune again so the freshly-added entry can't push us over the cap.
        _prune_nonces_locked(now)
    return nonce


def _consume_nonce(nonce: str, now: Optional[float] = None) -> bool:
    """Validate + CONSUME (single-use) a nonce; True iff it was live."""
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


def _loopback_only(request: Request) -> None:
    client_host = request.client.host if request.client else None
    if client_host not in _LOOPBACK_HOSTS:
        log.warning(
            "sensitivity-overrides endpoint refused non-loopback connection from %r",
            client_host,
        )
        raise HTTPException(
            status_code=403,
            detail=(
                "sensitivity-overrides endpoints are loopback-only; refusing "
                f"connection from {client_host!r}"
            ),
        )


router = APIRouter(
    prefix="/api/sensitivity",
    tags=["sensitivity"],
    dependencies=[Depends(_loopback_only)],
)


# ────────────────────────────────────────────────────────────────────────────
# DTOs
# ────────────────────────────────────────────────────────────────────────────


class OpenOverrideRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    skill: str = Field(..., min_length=1, max_length=64)
    workspace: str = Field(
        ..., min_length=1, max_length=128,
        description="Workspace identity, e.g. 'project:DemoDeal' or 'general:default'.",
    )
    provider: str = Field(
        ..., min_length=1, max_length=32,
        description="LLM provider key — 'anthropic' | 'openai' | 'ollama' | 'm27' etc.",
    )
    # NOTE: MNPI deliberately ABSENT from the Literal — the storage layer also
    # refuses MNPI defence-in-depth, but the schema is the first wall.
    ceiling: Literal["public", "internal", "confidential"] = Field(
        ..., description="Highest sensitivity tier this override grants. MNPI not allowed.",
    )
    duration_seconds: int = Field(
        DEFAULT_DURATION_SECONDS, ge=UNTIL_CLOSED_DURATION, le=MAX_DURATION_SECONDS,
        description=(
            f"Window duration in seconds. Default {DEFAULT_DURATION_SECONDS} "
            f"(5min), max {MAX_DURATION_SECONDS} (1h). {UNTIL_CLOSED_DURATION} = "
            "until-closed (no auto-expiry; operator must close it). Values "
            "between 1 and 59 are refused (422)."
        ),
    )
    justification: str = Field(
        ..., min_length=1, max_length=1024,
        description="Operator intent + audit-trail record. Required.",
    )
    confirmation_nonce: str = Field(
        ..., min_length=1, max_length=128,
        description=(
            "Single-use nonce from POST /api/sensitivity/overrides/challenge "
            "(F-8 defense-in-depth — a headless CSRF request can't read the "
            "challenge response, so can't supply this)."
        ),
    )


class OverrideDTO(BaseModel):
    id: str
    skill: str
    workspace: str
    provider: str
    ceiling: str
    opened_at: datetime
    expires_at: Optional[datetime] = None   # None = until-closed (no auto-expiry)
    justification: str
    closed_at: Optional[datetime] = None
    closed_reason: Optional[str] = None

    @classmethod
    def from_model(cls, o: Override) -> "OverrideDTO":
        return cls(
            id=o.id, skill=o.skill, workspace=o.workspace, provider=o.provider,
            ceiling=o.ceiling, opened_at=o.opened_at, expires_at=o.expires_at,
            justification=o.justification, closed_at=o.closed_at,
            closed_reason=o.closed_reason,
        )


class ListOverridesResponse(BaseModel):
    overrides: list[OverrideDTO]
    as_of: datetime


class ChallengeResponse(BaseModel):
    confirmation_nonce: str
    expires_in_seconds: float


# ────────────────────────────────────────────────────────────────────────────
# Endpoints
# ────────────────────────────────────────────────────────────────────────────


@router.post("/overrides/challenge", response_model=ChallengeResponse, status_code=201)
def override_challenge_endpoint() -> ChallengeResponse:
    """Mint a single-use confirmation nonce for the next override-open (F-8).

    The same-origin dashboard reads this response and includes the nonce in the
    open request; a cross-origin CSRF page can call this but cannot READ the
    response, so it cannot supply a valid nonce."""
    # AUTH-VULN-03 (Shannon run #2): rate-limit issuance so a CSRF/local flood
    # can't churn the 256-slot / 120s-TTL nonce pool and evict a live operator
    # nonce. capacity=5 + 1/s sustained ⇒ at most 5 + 120 = 125 mints in any TTL
    # window — well under the 256-slot pool, so the pool never fills and a
    # just-minted operator nonce survives its full TTL (codex r1).
    if not rate_limit.allow(
        "sensitivity-overrides:challenge", capacity=5, refill_per_sec=1.0,
    ):
        raise HTTPException(
            status_code=429,
            detail="too many challenge requests — slow down (nonce-pool DoS guard)",
        )
    return ChallengeResponse(
        confirmation_nonce=_issue_nonce(),
        expires_in_seconds=_NONCE_TTL_SECONDS,
    )


@router.post("/overrides", response_model=OverrideDTO, status_code=201)
def open_override_endpoint(payload: OpenOverrideRequest) -> OverrideDTO:
    """Open a new override window. A 403 lands for a missing/invalid/expired/
    reused confirmation nonce (F-8). A 422 lands for: empty justification,
    duration out of range, MNPI ceiling (refused absolute), or invalid
    skill/workspace/provider. ``duration_seconds=0`` opens an until-closed
    window (no auto-expiry; #llm-routing-postjune15 P2) — the response carries
    ``expires_at=null``. Supersedes any active window for the same
    (skill, workspace, provider) tuple."""
    # F-8: require + consume the operator-confirmation nonce BEFORE any state
    # change. Single-use — a replayed nonce is rejected.
    if not _consume_nonce(payload.confirmation_nonce):
        raise HTTPException(
            status_code=403,
            detail=(
                "missing, invalid, expired, or already-used confirmation nonce "
                "— obtain a fresh one from POST /api/sensitivity/overrides/challenge"
            ),
        )
    try:
        override = open_override(
            skill=payload.skill,
            workspace=payload.workspace,
            provider=payload.provider,
            ceiling=payload.ceiling,
            duration_seconds=payload.duration_seconds,
            justification=payload.justification,
        )
    except OverrideRefused as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    log.info(
        "sensitivity override opened: id=%s skill=%s workspace=%s "
        "provider=%s ceiling=%s expires_at=%s",
        override.id, override.skill, override.workspace, override.provider,
        override.ceiling,
        override.expires_at.isoformat() if override.expires_at else "until-closed",
    )
    return OverrideDTO.from_model(override)


@router.get("/overrides", response_model=ListOverridesResponse)
def list_overrides_endpoint() -> ListOverridesResponse:
    """List currently-active overrides. Returns the as_of timestamp so
    callers can compute time-remaining client-side."""
    now = datetime.now(timezone.utc)
    overrides = list_active_overrides(now=now)
    return ListOverridesResponse(
        overrides=[OverrideDTO.from_model(o) for o in overrides],
        as_of=now,
    )


@router.post("/overrides/{override_id}/close", response_model=OverrideDTO)
def close_override_endpoint(override_id: str) -> OverrideDTO:
    """Close an active override early. 404 if unknown / already closed /
    already expired."""
    try:
        closed = close_override(override_id)
    except OverrideNotFound as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    log.info(
        "sensitivity override closed: id=%s reason=%s",
        closed.id, closed.closed_reason,
    )
    return OverrideDTO.from_model(closed)
