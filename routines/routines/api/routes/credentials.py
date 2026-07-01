"""Credentials API routes — #25.

Implements OUTSTANDING.md ## CONTRACTS · credentials manager.

  * POST   /api/credentials                          — add a credential (api_key / oauth2 / user_password)
  * GET    /api/credentials                          — list summaries (NO secret data)
  * GET    /api/credentials/{provider}               — single summary (NO secret data)
  * DELETE /api/credentials/{provider}               — remove
  * POST   /api/credentials/{provider}/refresh       — OAuth2 access_token rotation

Security invariants enforced here (not on the store layer):

  1. NO endpoint returns secret fields. Even ``POST`` echoes only a
     ``CredentialSummary``.
  2. Every mutation writes an audit row to
     ``routines/runs/credentials.<action>.jsonl`` with ``{provider, kind}`` —
     never the secret value. ``mark_used`` doesn't audit (per-call noise).
  3. ``refresh`` acquires the per-provider asyncio lock so concurrent
     skill calls that both notice an expired access_token serialise into
     one refresh upstream.
"""

from __future__ import annotations

import ipaddress
import logging
import time
from typing import Literal, Union

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from routines.api.deps import ROUTINES_REPO
from routines.credentials import env_bridge
from routines.credentials import (
    APIKeyCredential,
    CredentialSummary,
    OAuth2Credential,
    UserPasswordCredential,
    get_lock_manager,
    get_store,
)
from routines.credentials.refresh import (
    RefreshError,
    RefreshNotImplemented,
    refresh_oauth2,
)
from routines.shared import audit

log = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────────────
# #25b — Loopback-only guard (Invariant 10 from 2026-05-26 security review)
# ────────────────────────────────────────────────────────────────────────────
#
# Credentials endpoints have NO authentication. Security depends on the bridge
# binding only to 127.0.0.1 (see app.py main()'s loopback warning). If the
# bridge is ever bound to 0.0.0.0 — e.g. for debugging from a phone, or by
# accident in a setup script — the credentials store becomes LAN-accessible.
#
# This dependency makes the refusal explicit and code-enforced: any non-
# loopback client connecting to /api/credentials/* gets a 403 BEFORE the
# handler runs. Belt-and-braces with the app-level bind warning.
#
# The Starlette TestClient sets ``request.client.host`` to ``"testclient"``
# — an ASGI in-process value that's not reachable from any network, so
# we allow it. Production never sees that string from a real socket.

_LOOPBACK_HOSTS = frozenset({
    "127.0.0.1",   # IPv4 loopback
    "::1",         # IPv6 loopback
    "localhost",   # DNS alias some clients resolve
    "testclient",  # Starlette ASGI in-process — not network-reachable
})


def _is_loopback_client(client_host: str | None) -> bool:
    """True only for a genuine loopback peer.

    With uvicorn started with ``proxy_headers=False`` (see ``app.py main()``),
    ``request.client.host`` is the real TCP socket peer -- a numeric loopback
    address (127.0.0.0/8, ::1), never a header-spoofable value. We accept the
    named entries in ``_LOOPBACK_HOSTS`` (incl. the Starlette in-process
    ``"testclient"`` sentinel) AND any address that parses as a loopback IP, so
    the whole 127.0.0.0/8 range is covered (#sec-loopback-proxy-headers).
    """
    if client_host is None:
        return False
    if client_host in _LOOPBACK_HOSTS:
        return True
    try:
        ip = ipaddress.ip_address(client_host)
    except ValueError:
        return False
    if ip.is_loopback:
        return True
    # IPv4-mapped IPv6 loopback (e.g. ::ffff:127.0.0.1) on a dual-stack peer.
    mapped = getattr(ip, "ipv4_mapped", None)
    return bool(mapped is not None and mapped.is_loopback)


def _loopback_only(request: Request) -> None:
    """Refuse any non-loopback connection to credentials endpoints.

    Raises 403 ``HTTPException`` before the route handler runs. The body
    surfaces the rejected client host so a misconfigured bind (0.0.0.0)
    shows up loud in the bridge log instead of silently exposing secrets.
    """
    client_host = request.client.host if request.client else None
    if not _is_loopback_client(client_host):
        log.warning(
            "credentials endpoint refused non-loopback connection from %r",
            client_host,
        )
        raise HTTPException(
            status_code=403,
            detail=(
                f"credentials endpoints are loopback-only; refusing "
                f"connection from {client_host!r}"
            ),
        )


router = APIRouter(dependencies=[Depends(_loopback_only)])


