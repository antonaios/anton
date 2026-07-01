"""Operator MNPI cloud-attestation endpoints (#llm-routing-postjune15 P5).

Four endpoints, all loopback-only (same pattern as ``sensitivity_overrides.py``):
  * POST   /api/mnpi/attestations/challenge   — mint a single-use nonce
  * POST   /api/mnpi/attestations             — grant an attestation
  * GET    /api/mnpi/attestations             — list active attestations
  * POST   /api/mnpi/attestations/{id}/revoke — revoke early

An attestation records that a cloud provider carries DPA + ZDR + no-training;
under ``AGENTIC_PLAN_TIER=enterprise`` an active attestation lets
EXPLICITLY-assigned MNPI route to that provider's cloud lane (the standing
auto-on gate). This is the single most sensitive operator action on the platform
— it relaxes the [no-mnpi-to-cloud](<vault>/CLAUDE.md#no-mnpi-to-cloud)
floor for one provider — so it carries the SAME defences as the confidential
override: loopback-only + a single-use confirmation nonce (F-8 CSRF
defence-in-depth). NEVER auto-seeded; the empty store reproduces the absolute
pre-P5 floor.
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from routines.api import rate_limit
from routines.mnpi_attestations import (
    DEFAULT_DURATION_SECONDS,
    MAX_DURATION_SECONDS,
    MIN_DURATION_SECONDS,
    Attestation,
    AttestationNotFound,
    AttestationRefused,
    grant_attestation,
    list_active_attestations,
    revoke_attestation,
)

log = logging.getLogger(__name__)

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "testclient"})


# ────────────────────────────────────────────────────────────────────────────
# Operator-confirmation nonce (F-8 — mirrors sensitivity_overrides.py)
# ────────────────────────────────────────────────────────────────────────────
#
# Granting an attestation is the operator action that lifts the MNPI→cloud
# floor. The F-1 Origin/Host CSRF middleware closes the cross-origin reach; the
# nonce adds a second wall: ``POST /attestations`` requires a single-use nonce
# minted by ``POST /attestations/challenge``. A headless CSRF request can call
# the challenge endpoint but cannot READ its (opaque, cross-origin) response, so
# it can't learn the nonce and the grant is refused.

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
    """Mint a single-use, TTL'd confirmation nonce."""
    now = time.monotonic() if now is None else now
    nonce = secrets.token_urlsafe(32)
    with _nonce_lock:
        _prune_nonces_locked(now)
        _nonces[nonce] = now + _NONCE_TTL_SECONDS
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
            "mnpi-attestations endpoint refused non-loopback connection from %r",
            client_host,
        )
        raise HTTPException(
            status_code=403,
            detail=(
                "mnpi-attestations endpoints are loopback-only; refusing "
                f"connection from {client_host!r}"
            ),
        )


router = APIRouter(
    prefix="/api/mnpi",
    tags=["mnpi-attestations"],
    dependencies=[Depends(_loopback_only)],
)


# ────────────────────────────────────────────────────────────────────────────
# DTOs
# ────────────────────────────────────────────────────────────────────────────


class GrantAttestationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: str = Field(
        ..., min_length=1, max_length=32,
        description="Provider key — 'anthropic'/'claude' | 'openai'/'codex'. Normalised server-side.",
    )
    dpa: StrictBool = Field(..., description="A signed Data Processing Agreement is in force. Must be JSON true.")
    zdr: StrictBool = Field(..., description="Zero-data-retention is contractually guaranteed. Must be JSON true.")
    no_training: StrictBool = Field(..., description="Payload contractually excluded from training. Must be JSON true.")
    granted_by: str = Field(
        ..., min_length=1, max_length=128,
        description="Operator identity recorded on the attestation (audit).",
    )
    duration_seconds: int = Field(
        DEFAULT_DURATION_SECONDS, ge=MIN_DURATION_SECONDS, le=MAX_DURATION_SECONDS,
        description=(
            f"Attestation validity in seconds. Default {DEFAULT_DURATION_SECONDS} "
            f"(~1yr), range [{MIN_DURATION_SECONDS}, {MAX_DURATION_SECONDS}]. "
            "Compliance should re-attest at least annually."
        ),
    )
    confirmation_nonce: str = Field(
        ..., min_length=1, max_length=128,
        description=(
            "Single-use nonce from POST /api/mnpi/attestations/challenge "
            "(F-8 defense-in-depth — a headless CSRF request can't read the "
            "challenge response, so can't supply this)."
        ),
    )


