"""Concrete cron-job registry — wires routine CLIs into the BridgeScheduler.

#23 scaffold shipped the BridgeScheduler with zero registered jobs. This
module is the follow-on: declarative registry of which routines fire on
which cron triggers, plus the ``register_all_jobs()`` entrypoint called
from the FastAPI lifespan.

Pattern:
  * Each job declared as a ``JobSpec`` tuple — keeps the cron table
    readable as a single block. Add a row to register a new job.
  * Every scheduled callable is wrapped by ``_wrap_for_audit`` so each
    fire writes a row to ``routines/runs/scheduler.<job_id>.jsonl``. The
    audit format mirrors the rest of the routines (per
    ``routines.shared.audit.write``).
  * Subprocess invocation (not in-process import) matches the existing
    bridge routes that already drive these CLIs:
    ``routines/api/routes/sectornews.py`` + ``promotion.py``. The benefit
    is twofold: (a) no per-routine refactor to extract a clean
    ``run()`` from the click command, and (b) crashes / unhandled
    exceptions in a routine don't bring down the bridge.

Jobstore choice: every cron job lands in the ``ephemeral`` jobstore.
Durability comes from re-registering every job in the FastAPI lifespan,
not from pickled SQLAlchemy rows. Operator-defined ad-hoc jobs would
target ``default`` (SQLAlchemyJobStore) where the callable must be
importable by string.

Timezone: every trigger declares ``Europe/London`` explicitly — the
operator's TZ per ``profile.md``. APScheduler accepts a ``str`` and
resolves it via ``pytz``/``ZoneInfo`` internally.

#66 — every spec declares an explicit ``concurrency`` policy
(``skip`` | ``queue`` | ``cancel_previous``) and ``catchup`` policy
(``skip-missed`` | ``fire-on-startup``). Today's 5 jobs all use
``concurrency="skip"`` + ``catchup="skip-missed"`` which preserves
historical behaviour. Future jobs choose explicitly — the field-name
vocabulary is borrowed from the Paperclip + AutoGPT eval intersection
so future authors stop reinventing semantics.
"""

from __future__ import annotations

import inspect
import json
import logging
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Literal, Optional

from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from routines.api.deps import RUNS_DIR
from routines.scheduler import get_scheduler
from routines.scheduler.suspension import load_suspended
from routines.shared import audit

logger = logging.getLogger(__name__)


# Per-job subprocess timeout. Each routine has a different runtime
# (vault-health = seconds, sector-news = a few minutes on Ollama).
# Generous default; tighten per-job if needed in the registry.
DEFAULT_TIMEOUT_SECONDS = 20 * 60   # 20 min


# Closed enums for the policy fields. Surfaced as module-level constants
# so tests + dashboard introspection can reference the canonical sets.
ConcurrencyPolicy = Literal["skip", "queue", "cancel_previous"]
CatchupPolicy = Literal["skip-missed", "fire-on-startup"]

_VALID_CONCURRENCY = ("skip", "queue", "cancel_previous")
_VALID_CATCHUP = ("skip-missed", "fire-on-startup")


# ────────────────────────────────────────────────────────────────────────────
# Per-job cancellation events (used by concurrency="cancel_previous")
# ────────────────────────────────────────────────────────────────────────────


# Module-level so the same instance is visible across overlapping runs of
# the same spec.id. The runner-wrapper signals here when a new fire arrives
# while a prior is still in flight under cancel_previous semantics.
_cancellation_events: dict[str, threading.Event] = {}
_cancellation_lock = threading.Lock()


def _request_cancellation(job_id: str) -> bool:
    """Set the cancellation event for the in-flight run of ``job_id``.

    Returns True if a prior event was found and signalled, False if no
    prior run was tracked."""
    with _cancellation_lock:
        prior = _cancellation_events.get(job_id)
        if prior is None:
            return False
        prior.set()
        return True