# ────────────────────────────────────────────────────────────────────────────
# Request payloads — mirror the store's three credential kinds, with one
# extra layer: the request models accept ``api_key`` / ``access_token`` /
# ``password`` as plain ``str`` (FastAPI's JSON body), then wrap them in
# ``SecretStr`` before persisting.
# ────────────────────────────────────────────────────────────────────────────


class APIKeyCredentialIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["api_key"]
    provider: str = Field(..., min_length=1)
    api_key: str = Field(..., min_length=1)
    metadata: dict = Field(default_factory=dict)


class OAuth2CredentialIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["oauth2"]
    provider: str = Field(..., min_length=1)
    access_token: str = Field(..., min_length=1)
    refresh_token: str = Field(..., min_length=1)
    expires_at: str = Field(..., min_length=1)        # ISO-8601
    scopes: list[str] = Field(default_factory=list)
    metadata: dict = Field(default_factory=dict)


class UserPasswordCredentialIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["user_password"]
    provider: str = Field(..., min_length=1)
    username: str = Field(..., min_length=1)
    password: str = Field(..., min_length=1)
    metadata: dict = Field(default_factory=dict)


# FastAPI resolves the union via the ``kind`` literal discriminator.
CredentialIn = Union[APIKeyCredentialIn, OAuth2CredentialIn, UserPasswordCredentialIn]


# ────────────────────────────────────────────────────────────────────────────
# Response shapes
# ────────────────────────────────────────────────────────────────────────────


class CredentialSummaryDTO(BaseModel):
    provider: str
    kind: Literal["api_key", "oauth2", "user_password"]
    created: str
    last_used: str | None = None
    expires_at: str | None = None
    metadata: dict = Field(default_factory=dict)


class ListCredentialsResponse(BaseModel):
    credentials: list[CredentialSummaryDTO]


class RefreshResponse(BaseModel):
    provider: str
    expires_at: str


# ────────────────────────────────────────────────────────────────────────────
# Conversions
# ────────────────────────────────────────────────────────────────────────────


def _build_stored(payload: CredentialIn):
    """Wrap inbound JSON into a ``SecretStr``-bearing store model."""
    if isinstance(payload, APIKeyCredentialIn):
        return APIKeyCredential(
            provider=payload.provider,
            api_key=SecretStr(payload.api_key),
            metadata=dict(payload.metadata),
        )
    if isinstance(payload, OAuth2CredentialIn):
        return OAuth2Credential(
            provider=payload.provider,
            access_token=SecretStr(payload.access_token),
            refresh_token=SecretStr(payload.refresh_token),
            expires_at=payload.expires_at,
            scopes=list(payload.scopes),
            metadata=dict(payload.metadata),
        )
    if isinstance(payload, UserPasswordCredentialIn):
        return UserPasswordCredential(
            provider=payload.provider,
            username=payload.username,
            password=SecretStr(payload.password),
            metadata=dict(payload.metadata),
        )
    raise TypeError(f"unsupported request kind: {type(payload).__name__}")


def _summary_to_dto(s: CredentialSummary) -> CredentialSummaryDTO:
    # F-19 (CX B-05): the core secret fields are SecretStr (never echoed), but
    # the free-form ``metadata`` dict was accepted on add/put AND echoed back in
    # the summary — so a secret placed there (e.g. a backup key, or an opaque
    # secret under an innocuous label the shape-based sanitizer can't catch)
    # would cross the wire. The metadata is still STORED server-side (internal
    # consumers read the store model, not this DTO) but is NO LONGER ECHOED —
    # the dashboard does not render it, so suppressing the echo loses nothing
    # while closing the leak completely (stronger than shape-based redaction).
    return CredentialSummaryDTO(
        provider=s.provider,
        kind=s.kind,  # type: ignore[arg-type]
        created=s.created,
        last_used=s.last_used,
        expires_at=s.expires_at,
        metadata={},   # F-19: never echo stored metadata
    )


# ────────────────────────────────────────────────────────────────────────────
# Routes
# ────────────────────────────────────────────────────────────────────────────


