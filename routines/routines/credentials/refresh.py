"""OAuth2 refresh — provider-specific implementations.

Skeleton for #25. Real provider wiring lands with #17 (MS Graph OAuth) and
any other OAuth2 integrations that follow. The skeleton locks the shape:

* Refresh is async (HTTP roundtrip to the provider's token endpoint).
* The caller MUST hold ``lock_manager.acquire(provider)`` — this module
  trusts the lock; it doesn't acquire one itself. Centralising lock
  acquisition in the bridge route keeps the audit trail clean.
* On success: tokens land in the store via ``update_oauth2_tokens()``.
* On failure: raise — never silently leave a stale access_token in place.

For v1, any concrete provider call raises ``NotImplementedError`` —
clients (e.g. the bridge endpoint) catch it and surface HTTP 501. The
shape of the exception lets the future #17 implementation drop in a
real ``_refresh_ms_graph()`` without restructuring callers.
"""

from __future__ import annotations

import logging
from typing import Awaitable, Callable, Protocol

from routines.credentials.lock_manager import get_lock_manager
from routines.credentials.store import (
    CredentialSummary,
    OAuth2Credential,
    get_store,
)

logger = logging.getLogger(__name__)


class RefreshError(RuntimeError):
    """Raised on any failure of an OAuth2 refresh attempt.

    The bridge route maps this to HTTP 502 (upstream failure). The
    ``NotImplementedError`` subclass below maps to 501 instead — that's
    "we know how to ask, we just haven't written the code for this
    provider yet" — semantically distinct from "the upstream IDP errored."
    """


class RefreshNotImplemented(RefreshError, NotImplementedError):
    """Raised when a refresh is requested for a provider whose specific
    refresh handler hasn't been wired yet. v1 always raises this — actual
    handlers land per provider as integrations come online (#17 first)."""


# ────────────────────────────────────────────────────────────────────────────
# Provider registry
# ────────────────────────────────────────────────────────────────────────────

# Each registered handler is an async function:
#     async def handler(cred: OAuth2Credential) -> dict
# where the dict carries the result fields the store needs:
#     {access_token, refresh_token?, expires_at}
# refresh_token is optional — many providers rotate access tokens only.
#
# Concrete handlers register themselves at import time via ``register_refresh_handler``.

RefreshHandler = Callable[[OAuth2Credential], Awaitable[dict]]

_HANDLERS: dict[str, RefreshHandler] = {}


def register_refresh_handler(provider: str, handler: RefreshHandler) -> None:
    """Register a provider-specific OAuth2 refresh handler. Idempotent
    (last registration wins). #17 calls this for ``ms-graph`` once that
    integration lands."""
    _HANDLERS[provider] = handler


def is_handler_registered(provider: str) -> bool:
    return provider in _HANDLERS


# ────────────────────────────────────────────────────────────────────────────
# Refresh entry point
# ────────────────────────────────────────────────────────────────────────────


async def refresh_oauth2(provider: str) -> CredentialSummary:
    """Refresh ``provider``'s OAuth2 access_token.

    Steps:
      1. Load the stored credential (must be ``kind=oauth2``).
      2. Dispatch to the provider's registered handler.
      3. Persist the new tokens via ``store.update_oauth2_tokens``.
      4. Return the post-refresh ``CredentialSummary``.

    Caller MUST hold ``lock_manager.acquire(provider)`` — this is enforced
    by an ``assert`` at the top of the function (#25b, 2026-05-26 security
    review Invariant 6). The previous docstring-only contract risked future
    callers — especially in ``before_llm_call`` hooks — silently bypassing
    per-provider serialisation and letting two concurrent refreshes race
    on the same one-shot upstream refresh_token.

    Raises:
        AssertionError           — caller did not hold the provider lock (#25b)
        KeyError                 — provider not configured
        TypeError                — provider exists but is not OAuth2
        RefreshNotImplemented    — handler not registered (v1 default)
        RefreshError             — handler ran but upstream IDP errored
    """
    # #25b — Invariant 6 enforcement: the docstring promise becomes code.
    # If you're calling this without ``async with lock_mgr.acquire(provider)``,
    # the per-provider refresh serialisation is broken and two concurrent
    # callers will invalidate each other's refresh_tokens upstream.
    lock_mgr = get_lock_manager()
    assert lock_mgr.is_locked(provider), (
        f"refresh_oauth2({provider!r}) requires the caller to hold "
        f"lock_manager.acquire({provider!r}); per-provider refresh "
        f"serialisation is otherwise silently bypassed"
    )

    store = get_store()
    cred = store.get_credential(provider)
    if cred is None:
        raise KeyError(f"provider {provider!r} not configured")
    if not isinstance(cred, OAuth2Credential):
        raise TypeError(
            f"provider {provider!r} is kind={cred.kind!r}; "
            f"refresh only valid for kind=oauth2"
        )

    handler = _HANDLERS.get(provider)
    if handler is None:
        raise RefreshNotImplemented(
            f"OAuth2 refresh handler for {provider!r} not registered — "
            f"this lands with #17 (MS Graph) and per-integration follow-ons. "
            f"Use ``register_refresh_handler({provider!r}, …)`` to wire one in."
        )

    try:
        result = await handler(cred)
    except (RefreshError, RefreshNotImplemented):
        raise
    except Exception as e:  # noqa: BLE001 — handlers may raise anything
        # F-37 (CX B-04, latent): never wrap the raw upstream error into the
        # exception MESSAGE — an IDP error response can embed token material,
        # and the credentials route logs RefreshError's message to the local
        # bridge log. Log the cause HERE through the audit key-scrubber;
        # raise a fixed-shape message (the chain stays on ``__cause__`` for
        # interactive debugging only).
        from routines.shared.audit import _sanitize_string

        logger.warning(
            "oauth2 refresh handler failed for %r: %s",
            provider, _sanitize_string(str(e)),
        )
        raise RefreshError(
            f"OAuth2 refresh upstream call failed for {provider!r} "
            f"({type(e).__name__}; cause redacted — see bridge log)"
        ) from e

    access_token = result.get("access_token")
    expires_at = result.get("expires_at")
    if not access_token or not expires_at:
        raise RefreshError(
            f"OAuth2 refresh handler for {provider!r} returned an incomplete "
            f"payload: missing access_token or expires_at"
        )

    return store.update_oauth2_tokens(
        provider,
        access_token=access_token,
        refresh_token=result.get("refresh_token"),
        expires_at=expires_at,
    )
