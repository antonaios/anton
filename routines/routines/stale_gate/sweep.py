"""Stale-gate sweep core (#steal-kocoro P3) — find + retire runs stuck on a gate.

## What this is (the Kocoro-lab/Shannon steal, adapted)

Kocoro-lab/Shannon's human-approval step blocks on a Temporal signal with a timer
where **timeout = denied** (default 60 min). ANTON's reality is split:

  * The **crew** lane (MetaGPT) already fails closed — a 5-minute reply timeout
    raises and audits ``timeout`` (`routines/api/routes/crew.py`).
  * The **composite** lane (Synapse HUMAN steps) waits *forever*, partly by
    design: the Football-Field approval UX is "walk away, approve hours later,"
    and Synapse checkpoints the pause durably. A literal 60-min deny would kill
    legitimate ``/pitch`` runs.

So instead of a short timeout-deny, this is a scheduler **stale-gate sweep**:
flag any run paused on a human step beyond a warn threshold, and — fail-closed
only at a LONG, operator-set horizon — auto-cancel it with an audit row.
Operator-friendly before the horizon, fail-closed at it.

It also retires **crew restart-orphans**: crew approval reply queues are
in-process (`_human_reply_q`), so a bridge restart mid-approval loses the reply
and the worker thread dies with the old process — the run is left ``started``
with no completion row, in-flight forever in the audit. Crew runs last minutes,
so a crew parent row non-terminal beyond a conservative threshold is an orphan;
the sweep writes a terminal ``lost`` completion so it stops looking in-flight (the
eval's "the sweep's audit should record this").

## Two lanes, two on-disk shapes

  * **Composite** — ``runs/composite.<key>.jsonl`` (written by
    ``routines/composite/audit_mirror.py``): records ``{ts, event, run_id,
    composite_key, ...}``. STUCK = a ``human_input_required`` event with no
    terminal event (``done`` / ``orchestration_error`` / ``run_error`` /
    ``run_cancelled``) AFTER it. Composites are Phase-6 / dormant, so these files
    do not exist yet at runtime — validator-first here.
  * **Crew** — ``runs/crew.<verb>.jsonl`` (written by ``write_structured(routine=
    crew.<verb>)`` legacy co-write): records ``{ts, routine, run_id, status,
    duration_ms, inputs:{verb, workspace_type, workspace_name, sensitivity}}``.
    ORPHANED = a ``status="started"`` row with no terminal-status row after it.
    Crew is LIVE, so this lane has immediate effect.

## Safety (the codex correctness + security review)

  * **Order-aware**, not "any terminal ever": a run is stuck only if the LATEST
    pause/start has no terminal after it (an out-of-order / reused terminal can't
    mask a later pause).
  * **Re-check before every destructive write**: ``_cancel`` / ``_finalize``
    re-read the specific run FRESH and confirm it is still stuck + still over the
    horizon + the file parses cleanly; they SKIP otherwise. This closes the
    scan→write TOCTOU (a run that completes/resumes between scan and write is
    never cancelled) and makes a corrupt / partially-written terminal row
    fail-SAFE (a destructive action is suppressed until the file parses cleanly).
    The re-check is best-effort, NOT lock-atomic with the append: a legitimate
    terminal arriving in the microsecond window between the re-read and the write
    could leave a redundant terminal record. **Accepted residual** — for the LIVE
    crew lane this is impossible (an orphan is non-terminal precisely because its
    worker DIED in a restart, so the sweep is the ONLY writer for that run); for
    composites it is dormant (Phase 6) and, once live, would require an operator to
    approve a ≥7-day-stale gate in that exact window, leaving a benign extra
    terminal record. Closing it fully needs a per-file lock SHARED with the audit
    writers (composite/crew ``audit_mirror``) — a cross-cutting change to the hot
    audit path, deferred as out of P3's small-blast-radius scope; revisit if
    composites go live at high frequency.
  * **``now`` normalised** (naive → UTC) so a caller-supplied naive clock can't
    raise mid-subtraction and silently skip a file.
  * **Deal-name hygiene**: the sweep audits ``run_id`` + ``composite_key`` / crew
    ``verb`` + hours — never the workspace deal name (the #57/P1 lesson); its own
    error strings carry only ``run_id`` + lane:key + the exception CLASS, never
    raw exception text (which could embed a path / deal name).

PURE scan (`scan_stuck_gates` / `detect_stale_gates` never write). Per-run /
per-file errors are isolated so one malformed record can't sink the sweep.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional

from routines.api.deps import RUNS_DIR
from routines.shared import audit

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Thresholds — operator-overridable env knobs (the retention.py pattern)
# ─────────────────────────────────────────────────────────────────────────────
DEFAULT_WARN_HOURS = 6           # flag a composite human-gate paused >= this
DEFAULT_CANCEL_HOURS = 168       # auto-cancel (fail-closed) at >= this (7 days)
DEFAULT_CREW_ORPHAN_HOURS = 2    # retire a crew run non-terminal >= this (restart orphan)

WARN_HOURS_ENV = "AGENTIC_STALE_GATE_WARN_HOURS"
CANCEL_HOURS_ENV = "AGENTIC_STALE_GATE_CANCEL_HOURS"
CREW_ORPHAN_HOURS_ENV = "AGENTIC_STALE_GATE_CREW_ORPHAN_HOURS"


def _env_int(name: str, default: int) -> int:
    """Read a positive int from env; fall back to ``default`` on missing /
    malformed / non-positive. A typo must never silently set a 0-hour horizon
    (which would auto-cancel everything immediately). Mirrors
    ``routines.shared.retention._env_int`` / ``dashboard.stale._env_int``."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        val = int(raw.strip())
    except (ValueError, AttributeError):
        logger.warning("stale-gate: %s=%r is not an int — using default %d", name, raw, default)
        return default
    if val <= 0:
        logger.warning(
            "stale-gate: %s=%d is not positive — using default %d (refusing a "
            "0-hour horizon that would cancel everything)", name, val, default,
        )
        return default
    return val