@router.post("/credentials", response_model=CredentialSummaryDTO, status_code=201)
def add_credential(payload: CredentialIn) -> CredentialSummaryDTO:
    """Add a new credential. Returns the summary (NO secret fields)."""
    run_id = audit.new_run_id()
    t0 = time.monotonic()
    store = get_store()
    try:
        cred = _build_stored(payload)
    except TypeError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e

    env_bridged = False
    try:
        summary = store.add(cred)
        # Best-effort, same contract as PUT — see put_credential.
        try:
            env_bridged = env_bridge.apply(payload.provider) is not None
        except Exception as e:  # noqa: BLE001
            log.warning(
                "credentials env-bridge failed after add of %r: %s",
                payload.provider, e,
            )
    except ValueError as e:
        # provider already configured
        audit.write_structured(
            actor={"type": "user", "id": "operator"},
            entity_type="credential",
            entity_id=payload.provider,
            action="add",
            routine="credentials.add",
            run_id=run_id,
            status="error",
            audit_dir=ROUTINES_REPO / "runs",
            inputs={"provider": payload.provider, "kind": payload.kind},
            error=str(e),
            duration_ms=int((time.monotonic() - t0) * 1000),
        )
        raise HTTPException(status_code=409, detail=str(e)) from e

    audit.write_structured(
        actor={"type": "user", "id": "operator"},
        entity_type="credential",
        entity_id=payload.provider,
        action="add",
        routine="credentials.add",
        run_id=run_id,
        status="ok",
        audit_dir=ROUTINES_REPO / "runs",
        inputs={"provider": payload.provider, "kind": payload.kind},
        outputs={"env_bridged": env_bridged},
        duration_ms=int((time.monotonic() - t0) * 1000),
    )
    return _summary_to_dto(summary)


@router.put("/credentials/{provider}", response_model=CredentialSummaryDTO)
def put_credential(provider: str, payload: CredentialIn) -> CredentialSummaryDTO:
    """Upsert / ROTATE a credential (#operator-tab v2 key entry). Unlike
    POST (409 when the provider exists), PUT replaces atomically via the
    store lock — no delete+add window with no key on disk. ``created``
    is preserved on rotation. Returns the summary (NO secret fields)."""
    if payload.provider != provider:
        raise HTTPException(
            status_code=422,
            detail=(
                f"path provider {provider!r} does not match body provider "
                f"{payload.provider!r}"
            ),
        )
    run_id = audit.new_run_id()
    t0 = time.monotonic()
    store = get_store()
    try:
        cred = _build_stored(payload)
    except TypeError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e

    existed = store.has(provider)
    summary = store.replace(cred)
    # Make the rotated key effective for env-reading consumers (sector-news
    # etc.) — store wins inside the bridge process; see env_bridge docstring.
    # BEST-EFFORT (codex SEV-2): the store is the source of truth and the
    # write has committed — a bridge failure must not 500 the rotation
    # into looking failed. A kind switch AWAY from api_key on a mapped
    # provider clears the old bridged secret instead of stranding it.
    env_bridged = False
    try:
        if isinstance(cred, APIKeyCredential):
            env_bridged = env_bridge.apply(provider) is not None
        else:
            env_bridge.clear(provider)
    except Exception as e:  # noqa: BLE001
        log.warning(
            "credentials env-bridge failed after replace of %r "
            "(store updated; env refreshes on bridge restart): %s",
            provider, e,
        )

    audit.write_structured(
        actor={"type": "user", "id": "operator"},
        entity_type="credential",
        entity_id=provider,
        action="replace" if existed else "add",
        routine="credentials.replace",
        run_id=run_id,
        status="ok",
        audit_dir=ROUTINES_REPO / "runs",
        inputs={"provider": provider, "kind": payload.kind, "rotated": existed},
        outputs={"env_bridged": env_bridged},
        duration_ms=int((time.monotonic() - t0) * 1000),
    )
    return _summary_to_dto(summary)


@router.get("/credentials", response_model=ListCredentialsResponse)
def list_credentials() -> ListCredentialsResponse:
    """List all credential summaries. NEVER returns secret fields."""
    summaries = get_store().list_summaries()
    return ListCredentialsResponse(
        credentials=[_summary_to_dto(s) for s in summaries],
    )


@router.get("/credentials/{provider}", response_model=CredentialSummaryDTO)
def get_credential_summary(provider: str) -> CredentialSummaryDTO:
    summary = get_store().get_summary(provider)
    if summary is None:
        raise HTTPException(
            status_code=404, detail=f"provider {provider!r} not configured",
        )
    return _summary_to_dto(summary)


