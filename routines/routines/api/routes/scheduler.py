"""Scheduler API — list, pause, resume, run-now, and history for the
bridge-embedded APScheduler.

Endpoints (contract locked in OUTSTANDING ## CONTRACTS · scheduler CRUD):

  * GET  /api/scheduler/jobs                            — list (read-only)
  * POST /api/scheduler/jobs/{id}/pause                 — pause a job
  * POST /api/scheduler/jobs/{id}/resume                — resume a paused job
  * POST /api/scheduler/jobs/{id}/run-now               — fire on next tick
  * GET  /api/scheduler/jobs/{id}/history?limit=10      — last N audit rows
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from apscheduler.jobstores.base import JobLookupError
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from routines.api.deps import RUNS_DIR
from routines.scheduler import get_scheduler
from routines.scheduler import suspension
from routines.scheduler.jobs import get_job_specs
from routines.shared import audit

router = APIRouter()
log = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────────────
# DTOs
# ────────────────────────────────────────────────────────────────────────────


class JobDTO(BaseModel):
    id: str
    name: str
    func: str
    trigger: str
    next_run: str | None = None
    jobstore: str
    coalesce: bool
    max_instances: int
    misfire_grace_time: int | None = None


class JobsListResponse(BaseModel):
    running: bool
    jobs: list[JobDTO]


class PauseResponse(BaseModel):
    # ``durable`` (contract amendment 2026-06-10): True when the pause is
    # persisted to state/scheduler-paused.json and survives bridge
    # restarts — i.e. the id is a cron-registry spec. False for ad-hoc /
    # one-shot jobs, whose pause remains live-only.
    id: str
    paused: bool = True
    durable: bool = False


class ResumeResponse(BaseModel):
    id: str
    paused: bool = False
    durable: bool = False


class RunNowResponse(BaseModel):
    id: str
    status: str = "queued"
    run_id: str


class SchedulerRunRecord(BaseModel):
    ts: str
    run_id: str
    status: str
    duration_ms: int | None = None
    error_class: str | None = None
    error: str | None = None


class JobHistoryResponse(BaseModel):
    runs: list[SchedulerRunRecord]


# ────────────────────────────────────────────────────────────────────────────
# GET /api/scheduler/jobs
# ────────────────────────────────────────────────────────────────────────────


@router.get("/scheduler/jobs", response_model=JobsListResponse)
def list_scheduler_jobs() -> JobsListResponse:
    """List every registered job in either jobstore.

    Returns ``running=false`` if the scheduler is paused or not yet started
    — the dashboard surfaces that as a "scheduler offline" indicator rather
    than a 500."""
    sched = get_scheduler()
    return JobsListResponse(
        running=sched.running,
        jobs=[JobDTO(**j) for j in sched.list_jobs()],
    )


# ────────────────────────────────────────────────────────────────────────────
# Pause / Resume
# ────────────────────────────────────────────────────────────────────────────


def _get_job_or_404(job_id: str):
    """Look up a registered job by id. Raises 404 if not found."""
    sched = get_scheduler()
    job = sched._scheduler.get_job(job_id)  # noqa: SLF001 — public method via wrapper
    if job is None:
        raise HTTPException(status_code=404, detail=f"job {job_id!r} not found")
    return sched, job


def _is_registry_spec(job_id: str) -> bool:
    """True when ``job_id`` is a cron-registry spec — the only ids whose
    pause state is persisted. Ad-hoc jobs and ``-catchup`` one-shots stay
    live-only (the catch-up twin is suppressed at boot via its base spec)."""
    return any(spec.id == job_id for spec in get_job_specs())


@router.post("/scheduler/jobs/{job_id}/pause", response_model=PauseResponse)
def pause_scheduler_job(job_id: str) -> PauseResponse:
    """Pause a job — keeps it registered but skips future triggers until
    resume. Audit-logged so a forensic walk-back can correlate quiet
    periods with operator action.

    Cron-registry specs get a DURABLE pause: the id is persisted to
    ``state/scheduler-paused.json`` so ``register_all_jobs()`` re-registers
    the job paused on every bridge restart (the ephemeral jobstore would
    otherwise re-arm it — the 2026-06-10 incident). Any pending
    ``{job_id}-catchup`` one-shot is removed too, so a paused
    ``fire-on-startup`` job can't sneak its catch-up in."""
    sched, _ = _get_job_or_404(job_id)
    sched._scheduler.pause_job(job_id)  # noqa: SLF001
    durable = _is_registry_spec(job_id)
    removed_catchup = False
    if durable:
        # Live-state cleanup BEFORE persistence: the catch-up twin must die
        # with the pause even when the persist below fails (codex SEV-2 —
        # otherwise the pending one-shot still fires in this process).
        try:
            sched._scheduler.remove_job(f"{job_id}-catchup")  # noqa: SLF001
            removed_catchup = True
        except JobLookupError:
            pass   # no pending catch-up twin — the common case
        try:
            suspension.suspend(job_id)
        except OSError as e:
            # The live pause DID apply — but it would silently lift at the
            # next restart, which is the exact failure this endpoint now
            # guarantees against. Surface loudly instead.
            raise HTTPException(
                status_code=500,
                detail=(
                    f"job {job_id!r} paused live, but persisting the pause "
                    f"failed ({e}) — pause will NOT survive a bridge restart"
                ),
            ) from e
    audit.write_structured(
        actor={"type": "user", "id": "operator"},
        entity_type="scheduler_job",
        entity_id=job_id,
        action="pause",
        routine=f"scheduler.{job_id}",
        run_id=audit.new_run_id(),
        status="paused",
        audit_dir=RUNS_DIR,
        inputs={"action": "pause"},
        outputs={"durable": durable, "removed_catchup": removed_catchup},
    )
    return PauseResponse(id=job_id, paused=True, durable=durable)


