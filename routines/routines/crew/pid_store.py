"""run_id → subprocess-HANDLE map for the crew lane (#31).

In-memory and dies with the bridge — deliberately, matching the v1 "no crew
resume after bridge restart" rule (METAGPT-INTEGRATION-SPEC.md §7.2). Thread-
safe because ``/cancel`` (request thread) races the crew worker thread.

F-40 (CX A-05): the store keeps the ``subprocess.Popen`` OBJECT, not the
integer PID. Cancelling by raw PID races PID reuse — between the child's
exit and the worker thread's ``pop()`` the OS can hand the number to an
unrelated process, and ``os.kill`` would terminate the wrong target. The
Popen handle is pinned to the specific process (on Windows the open handle
also prevents the PID from being recycled), so ``proc.terminate()`` can
never cross-kill.

Also tracks a per-run **cancelled flag** so the worker thread can tell an
operator cancel apart from a genuine crash after the subprocess dies: the
cancel endpoint terminates the child *and* marks the run; the worker thread
checks the mark when the process exits non-zero and writes
``status="cancelled"`` instead of ``status="error"`` to the audit. (The
staged spec's audit matrix required this distinction but its sketch had no
mechanism for it.)
"""

from __future__ import annotations

import threading
from typing import Protocol


class ProcessHandle(Protocol):
    """The slice of ``subprocess.Popen`` the store's consumers rely on."""

    pid: int

    def poll(self) -> int | None: ...
    def terminate(self) -> None: ...


class PIDStore:
    """Thread-safe run_id → process-handle map + cancelled-run marks."""

    def __init__(self) -> None:
        self._procs: dict[str, ProcessHandle] = {}
        self._cancelled: set[str] = set()
        self._lock = threading.Lock()

    def put(self, run_id: str, proc: ProcessHandle) -> None:
        with self._lock:
            self._procs[run_id] = proc

    def get(self, run_id: str) -> ProcessHandle | None:
        with self._lock:
            return self._procs.get(run_id)

    def pop(self, run_id: str) -> ProcessHandle | None:
        with self._lock:
            return self._procs.pop(run_id, None)

    def mark_cancelled(self, run_id: str) -> None:
        with self._lock:
            self._cancelled.add(run_id)

    def was_cancelled(self, run_id: str) -> bool:
        """Consume the cancelled mark (one-shot read; keeps the set bounded)."""
        with self._lock:
            if run_id in self._cancelled:
                self._cancelled.discard(run_id)
                return True
            return False

    def _reset_for_tests(self) -> None:
        with self._lock:
            self._procs.clear()
            self._cancelled.clear()


# Module singleton — the bridge route + proxy share it.
pid_store = PIDStore()

__all__ = ["PIDStore", "ProcessHandle", "pid_store"]