# ────────────────────────────────────────────────────────────────────────────
# Job spec + registry
# ────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class JobSpec:
    """One row in the cron registry.

    Fields:
      * ``id``           — APScheduler job id; also the basename of the
        audit JSONL file (``runs/scheduler.<id>.jsonl``). Stable across
        bridge restarts so history queries work.
      * ``description``  — human-readable label for the dashboard.
      * ``module_cli``   — Python module path of the click CLI to launch
        as a subprocess (e.g. ``"routines.morning_brief.cli"``).
      * ``cli_args``     — list of args after the module (e.g. ``["generate"]``).
      * ``trigger``      — APScheduler ``CronTrigger`` instance.
      * ``sensitivity``  — declared sensitivity tier; surfaces in the
        audit row for compliance review.
      * ``concurrency``  — #66 — what to do when a new trigger fires while
        the prior run is still in flight. ``skip`` (default) drops the new
        trigger via ``max_instances=1`` + ``coalesce=True``; ``queue``
        permits up to ``max_concurrent_instances`` overlapping runs;
        ``cancel_previous`` signals the prior runner to bail (cooperative —
        the runner must accept a ``cancel: threading.Event`` kwarg).
      * ``catchup``      — #66 — what to do at bridge start if a scheduled
        fire was missed. ``skip-missed`` (default, current behaviour)
        relies on ``coalesce`` to merge missed fires; ``fire-on-startup``
        queues a one-shot ``DateTrigger`` immediately after registration
        to catch up the missed window.
      * ``max_concurrent_instances`` — only consulted when
        ``concurrency="queue"``. Default 5. APScheduler enforces via
        ``max_instances``.
    """

    id: str
    description: str
    module_cli: str
    cli_args: tuple[str, ...]
    trigger: CronTrigger
    sensitivity: str = "internal"
    concurrency: ConcurrencyPolicy = "skip"
    catchup: CatchupPolicy = "skip-missed"
    max_concurrent_instances: int = 1

    def __post_init__(self) -> None:
        if self.concurrency not in _VALID_CONCURRENCY:
            raise ValueError(
                f"JobSpec(id={self.id!r}): concurrency must be one of "
                f"{_VALID_CONCURRENCY}, got {self.concurrency!r}"
            )
        if self.catchup not in _VALID_CATCHUP:
            raise ValueError(
                f"JobSpec(id={self.id!r}): catchup must be one of "
                f"{_VALID_CATCHUP}, got {self.catchup!r}"
            )
        if self.max_concurrent_instances < 1:
            raise ValueError(
                f"JobSpec(id={self.id!r}): max_concurrent_instances must "
                f"be >= 1, got {self.max_concurrent_instances}"
            )


_LONDON = "Europe/London"


