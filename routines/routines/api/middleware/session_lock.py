"""#59 — per-session coalescing lock with stale-timeout auto-release.

Today's ``POST /api/sessions/{id}/messages`` is silent on concurrency:
double-click on the chat composer fires two requests; both go to
``store.append_message`` and both invoke ``sess_router.route_and_respond``,
producing duplicate user messages + duplicate LLM calls. Once async skill
persistence lands (#3c, deferred), an orphaned crashed run also leaves
the session ``in_progress`` forever with nothing to release it.

This module provides a per-session lock with three branches the API
route uses to discriminate:

1. **Fresh acquire** — no entry for ``session_id``; new lock created and
   acquired with ``(run_id, now)``.
2. **Idempotent same-id retry** — entry exists and ``entry.run_id ==
   run_id``. The lock is held by THIS run already; ``SessionLockBusy``
   is raised with ``pending_run_id == run_id`` and the route returns 409
   so the retrying client knows to wait, not double-fire. (v2 will share
   a Future so the retry receives the same response — see brief §"Optional
   polish items".)
3. **Different-id contention** — entry exists and ``entry.run_id !=
   run_id``. If the lock is < ``stale_after`` seconds old, raise
   ``SessionLockBusy`` (route → 409). If ≥ ``stale_after``, the prior
   run is assumed crashed: WARNING logged, lock force-released, fresh
   one acquired for the new ``run_id``.

The route is sync (``def post_message`` — not ``async def``), so the
primitive is ``threading.Lock``, not ``asyncio.Lock``. The bridge runs
sync route handlers in a thread pool (Starlette → anyio → worker
threads); ``threading.Lock`` is the correct cross-thread primitive
there. The locks-table meta-lock is also a ``threading.Lock`` — it
protects the dict mutation only, not the per-session locks themselves.

Why not ``asyncio.Lock`` (as the brief sketched)? Because the route is
sync — switching it to async would put the LLM call (the slow blocking
step) on the event loop, blocking all other routes. Easier to keep the
route sync and use thread-friendly primitives.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)

STALE_AFTER_SEC = 60.0


@dataclass
class _LockEntry:
    run_id: str
    acquired_at: float
    lock: threading.Lock


_session_locks: dict[str, _LockEntry] = {}
_locks_table_lock = threading.Lock()  # protects the dict itself only


class SessionLockBusy(Exception):
    """Raised when an acquire would conflict with an in-flight run.

    Two cases (the caller decides whether to differentiate in HTTP
    responses):

      * ``pending_run_id == request_run_id`` — same run retrying while
        the first call is in flight. Client should poll / wait, NOT
        retry with a new id.
      * ``pending_run_id != request_run_id`` — genuinely concurrent
        different-id contention. Client (or upstream coalescer) needs
        to back off or wait for the prior run to finish.
    """

    def __init__(self, session_id: str, pending_run_id: str, acquired_at: float):
        self.session_id = session_id
        self.pending_run_id = pending_run_id
        self.acquired_at = acquired_at
        self.acquired_age_sec = time.time() - acquired_at
        super().__init__(
            f"session {session_id} has pending run {pending_run_id} "
            f"acquired {self.acquired_age_sec:.1f}s ago"
        )


def acquire_session_lock(
    session_id: str,
    run_id: str,
    stale_after: float = STALE_AFTER_SEC,
) -> None:
    """Acquire the lock for ``session_id`` on behalf of ``run_id``.

    Returns ``None`` on success. The caller MUST pair every successful
    acquire with ``release_session_lock(session_id, run_id)`` in a
    ``try/finally`` so the lock releases even if the route raises.

    Raises ``SessionLockBusy`` if:

      * Same ``run_id`` is already in flight (idempotency: the second
        call is told to wait, not double-fire), OR
      * A different ``run_id`` holds the lock and its age is
        ``< stale_after``.

    If a different ``run_id`` holds the lock and its age is
    ``>= stale_after``, force-releases (WARNING log) and acquires fresh.
    """
    with _locks_table_lock:
        entry = _session_locks.get(session_id)
        now = time.time()

        if entry is None:
            lock = threading.Lock()
            acquired = lock.acquire(blocking=False)
            assert acquired, "freshly-created lock should always acquire"
            _session_locks[session_id] = _LockEntry(run_id, now, lock)
            return

        if entry.run_id == run_id:
            # Same-id retry while still in flight — explicit conflict so
            # the route returns 409 instead of silently double-firing.
            raise SessionLockBusy(session_id, entry.run_id, entry.acquired_at)

        age = now - entry.acquired_at
        if age < stale_after:
            raise SessionLockBusy(session_id, entry.run_id, entry.acquired_at)

        # Stale prior run — assume crashed. Force-release + acquire fresh.
        log.warning(
            "session_lock: force-releasing stale lock — session=%s held_by=%s age=%.1fs",
            session_id, entry.run_id, age,
        )
        try:
            entry.lock.release()
        except RuntimeError:
            # Already released (defensive — shouldn't happen in our code
            # path, but the table's source of truth is _session_locks,
            # not the lock object's internal state).
            pass

        lock = threading.Lock()
        acquired = lock.acquire(blocking=False)
        assert acquired, "freshly-created lock should always acquire"
        _session_locks[session_id] = _LockEntry(run_id, now, lock)


def release_session_lock(session_id: str, run_id: str) -> None:
    """Release the lock if held by ``run_id``; no-op otherwise.

    Defensive against double-release and against releasing after a
    stale-force-release handed the lock to a different run_id (the
    original owner's ``finally`` block fires later and finds the entry
    owned by someone else — must be a no-op, not an error).
    """
    with _locks_table_lock:
        entry = _session_locks.get(session_id)
        if entry is None or entry.run_id != run_id:
            return
        try:
            entry.lock.release()
        except RuntimeError:
            pass
        del _session_locks[session_id]


def _peek_entry(session_id: str) -> _LockEntry | None:
    """Test/diagnostic helper — read the current entry without acquiring.

    Not part of the public API contract. Tests use this to assert
    invariants on the table after each operation.
    """
    with _locks_table_lock:
        return _session_locks.get(session_id)


def _reset_for_tests() -> None:
    """Drop all lock state. Test-only — never call from app code."""
    with _locks_table_lock:
        for entry in _session_locks.values():
            try:
                entry.lock.release()
            except RuntimeError:
                pass
        _session_locks.clear()


__all__ = [
    "STALE_AFTER_SEC",
    "SessionLockBusy",
    "acquire_session_lock",
    "release_session_lock",
]
