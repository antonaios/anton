"""Bridge-embedded APScheduler.

Lifts the **pattern** (not the file) from AutoGPT's
``backend/executor/scheduler.py`` — see [[AUTOGPT-EVALUATION]] §2.2. The
goal is to make the bridge its own scheduler so that:

  * Cron jobs survive bridge restarts (SQLAlchemy-backed SQLite jobstore).
  * One-off / ephemeral jobs don't pollute the persistent store
    (in-memory jobstore).
  * The dashboard CRUDs schedules via REST instead of a separate Windows
    Task Scheduler surface.

Two jobstores, one ``BackgroundScheduler``:

  * ``default``   → ``SQLAlchemyJobStore`` at ``<state>/schedules.db``
    (jobs registered without a jobstore name persist here).
  * ``ephemeral`` → ``MemoryJobStore``
    (pass ``jobstore="ephemeral"`` when adding a job).

#23 STATUS: framework only — no specific job is wired (morning-brief etc.
land in a follow-on session). Operator wires concrete jobs via either
``scheduler.add_job(...)`` at app-startup time or, eventually, via the
forthcoming POST /api/scheduler/jobs endpoint.

Windows-Service note: see ``routines/scheduler/README.md`` for the nssm
install pattern that pins the bridge as a persistent system service."""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Any

from apscheduler.jobstores.memory import MemoryJobStore
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.background import BackgroundScheduler

logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────────────
# State-directory resolution
# ────────────────────────────────────────────────────────────────────────────

DEFAULT_STATE_DIR_ENV = "AGENTIC_SCHEDULER_STATE_DIR"


def _default_state_dir() -> Path:
    """Resolve where ``schedules.db`` lives.

    Order:
      1. ``AGENTIC_SCHEDULER_STATE_DIR`` env var (tests use this).
      2. ``<routines-repo>/state``.
    """
    env = os.environ.get(DEFAULT_STATE_DIR_ENV)
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[2] / "state"


# ────────────────────────────────────────────────────────────────────────────
# BridgeScheduler
# ────────────────────────────────────────────────────────────────────────────