# Cron registry. Add a row + re-fire ``register_all_jobs()`` (or restart
# the bridge) to wire a new routine. ID stability matters for audit-log
# continuity — don't rename ids casually.
#
# Every spec declares concurrency + catchup explicitly per #66, even
# when accepting the defaults. The explicit declarations document the
# author's intent and prevent silent semantic drift on future
# default changes.
_JOB_SPECS: tuple[JobSpec, ...] = (
    JobSpec(
        id="morning-brief",
        description="Morning brief — overnight Ollama-synthesised summary",
        module_cli="routines.morning_brief.cli",
        cli_args=("generate",),
        trigger=CronTrigger(day_of_week="mon-fri", hour=6, minute=30, timezone=_LONDON),
        sensitivity="internal",
        concurrency="skip",
        catchup="skip-missed",
    ),
    JobSpec(
        id="daily-digest",
        description="Daily digest — EOD audit + vault-write wrap-up",
        module_cli="routines.daily_digest.cli",
        cli_args=("generate",),
        trigger=CronTrigger(hour=17, minute=0, timezone=_LONDON),
        sensitivity="internal",
        concurrency="skip",
        catchup="skip-missed",
    ),
    JobSpec(
        id="vault-health-links",
        description="Vault health — orphan wikilink sweep",
        module_cli="routines.vault_health.cli",
        cli_args=("links",),
        trigger=CronTrigger(day_of_week="mon", hour=8, minute=0, timezone=_LONDON),
        sensitivity="internal",
        concurrency="skip",
        catchup="skip-missed",
    ),
    JobSpec(
        id="vault-health-freshness",
        description="Vault health — sector claim freshness sweep",
        module_cli="routines.vault_health.cli",
        cli_args=("freshness",),
        trigger=CronTrigger(day_of_week="mon", hour=8, minute=30, timezone=_LONDON),
        sensitivity="internal",
        concurrency="skip",
        catchup="skip-missed",
    ),
    JobSpec(
        id="sector-news",
        description="Sector newsletter — Firecrawl + Ollama per news-coverage row (fallback: active sectors)",
        module_cli="routines.sectornews.cli",
        cli_args=("run-all",),
        trigger=CronTrigger(day_of_week="mon-fri", hour=7, minute=0, timezone=_LONDON),
        sensitivity="internal",
        concurrency="skip",
        catchup="skip-missed",
    ),
    JobSpec(
        # #73 — Dream Cycle Phase 5 weekly self-reflection. Sandwich Sun
        # 18:00 BST: after weekend silence, before Mon 06:30 morning-brief
        # so insights are ready when the operator opens the dashboard.
        # catchup="fire-on-startup" because the routine's whole purpose is
        # operability surfacing — a missed fire-time after a bridge restart
        # should still produce output rather than silently drop.
        id="system-insights",
        description="System self-reflection — Dream Cycle Phase 5 (#73)",
        module_cli="routines.learning.system_insights.cli",
        cli_args=("analyse",),
        trigger=CronTrigger(day_of_week="sun", hour=18, minute=0, timezone=_LONDON),
        sensitivity="internal",
        concurrency="skip",
        catchup="fire-on-startup",
    ),
    JobSpec(
        # WS2 Decision-3 — weekly snapshot of the canonical precedent
        # transactions tracker. Mon 02:30 London: between the Sun 18:00
        # system-insights and the Mon 06:30 morning-brief — no collision
        # with any existing job. Snapshot is idempotent (same-day skips,
        # live-unchanged skips) so a missed fire is not catastrophic;
        # catchup="skip-missed" is the conservative default.
        id="precedent-tracker-snapshot",
        description="Weekly snapshot of the canonical precedent transactions tracker",
        module_cli="routines.dealtracker.cli",
        cli_args=("snapshot",),
        trigger=CronTrigger(day_of_week="mon", hour=2, minute=30, timezone=_LONDON),
        sensitivity="internal",
        concurrency="skip",
        catchup="skip-missed",
    ),
    JobSpec(
        # #44 — calendar-driven public-company earnings tracker. Daily 07:30
        # London: AFTER sector-news (07:00) so the morning fetch/extract budget
        # is spent in a predictable order. The sweep is idempotent (a re-fire on
        # an already-captured period is a no-op) AND self-catching-up within the
        # day (overdue companies are re-swept until their announcement is
        # captured), so a missed fire is not catastrophic — but
        # catchup="fire-on-startup" still queues a one-shot catch-up on bridge
        # restart so a down-at-07:30 morning doesn't silently skip the day's
        # reporters. sensitivity="public" — published results (extraction is
        # local per #no-mnpi-to-cloud, was cited as §5.4; the routine just
        # reads public announcements).
        id="earnings-tracker",
        description="Earnings tracker — calendar-driven results capture per watched public company",
        module_cli="routines.earnings.cli",
        cli_args=("run",),
        trigger=CronTrigger(hour=7, minute=30, timezone=_LONDON),
        sensitivity="public",
        concurrency="skip",
        catchup="fire-on-startup",
    ),
    JobSpec(
        # #38 — weekly week-in-review DRAFT. Mon 07:30 London: after the
        # Mon 06:30 morning-brief, before the Mon 08:00 vault-health sweeps.
        # catchup="fire-on-startup" so a missed Monday after a weekend bridge
        # restart still produces the week's draft (idempotent per ISO week).
        id="week-in-review",
        description="Week-in-review — weekly DRAFT for operator review",
        module_cli="routines.week_in_review.cli",
        cli_args=("generate",),
        trigger=CronTrigger(day_of_week="mon", hour=7, minute=30, timezone=_LONDON),
        sensitivity="internal",
        concurrency="skip",
        catchup="fire-on-startup",
    ),
    JobSpec(
        # #ops-retention — weekly age-only prune + VACUUM of the audit /
        # telemetry surfaces (runs/*.jsonl, telemetry/llm_calls.jsonl,
        # state/audit_index.db) so they don't grow unbounded (DUR-1). Sun
        # 04:00 London: a quiet off-peak slot that collides with no other job
        # (Sun 18:00 system-insights, Mon 02:30 precedent-tracker, Mon 06:30+
        # morning sweeps). Prune is AGE-ONLY + crash-safe + idempotent
        # (re-running prunes nothing new), so a missed fire is harmless →
        # catchup="skip-missed". Window is the single AGENTIC_RETENTION_DAYS
        # knob (default 90d); the subprocess inherits it via env.copy() in the
        # runner. sensitivity="internal" — it touches operator-private
        # telemetry but performs no network/vault writes.
        id="retention",
        description="Retention — weekly age-prune + VACUUM of audit/telemetry state",
        module_cli="routines.shared.retention_cli",
        cli_args=("run",),
        trigger=CronTrigger(day_of_week="sun", hour=4, minute=0, timezone=_LONDON),
        sensitivity="internal",
        concurrency="skip",
        catchup="skip-missed",
    ),
    JobSpec(
        # #steal-kocoro P3 — stale-gate sweep. Flags runs paused on a human gate
        # past the warn threshold (AGENTIC_STALE_GATE_WARN_HOURS, default 6h),
        # fail-closed auto-cancels them past a long horizon
        # (AGENTIC_STALE_GATE_CANCEL_HOURS, default 168h = 7d), and retires crew
        # runs orphaned by a bridge restart (AGENTIC_STALE_GATE_CREW_ORPHAN_HOURS,
        # default 2h). Adapted from Shannon's "approval timeout = denied" — but
        # fail-closed only at a LONG horizon, never a 60-min deny that would kill
        # a walk-away /pitch gate. Every 6h (03/09/15/21 London) so a stuck gate
        # surfaces within hours, at quiet slots that collide with no other job
        # (02:30, 04:00, 06:30, 07:00, 07:30, 08:00, 08:30, 17:00, 18:00). The
        # sweep is idempotent (a cancelled/finalized run becomes terminal and is
        # never re-detected) + read-only on fresh gates, so a missed fire is
        # harmless → catchup="skip-missed". sensitivity="internal" — it reads
        # operator-private run metadata + writes audit/cancel records, no network.
        id="stale-gate",
        description="Stale-gate sweep — retire runs stuck on a human-approval step",
        module_cli="routines.stale_gate.cli",
        cli_args=("run",),
        trigger=CronTrigger(hour="3,9,15,21", minute=0, timezone=_LONDON),
        sensitivity="internal",
        concurrency="skip",
        catchup="skip-missed",
    ),
)