class AttestationDTO(BaseModel):
    id: str
    provider: str
    dpa: bool
    zdr: bool
    no_training: bool
    granted_by: str
    granted_at: datetime
    expires_at: datetime
    revoked_at: Optional[datetime] = None
    revoked_reason: Optional[str] = None

    @classmethod
    def from_model(cls, a: Attestation) -> "AttestationDTO":
        return cls(
            id=a.id, provider=a.provider, dpa=a.dpa, zdr=a.zdr,
            no_training=a.no_training, granted_by=a.granted_by,
            granted_at=a.granted_at, expires_at=a.expires_at,
            revoked_at=a.revoked_at, revoked_reason=a.revoked_reason,
        )


class ListAttestationsResponse(BaseModel):
    attestations: list[AttestationDTO]
    as_of: datetime


class ChallengeResponse(BaseModel):
    confirmation_nonce: str
    expires_in_seconds: float


# ────────────────────────────────────────────────────────────────────────────
# Endpoints
# ────────────────────────────────────────────────────────────────────────────


@router.post("/attestations/challenge", response_model=ChallengeResponse, status_code=201)
def attestation_challenge_endpoint() -> ChallengeResponse:
    """Mint a single-use confirmation nonce for the next attestation grant (F-8).

    The same-origin dashboard reads this response and includes the nonce in the
    grant request; a cross-origin CSRF page can call this but cannot READ the
    response, so it cannot supply a valid nonce."""
    # AUTH-VULN-03 (Shannon run #2): rate-limit issuance so a CSRF/local flood
    # can't churn the 256-slot / 120s-TTL nonce pool and evict a live operator
    # nonce. capacity=5 + 1/s sustained ⇒ at most 5 + 120 = 125 mints in any TTL
    # window — well under the 256-slot pool, so the pool never fills and a
    # just-minted operator nonce survives its full TTL (codex r1).
    if not rate_limit.allow(
        "mnpi-attestations:challenge", capacity=5, refill_per_sec=1.0,
    ):
        raise HTTPException(
            status_code=429,
            detail="too many challenge requests — slow down (nonce-pool DoS guard)",
        )
    return ChallengeResponse(
        confirmation_nonce=_issue_nonce(),
        expires_in_seconds=_NONCE_TTL_SECONDS,
    )


@router.post("/attestations", response_model=AttestationDTO, status_code=201)
def grant_attestation_endpoint(payload: GrantAttestationRequest) -> AttestationDTO:
    """Grant a per-provider MNPI cloud-attestation. A 403 lands for a
    missing/invalid/expired/reused confirmation nonce (F-8). A 422 lands for: a
    missing protection (dpa/zdr/no_training must all be true), empty
    provider/granted_by, or a duration out of range. Supersedes any live
    attestation for the same provider."""
    if not _consume_nonce(payload.confirmation_nonce):
        raise HTTPException(
            status_code=403,
            detail=(
                "missing, invalid, expired, or already-used confirmation nonce "
                "— obtain a fresh one from POST /api/mnpi/attestations/challenge"
            ),
        )
    try:
        attestation = grant_attestation(
            provider=payload.provider,
            dpa=payload.dpa,
            zdr=payload.zdr,
            no_training=payload.no_training,
            granted_by=payload.granted_by,
            duration_seconds=payload.duration_seconds,
        )
    except AttestationRefused as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    log.warning(
        "MNPI cloud-attestation GRANTED: id=%s provider=%s granted_by=%s "
        "expires_at=%s (enterprise-MNPI cloud now permitted for this provider)",
        attestation.id, attestation.provider, attestation.granted_by,
        attestation.expires_at.isoformat(),
    )
    return AttestationDTO.from_model(attestation)


@router.get("/attestations", response_model=ListAttestationsResponse)
def list_attestations_endpoint() -> ListAttestationsResponse:
    """List currently-active attestations (not revoked, not expired)."""
    now = datetime.now(timezone.utc)
    attestations = list_active_attestations(now=now)
    return ListAttestationsResponse(
        attestations=[AttestationDTO.from_model(a) for a in attestations],
        as_of=now,
    )


@router.post("/attestations/{attestation_id}/revoke", response_model=AttestationDTO)
def revoke_attestation_endpoint(attestation_id: str) -> AttestationDTO:
    """Revoke a live attestation early. 404 if unknown / already revoked. Once
    revoked, MNPI for that provider returns to the local-only floor immediately."""
    try:
        revoked = revoke_attestation(attestation_id)
    except AttestationNotFound as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    log.warning(
        "MNPI cloud-attestation REVOKED: id=%s provider=%s reason=%s "
        "(MNPI for this provider back to local-only)",
        revoked.id, revoked.provider, revoked.revoked_reason,
    )
    return AttestationDTO.from_model(revoked)
