"""Per-provider asyncio lock dispenser.

Single-user variant of AutoGPT's ``IntegrationCredentialsManager.acquire()``
(see AUTOGPT-EVALUATION.md §2.3). AutoGPT uses Redis to coordinate refresh
locks across workers; ANTON is single-user / in-process, so a plain
``asyncio.Lock`` per provider is sufficient.

The contract is the same: only one OAuth refresh at a time **for a given
provider**, but reads for *other* providers stay parallel. Without this,
two concurrent skill calls that both notice an expired access_token would
each fire a refresh — and the second one would invalidate the first
(refresh tokens are one-shot at most upstreams).

Usage:
    lock_mgr = get_lock_manager()
    async with lock_mgr.acquire("ms-graph"):
        # do refresh, write back to store
        ...
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional


class CredentialLockManager:
    """Hands out one ``asyncio.Lock`` per provider key, lazily.

    Locks live for the process lifetime — there's no eviction. The set of
    providers is bounded (≤ 20 in any realistic ANTON deployment) so this
    is fine; if it ever isn't, swap in a ``weakref.WeakValueDictionary``.
    """

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        # Guards the dict itself — needed because two coroutines could race
        # to create the lock for the same provider on the very first call.
        self._dict_lock = asyncio.Lock()

    async def _lock_for(self, provider: str) -> asyncio.Lock:
        async with self._dict_lock:
            lock = self._locks.get(provider)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[provider] = lock
        return lock

    @asynccontextmanager
    async def acquire(self, provider: str) -> AsyncIterator[None]:
        """Hold the provider's lock for the duration of the context block.

        Different providers acquire independently — ``acquire("anthropic")``
        and ``acquire("ms-graph")`` never block each other.
        """
        lock = await self._lock_for(provider)
        async with lock:
            yield

    def is_locked(self, provider: str) -> bool:
        """Inspection helper — useful in tests + diagnostics. Returns False
        if the provider's lock doesn't exist yet."""
        lock = self._locks.get(provider)
        return lock.locked() if lock is not None else False

    def reset_for_tests(self) -> None:
        """Drop the lock dict — tests use this between cases."""
        self._locks.clear()


# ────────────────────────────────────────────────────────────────────────────
# Process-local singleton
# ────────────────────────────────────────────────────────────────────────────


_singleton: Optional[CredentialLockManager] = None


def get_lock_manager() -> CredentialLockManager:
    global _singleton
    if _singleton is None:
        _singleton = CredentialLockManager()
    return _singleton


def reset_lock_manager_for_tests() -> None:
    global _singleton
    _singleton = None