def get_job_specs() -> tuple[JobSpec, ...]:
    """Expose the registry — used by tests + a future endpoint for
    auto-documenting the schedule."""
    return _JOB_SPECS


# ────────────────────────────────────────────────────────────────────────────
# Per-call audit wrapper
# ────────────────────────────────────────────────────────────────────────────


def _runner_accepts_cancel(fn: Callable[..., Any]) -> bool:
    """Cooperative cancellation: only inject ``cancel`` if the callable
    accepts it. Existing subprocess-backed jobs don't; future hand-rolled
    Python jobs can opt in by declaring the param."""
    try:
        sig = inspect.signature(fn)
    except (ValueError, TypeError):
        return False
    return "cancel" in sig.parameters


def _wrap_for_audit(spec: JobSpec, fn: Callable[..., dict[str, Any]]) -> Callable[[], None]:
    """Wrap ``fn`` so every fire writes a per-call audit row.

    ``fn`` is expected to return a dict of outputs (e.g. subprocess
    returncode + stdout sample) on success or raise on failure. The
    wrapper handles the audit on both paths.

    Under ``concurrency="cancel_previous"``, the wrapper signals any
    prior in-flight runner via ``_cancellation_events[spec.id]`` before
    starting its own fire. Cooperative cancellation: ``fn`` only sees
    the event if it accepts a ``cancel`` kwarg."""
    def runner() -> None:
        # #66 — cancel-previous handshake. Set the prior event (if any)
        # then install ours so the NEXT fire can cancel us in turn.
        if spec.concurrency == "cancel_previous":
            _request_cancellation(spec.id)
        my_cancel = threading.Event()
        with _cancellation_lock:
            _cancellation_events[spec.id] = my_cancel

        run_id = audit.new_run_id()
        t0 = time.monotonic()
        log = logging.getLogger(f"scheduler.{spec.id}")
        log.info("scheduler fire: id=%s run_id=%s", spec.id, run_id)
        try:
            if _runner_accepts_cancel(fn):
                outputs = fn(cancel=my_cancel) or {}
            else:
                outputs = fn() or {}
            audit.write_structured(
                actor={"type": "system", "id": f"scheduler:{spec.id}"},
                entity_type="scheduler_job",
                entity_id=spec.id,
                action="run",
                routine=f"scheduler.{spec.id}",
                audit_dir=RUNS_DIR,
                run_id=run_id,
                status="ok",
                inputs={
                    "sensitivity": spec.sensitivity,
                    "module_cli": spec.module_cli,
                    "cli_args": list(spec.cli_args),
                    "concurrency": spec.concurrency,
                    "catchup": spec.catchup,
                },
                outputs=outputs,
                duration_ms=int((time.monotonic() - t0) * 1000),
                details={"status": "ok", "outputs": outputs},
            )
            log.info("scheduler ok: id=%s", spec.id)
        except Exception as e:  # noqa: BLE001 — audit + re-raise
            audit.write_structured(
                actor={"type": "system", "id": f"scheduler:{spec.id}"},
                entity_type="scheduler_job",
                entity_id=spec.id,
                action="run",
                routine=f"scheduler.{spec.id}",
                audit_dir=RUNS_DIR,
                run_id=run_id,
                status="error",
                inputs={
                    "sensitivity": spec.sensitivity,
                    "module_cli": spec.module_cli,
                    "cli_args": list(spec.cli_args),
                    "concurrency": spec.concurrency,
                    "catchup": spec.catchup,
                },
                error=f"{type(e).__name__}: {e}",
                duration_ms=int((time.monotonic() - t0) * 1000),
                details={"status": "error", "error": f"{type(e).__name__}: {e}"},
            )
            log.exception("scheduler error: id=%s", spec.id)
            raise
        finally:
            with _cancellation_lock:
                # Only clear if our event is still the registered one — a
                # newer fire may have overwritten it.
                if _cancellation_events.get(spec.id) is my_cancel:
                    del _cancellation_events[spec.id]

    runner.__name__ = f"scheduled_{spec.id.replace('-', '_')}"
    return runner