def warn_hours() -> int:
    return _env_int(WARN_HOURS_ENV, DEFAULT_WARN_HOURS)


def cancel_hours() -> int:
    return _env_int(CANCEL_HOURS_ENV, DEFAULT_CANCEL_HOURS)


def crew_orphan_hours() -> int:
    return _env_int(CREW_ORPHAN_HOURS_ENV, DEFAULT_CREW_ORPHAN_HOURS)


def effective_thresholds() -> tuple[int, int, int]:
    """The ``(warn, cancel, crew_orphan)`` hours the sweep ACTUALLY uses, with the
    ``cancel <= warn`` clamp applied (cancel is bumped to ``warn + 1`` so a gate is
    always warned before it can be cancelled — fail-SAFE). Centralised so the CLI
    ``show`` and ``run_sweep`` report/use the SAME horizon and the operator never
    sees a value the sweep won't use (codex SEV-3)."""
    warn_h = warn_hours()
    cancel_h = cancel_hours()
    if cancel_h <= warn_h:
        logger.warning(
            "stale-gate: cancel horizon (%dh) <= warn (%dh) — clamping cancel to %dh "
            "so a gate is always warned before it is cancelled", cancel_h, warn_h, warn_h + 1,
        )
        cancel_h = warn_h + 1
    return warn_h, cancel_h, crew_orphan_hours()


# ─────────────────────────────────────────────────────────────────────────────
# Lane record vocabulary
# ─────────────────────────────────────────────────────────────────────────────
_COMPOSITE_TERMINAL = frozenset({"done", "orchestration_error", "run_error", "run_cancelled"})
_COMPOSITE_PAUSE = "human_input_required"
_CREW_STARTED = "started"
# "lost" is the status THIS sweep writes when it retires an orphan.
_CREW_TERMINAL_STATUSES = frozenset({"ok", "error", "cancelled", "timeout", "refused", "lost"})

Lane = Literal["composite", "crew"]
Kind = Literal["human_gate", "crew_orphan"]


@dataclass
class StuckGate:
    """One run stuck on a human gate (composite) or orphaned by a restart (crew).

    ``key`` is the composite key (``pitch``) or the crew verb (``hello_world``).
    ``hours_paused`` is float hours since the pause/start event. ``workspace_*``
    are carried (read from the start row) ONLY to thread into the crew finalize
    record — never put into the sweep's own audit details."""

    run_id: str
    lane: Lane
    key: str
    kind: Kind
    paused_since: str            # ISO-8601 UTC of the pause (composite) / start (crew) event
    hours_paused: float
    prompt: Optional[str] = None         # composite human-gate prompt
    step_id: Optional[str] = None        # composite paused step
    sensitivity: str = "internal"
    workspace_type: Optional[str] = None  # crew finalize context (not audited by the sweep)
    workspace_name: Optional[str] = None

    def public_dict(self) -> dict[str, Any]:
        """A deal-name-free view for the sweep's own audit / summary surfaces."""
        return {
            "run_id": self.run_id,
            "lane": self.lane,
            "key": self.key,
            "kind": self.kind,
            "paused_since": self.paused_since,
            "hours_paused": round(self.hours_paused, 2),
            "step_id": self.step_id,
            "sensitivity": self.sensitivity,
        }


