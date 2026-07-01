"""Retention / rotation for the audit + telemetry surfaces (#ops-retention).

The bridge writes three classes of unbounded, append-only state:

  * ``routines/runs/*.jsonl``        — per-routine + unified activity audit
    (``write_structured`` / legacy ``write``). Load-bearing, compliance-
    relevant audit trail.
  * ``routines/telemetry/llm_calls.jsonl`` — per-call LLM cost/token
    telemetry. The budget gate re-scans the current-month window of this
    file on every cloud call, so unbounded growth is both a disk AND a
    hot-path concern (#eff-hotpath-batch).
  * ``routines/state/audit_index.db`` — the queryable SQLite sidecar that
    mirrors every structured audit write.

``audit_db.py`` self-describes the JSONL as "rotation-friendly" but no
rotation was ever implemented → the files grow forever (DUR-1). This module
is that rotation, prune-by-AGE only:

  * JSONL files: rewrite keeping only lines whose ``ts`` is within the
    window, crash-safe (write-temp → ``os.replace``). The CURRENT file is
    never deleted — we rewrite it in place (or leave it untouched when
    nothing is old enough to drop).
  * SQLite: parse each row's ``ts`` (fail-safe — undatable rows kept) and
    delete the rowids that are strictly older than the cutoff in one
    transaction, followed by ``VACUUM`` to reclaim the freed pages.

SAFETY (this is the security trail — be conservative):
  * Prune by AGE only. A row/line is dropped iff its ``ts`` is strictly
    older than ``cutoff = now - N days``. Lines with a missing / unparseable
    ``ts`` are KEPT (fail-safe — never drop a row we can't date).
  * The window ``N`` is a single config knob (``AGENTIC_RETENTION_DAYS``,
    default 90). 90d is deliberately conservative; raise it freely.
  * Crash-safe: the JSONL rewrite writes a sibling ``.retain.tmp`` then
    atomically replaces the original, so a crash mid-rewrite leaves the
    original intact. The SQLite prune is one transaction; ``VACUUM`` is a
    separate, idempotent step.
  * Everything pruned is logged (counts per file + total bytes reclaimed).

Wiring: invoked by the ``retention`` CLI, which the scheduler runs as a
weekly job (``routines/scheduler/jobs.py::_JOB_SPECS``) — same subprocess
pattern as the other cron routines, so a crash in retention can't take the
bridge down and every fire writes a scheduler audit row.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────────────
# Config knob — the single retention window
# ────────────────────────────────────────────────────────────────────────────

# Default retention window in days. Conservative on purpose — the audit
# trail is compliance-relevant, so we keep 90 days and make it trivial to
# raise via the env var. There is exactly ONE knob.
DEFAULT_RETENTION_DAYS = 90
RETENTION_DAYS_ENV = "AGENTIC_RETENTION_DAYS"


def retention_days() -> int:
    """Resolve the retention window from ``AGENTIC_RETENTION_DAYS`` (default 90).

    A non-integer or non-positive value falls back to the default with a
    warning — retention must never silently prune with a window of 0 (which
    would delete everything) or crash on a typo'd env var.
    """
    raw = os.environ.get(RETENTION_DAYS_ENV)
    if raw is None or raw == "":
        return DEFAULT_RETENTION_DAYS
    try:
        val = int(raw)
    except (TypeError, ValueError):
        logger.warning(
            "%s=%r is not an integer — falling back to %d days",
            RETENTION_DAYS_ENV, raw, DEFAULT_RETENTION_DAYS,
        )
        return DEFAULT_RETENTION_DAYS
    if val <= 0:
        logger.warning(
            "%s=%d is not positive — falling back to %d days (refusing a "
            "window that would prune everything)",
            RETENTION_DAYS_ENV, val, DEFAULT_RETENTION_DAYS,
        )
        return DEFAULT_RETENTION_DAYS
    return val


# ────────────────────────────────────────────────────────────────────────────
# Result types
# ────────────────────────────────────────────────────────────────────────────


@dataclass
class JsonlPruneResult:
    """Per-file outcome of a JSONL prune."""

    path: str
    kept: int = 0
    pruned: int = 0
    undated_kept: int = 0          # lines with no/bad ts — KEPT (fail-safe)
    bytes_before: int = 0
    bytes_after: int = 0
    rewritten: bool = False        # False when nothing was old enough to drop
    skipped_changed: bool = False  # file grew/changed mid-prune → skip rewrite
    error: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "kept": self.kept,
            "pruned": self.pruned,
            "undated_kept": self.undated_kept,
            "bytes_before": self.bytes_before,
            "bytes_after": self.bytes_after,
            "rewritten": self.rewritten,
            "skipped_changed": self.skipped_changed,
            "error": self.error,
        }


@dataclass
class SqlitePruneResult:
    db_path: str
    deleted: int = 0
    vacuumed: bool = False
    bytes_before: int = 0
    bytes_after: int = 0
    error: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "db_path": self.db_path,
            "deleted": self.deleted,
            "vacuumed": self.vacuumed,
            "bytes_before": self.bytes_before,
            "bytes_after": self.bytes_after,
            "error": self.error,
        }


@dataclass
class RetentionSummary:
    days: int
    cutoff_iso: str
    jsonl: list[JsonlPruneResult] = field(default_factory=list)
    sqlite: list[SqlitePruneResult] = field(default_factory=list)

    def total_lines_pruned(self) -> int:
        return sum(r.pruned for r in self.jsonl)

    def total_rows_deleted(self) -> int:
        return sum(r.deleted for r in self.sqlite)

    def as_dict(self) -> dict[str, Any]:
        return {
            "days": self.days,
            "cutoff_iso": self.cutoff_iso,
            "total_lines_pruned": self.total_lines_pruned(),
            "total_rows_deleted": self.total_rows_deleted(),
            "jsonl": [r.as_dict() for r in self.jsonl],
            "sqlite": [r.as_dict() for r in self.sqlite],
        }


# ────────────────────────────────────────────────────────────────────────────
# ts parsing — fail-safe (unparseable ⇒ keep)
# ────────────────────────────────────────────────────────────────────────────


def _parse_ts(value: Any) -> Optional[datetime]:
    """Parse an ISO-8601 ``ts`` to an aware UTC datetime, or ``None``.

    Accepts the trailing-``Z`` form the codebase emits in places. A naive
    datetime is assumed UTC (the codebase stamps UTC). ``None`` signals
    "undatable" → the caller KEEPS the line/row (never prune what we can't
    date)."""
    if not isinstance(value, str) or not value:
        return None
    s = value
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# ────────────────────────────────────────────────────────────────────────────
# JSONL pruning — crash-safe, age-only
# ────────────────────────────────────────────────────────────────────────────


def prune_jsonl_file(
    path: Path,
    *,
    cutoff: datetime,
    dry_run: bool = False,
) -> JsonlPruneResult:
    """Rewrite ``path`` keeping only lines whose ``ts`` is >= ``cutoff``.

    Crash-safe: writes a sibling temp file then ``os.replace``s it over the
    original (atomic on Windows + POSIX). If NO line is old enough to drop,
    the file is left completely untouched (no rewrite, no temp churn) so an
    actively-appended current file isn't needlessly rewritten.

    Fail-safe: a line that isn't valid JSON, or whose ``ts`` is missing /
    unparseable, is KEPT — we never drop a line we can't confidently date.

    Concurrent-append guard: the size re-stat happens IMMEDIATELY before the
    ``os.replace`` (inside ``_atomic_rewrite_lines``, after the temp file is
    fully written), NOT before the temp write — otherwise an append during the
    temp write+fsync (not microsecond-scale for a large file) would still be
    lost. If the size changed since we read it, an unlocked writer appended
    audit lines in the meantime — replacing would lose them. We instead SKIP
    the rewrite (``skipped_changed=True``) and leave the file intact; the next
    run prunes it. This closes the bulk of the concurrent-append race
    (whole-window data loss → harmless skip-and-retry) and shrinks the residual
    to the ``os.replace`` call itself. A microsecond-scale TOCTOU between the
    final stat and ``os.replace`` remains; fully eliminating it would require
    writer-side locking or a seal-and-rotate scheme (a deferred design
    decision).

    Returns a per-file result. Never raises for an expected condition
    (missing file, parse issues) — only truly exceptional IO errors surface
    via ``result.error`` (caught here, logged, NOT re-raised).
    """
    result = JsonlPruneResult(path=str(path))
    if not path.is_file():
        return result

    try:
        result.bytes_before = path.stat().st_size
    except OSError:
        pass

    kept_lines: list[str] = []
    pruned = 0
    undated_kept = 0

    try:
        with path.open("r", encoding="utf-8") as f:
            for raw in f:
                stripped = raw.strip()
                if not stripped:
                    # Drop blank lines silently (they carry no audit content);
                    # they don't count as pruned audit rows.
                    continue
                try:
                    rec = json.loads(stripped)
                    ts = _parse_ts(rec.get("ts")) if isinstance(rec, dict) else None
                except (json.JSONDecodeError, AttributeError):
                    # Unparseable line → keep verbatim (fail-safe).
                    kept_lines.append(stripped)
                    undated_kept += 1
                    continue
                if ts is None:
                    # No usable timestamp → keep (fail-safe).
                    kept_lines.append(stripped)
                    undated_kept += 1
                    continue
                if ts < cutoff:
                    pruned += 1
                else:
                    kept_lines.append(stripped)
    except OSError as e:
        result.error = f"read failed: {e}"
        logger.warning("retention: could not read %s: %s", path, e)
        return result

    result.kept = len(kept_lines)
    result.pruned = pruned
    result.undated_kept = undated_kept

    if pruned == 0:
        # Nothing old enough to drop — leave the file untouched (this is the
        # common steady-state path; do not rewrite an actively-appended file
        # for no reason).
        result.bytes_after = result.bytes_before
        return result

    if dry_run:
        result.bytes_after = result.bytes_before
        return result

    # Crash-safe rewrite with a concurrent-append guard. _atomic_rewrite_lines
    # writes a temp sibling, then re-stats the original IMMEDIATELY before the
    # os.replace: if an unlocked writer appended audit lines while we built the
    # temp, the size no longer matches expected_size and it skips the swap
    # (returns False) so those lines aren't lost — we prune next run instead.
    # This shrinks the residual race to the os.replace call itself.
    try:
        replaced = _atomic_rewrite_lines(
            path, kept_lines, expected_size=result.bytes_before
        )
        if replaced:
            result.rewritten = True
        else:
            result.skipped_changed = True
            result.rewritten = False
            logger.warning(
                "retention: %s changed during prune (was %d bytes); skipping "
                "rewrite, will retry next run", path, result.bytes_before,
            )
        try:
            result.bytes_after = path.stat().st_size
        except OSError:
            pass
    except OSError as e:
        result.error = f"rewrite failed: {e}"
        logger.warning("retention: rewrite of %s failed (original intact): %s", path, e)

    return result


def _atomic_rewrite_lines(
    path: Path, lines: list[str], *, expected_size: int | None = None
) -> bool:
    """Write ``lines`` (newline-terminated) to ``path`` atomically.

    Temp file is created in the target's own directory so the final
    ``os.replace`` is an intra-filesystem rename (atomic). The original is
    only ever replaced once the temp file is fully written + flushed +
    fsynced — a crash before the replace leaves the original untouched.

    Concurrent-append guard: when ``expected_size`` is given, the original is
    re-stat'd IMMEDIATELY before ``os.replace`` (after the temp is fully
    written + fsynced). If its size no longer matches ``expected_size``, an
    unlocked writer appended to it while we built the temp — replacing now
    would drop those lines, so we unlink the temp and return ``False``
    (skipped) instead of swapping. Doing the check here, as late as possible,
    shrinks the residual race to the ``os.replace`` call itself (vs. before the
    temp write, which left the whole temp-write+fsync window exposed).

    Returns ``True`` if the original was replaced, ``False`` if the swap was
    skipped because the file changed underneath us.
    """
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".retain.tmp", dir=str(parent)
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tf:
            for line in lines:
                tf.write(line + "\n")
            tf.flush()
            os.fsync(tf.fileno())
        if expected_size is not None:
            # Final check, as late as possible before the swap.
            try:
                current_size = path.stat().st_size
            except OSError:
                current_size = None
            if current_size != expected_size:
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
                return False
        os.replace(str(tmp_path), str(path))
        return True
    except BaseException:
        # Clean up the temp file on any failure; leave the original intact.
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
        raise


def prune_jsonl_dir(
    directory: Path,
    *,
    cutoff: datetime,
    dry_run: bool = False,
) -> list[JsonlPruneResult]:
    """Prune every ``*.jsonl`` directly under ``directory`` (non-recursive).

    Sorted for deterministic ordering / logging. A missing directory yields
    an empty list."""
    if not directory.is_dir():
        return []
    results: list[JsonlPruneResult] = []
    for jsonl in sorted(directory.glob("*.jsonl")):
        results.append(prune_jsonl_file(jsonl, cutoff=cutoff, dry_run=dry_run))
    return results


# ────────────────────────────────────────────────────────────────────────────
# SQLite pruning — transactional DELETE + VACUUM
# ────────────────────────────────────────────────────────────────────────────


def prune_audit_db(
    db_path: Path,
    *,
    cutoff: datetime,
    dry_run: bool = False,
    table: str = "audit",
    ts_column: str = "ts",
) -> SqlitePruneResult:
    """Delete rows older than ``cutoff`` from the audit SQLite, then VACUUM.

    Age-only AND fail-safe: rather than a lexical ``WHERE ts < cutoff``
    DELETE, we SELECT every ``(rowid, ts)`` and PARSE each ``ts`` with the
    module's ``_parse_ts``. A row is marked for deletion ONLY IF its ``ts``
    parses to a datetime that is strictly < ``cutoff``. A row whose ``ts`` is
    missing / empty / unparseable is KEPT — exactly the "never drop a row we
    can't date" fail-safe the JSONL path upholds. (A lexical compare would
    sort an empty/garbage ``ts`` LOW and silently DELETE it.)

    The marked rowids are deleted in batches (<= 500 rowids per
    ``DELETE ... WHERE rowid IN (...)`` to stay well under SQLite's variable
    limit), inside ONE explicit BEGIN/COMMIT transaction; VACUUM runs
    afterwards to reclaim pages.

    Connection is per-call + hardened with WAL + busy_timeout (mirrors
    ``audit_db._apply_pragmas``) so this maintenance pass cooperates with
    concurrent audit writers instead of erroring on a lock. NOTE: ``VACUUM``
    cannot run inside a transaction and acquires a write lock for its
    duration — acceptable for a weekly off-peak job.

    Best-effort by contract: caller wraps / logs; we catch + record errors
    on the result rather than raising, since a failed prune must not crash
    the scheduler job (the audit trail itself is unharmed).
    """
    result = SqlitePruneResult(db_path=str(db_path))
    if not db_path.is_file():
        return result

    try:
        result.bytes_before = db_path.stat().st_size
    except OSError:
        pass

    try:
        # isolation_level=None → autocommit: each statement (incl. VACUUM)
        # runs on its own, so sqlite3 does NOT wrap VACUUM in an implicit
        # transaction (VACUUM inside a transaction raises). We make the DELETE
        # atomic explicitly with BEGIN/COMMIT.
        conn = sqlite3.connect(str(db_path), isolation_level=None)
        try:
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA journal_mode=WAL")

            # Parse-based, fail-safe selection (mirrors the JSONL path): a
            # row is dropped ONLY if its ts parses AND is strictly older than
            # the cutoff. Missing/unparseable ts → kept.
            doomed: list[int] = []
            for rowid, ts in conn.execute(
                f"SELECT rowid, {ts_column} FROM {table}"
            ):
                parsed = _parse_ts(ts)
                if parsed is not None and parsed < cutoff:
                    doomed.append(rowid)

            if dry_run:
                result.deleted = len(doomed)
                result.bytes_after = result.bytes_before
                return result

            conn.execute("BEGIN")
            try:
                deleted = 0
                # Chunk to stay well under SQLite's variable limit (999).
                for start in range(0, len(doomed), 500):
                    batch = doomed[start:start + 500]
                    placeholders = ",".join("?" * len(batch))
                    cur = conn.execute(
                        f"DELETE FROM {table} WHERE rowid IN ({placeholders})",
                        batch,
                    )
                    deleted += cur.rowcount if cur.rowcount is not None else 0
                result.deleted = deleted
                conn.execute("COMMIT")
            except sqlite3.Error:
                conn.execute("ROLLBACK")
                raise
            # VACUUM outside any transaction (autocommit) to reclaim freed pages.
            conn.execute("VACUUM")
            result.vacuumed = True
        finally:
            conn.close()
        try:
            result.bytes_after = db_path.stat().st_size
        except OSError:
            pass
    except sqlite3.Error as e:
        result.error = f"sqlite prune failed: {e}"
        logger.warning("retention: audit_db prune of %s failed: %s", db_path, e)

    return result


# ────────────────────────────────────────────────────────────────────────────
# Orchestrator
# ────────────────────────────────────────────────────────────────────────────


def run_retention(
    *,
    days: Optional[int] = None,
    now: Optional[datetime] = None,
    runs_dir: Optional[Path] = None,
    telemetry_jsonl: Optional[Path] = None,
    audit_db_path: Optional[Path] = None,
    dry_run: bool = False,
) -> RetentionSummary:
    """Run the full retention pass and return a structured summary.

    Args:
        days: retention window; defaults to ``retention_days()`` (env knob).
        now: injectable clock for tests; defaults to ``datetime.now(UTC)``.
        runs_dir / telemetry_jsonl / audit_db_path: injectable targets for
            tests; default to the live bridge locations.
        dry_run: count what WOULD be pruned without writing.

    Prunes (all age-only, all crash-safe):
        * every ``runs/*.jsonl`` audit file
        * ``telemetry/llm_calls.jsonl`` (also shrinks the budget-gate scan)
        * the ``audit_index.db`` SQLite sidecar (DELETE + VACUUM)

    Logs a one-line summary of what was pruned.
    """
    window = days if days is not None else retention_days()
    # Single choke point: the window must be a WHOLE number of days >= 1.
    # ``retention_days()`` guards the env var; this guards the explicit arg.
    # A non-integral (e.g. 0.5), tiny-positive, zero, or negative value would
    # collapse the cutoff toward "now" (a fractional day can round to a
    # zero-duration timedelta) and prune nearly everything — refuse and fall
    # back to the conservative default. An integral float (30.0) is accepted
    # as that int; a bool is rejected.
    if isinstance(window, bool) or not isinstance(window, int):
        if isinstance(window, float) and window.is_integer() and window >= 1:
            window = int(window)
        else:
            logger.warning(
                "retention: refusing non-integral window days=%r; falling back to %d",
                window, DEFAULT_RETENTION_DAYS,
            )
            window = DEFAULT_RETENTION_DAYS
    if window < 1:
        logger.warning(
            "retention: refusing non-positive window days=%d; falling back to %d",
            window, DEFAULT_RETENTION_DAYS,
        )
        window = DEFAULT_RETENTION_DAYS
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    cutoff = now - timedelta(days=window)

    runs_dir = runs_dir if runs_dir is not None else _default_runs_dir()
    telemetry_jsonl = (
        telemetry_jsonl if telemetry_jsonl is not None else _default_telemetry_jsonl()
    )
    audit_db_path = (
        audit_db_path if audit_db_path is not None else _default_audit_db_path()
    )

    summary = RetentionSummary(days=window, cutoff_iso=cutoff.isoformat())

    # 1) runs/*.jsonl (the audit trail).
    summary.jsonl.extend(prune_jsonl_dir(runs_dir, cutoff=cutoff, dry_run=dry_run))

    # 2) telemetry/llm_calls.jsonl (cost telemetry + budget-gate scan input).
    summary.jsonl.append(
        prune_jsonl_file(telemetry_jsonl, cutoff=cutoff, dry_run=dry_run)
    )

    # 3) audit_index.db (queryable SQLite sidecar).
    summary.sqlite.append(
        prune_audit_db(audit_db_path, cutoff=cutoff, dry_run=dry_run)
    )

    logger.info(
        "retention: window=%dd cutoff=%s pruned %d jsonl lines across %d files "
        "+ %d sqlite rows%s",
        window, cutoff.isoformat(), summary.total_lines_pruned(),
        len(summary.jsonl), summary.total_rows_deleted(),
        " (dry-run)" if dry_run else "",
    )
    return summary


# ────────────────────────────────────────────────────────────────────────────
# Default target resolution (live bridge locations)
# ────────────────────────────────────────────────────────────────────────────


def _default_runs_dir() -> Path:
    from routines.api.deps import RUNS_DIR
    return RUNS_DIR


def _default_telemetry_jsonl() -> Path:
    from routines.telemetry import llm_writer
    return llm_writer.LLM_CALLS_JSONL


def _default_audit_db_path() -> Path:
    from routines.shared import audit_db
    return audit_db.AUDIT_DB_PATH


__all__ = [
    "DEFAULT_RETENTION_DAYS",
    "RETENTION_DAYS_ENV",
    "retention_days",
    "JsonlPruneResult",
    "SqlitePruneResult",
    "RetentionSummary",
    "prune_jsonl_file",
    "prune_jsonl_dir",
    "prune_audit_db",
    "run_retention",
]