# ────────────────────────────────────────────────────────────────────────────
# Subprocess runner — the "fn" injected into _wrap_for_audit for each job
# ────────────────────────────────────────────────────────────────────────────


def _make_subprocess_runner(spec: JobSpec, *, timeout: int = DEFAULT_TIMEOUT_SECONDS) -> Callable[[], dict[str, Any]]:
    """Return a zero-arg callable that subprocesses ``python -m <module_cli> <args>``.

    Captures stdout/stderr so a stack trace from the routine surfaces in
    the audit log. Non-zero exit → raises ``CalledProcessError`` so the
    audit wrapper records ``status="error"``."""
    def run() -> dict[str, Any]:
        cmd = [sys.executable, "-m", spec.module_cli, *spec.cli_args]
        # Resolve cwd to the routines repo so relative imports + config
        # lookup match the manual-CLI behaviour.
        cwd = str(Path(__file__).resolve().parents[2])
        try:
            proc = subprocess.run(
                cmd,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=True,
                # Inherit env (operator's profile + AGENTIC_* vars).
                env=os.environ.copy(),
            )
        except subprocess.TimeoutExpired as e:
            raise RuntimeError(
                f"scheduler {spec.id!r} timed out after {timeout}s "
                f"({' '.join(cmd)})"
            ) from e
        except subprocess.CalledProcessError as e:
            tail = (e.stderr or "")[-500:].strip()
            raise RuntimeError(
                f"scheduler {spec.id!r} exit {e.returncode}: {tail}"
            ) from e
        # Trim stdout sample so the audit row stays compact.
        stdout_sample = (proc.stdout or "")[-500:].strip() or None
        return {
            "returncode": proc.returncode,
            "stdout_sample": stdout_sample,
        }

    run.__name__ = f"subprocess_{spec.id.replace('-', '_')}"
    return run