@router.post("/scheduler/jobs/{job_id}/resume", response_model=ResumeResponse)
def resume_scheduler_job(job_id: str) -> ResumeResponse:
    """Resume a paused job. For cron-registry specs, also removes the id
    from the persisted paused set so the next bridge restart registers the
    job active again. No retroactive catch-up fires — the next natural
    trigger (or POST run-now) takes it from here."""
    sched, _ = _get_job_or_404(job_id)
    sched._scheduler.resume_job(job_id)  # noqa: SLF001
    durable = _is_registry_spec(job_id)
    if durable:
        try:
            suspension.unsuspend(job_id)
        except OSError as e:
            raise HTTPException(
                status_code=500,
                detail=(
                    f"job {job_id!r} resumed live, but removing the persisted "
                    f"pause failed ({e}) — job would re-pause at the next "
                    f"bridge restart"
                ),
            ) from e
    audit.write_structured(
        actor={"type": "user", "id": "operator"},
        entity_type="scheduler_job",
        entity_id=job_id,
        action="resume",
        routine=f"scheduler.{job_id}",
        run_id=audit.new_run_id(),
        status="resumed",
        audit_dir=RUNS_DIR,
        inputs={"action": "resume"},
        outputs={"durable": durable},
    )
    return ResumeResponse(id=job_id, paused=False, durable=durable)


# ────────────────────────────────────────────────────────────────────────────
# Run-now
# ────────────────────────────────────────────────────────────────────────────


@router.post("/scheduler/jobs/{job_id}/run-now", response_model=RunNowResponse)
def run_scheduler_job_now(job_id: str) -> RunNowResponse:
    """Fire ``job_id`` on the scheduler's next tick.

    Implementation: nudge ``next_run_time`` forward to ``now`` so
    APScheduler's main loop picks it up immediately (within seconds, not
    minutes). The actual run happens in the scheduler's worker thread —
    the response is "queued", not "ran". Correlate the audit row in
    ``runs/scheduler.<id>.jsonl`` via the returned ``run_id``."""
    sched, job = _get_job_or_404(job_id)
    run_id = audit.new_run_id()
    try:
        # APScheduler's modify() accepts ``next_run_time`` as a tz-aware
        # datetime. Trigger timezone is set on each spec; reuse it so the
        # comparison stays consistent.
        now_tz = datetime.now(getattr(job.trigger, "timezone", timezone.utc))
        job.modify(next_run_time=now_tz)
    except Exception as e:  # noqa: BLE001 — surface as 500 with detail
        audit.write_structured(
            actor={"type": "user", "id": "operator"},
            entity_type="scheduler_job",
            entity_id=job_id,
            action="run-now",
            routine=f"scheduler.{job_id}",
            run_id=run_id,
            status="error",
            audit_dir=RUNS_DIR,
            inputs={"action": "run-now"},
            error=f"{type(e).__name__}: {e}",
        )
        raise HTTPException(500, f"run-now failed: {e}") from e
    audit.write_structured(
        actor={"type": "user", "id": "operator"},
        entity_type="scheduler_job",
        entity_id=job_id,
        action="run-now",
        routine=f"scheduler.{job_id}",
        run_id=run_id,
        status="queued",
        audit_dir=RUNS_DIR,
        inputs={"action": "run-now"},
        outputs={"next_run": now_tz.isoformat()},
    )
    return RunNowResponse(id=job_id, status="queued", run_id=run_id)


# ────────────────────────────────────────────────────────────────────────────
# History
# ────────────────────────────────────────────────────────────────────────────


@router.get("/scheduler/jobs/{job_id}/history", response_model=JobHistoryResponse)
def scheduler_job_history(
    job_id: str,
    limit: int = Query(10, ge=1, le=200),
) -> JobHistoryResponse:
    """Tail the last ``limit`` audit rows from ``runs/scheduler.<job_id>.jsonl``.

    Returns latest first. 404 only when the job isn't registered today —
    NOT when the audit log is missing (a freshly-registered job has no
    history yet; empty array is the natural response)."""
    # Validate registration so a typo'd id returns 404 not "empty list".
    _get_job_or_404(job_id)

    audit_path: Path = RUNS_DIR / f"scheduler.{job_id}.jsonl"
    if not audit_path.is_file():
        return JobHistoryResponse(runs=[])

    rows: list[SchedulerRunRecord] = []
    try:
        with audit_path.open("r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError as e:
        log.warning("scheduler history: read failed for %s: %s", audit_path, e)
        return JobHistoryResponse(runs=[])

    # Walk in reverse — latest first — and stop once we have ``limit``.
    for raw in reversed(lines):
        line = raw.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        rows.append(SchedulerRunRecord(
            ts=str(rec.get("ts", "")),
            run_id=str(rec.get("run_id", "")),
            status=str(rec.get("status", "ok")),
            duration_ms=rec.get("duration_ms"),
            error_class=_extract_error_class(rec.get("error")),
            error=rec.get("error"),
        ))
        if len(rows) >= limit:
            break
    return JobHistoryResponse(runs=rows)


def _extract_error_class(error: Optional[str]) -> Optional[str]:
    """Pull the leading ``<ClassName>:`` off the audit error string so the
    dashboard can render it as a separate field. Falls back to ``None``
    when the format doesn't match."""
    if not error or not isinstance(error, str):
        return None
    head, sep, _rest = error.partition(":")
    return head.strip() if sep else None
