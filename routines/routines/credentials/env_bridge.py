"""Store→environment bridge for API-key credentials (#operator-tab v2).

The encrypted credentials store (#25) is where the OPERATOR tab writes
keys — but today's consumers read ENV VARS (sector-news reads
``FIRECRAWL_API_KEY`` / ``TAVILY_API_KEY``; the macro bar's SONIA leg
reads ``FRED_API_KEY``). This module closes that gap: known API-key
providers in the store are exported into ``os.environ`` at bridge boot
and on every store write, so env-reading consumers — including
scheduler-spawned subprocesses, which inherit the bridge's environment —
pick them up without code changes.

Precedence (OPERATOR DECISION, 2026-06-10): **the store wins inside the
bridge process.** Rotating a key from the tab must take effect even when
an OS-level ``setx`` copy exists — otherwise the tab's rotation silently
does nothing. The pre-override env value is snapshotted once and
restored when the stored credential is deleted, and the status surface
reports when both copies exist so the operator can retire the ``setx``
one. The OS-level copy itself is never touched.

Never logs or returns secret material — values flow only
store → ``os.environ``.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Literal, Optional

logger = logging.getLogger(__name__)


# Known API-key providers → the env var their consumer reads. Providers
# outside this map are stored fine but not env-bridged (future consumers
# should read the store directly).
PROVIDER_ENV_MAP: dict[str, str] = {
    "firecrawl": "FIRECRAWL_API_KEY",
    "tavily": "TAVILY_API_KEY",
    "fred": "FRED_API_KEY",
}

_lock = threading.Lock()
# Pre-override env values, snapshotted the FIRST time we override each
# var in this process. None = the var was absent before we set it.
_shadowed: dict[str, Optional[str]] = {}


def apply(provider: str) -> Optional[str]:
    """Export ``provider``'s stored api_key into its env var.

    Returns the env var name when bridged, None when the provider isn't
    in the map / isn't stored / isn't an api_key credential.

    The store read happens INSIDE the bridge lock (codex SEV-2): a
    concurrent DELETE's ``clear()`` serialises against this whole block,
    so it either sees the store already empty here (no-op) or restores
    the snapshot after we set it — a deleted secret can never be left
    in the environment. Lock order is always bridge→store; nothing takes
    them in the other order.
    """
    from routines.credentials.store import APIKeyCredential, get_store

    env_var = PROVIDER_ENV_MAP.get(provider)
    if env_var is None:
        return None

    with _lock:
        cred = get_store().get_credential(provider)
        if not isinstance(cred, APIKeyCredential):
            return None
        if env_var not in _shadowed:
            _shadowed[env_var] = os.environ.get(env_var)
        os.environ[env_var] = cred.api_key.get_secret_value()
    logger.info("credentials env-bridge: %s -> %s (value not logged)", provider, env_var)
    return env_var


def clear(provider: str) -> None:
    """Undo this process's override for ``provider`` (after a DELETE):
    restore the snapshotted pre-override value, or unset the var if it
    only existed because of the bridge."""
    env_var = PROVIDER_ENV_MAP.get(provider)
    if env_var is None:
        return
    with _lock:
        if env_var not in _shadowed:
            return                      # we never overrode it
        original = _shadowed.pop(env_var)
        if original is None:
            os.environ.pop(env_var, None)
        else:
            os.environ[env_var] = original
    logger.info("credentials env-bridge: %s override cleared", provider)


def apply_all() -> list[str]:
    """Bridge every mapped provider present in the store. Called at app
    lifespan. Returns the bridged provider names (for the boot log)."""
    bridged: list[str] = []
    for provider in PROVIDER_ENV_MAP:
        try:
            if apply(provider):
                bridged.append(provider)
        except Exception as e:  # noqa: BLE001 — boot must not die on one provider
            logger.warning("credentials env-bridge: %s failed: %s", provider, e)
    return bridged


EffectiveSource = Literal["store", "store-over-env", "env", "none"]


def key_status() -> dict[str, dict]:
    """STATUS-only view for the operator-config surface — which copy of
    each known key is effective. Never contains secret material."""
    from routines.credentials.store import get_store

    store = get_store()
    stored = {s.provider for s in store.list_summaries() if s.kind == "api_key"}
    out: dict[str, dict] = {}
    with _lock:
        for provider, env_var in PROVIDER_ENV_MAP.items():
            in_store = provider in stored
            if env_var in _shadowed:
                # We overrode this var; the pre-override snapshot tells us
                # whether an independent env copy exists underneath.
                env_independent = _shadowed[env_var] is not None
            else:
                env_independent = bool(os.environ.get(env_var, "").strip())
            if in_store:
                effective: EffectiveSource = (
                    "store-over-env" if env_independent else "store"
                )
            elif env_independent:
                effective = "env"
            else:
                effective = "none"
            out[provider] = {
                "env_var": env_var,
                "store": in_store,
                "env": env_independent,
                "effective": effective,
            }
    return out


def reset_for_tests() -> None:
    """Tests only. Restores every shadowed env var to its pre-override
    value before dropping the snapshot (codex SEV-3 — a bare clear would
    strand bridged secrets in the environment)."""
    with _lock:
        for env_var, original in _shadowed.items():
            if original is None:
                os.environ.pop(env_var, None)
            else:
                os.environ[env_var] = original
        _shadowed.clear()
