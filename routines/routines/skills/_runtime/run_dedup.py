"""#63 L2 — per-``run_id`` idempotency/dedup for skill routes.

The chat path coalesces on ``run_id`` (``api/middleware/session_lock.py``), but
SKILL routes do NOT — a network-retried request carrying the SAME
``X-ANTON-Run-Id`` (the #59 dashboard mints one per logical action and REUSES it
on retry) would re-execute the body, double-firing side effects (duplicate #76
proposals, a double budget charge). The ``@anton_skill`` wrapper carries the
run_id (`current_run_id()`), so it records each run inflight → done and
deterministically coalesces a duplicate:

  * **fresh** run_id → register inflight (mint an OWNER TOKEN), run the body.
  * **inflight** duplicate (a retry while the first is still running) → reject
    with :class:`RunInFlight` (the wrapper maps it to 409: wait, don't re-fire).
  * **done** duplicate → REPLAY the first run's outcome — a FRESH reconstruction
    of the cached snapshot (the gold-standard idempotency semantic: a
    lost-response retry gets the original answer, 200, not a second execution).
  * the body **failed** → ``abandon_run`` drops the entry so a retry can
    re-attempt (a transient 500 is legitimately retryable).

**Owner tokens (codex-5.5 SEV-1).** A crashed inflight run is reclaimable after
``STALE_AFTER_SEC`` so a run_id can't wedge forever — but the original thread may
still be alive. So ``begin_run`` mints a per-attempt owner token; only the holder
of the current token may ``complete_run`` / ``abandon_run``. A late completion
from a reclaimed-over attempt finds a different token and is a clean no-op
(it can't mark the new attempt done with stale data, or drop its entry).

In-memory + single-process (mirrors ``session_lock``). A ``threading.Lock``
guards the table (sync route handlers run in Starlette's thread pool). All time
is ``time.monotonic`` (immune to wall-clock jumps — codex SEV-3). ``done``
entries evict after ``DONE_TTL_SEC`` so the table stays bounded.

ONLY the first-call path uses this. A RESUME re-invocation reuses the run_id but
is guarded by the suspension's atomic single-winner claim
(:mod:`routines.skills._runtime.suspensions`), not this dedup. **Note:** a
suspend's 202 is cached as the run's outcome, so a retry of the ORIGINAL request
replays that 202 for up to ``DONE_TTL_SEC`` even after the operator has resumed
the run elsewhere — harmless (resume's claim is the real guard; the client would
just re-see an already-consumed suspension), but a known client contract.
"""

from __future__ import annotations

import copy
import logging
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Optional

from starlette.responses import Response

log = logging.getLogger(__name__)

STALE_AFTER_SEC = 120.0   # an inflight run older than this is assumed crashed
DONE_TTL_SEC = 300.0      # cached results evict after this so the table is bounded


@dataclass
class _RunEntry:
    state: str                 # "inflight" | "done"
    token: str                 # per-attempt owner token (anti-reclaim-race)
    started_at: float          # monotonic
    result: Any = None         # done: the cached snapshot (see _snapshot)
    is_response: bool = False   # True if ``result`` snapshots a Response
    completed_at: Optional[float] = None  # monotonic


@dataclass
class BeginResult:
    """Outcome of :func:`begin_run`. ``proceed`` → run the body with ``token``;
    otherwise ``replay`` is a FRESH reconstruction of the prior outcome."""

    proceed: bool
    token: Optional[str] = None
    replay: Any = None


_runs: dict[str, _RunEntry] = {}
_table_lock = threading.Lock()


class RunInFlight(Exception):
    """A run with this ``run_id`` is already executing (a retry arrived before
    the first call finished). The wrapper maps this to HTTP 409 — the client
    should wait for the original, not re-fire."""

    def __init__(self, run_id: str, age_sec: float):
        self.run_id = run_id
        self.age_sec = age_sec
        super().__init__(
            f"run {run_id} is already in flight (started {age_sec:.1f}s ago)"
        )


def _evict_stale_done(now: float) -> None:
    """Drop ``done`` entries past their TTL. Caller holds ``_table_lock``."""
    expired = [
        rid for rid, e in _runs.items()
        if e.state == "done" and e.completed_at is not None
        and (now - e.completed_at) > DONE_TTL_SEC
    ]
    for rid in expired:
        del _runs[rid]