@router.delete("/credentials/{provider}", status_code=204)
def delete_credential(provider: str) -> None:
    run_id = audit.new_run_id()
    t0 = time.monotonic()
    removed = get_store().remove(provider)
    if removed:
        # Restore the pre-override env value (or unset) for this process.
        env_bridge.clear(provider)
    if not removed:
        audit.write_structured(
            actor={"type": "user", "id": "operator"},
            entity_type="credential",
            entity_id=provider,
            action="remove",
            routine="credentials.remove",
            run_id=run_id,
            status="error",
            audit_dir=ROUTINES_REPO / "runs",
            inputs={"provider": provider},
            error="provider not configured",
            duration_ms=int((time.monotonic() - t0) * 1000),
        )
        raise HTTPException(
            status_code=404, detail=f"provider {provider!r} not configured",
        )
    audit.write_structured(
        actor={"type": "user", "id": "operator"},
        entity_type="credential",
        entity_id=provider,
        action="remove",
        routine="credentials.remove",
        run_id=run_id,
        status="ok",
        audit_dir=ROUTINES_REPO / "runs",
        inputs={"provider": provider},
        duration_ms=int((time.monotonic() - t0) * 1000),
    )


@router.post(
    "/credentials/{provider}/refresh", response_model=RefreshResponse,
)
async def refresh_credential(provider: str) -> RefreshResponse:
    """OAuth2 access_token rotation. v1 skeleton — concrete handlers land
    per integration (#17 ms-graph first). Maps:

      * provider not configured       → 404
      * provider not OAuth2           → 422
      * handler not registered (v1)   → 501
      * upstream IDP error            → 502
    """
    run_id = audit.new_run_id()
    t0 = time.monotonic()
    lock_mgr = get_lock_manager()
    try:
        async with lock_mgr.acquire(provider):
            summary = await refresh_oauth2(provider)
    # Refresh error handling: NEVER surface ``str(e)`` on the wire OR in the
    # audit JSONL. A refresh exception that wraps an upstream IDP error can
    # embed token material (refresh_token / access_token / auth headers); the
    # old ``error=str(e)`` / ``detail=str(e)`` would leak it to the persistent
    # audit row AND the client. Use fixed, secret-free messages everywhere; log
    # the raw cause LOCALLY (bridge log only) for diagnostics. (codex-5.5 SEV-2.)
    except KeyError as e:
        audit.write_structured(
            actor={"type": "user", "id": "operator"},
            entity_type="credential", entity_id=provider, action="refresh",
            routine="credentials.refresh", run_id=run_id, status="error",
            audit_dir=ROUTINES_REPO / "runs",
            inputs={"provider": provider}, error="provider not configured",
            duration_ms=int((time.monotonic() - t0) * 1000),
        )
        raise HTTPException(
            status_code=404, detail=f"provider {provider!r} not configured",
        ) from e
    except TypeError as e:
        audit.write_structured(
            actor={"type": "user", "id": "operator"},
            entity_type="credential", entity_id=provider, action="refresh",
            routine="credentials.refresh", run_id=run_id, status="error",
            audit_dir=ROUTINES_REPO / "runs",
            inputs={"provider": provider}, error="not an oauth2 credential",
            duration_ms=int((time.monotonic() - t0) * 1000),
        )
        raise HTTPException(
            status_code=422,
            detail=f"provider {provider!r} is not an oauth2 credential — kind=oauth2 required",
        ) from e
    except RefreshNotImplemented as e:
        audit.write_structured(
            actor={"type": "user", "id": "operator"},
            entity_type="credential", entity_id=provider, action="refresh",
            routine="credentials.refresh", run_id=run_id, status="error",
            audit_dir=ROUTINES_REPO / "runs",
            inputs={"provider": provider}, error="handler not registered",
            duration_ms=int((time.monotonic() - t0) * 1000),
        )
        raise HTTPException(
            status_code=501,
            detail="oauth2 refresh handler not registered for this provider",
        ) from e
    except RefreshError as e:
        # Upstream-IDP failure — the high-risk path. Log the raw cause LOCALLY
        # for diagnostics; return + audit a fixed code only.
        log.warning("credentials refresh failed for provider %r: %s", provider, e)
        audit.write_structured(
            actor={"type": "user", "id": "operator"},
            entity_type="credential", entity_id=provider, action="refresh",
            routine="credentials.refresh", run_id=run_id, status="error",
            audit_dir=ROUTINES_REPO / "runs",
            inputs={"provider": provider}, error="oauth_refresh_failed",
            duration_ms=int((time.monotonic() - t0) * 1000),
        )
        raise HTTPException(status_code=502, detail="OAuth2 refresh failed") from e

    audit.write_structured(
        actor={"type": "user", "id": "operator"},
        entity_type="credential", entity_id=provider, action="refresh",
        routine="credentials.refresh", run_id=run_id, status="ok",
        audit_dir=ROUTINES_REPO / "runs",
        inputs={"provider": provider},
        outputs={"expires_at": summary.expires_at},
        duration_ms=int((time.monotonic() - t0) * 1000),
    )
    return RefreshResponse(
        provider=summary.provider,
        expires_at=summary.expires_at or "",
    )