@dataclass
class SweepResult:
    scanned: int = 0
    fresh: int = 0                                    # human-gates still < warn
    warned: list[StuckGate] = field(default_factory=list)
    cancelled: list[StuckGate] = field(default_factory=list)
    finalized: list[StuckGate] = field(default_factory=list)
    skipped: list[StuckGate] = field(default_factory=list)   # re-check skipped (resolved/corrupt)
    errors: list[str] = field(default_factory=list)
    dry_run: bool = False

    def summary(self) -> dict[str, Any]:
        """Counts + run-id lists (NO deal names) — the audit/CLI surface."""
        return {
            "scanned": self.scanned,
            "fresh": self.fresh,
            "warned": [g.public_dict() for g in self.warned],
            "cancelled": [g.public_dict() for g in self.cancelled],
            "finalized": [g.public_dict() for g in self.finalized],
            "skipped": [g.public_dict() for g in self.skipped],
            "errors": self.errors,
            "dry_run": self.dry_run,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Time + IO helpers
# ─────────────────────────────────────────────────────────────────────────────
def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _ensure_utc(now: Optional[datetime]) -> datetime:
    """Resolve a caller ``now`` to an aware UTC datetime. ``None`` → wall clock; a
    naive value is assumed UTC; an aware value is converted. Without this a naive
    ``now`` would raise on ``aware - naive`` and the per-file catch would silently
    skip the file (codex SEV-3, false negatives)."""
    if now is None:
        return _now_utc()
    return now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now.astimezone(timezone.utc)


def _parse_ts(raw: Any) -> Optional[datetime]:
    """Coerce an ISO-8601 UTC string (``...Z`` or ``...+00:00``) to an aware
    datetime, or ``None`` if unparseable. Naive values are assumed UTC."""
    if not isinstance(raw, str) or not raw:
        return None
    text = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _hours_between(then: datetime, now: datetime) -> float:
    """Float hours from ``then`` to ``now``, clipped to >= 0 (no negative ages
    from clock skew)."""
    return max(0.0, (now - then).total_seconds() / 3600.0)


def _read_records(path: Path) -> tuple[list[dict[str, Any]], bool]:
    """Return ``(records, clean)`` for a JSONL file. ``clean`` is ``False`` if ANY
    line failed to parse or wasn't an object, or the file was unreadable — so a
    DESTRUCTIVE caller can fail-safe (a corrupt / partially-written terminal row
    must not let a completed run look stuck and get cancelled; codex SEV-2). Blank
    lines are ignored and don't dirty the file."""
    records: list[dict[str, Any]] = []
    clean = True
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    clean = False
                    continue
                if isinstance(rec, dict):
                    records.append(rec)
                else:
                    clean = False
    except OSError as e:
        # SECURITY (security review): filename + exception CLASS only — the full
        # path / raw OSError text could embed sensitive context.
        logger.warning("stale-gate: could not read %s: %s", path.name, type(e).__name__)
        return [], False
    return records, clean


def _records_for_run(records: list[dict[str, Any]], run_id: str) -> list[dict[str, Any]]:
    """The chronological subset of ``records`` for one ``run_id``."""
    return [r for r in records if r.get("run_id") == run_id]


def _group_by_run(records: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Group records by ``run_id`` preserving file (chronological) order."""
    by_run: dict[str, list[dict[str, Any]]] = {}
    for rec in records:
        rid = rec.get("run_id")
        if isinstance(rid, str) and rid:
            by_run.setdefault(rid, []).append(rec)
    return by_run


def _composite_key_from_filename(name: str) -> Optional[str]:
    """``composite.pitch.jsonl`` → ``pitch``; ``None`` if not that shape."""
    if name.startswith("composite.") and name.endswith(".jsonl"):
        return name[len("composite."):-len(".jsonl")] or None
    return None


def _crew_verb_from_filename(name: str) -> Optional[str]:
    """``crew.hello_world.jsonl`` → ``hello_world``; skips the ``.roles.jsonl``
    sibling (role rows are not parent runs)."""
    if name.startswith("crew.") and name.endswith(".jsonl") and not name.endswith(".roles.jsonl"):
        return name[len("crew."):-len(".jsonl")] or None
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Order-aware run-state (shared by the scan + the pre-write re-check)
# ─────────────────────────────────────────────────────────────────────────────
def _composite_stuck_state(
    events: list[dict[str, Any]],
) -> tuple[Optional[str], Optional[str], Optional[str], str]:
    """``(paused_ts, prompt, step_id, sensitivity)`` if a composite run is
    CURRENTLY stuck — its LATEST ``human_input_required`` has no terminal event
    after it — else ``(None, ...)``. Order-aware so a prior / out-of-order
    terminal can't mask a later pause (codex SEV-2)."""
    last_pause = -1
    last_terminal = -1
    paused_ts = prompt = step_id = None
    sensitivity = "internal"
    for i, ev in enumerate(events):
        et = ev.get("event")
        if et == _COMPOSITE_PAUSE:
            last_pause = i
            paused_ts, prompt, step_id = ev.get("ts"), ev.get("prompt"), ev.get("step_id")
        elif et in _COMPOSITE_TERMINAL:
            last_terminal = i
        if et == "run_started":
            sens = ev.get("sensitivity")
            if isinstance(sens, str) and sens:
                sensitivity = sens
    if last_pause >= 0 and last_terminal < last_pause:
        return paused_ts, prompt, step_id, sensitivity
    return None, None, None, sensitivity


def _crew_orphan_state(
    rows: list[dict[str, Any]],
) -> tuple[Optional[str], Optional[str], Optional[str], str]:
    """``(start_ts, workspace_type, workspace_name, sensitivity)`` if a crew run is
    CURRENTLY orphaned — its LATEST ``started`` row has no terminal-status row
    after it — else ``(None, ...)``. Order-aware (codex SEV-2)."""
    last_start = -1
    last_terminal = -1
    start_ts = wtype = wname = None
    sensitivity = "internal"
    for i, rec in enumerate(rows):
        status = rec.get("status")
        if status == _CREW_STARTED:
            last_start = i
            start_ts = rec.get("ts")
            inputs = rec.get("inputs")
            if isinstance(inputs, dict):
                wtype, wname = inputs.get("workspace_type"), inputs.get("workspace_name")
                sens = inputs.get("sensitivity")
                if isinstance(sens, str) and sens:
                    sensitivity = sens
        elif isinstance(status, str) and status in _CREW_TERMINAL_STATUSES:
            last_terminal = i
    if last_start >= 0 and last_terminal < last_start:
        return start_ts, wtype, wname, sensitivity
    return None, None, None, sensitivity


# ─────────────────────────────────────────────────────────────────────────────
# Per-lane scanners (PURE — no writes)
# ─────────────────────────────────────────────────────────────────────────────
def _scan_composite_file(path: Path, key: str, *, now: datetime) -> list[StuckGate]:
    records, _clean = _read_records(path)
    out: list[StuckGate] = []
    for rid, events in _group_by_run(records).items():
        paused_ts, prompt, step_id, sensitivity = _composite_stuck_state(events)
        if paused_ts is None:
            continue
        ts = _parse_ts(paused_ts)
        if ts is None:
            continue
        out.append(StuckGate(
            run_id=rid, lane="composite", key=key, kind="human_gate",
            paused_since=paused_ts, hours_paused=_hours_between(ts, now),
            prompt=prompt, step_id=step_id, sensitivity=sensitivity,
        ))
    return out


def _scan_crew_file(path: Path, verb: str, *, now: datetime) -> list[StuckGate]:
    records, _clean = _read_records(path)
    out: list[StuckGate] = []
    for rid, rows in _group_by_run(records).items():
        start_ts, wtype, wname, sensitivity = _crew_orphan_state(rows)
        if start_ts is None:
            continue
        ts = _parse_ts(start_ts)
        if ts is None:
            continue
        out.append(StuckGate(
            run_id=rid, lane="crew", key=verb, kind="crew_orphan",
            paused_since=start_ts, hours_paused=_hours_between(ts, now),
            sensitivity=sensitivity, workspace_type=wtype, workspace_name=wname,
        ))
    return out


def scan_stuck_gates(
    runs_dir: Optional[Path] = None, *, now: Optional[datetime] = None,
) -> list[StuckGate]:
    """All stuck human-gates (composite) + crew restart-orphans under ``runs_dir``.

    PURE — reads only. A missing runs dir yields ``[]``. Per-file errors are
    isolated. Sorted oldest-first (most stuck first)."""
    directory = runs_dir if runs_dir is not None else RUNS_DIR
    now_dt = _ensure_utc(now)
    if not directory.is_dir():
        return []
    out: list[StuckGate] = []
    for path in sorted(directory.glob("*.jsonl")):
        try:
            key = _composite_key_from_filename(path.name)
            if key is not None:
                out.extend(_scan_composite_file(path, key, now=now_dt))
                continue
            verb = _crew_verb_from_filename(path.name)
            if verb is not None:
                out.extend(_scan_crew_file(path, verb, now=now_dt))
        except Exception as e:  # noqa: BLE001 — one bad file must not sink the sweep
            logger.warning("stale-gate: scan failed for %s: %s", path.name, type(e).__name__)
    out.sort(key=lambda g: g.hours_paused, reverse=True)
    return out


def detect_stale_gates(
    runs_dir: Optional[Path] = None, *, now: Optional[datetime] = None,
    threshold_hours: Optional[int] = None,
) -> list[StuckGate]:
    """Composite human-gates paused at/over the warn threshold — the read-only
    surface a dashboard / Inbox chip consumes live (the deferred follow-on, the
    P1 pattern). Crew orphans are excluded (they're a retire concern, not an
    operator-actionable pending gate)."""
    threshold = threshold_hours if threshold_hours is not None else warn_hours()
    return [
        g for g in scan_stuck_gates(runs_dir, now=now)
        if g.kind == "human_gate" and g.hours_paused >= threshold
    ]


# ─────────────────────────────────────────────────────────────────────────────
# The sweep (classify + act, with a fail-safe re-check before every write)
# ─────────────────────────────────────────────────────────────────────────────
def run_sweep(
    runs_dir: Optional[Path] = None, *, now: Optional[datetime] = None,
    dry_run: bool = False,
) -> SweepResult:
    """Scan, classify each stuck gate, and act past the horizons.

    Composite human-gates: ``< warn`` → fresh; ``[warn, cancel)`` → WARNED (flag
    only, no write); ``>= cancel`` → CANCELLED (fail-closed). Crew orphans ``>=
    crew_orphan`` → FINALIZED. Every destructive action RE-CHECKS the run fresh
    first and SKIPS (→ ``result.skipped``) if it resolved / dropped below the
    horizon / the file no longer parses cleanly. ``dry_run`` classifies + reports
    but writes nothing. Idempotent: cancelled / finalized runs become terminal."""
    directory = runs_dir if runs_dir is not None else RUNS_DIR
    now_dt = _ensure_utc(now)
    warn_h, cancel_h, crew_h = effective_thresholds()

    result = SweepResult(dry_run=dry_run)
    gates = scan_stuck_gates(directory, now=now_dt)
    result.scanned = len(gates)

    for gate in gates:
        try:
            if gate.kind == "crew_orphan":
                if gate.hours_paused < crew_h:
                    continue  # legitimately-running crew (minutes old) — leave alone
                if dry_run or _finalize_crew_orphan(gate, directory, crew_h, now=now_dt):
                    result.finalized.append(gate)
                else:
                    result.skipped.append(gate)
                continue
            # composite human gate
            if gate.hours_paused >= cancel_h:
                if dry_run or _cancel_composite_gate(gate, directory, cancel_h, now=now_dt):
                    result.cancelled.append(gate)
                else:
                    result.skipped.append(gate)
            elif gate.hours_paused >= warn_h:
                result.warned.append(gate)
            else:
                result.fresh += 1
        except Exception as e:  # noqa: BLE001 — one failed action must not sink the rest
            # SECURITY (codex): record only run_id + lane:key + the exception CLASS —
            # never raw exception TEXT or a traceback (which could embed a path /
            # deal name) in EITHER the structured result OR the logs (so no
            # logger.exception / exc_info here).
            logger.warning(
                "stale-gate: action failed for run_id=%s (%s:%s): %s",
                gate.run_id, gate.lane, gate.key, type(e).__name__,
            )
            result.errors.append(f"{gate.run_id} ({gate.lane}:{gate.key}): {type(e).__name__}")

    logger.info(
        "stale-gate: scanned=%d fresh=%d warned=%d cancelled=%d finalized=%d skipped=%d errors=%d%s",
        result.scanned, result.fresh, len(result.warned), len(result.cancelled),
        len(result.finalized), len(result.skipped), len(result.errors),
        " (dry-run)" if dry_run else "",
    )
    return result


def _cancel_composite_gate(
    gate: StuckGate, runs_dir: Path, horizon_hours: int, *, now: datetime,
) -> bool:
    """Fail-closed auto-cancel, with a pre-write re-check. RE-READS the composite
    file fresh and confirms (a) it parses cleanly, (b) the run is STILL stuck on a
    human gate, (c) it is STILL over the horizon — SKIPS (returns ``False``)
    otherwise, so a run that completed / resumed since the scan, or whose file is
    corrupt, is never cancelled (codex TOCTOU + corrupt-row + idempotency). On a
    clean pass it writes the ``run_cancelled`` terminal record (⇒ idempotent) + a
    deal-name-free ``composite_run`` audit row; returns ``True``."""
    from routines.composite import audit_mirror

    path = runs_dir / f"composite.{gate.key}.jsonl"
    records, clean = _read_records(path)
    if not clean:
        logger.warning("stale-gate: skip cancel for %s — composite file has parse errors", gate.run_id)
        return False
    paused_ts, _prompt, step_id, _sens = _composite_stuck_state(_records_for_run(records, gate.run_id))
    if paused_ts is None:
        logger.info("stale-gate: skip cancel for %s — resolved since scan", gate.run_id)
        return False
    ts = _parse_ts(paused_ts)
    if ts is None or _hours_between(ts, now) < horizon_hours:
        logger.info("stale-gate: skip cancel for %s — below horizon on re-check", gate.run_id)
        return False

    hours = _hours_between(ts, now)
    audit_mirror.record_run_cancelled(
        gate.run_id, gate.key,
        reason=f"stale_gate_auto_cancel: human gate paused {hours:.1f}h >= {horizon_hours}h horizon",
        runs_dir=runs_dir,
    )
    audit.write_structured(
        actor={"type": "system", "id": "scheduler:stale-gate"},
        entity_type="composite_run",
        entity_id=gate.run_id,
        action="stale_gate_auto_cancel",
        run_id=gate.run_id,
        status="cancelled",
        details={
            "lane": "composite",
            "composite_key": gate.key,
            "step_id": step_id,
            "hours_paused": round(hours, 2),
            "horizon_hours": horizon_hours,
            "reason": "human_gate_exceeded_horizon",
        },
    )
    return True


def _finalize_crew_orphan(
    gate: StuckGate, runs_dir: Path, orphan_hours: int, *, now: datetime,
) -> bool:
    """Retire a crew restart-orphan, with a pre-write re-check. RE-READS the crew
    file fresh and confirms it parses cleanly + the run is STILL orphaned (a
    started row with no terminal after it) + STILL over the threshold — SKIPS
    (returns ``False``) otherwise, so a run that completed since the scan is never
    finalized. On a clean pass it writes a terminal ``lost`` completion (⇒
    idempotent ⇒ stops it looking in-flight); returns ``True``. The completion row
    records the workspace fields the crew lane already records by design, and is
    itself a ``write_structured`` call, so it also lands in the activity stream."""
    from routines.crew import audit_mirror as crew_audit

    path = runs_dir / f"crew.{gate.key}.jsonl"
    records, clean = _read_records(path)
    if not clean:
        logger.warning("stale-gate: skip finalize for %s — crew file has parse errors", gate.run_id)
        return False
    start_ts, wtype, wname, sensitivity = _crew_orphan_state(_records_for_run(records, gate.run_id))
    if start_ts is None:
        logger.info("stale-gate: skip finalize for %s — completed since scan", gate.run_id)
        return False
    ts = _parse_ts(start_ts)
    if ts is None or _hours_between(ts, now) < orphan_hours:
        logger.info("stale-gate: skip finalize for %s — below threshold on re-check", gate.run_id)
        return False

    hours = _hours_between(ts, now)
    crew_audit.write_parent_completion(
        verb=gate.key,
        run_id=gate.run_id,
        audit_dir=runs_dir,
        status="lost",
        workspace_type=wtype or "",
        workspace_name=wname or "",
        sensitivity=sensitivity,
        duration_ms=None,
        result=None,
        error=(
            f"stale_gate_finalize: crew run non-terminal {hours:.1f}h >= {orphan_hours}h "
            f"— orphaned by a bridge restart (in-process approval queue + worker "
            f"lost); finalized as lost"
        ),
    )
    return True