def _snapshot(result: Any, is_response: bool) -> Any:
    """An IMMUTABLE-ish snapshot for replay — never the live object (codex
    SEV-2). A Response → its (status, body bytes, media_type); anything else →
    stored as-is and deep-copied on read."""
    if is_response and isinstance(result, Response):
        return {
            "status_code": result.status_code,
            "body": result.body,
            "media_type": result.media_type,
        }
    return result


def _reconstruct(entry: _RunEntry) -> Any:
    """Build a FRESH replay value from the snapshot so two retries never share a
    mutable instance (codex SEV-2)."""
    if entry.is_response:
        snap = entry.result
        return Response(
            content=snap["body"],
            status_code=snap["status_code"],
            media_type=snap["media_type"],
        )
    try:
        return copy.deepcopy(entry.result)
    except Exception:  # noqa: BLE001 — exotic/unpicklable result: best-effort as-is
        return entry.result


def begin_run(run_id: str, *, stale_after: float = STALE_AFTER_SEC) -> BeginResult:
    """Register ``run_id`` as inflight, or surface a prior outcome.

    Returns a :class:`BeginResult`:
      * ``proceed=True`` (fresh or reclaimed-stale): run the body, then pass the
        returned ``token`` to exactly one of ``complete_run`` / ``abandon_run``.
      * ``proceed=False``: a completed duplicate — ``replay`` is a fresh
        reconstruction of the original outcome; return it without re-running.

    Raises :class:`RunInFlight` if a non-stale inflight entry exists.
    """
    now = time.monotonic()
    with _table_lock:
        _evict_stale_done(now)
        entry = _runs.get(run_id)
        if entry is None:
            token = uuid.uuid4().hex
            _runs[run_id] = _RunEntry(state="inflight", token=token, started_at=now)
            return BeginResult(proceed=True, token=token)
        if entry.state == "done":
            return BeginResult(proceed=False, replay=_reconstruct(entry))
        # inflight
        age = now - entry.started_at
        if age < stale_after:
            raise RunInFlight(run_id, age)
        # stale inflight — assume the prior run crashed. Reclaim with a NEW token
        # so a late completion from the crashed attempt can't mutate this one.
        log.warning("run_dedup: reclaiming stale inflight run=%s age=%.1fs", run_id, age)
        token = uuid.uuid4().hex
        _runs[run_id] = _RunEntry(state="inflight", token=token, started_at=now)
        return BeginResult(proceed=True, token=token)


def complete_run(run_id: str, token: str, result: Any, *, is_response: bool = False) -> None:
    """Mark ``run_id`` done and cache a snapshot for replay. No-op unless the
    inflight entry is still owned by ``token`` (a reclaimed-over attempt must not
    overwrite the newer run — codex SEV-1)."""
    now = time.monotonic()
    with _table_lock:
        entry = _runs.get(run_id)
        if entry is None or entry.state != "inflight" or entry.token != token:
            return
        entry.state = "done"
        entry.result = _snapshot(result, is_response)
        entry.is_response = is_response
        entry.completed_at = now


def abandon_run(run_id: str, token: str) -> None:
    """Drop the inflight entry so a retry can re-attempt — called when the body
    FAILED. No-op unless still owned by ``token`` (a reclaimed-over attempt's
    failure must not delete the newer run's entry — codex SEV-1)."""
    with _table_lock:
        entry = _runs.get(run_id)
        if entry is None or entry.state != "inflight" or entry.token != token:
            return
        del _runs[run_id]


def _peek(run_id: str) -> Optional[_RunEntry]:
    """Test/diagnostic read without mutating. Not a public contract."""
    with _table_lock:
        return _runs.get(run_id)


def _reset_for_tests() -> None:
    """Drop all dedup state. Test-only."""
    with _table_lock:
        _runs.clear()


__all__ = [
    "STALE_AFTER_SEC",
    "DONE_TTL_SEC",
    "RunInFlight",
    "BeginResult",
    "begin_run",
    "complete_run",
    "abandon_run",
]