# ────────────────────────────────────────────────────────────────────────────
# Catch-up scheduling — fire-on-startup support
# ────────────────────────────────────────────────────────────────────────────


def _read_last_audit_ts(spec_id: str) -> Optional[datetime]:
    """Read the most-recent ``ts`` from ``runs/scheduler.<id>.jsonl``.

    Returns ``None`` when the log doesn't exist or contains no parseable
    rows. Used by the fire-on-startup catch-up check."""
    log_path = RUNS_DIR / f"scheduler.{spec_id}.jsonl"
    if not log_path.is_file():
        return None
    try:
        latest: Optional[datetime] = None
        with log_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts_str = rec.get("ts")
                if not isinstance(ts_str, str):
                    continue
                if ts_str.endswith("Z"):
                    ts_str = ts_str[:-1] + "+00:00"
                try:
                    ts = datetime.fromisoformat(ts_str)
                except ValueError:
                    continue
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if latest is None or ts > latest:
                    latest = ts
        return latest
    except OSError as e:
        logger.warning("scheduler: failed to read audit log %s: %s", log_path, e)
        return None


def _expected_previous_fire(trigger: CronTrigger, now: datetime) -> Optional[datetime]:
    """Iterate forward from one week ago to find the most-recent fire time
    on or before ``now``.

    Returns ``None`` if no fire would have happened in the last week.
    Bounded iteration (max ~50 fires for every-15-min jobs) keeps this
    cheap even on long-running bridges."""
    cursor = now - timedelta(days=7)
    prev_fire: Optional[datetime] = None
    next_fire = trigger.get_next_fire_time(None, cursor)
    iterations = 0
    while next_fire is not None and next_fire <= now and iterations < 2000:
        prev_fire = next_fire
        next_fire = trigger.get_next_fire_time(prev_fire, prev_fire + timedelta(seconds=1))
        iterations += 1
    return prev_fire


def should_catchup_fire(
    spec: JobSpec,
    *,
    now: Optional[datetime] = None,
) -> bool:
    """Return True when ``spec.catchup=="fire-on-startup"`` AND the last
    audit timestamp is older than the most-recent expected fire (or there's
    no audit history at all)."""
    if spec.catchup != "fire-on-startup":
        return False
    now = now or datetime.now(timezone.utc)
    expected_prev = _expected_previous_fire(spec.trigger, now)
    if expected_prev is None:
        return False
    last_ts = _read_last_audit_ts(spec.id)
    if last_ts is None:
        return True
    return last_ts < expected_prev


# ────────────────────────────────────────────────────────────────────────────
# Public registration entrypoint
# ────────────────────────────────────────────────────────────────────────────


def _max_instances_for(spec: JobSpec) -> int:
    """Translate spec.concurrency → APScheduler max_instances."""
    if spec.concurrency == "queue":
        return max(2, spec.max_concurrent_instances)
    if spec.concurrency == "cancel_previous":
        # Allow the new fire to start while the old one is still cooperating
        # with cancellation; the prior runner exits via cancel.is_set().
        return 2
    return 1  # "skip"


def _coalesce_for(spec: JobSpec) -> bool:
    """Translate spec.concurrency + spec.catchup → APScheduler coalesce.

    Coalesce=True is the safe default — merges piled-up missed fires into
    a single fire (so a 3-day bridge outage doesn't trigger 3 morning
    briefs in a row). queue mode opts out so distinct fires stay distinct.
    """
    if spec.concurrency == "queue":
        return False
    return True