class BridgeScheduler:
    """Thin wrapper around APScheduler ``BackgroundScheduler``.

    Encapsulates jobstore wiring + start/stop lifecycle so the FastAPI
    ``lifespan`` and the tests can both drive a clean instance.

    Public API:
      * ``start()`` / ``stop()``
      * ``add_job(func, trigger, *, jobstore='default', ...) -> Job``
      * ``list_jobs() -> list[JobInfo]``  (read-only DTO for the dashboard)
      * ``running`` — bool
    """

    def __init__(
        self,
        *,
        state_dir: Path | None = None,
        timezone: str = "UTC",
    ) -> None:
        self.state_dir = (state_dir or _default_state_dir()).resolve()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.state_dir / "schedules.db"
        self._lock = threading.Lock()
        self._scheduler = self._build_scheduler(timezone=timezone)

    def _build_scheduler(self, *, timezone: str) -> BackgroundScheduler:
        """Construct the underlying ``BackgroundScheduler`` with two jobstores.

        URL format follows SQLAlchemy: ``sqlite:///<path>`` where the file
        path is **absolute** on disk. APScheduler creates the table schema
        on first connect — no migration required."""
        sqlite_url = f"sqlite:///{self.db_path.as_posix()}"
        jobstores = {
            "default":   SQLAlchemyJobStore(url=sqlite_url),
            "ephemeral": MemoryJobStore(),
        }
        return BackgroundScheduler(
            jobstores=jobstores,
            timezone=timezone,
            # Reasonable defaults; tune later if needed.
            job_defaults={
                "coalesce": True,           # collapse missed runs into one
                "max_instances": 1,          # no overlapping runs of the same job
                "misfire_grace_time": 60,    # 60s grace for missed triggers
            },
        )

    # ── lifecycle ─────────────────────────────────────────────────────────

    def start(self) -> None:
        """Idempotent start. Safe under FastAPI reload."""
        with self._lock:
            if self._scheduler.running:
                logger.debug("BridgeScheduler already running — start() no-op")
                return
            self._scheduler.start(paused=False)
            logger.info(
                "BridgeScheduler started (db=%s, jobs=%d)",
                self.db_path, len(self._scheduler.get_jobs()),
            )

    def stop(self, *, wait: bool = False) -> None:
        """Idempotent stop."""
        with self._lock:
            if not self._scheduler.running:
                return
            self._scheduler.shutdown(wait=wait)
            logger.info("BridgeScheduler stopped")

    @property
    def running(self) -> bool:
        return self._scheduler.running

    # ── job CRUD ──────────────────────────────────────────────────────────

    def add_job(
        self,
        func: Any,
        trigger: Any = None,
        *,
        jobstore: str = "default",
        id: str | None = None,
        replace_existing: bool = False,
        **kwargs: Any,
    ) -> Any:
        """Register a job. ``jobstore="ephemeral"`` for in-memory only.

        ``func`` must be **importable** by string (e.g. ``"routines.morning_brief.run"``)
        when targeting the SQLAlchemy jobstore — pickled callables don't survive
        across processes. APScheduler accepts both forms; we don't restrict here
        because in-process jobs can pass a callable directly to ``ephemeral``."""
        return self._scheduler.add_job(
            func,
            trigger=trigger,
            jobstore=jobstore,
            id=id,
            replace_existing=replace_existing,
            **kwargs,
        )

    def remove_job(self, job_id: str, *, jobstore: str | None = None) -> None:
        self._scheduler.remove_job(job_id, jobstore=jobstore)

    def list_jobs(self) -> list[dict[str, Any]]:
        """Read-only DTO list for ``GET /api/scheduler/jobs``.

        Returns serialisable dicts (no APScheduler internals leak out). Pending
        jobs (scheduler paused, next_run_time=None) are included with
        ``next_run`` set to ``None``."""
        jobs = self._scheduler.get_jobs()
        out: list[dict[str, Any]] = []
        for j in jobs:
            out.append({
                "id": j.id,
                "name": j.name or j.id,
                "func": _describe_func(j.func_ref or j.func),
                "trigger": str(j.trigger),
                "next_run": j.next_run_time.isoformat() if j.next_run_time else None,
                "jobstore": j._jobstore_alias or "default",  # noqa: SLF001 — public attr is missing in APS 3
                "coalesce": j.coalesce,
                "max_instances": j.max_instances,
                "misfire_grace_time": j.misfire_grace_time,
            })
        return out


def _describe_func(func_ref: Any) -> str:
    """Coerce APScheduler's func reference (string path OR callable) into a
    readable label for the dashboard."""
    if isinstance(func_ref, str):
        return func_ref
    mod = getattr(func_ref, "__module__", "?")
    name = getattr(func_ref, "__qualname__", getattr(func_ref, "__name__", repr(func_ref)))
    return f"{mod}.{name}"


# ────────────────────────────────────────────────────────────────────────────
# Module singleton (consumed by app.lifespan)
# ────────────────────────────────────────────────────────────────────────────


_bridge_scheduler: BridgeScheduler | None = None
_singleton_lock = threading.Lock()


def get_scheduler() -> BridgeScheduler:
    """Module-level singleton. Lazily instantiated on first call."""
    global _bridge_scheduler
    with _singleton_lock:
        if _bridge_scheduler is None:
            _bridge_scheduler = BridgeScheduler()
    return _bridge_scheduler


def reset_scheduler_for_tests() -> None:
    """Drop the singleton — tests use this to rebuild against a tmp state dir
    after monkeypatching ``AGENTIC_SCHEDULER_STATE_DIR``."""
    global _bridge_scheduler
    with _singleton_lock:
        if _bridge_scheduler is not None and _bridge_scheduler.running:
            _bridge_scheduler.stop(wait=False)
        _bridge_scheduler = None
