"""Telemetry readers — six sources, normalised to ``TelemetryEvent``.

Each reader is a pure function: ``(window: timedelta, now: datetime) ->
list[TelemetryEvent]``. They never write, never mutate; failing rows are
logged + skipped (tolerant by design — observability code that crashes on
malformed telemetry would be self-defeating).

Sources:
  1. ``routines/runs/*.jsonl``           — per-routine audit JSONL (legacy)
  2. ``routines/state/audit_index.db``   — structured activity log (#60)
  3. ``routines/runs/scheduler.*.jsonl`` — per-job fire history + miss
                                            computation against cron spec
  4. ``routines/telemetry/llm_calls.jsonl`` — LLMCallRecord (#22 + #67)
  5. ``routines/state/budgets.db``       — budget incidents (#57)
  6. ``routines/metrics/audit_failures.jsonl`` — @safe_audit failures (#68)

Window default: 7 days. Defaults are baked into the analyser; readers
themselves only filter rows whose ``ts`` predates ``now - window``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Literal, Optional

log = logging.getLogger(__name__)


def _runs_dir() -> Path:
    """Lazy lookup so tests can monkeypatch ``routines.api.deps.RUNS_DIR``
    and have the patched value picked up on every call."""
    from routines.api import deps as deps_module
    return deps_module.RUNS_DIR


TelemetrySource = Literal[
    "audit",
    "activity_db",
    "scheduler",
    "llm_calls",
    "budget",
    "audit_failures",
]


@dataclass
class TelemetryEvent:
    """One normalised telemetry row across all six sources.

    Fields:
        ts: UTC datetime when the event occurred
        source: which reader produced it (see ``TelemetrySource``)
        kind: subtype within source — e.g. ``"scheduler.fire"``,
            ``"scheduler.miss"``, ``"llm.call"``, ``"budget.incident"``,
            ``"audit.error"``
        actor: routine / actor id (best-effort across sources)
        entity: post-#60 entity_id (skill name, job id, scope id, etc.)
        payload: original row, preserved verbatim so the analyser can
            extract source-specific fields (latency, cost, error message)
            without forcing every reader to enumerate them
    """

    ts: datetime
    source: TelemetrySource
    kind: str
    actor: Optional[str] = None
    entity: Optional[str] = None
    payload: dict[str, Any] = field(default_factory=dict)


# ────────────────────────────────────────────────────────────────────────────
# Time helpers
# ────────────────────────────────────────────────────────────────────────────


def _parse_ts(ts_str: Any) -> Optional[datetime]:
    """Tolerant ISO-8601 parser. Returns None for unparseable values."""
    if not isinstance(ts_str, str) or not ts_str:
        return None
    s = ts_str
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    """Yield parsed JSON dicts from a JSONL file. Skips blank + invalid lines."""
    if not path.is_file():
        return
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    yield obj
    except OSError as e:
        log.warning("system_insights: failed to read %s: %s", path, e)


def _within_window(
    ts: datetime, *, now: datetime, window: timedelta,
) -> bool:
    return ts >= (now - window) and ts <= now


# ────────────────────────────────────────────────────────────────────────────
# 1. Audit JSONL — routines/runs/<routine>.jsonl
# ────────────────────────────────────────────────────────────────────────────


# Skip these files (they're already covered by dedicated readers, or
# are operator-action audit rows that don't belong in self-reflection).
_AUDIT_JSONL_SKIP_PREFIXES = (
    "scheduler.",        # read by read_scheduler_history
    "budgets.incident",  # read by read_budget_incidents
    "activity.jsonl",    # read by read_audit_db
    "learning-events",   # operator behaviour, not system behaviour
)


def read_audit_jsonl(
    *,
    window: timedelta = timedelta(days=7),
    now: Optional[datetime] = None,
    runs_dir: Optional[Path] = None,
) -> list[TelemetryEvent]:
    """Scan ``routines/runs/*.jsonl`` for routine-level audit rows.

    Surfaces routine ``status="error"`` rows + slow durations. Skips files
    already covered by dedicated readers (scheduler.*, budgets.incident,
    activity.jsonl, learning-events).
    """
    now = now or datetime.now(timezone.utc)
    runs = runs_dir or _runs_dir()
    out: list[TelemetryEvent] = []
    if not runs.is_dir():
        return out

    for path in sorted(runs.glob("*.jsonl")):
        name = path.name
        if any(name.startswith(p) for p in _AUDIT_JSONL_SKIP_PREFIXES):
            continue
        for row in _iter_jsonl(path):
            ts = _parse_ts(row.get("ts"))
            if ts is None or not _within_window(ts, now=now, window=window):
                continue
            routine = str(row.get("routine") or path.stem)
            status = str(row.get("status") or "")
            kind = f"audit.{status}" if status else "audit"
            inputs = row.get("inputs") if isinstance(row.get("inputs"), dict) else None
            entity = None
            if inputs:
                for key in ("name", "ticker", "session_id", "path", "id"):
                    v = inputs.get(key)
                    if isinstance(v, str) and v:
                        entity = v
                        break
            out.append(TelemetryEvent(
                ts=ts,
                source="audit",
                kind=kind,
                actor=f"routine:{routine}",
                entity=entity,
                payload=row,
            ))
    return out


# ────────────────────────────────────────────────────────────────────────────
# 2. Activity DB — routines/state/audit_index.db (#60)
# ────────────────────────────────────────────────────────────────────────────


def read_audit_db(
    *,
    window: timedelta = timedelta(days=7),
    now: Optional[datetime] = None,
    limit: int = 5000,
) -> list[TelemetryEvent]:
    """Query ``audit_index.db`` for structured activity in the window.

    Surfaces all entity_type rows; the analyser decides what to cluster.
    Tolerant of a missing DB (returns empty).
    """
    now = now or datetime.now(timezone.utc)
    since_iso = (now - window).isoformat(timespec="seconds")

    try:
        from routines.shared.audit_db import query_audit
        rows = query_audit(since=since_iso, limit=limit)
    except Exception as e:  # noqa: BLE001 — observability never crashes
        log.warning("system_insights: read_audit_db failed: %s", e)
        return []

    out: list[TelemetryEvent] = []
    for row in rows:
        ts = _parse_ts(row.get("ts"))
        if ts is None:
            continue
        actor = row.get("actor") if isinstance(row.get("actor"), dict) else {}
        actor_id = str(actor.get("id") or "")
        action = str(row.get("action") or "")
        out.append(TelemetryEvent(
            ts=ts,
            source="activity_db",
            kind=f"activity.{action}" if action else "activity",
            actor=actor_id or None,
            entity=str(row.get("entity_id") or "") or None,
            payload=row,
        ))
    return out


# ────────────────────────────────────────────────────────────────────────────
# 3. Scheduler history + miss computation
# ────────────────────────────────────────────────────────────────────────────


def _count_expected_fires(
    trigger: Any,
    *,
    since: datetime,
    until: datetime,
) -> int:
    """Walk a CronTrigger from ``since`` to ``until`` counting fires.

    Tolerant of any APScheduler-shaped trigger that exposes
    ``get_next_fire_time(prev, now)``.
    """
    count = 0
    cursor = since
    iterations = 0
    while iterations < 5000:
        try:
            next_fire = trigger.get_next_fire_time(None, cursor)
        except Exception:  # noqa: BLE001
            break
        if next_fire is None or next_fire > until:
            break
        count += 1
        cursor = next_fire + timedelta(seconds=1)
        iterations += 1
    return count


def read_scheduler_history(
    *,
    window: timedelta = timedelta(days=7),
    now: Optional[datetime] = None,
    runs_dir: Optional[Path] = None,
    job_specs: Optional[Iterable[Any]] = None,
) -> list[TelemetryEvent]:
    """Scan scheduler audit JSONLs + compute misses against the cron spec.

    For each registered ``JobSpec``:
      * Emits one ``scheduler.fire`` event per audit row in the window
      * Emits one synthetic ``scheduler.miss`` event when actual fires <
        expected fires (payload carries ``expected``, ``actual``,
        ``miss_count``, ``miss_pct``)

    A job with no audit file at all is treated as 100% miss — emits the
    same miss event with ``actual=0`` so the analyser can flag a job that
    silently stopped firing.
    """
    now = now or datetime.now(timezone.utc)
    runs = runs_dir or _runs_dir()
    out: list[TelemetryEvent] = []

    if job_specs is None:
        try:
            from routines.scheduler.jobs import get_job_specs
            job_specs = get_job_specs()
        except Exception as e:  # noqa: BLE001
            log.warning("system_insights: scheduler.get_job_specs unavailable: %s", e)
            job_specs = []

    since = now - window
    for spec in job_specs:
        job_id = getattr(spec, "id", None)
        if not job_id:
            continue
        log_path = runs / f"scheduler.{job_id}.jsonl"

        # Per-fire events
        actual = 0
        for row in _iter_jsonl(log_path):
            ts = _parse_ts(row.get("ts"))
            if ts is None or not _within_window(ts, now=now, window=window):
                continue
            status = str(row.get("status") or "")
            actual += 1
            out.append(TelemetryEvent(
                ts=ts,
                source="scheduler",
                kind=f"scheduler.{status or 'fire'}",
                actor=f"scheduler:{job_id}",
                entity=job_id,
                payload=row,
            ))

        # Synthetic miss event
        try:
            expected = _count_expected_fires(
                spec.trigger, since=since, until=now,
            )
        except Exception as e:  # noqa: BLE001
            log.warning(
                "system_insights: expected-fires calc failed for %s: %s",
                job_id, e,
            )
            continue
        if expected <= 0:
            continue
        miss_count = max(0, expected - actual)
        miss_pct = miss_count / expected if expected else 0.0
        if miss_count > 0:
            out.append(TelemetryEvent(
                ts=now,
                source="scheduler",
                kind="scheduler.miss",
                actor=f"scheduler:{job_id}",
                entity=job_id,
                payload={
                    "expected": expected,
                    "actual": actual,
                    "miss_count": miss_count,
                    "miss_pct": miss_pct,
                    "window_days": window.total_seconds() / 86400,
                },
            ))
    return out


# ────────────────────────────────────────────────────────────────────────────
# 4. LLM calls — routines/telemetry/llm_calls.jsonl
# ────────────────────────────────────────────────────────────────────────────


def read_llm_calls(
    *,
    window: timedelta = timedelta(days=7),
    now: Optional[datetime] = None,
    jsonl_path: Optional[Path] = None,
) -> list[TelemetryEvent]:
    """Scan ``llm_calls.jsonl`` for per-call telemetry in the window.

    Tolerant of pre-#60 rows missing ``provider`` field. Each row → one
    event; the analyser aggregates by model / status / cost downstream.
    """
    now = now or datetime.now(timezone.utc)
    if jsonl_path is None:
        from routines.telemetry import llm_writer
        jsonl_path = llm_writer.LLM_CALLS_JSONL

    out: list[TelemetryEvent] = []
    for row in _iter_jsonl(jsonl_path):
        ts = _parse_ts(row.get("ts"))
        if ts is None or not _within_window(ts, now=now, window=window):
            continue
        status = str(row.get("status") or "ok")
        model = str(row.get("model") or "unknown")
        out.append(TelemetryEvent(
            ts=ts,
            source="llm_calls",
            kind=f"llm.{status}",
            actor=str(row.get("provider") or "unknown"),
            entity=model,
            payload=row,
        ))
    return out


# ────────────────────────────────────────────────────────────────────────────
# 5. Budget incidents — routines/state/budgets.db (#57)
# ────────────────────────────────────────────────────────────────────────────


def read_budget_incidents(
    *,
    window: timedelta = timedelta(days=7),
    now: Optional[datetime] = None,
    include_closed: bool = False,
) -> list[TelemetryEvent]:
    """Query ``budgets.db`` incidents opened within the window.

    Default: only open / paused incidents (the operator's pain surface).
    ``include_closed=True`` widens to all rows for backtesting.
    """
    now = now or datetime.now(timezone.utc)
    out: list[TelemetryEvent] = []
    try:
        from routines.budgets.incidents import (
            list_all_incidents,
            list_open_incidents,
        )
        incidents = (
            list_all_incidents(limit=500)
            if include_closed
            else list_open_incidents()
        )
    except Exception as e:  # noqa: BLE001
        log.warning("system_insights: budget incidents read failed: %s", e)
        return out

    for inc in incidents:
        opened = inc.opened_at
        if opened.tzinfo is None:
            opened = opened.replace(tzinfo=timezone.utc)
        if not _within_window(opened, now=now, window=window):
            continue
        scope_a = inc.scope.a or ""
        scope_b = inc.scope.b or ""
        scope_label = (
            f"{inc.scope.kind}:{scope_a}:{scope_b}".rstrip(":")
        )
        out.append(TelemetryEvent(
            ts=opened,
            source="budget",
            kind="budget.incident",
            actor=inc.scope.kind,
            entity=scope_label,
            payload={
                "id": inc.id,
                "scope_kind": inc.scope.kind,
                "scope_a": inc.scope.a,
                "scope_b": inc.scope.b,
                "status": inc.status,
                "current_pct": inc.current_pct,
                "hard_pct": inc.hard_pct,
                "cap_usd": inc.cap_usd,
                "current_spend_usd": inc.current_spend_usd,
            },
        ))
    return out


# ────────────────────────────────────────────────────────────────────────────
# 6. Audit failures — routines/metrics/audit_failures.jsonl (#68)
# ────────────────────────────────────────────────────────────────────────────


def read_audit_failures(
    *,
    window: timedelta = timedelta(days=7),
    now: Optional[datetime] = None,
    jsonl_path: Optional[Path] = None,
) -> list[TelemetryEvent]:
    """Scan ``audit_failures.jsonl`` for @safe_audit-caught failures.

    Each failure row carries ``error_class``, ``error_message``, ``fn`` —
    the analyser clusters on error_class + fn to surface bug patterns.
    """
    now = now or datetime.now(timezone.utc)
    if jsonl_path is None:
        from routines.shared import audit as audit_mod
        jsonl_path = audit_mod.AUDIT_FAILURES_LOG

    out: list[TelemetryEvent] = []
    for row in _iter_jsonl(jsonl_path):
        ts = _parse_ts(row.get("ts"))
        if ts is None or not _within_window(ts, now=now, window=window):
            continue
        err_class = str(row.get("error_class") or "Unknown")
        fn = str(row.get("fn") or "unknown")
        out.append(TelemetryEvent(
            ts=ts,
            source="audit_failures",
            kind=f"audit_failure.{err_class}",
            actor=fn,
            entity=err_class,
            payload=row,
        ))
    return out


# ────────────────────────────────────────────────────────────────────────────
# Aggregate entrypoint
# ────────────────────────────────────────────────────────────────────────────


def read_all_sources(
    *,
    window: timedelta = timedelta(days=7),
    now: Optional[datetime] = None,
    runs_dir: Optional[Path] = None,
    llm_calls_path: Optional[Path] = None,
    audit_failures_path: Optional[Path] = None,
    include_closed_incidents: bool = False,
) -> dict[str, list[TelemetryEvent]]:
    """Read every source; return a dict keyed by source name.

    Failure of one source never propagates to another — each reader is
    independently try/except-wrapped so a corrupted DB or missing file
    doesn't blackhole the rest of the analysis.
    """
    now = now or datetime.now(timezone.utc)

    def _safe(name: str, fn, **kwargs) -> list[TelemetryEvent]:
        try:
            return fn(**kwargs)
        except Exception as e:  # noqa: BLE001 — per-source isolation
            log.warning(
                "system_insights: reader %r failed (non-fatal): %s", name, e,
            )
            return []

    return {
        "audit": _safe(
            "audit",
            read_audit_jsonl,
            window=window, now=now, runs_dir=runs_dir,
        ),
        "activity_db": _safe(
            "activity_db",
            read_audit_db,
            window=window, now=now,
        ),
        "scheduler": _safe(
            "scheduler",
            read_scheduler_history,
            window=window, now=now, runs_dir=runs_dir,
        ),
        "llm_calls": _safe(
            "llm_calls",
            read_llm_calls,
            window=window, now=now, jsonl_path=llm_calls_path,
        ),
        "budget": _safe(
            "budget",
            read_budget_incidents,
            window=window, now=now,
            include_closed=include_closed_incidents,
        ),
        "audit_failures": _safe(
            "audit_failures",
            read_audit_failures,
            window=window, now=now, jsonl_path=audit_failures_path,
        ),
    }


__all__ = [
    "TelemetryEvent",
    "TelemetrySource",
    "read_audit_jsonl",
    "read_audit_db",
    "read_scheduler_history",
    "read_llm_calls",
    "read_budget_incidents",
    "read_audit_failures",
    "read_all_sources",
]