def register_all_jobs() -> list[str]:
    """Register every job in ``_JOB_SPECS`` against the singleton scheduler.

    Idempotent — calls ``replace_existing=True`` so re-registering on a
    bridge reload won't 409 on the second pass. Returns the list of
    registered job ids (including any one-shot catch-up jobs queued under
    ``catchup="fire-on-startup"``).

    Durable suspension: ids in the persisted paused set
    (``state/scheduler-paused.json``, maintained by the pause/resume
    endpoints) register with ``next_run_time=None`` — present in the job
    list, but no trigger fires until resumed. A suspended
    ``fire-on-startup`` spec also skips its catch-up one-shot: a job the
    operator silenced must not fire a startup catch-up either."""
    sched = get_scheduler()
    registered: list[str] = []
    now = datetime.now(timezone.utc)
    suspended = load_suspended()

    stale = suspended - {sp.id for sp in _JOB_SPECS}
    if stale:
        logger.warning(
            "scheduler: paused set contains ids not in the registry "
            "(renamed/removed spec?): %s", sorted(stale),
        )

    for spec in _JOB_SPECS:
        is_suspended = spec.id in suspended
        runner = _wrap_for_audit(spec, _make_subprocess_runner(spec))
        # next_run_time=None means "add paused" to APScheduler — but only
        # when passed explicitly; an active job must omit the kwarg so the
        # trigger computes its own first fire.
        suspended_kwargs: dict[str, Any] = {"next_run_time": None} if is_suspended else {}
        try:
            sched.add_job(
                runner,
                trigger=spec.trigger,
                jobstore="ephemeral",
                id=spec.id,
                name=spec.description,
                replace_existing=True,
                misfire_grace_time=600,   # 10 min grace — bridge restart tolerance
                coalesce=_coalesce_for(spec),
                max_instances=_max_instances_for(spec),
                **suspended_kwargs,
            )
            registered.append(spec.id)
        except Exception as e:  # noqa: BLE001 — one bad spec shouldn't kill the rest
            logger.warning("scheduler: failed to register %r: %s", spec.id, e)
            continue

        if is_suspended:
            logger.info("scheduler: %r registered SUSPENDED (durable pause)", spec.id)
            continue   # no catch-up for a suspended job

        # Catch-up: register a one-shot DateTrigger if we missed the most-
        # recent expected fire. Distinct job id so the regular cron entry
        # is unaffected.
        if should_catchup_fire(spec, now=now):
            catchup_id = f"{spec.id}-catchup"
            try:
                sched.add_job(
                    runner,
                    trigger=DateTrigger(run_date=now + timedelta(seconds=2)),
                    jobstore="ephemeral",
                    id=catchup_id,
                    name=f"{spec.description} (catch-up fire)",
                    replace_existing=True,
                    misfire_grace_time=60,
                )
                registered.append(catchup_id)
                logger.info(
                    "scheduler: queued catch-up fire for %r (audit gap detected)",
                    spec.id,
                )
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "scheduler: catch-up fire registration failed for %r: %s",
                    spec.id, e,
                )

    logger.info(
        "scheduler: registered %d/%d specs (%d suspended, + catch-ups): %s",
        sum(1 for sp in _JOB_SPECS if sp.id in registered),
        len(_JOB_SPECS),
        sum(1 for sp in _JOB_SPECS if sp.id in suspended),
        ", ".join(registered),
    )
    return registered


__all__ = [
    "JobSpec",
    "ConcurrencyPolicy",
    "CatchupPolicy",
    "DEFAULT_TIMEOUT_SECONDS",
    "get_job_specs",
    "register_all_jobs",
    "should_catchup_fire",
    "_request_cancellation",      # exported for tests
    "_runner_accepts_cancel",     # exported for tests
    "_wrap_for_audit",            # exported for tests
    "_make_subprocess_runner",    # exported for tests
    "_read_last_audit_ts",        # exported for tests
    "_expected_previous_fire",    # exported for tests
]
