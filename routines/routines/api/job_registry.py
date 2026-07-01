"""F-17 (HR S-12) — concurrency registry for fire-and-forget subprocess jobs.

The detached launchers — ``recall index`` (``recall.py``), ``memory-promote
run-all`` (``promotion.py``), and ``earnings`` (``earnings.py``) — used a bare
``subprocess.Popen`` with NO PID registry, concurrency cap, or dedup (unlike the
crew lane, which has all three). A CSRF ``{}``-Blob loop could therefore spawn
UNBOUNDED detached subprocesses → process / Ollama / Firecrawl exhaustion that
persists after the page closes.

This gives each job KIND a small in-process registry: live PIDs are tracked,
dead ones reaped, runaway ones past a wall-clock ceiling killed, and a new
launch refused with ``JobConcurrencyExceeded`` (→ 409) once the per-kind cap is
reached. The registry is in-process (the bridge is single-process) and guarded
by one lock — same model as ``crew/proxy``'s PID store.
"""

from __future__ import annotations

import logging
import subprocess
import threading
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)

# One concurrent run per kind by default — these jobs are full-vault sweeps /
# crawls; a second concurrent run is almost always an accidental double-fire,
# not intent. The operator's scheduled crons fire them serially.
DEFAULT_MAX_CONCURRENT = 1

# Hard wall-clock ceiling: a detached job still running past this on the next
# launch attempt is assumed runaway and killed + reaped. Generous — a full
# reindex / earnings sweep is minutes, not hours.
DEFAULT_MAX_WALLCLOCK_SEC = 30 * 60.0


class JobConcurrencyExceeded(Exception):
    """A launch was refused because the per-kind concurrency cap is reached."""

    def __init__(self, kind: str, running: int, cap: int):
        self.kind = kind
        self.running = running
        self.cap = cap
        super().__init__(
            f"job kind {kind!r} already has {running} run(s) in flight "
            f"(cap {cap}); refusing to launch another"
        )


@dataclass
class _TrackedProc:
    proc: subprocess.Popen
    started_at: float


_registry: dict[str, list[_TrackedProc]] = {}
_lock = threading.Lock()


def _reap_locked(kind: str, max_wallclock_sec: float) -> list[_TrackedProc]:
    """Drop finished procs; kill + drop runaway ones. Caller holds ``_lock``."""
    procs = _registry.get(kind, [])
    alive: list[_TrackedProc] = []
    now = time.monotonic()
    for tp in procs:
        if tp.proc.poll() is not None:
            continue  # already exited — reap
        if now - tp.started_at > max_wallclock_sec:
            log.warning(
                "job_registry: killing runaway %s pid=%s (age %.0fs > %.0fs)",
                kind, tp.proc.pid, now - tp.started_at, max_wallclock_sec,
            )
            try:
                tp.proc.kill()
            except OSError:
                pass
            continue
        alive.append(tp)
    _registry[kind] = alive
    return alive


def launch_tracked(
    kind: str,
    cmd: list[str],
    *,
    cwd: str | None = None,
    max_concurrent: int = DEFAULT_MAX_CONCURRENT,
    max_wallclock_sec: float = DEFAULT_MAX_WALLCLOCK_SEC,
    **popen_kwargs,
) -> subprocess.Popen:
    """Launch ``cmd`` as a tracked detached subprocess for job ``kind``.

    Reaps dead/runaway prior runs first, then refuses with
    ``JobConcurrencyExceeded`` if the live count is already at ``max_concurrent``.
    Returns the started ``Popen`` on success.
    """
    with _lock:
        alive = _reap_locked(kind, max_wallclock_sec)
        if len(alive) >= max_concurrent:
            raise JobConcurrencyExceeded(kind, len(alive), max_concurrent)
        proc = subprocess.Popen(cmd, cwd=cwd, **popen_kwargs)
        alive.append(_TrackedProc(proc=proc, started_at=time.monotonic()))
        _registry[kind] = alive
        return proc


def running_count(kind: str) -> int:
    """Live (post-reap) run count for ``kind`` — diagnostics / tests."""
    with _lock:
        return len(_reap_locked(kind, DEFAULT_MAX_WALLCLOCK_SEC))


def _reset_for_tests() -> None:
    """Drop all registry state (test-only; does NOT kill live procs)."""
    with _lock:
        _registry.clear()


__all__ = [
    "DEFAULT_MAX_CONCURRENT",
    "DEFAULT_MAX_WALLCLOCK_SEC",
    "JobConcurrencyExceeded",
    "launch_tracked",
    "running_count",
]
