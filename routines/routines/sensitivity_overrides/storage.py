"""SQLite storage for sensitivity overrides.

DB lives at ``routines/state/sensitivity_overrides.db`` (matching the
budgets / dismissals / sessions convention). Single table; the find/list
queries filter on (closed_at IS NULL) + (expires_at IS NULL OR expires_at > now)
so expired overrides naturally drop out of "active" without a background
sweeper, while an until-closed window (``expires_at`` NULL, #llm-routing-
postjune15 P2) stays active until explicitly closed OR until the
``UNTIL_CLOSED_HARD_CAP_SECONDS`` (24h) defense-in-depth cap from ``opened_at``
(also enforced in the WHERE clauses — still no sweeper).
"""

from __future__ import annotations

import os
import secrets
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from .policy import (
    DEFAULT_DURATION_SECONDS,
    MAX_DURATION_SECONDS,
    MIN_DURATION_SECONDS,
    UNTIL_CLOSED_DURATION,
    UNTIL_CLOSED_HARD_CAP_SECONDS,
    Override,
    OverrideCeiling,
    OverrideRefused,
)

_DEFAULT_DB_PATH = (
    Path(__file__).resolve().parents[2] / "state" / "sensitivity_overrides.db"
)

# Allow tests + an env var to redirect the DB path.
def _db_path() -> Path:
    override = os.environ.get("AGENTIC_SENSITIVITY_OVERRIDES_DB")
    if override:
        return Path(override)
    return _DEFAULT_DB_PATH


_INIT_LOCK = threading.Lock()
_INITIALISED: set[str] = set()


def _conn() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path), isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode = WAL")
    con.execute("PRAGMA synchronous = NORMAL")
    _ensure_schema(con)
    return con


# Column definition shared by the live-table create + the nullable-migration
# rebuild so the two can't drift. ``expires_at`` is NULLABLE: a NULL row is an
# until-closed window (#llm-routing-postjune15 P2, no auto-expiry); a non-NULL
# row auto-expires at that timestamp.
_COLUMNS_DDL = """(
                id              TEXT PRIMARY KEY,
                skill           TEXT NOT NULL,
                workspace       TEXT NOT NULL,
                provider        TEXT NOT NULL,
                ceiling         TEXT NOT NULL,
                opened_at       TEXT NOT NULL,    -- ISO 8601 UTC
                expires_at      TEXT,             -- ISO 8601 UTC, NULL = until-closed
                justification   TEXT NOT NULL,
                closed_at       TEXT,
                closed_reason   TEXT,
                CHECK (ceiling IN ('public','internal','confidential')),
                CHECK (length(justification) > 0)
            )"""

# Explicit column list for the migration copy (never SELECT * across a rebuild).
_OVERRIDE_COLUMNS = (
    "id, skill, workspace, provider, ceiling, opened_at, expires_at, "
    "justification, closed_at, closed_reason"
)


def _close_duplicate_actives(con: sqlite3.Connection) -> None:
    """Close all-but-newest active row per (skill, workspace, provider) tuple so
    the partial unique index can build. F-39 (CX B-10): a DB written before the
    open_override transaction fix — or before the unique index existed — could
    hold concurrent-open duplicates. Idempotent (no duplicates → no-op)."""
    con.execute(
        """
        UPDATE sensitivity_overrides
           SET closed_at = strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now'),
               closed_reason = 'superseded'
         WHERE closed_at IS NULL
           AND id NOT IN (
               SELECT id FROM (
                   SELECT id, ROW_NUMBER() OVER (
                       PARTITION BY skill, workspace, provider
                       ORDER BY opened_at DESC, id DESC) AS rn
                     FROM sensitivity_overrides
                    WHERE closed_at IS NULL
               ) WHERE rn = 1
           )
        """
    )


def _create_indexes(con: sqlite3.Connection) -> None:
    """Create the lookup index + the partial unique index ("at most ONE active
    row per tuple"). Idempotent (IF NOT EXISTS). Call AFTER
    _close_duplicate_actives so the unique index can build."""
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_overrides_active "
        "ON sensitivity_overrides (skill, workspace, provider, closed_at, expires_at)"
    )
    con.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_overrides_one_active "
        "ON sensitivity_overrides (skill, workspace, provider) "
        "WHERE closed_at IS NULL"
    )


def _migrate_expires_at_nullable(con: sqlite3.Connection) -> None:
    """One-time migration: relax ``expires_at`` from the original NOT NULL to
    nullable so an until-closed window can store NULL (#llm-routing-postjune15
    P2). SQLite can't ALTER a column's NOT NULL, so rebuild the table when the
    old schema is detected. No-op on a fresh / already-migrated DB.

    The rebuild AND its index recreation run in ONE transaction (Codex SEV-2):
    the new table is never committed without its indexes — including the partial
    unique index that enforces one-active-row-per-tuple — so a crash can't leave
    the safety invariant unprotected. Preserves every existing audit row."""
    cols = con.execute("PRAGMA table_info(sensitivity_overrides)").fetchall()
    expires_col = next((c for c in cols if c["name"] == "expires_at"), None)
    if expires_col is None or expires_col["notnull"] == 0:
        return  # table absent (just created nullable) or already migrated
    con.execute("BEGIN IMMEDIATE")
    try:
        con.execute("ALTER TABLE sensitivity_overrides RENAME TO _sov_migrate_old")
        con.execute(f"CREATE TABLE sensitivity_overrides {_COLUMNS_DDL}")
        con.execute(
            f"INSERT INTO sensitivity_overrides ({_OVERRIDE_COLUMNS}) "
            f"SELECT {_OVERRIDE_COLUMNS} FROM _sov_migrate_old"
        )
        # Dropping the old table also drops its (renamed) indexes, freeing their
        # names so the recreate below lands on the NEW table.
        con.execute("DROP TABLE _sov_migrate_old")
        # Close any duplicate actives (an old enough DB may predate the unique
        # index), then rebuild the indexes — all inside this same transaction.
        _close_duplicate_actives(con)
        _create_indexes(con)
        con.execute("COMMIT")
    except BaseException:
        con.execute("ROLLBACK")
        raise


def _ensure_schema(con: sqlite3.Connection) -> None:
    """Create the table if missing, migrate ``expires_at`` to nullable if an
    older DB still has it NOT NULL, then ensure indexes. Idempotent +
    once-per-path."""
    path = str(_db_path())
    if path in _INITIALISED:
        return
    with _INIT_LOCK:
        if path in _INITIALISED:
            return
        con.execute(f"CREATE TABLE IF NOT EXISTS sensitivity_overrides {_COLUMNS_DDL}")
        _migrate_expires_at_nullable(con)
        # Fresh-DB / already-migrated path. (When the migration ran, it already
        # created these inside its own transaction; these calls are then
        # idempotent no-ops.)
        _close_duplicate_actives(con)
        _create_indexes(con)
        _INITIALISED.add(path)


# ────────────────────────────────────────────────────────────────────────────
# Public API
# ────────────────────────────────────────────────────────────────────────────


class OverrideNotFound(LookupError):
    """Raised by close_override when the id doesn't match an active override."""


def open_override(
    *,
    skill: str,
    workspace: str,
    provider: str,
    ceiling: OverrideCeiling,
    duration_seconds: int = DEFAULT_DURATION_SECONDS,
    justification: str,
    now: Optional[datetime] = None,
) -> Override:
    """Open a new override window. Supersedes any currently-active window for
    the same (skill, workspace, provider) tuple (closes the old with reason
    'superseded').

    ``duration_seconds == UNTIL_CLOSED_DURATION`` (0) opens an until-closed
    window (#llm-routing-postjune15 P2): ``expires_at`` is stored NULL, so it
    never auto-expires — it drops only on explicit close / supersede.

    Refuses (OverrideRefused → route maps to 422):
      * empty / whitespace justification
      * ceiling=='MNPI' (absolute rule, see CLAUDE.md §5.2)
      * duration_seconds neither UNTIL_CLOSED_DURATION nor in
        [MIN_DURATION_SECONDS, MAX_DURATION_SECONDS]
      * empty skill / workspace / provider
    """
    if not (skill and skill.strip()):
        raise OverrideRefused("skill is required")
    if not (workspace and workspace.strip()):
        raise OverrideRefused("workspace is required")
    if not (provider and provider.strip()):
        raise OverrideRefused("provider is required")
    if not (justification and justification.strip()):
        raise OverrideRefused(
            "justification is required (operator intent + audit-trail record)"
        )
    if ceiling not in ("public", "internal", "confidential"):
        # MNPI is filtered out here too — the Literal type would also catch it
        # at the Pydantic layer, but defence in depth.
        raise OverrideRefused(
            f"ceiling {ceiling!r} is not allowed — override cannot raise to "
            "MNPI (CLAUDE.md §5.2 absolute); only public / internal / "
            "confidential are valid."
        )
    until_closed = duration_seconds == UNTIL_CLOSED_DURATION
    if not until_closed and not (MIN_DURATION_SECONDS <= duration_seconds <= MAX_DURATION_SECONDS):
        raise OverrideRefused(
            f"duration_seconds {duration_seconds} outside the allowed range "
            f"[{MIN_DURATION_SECONDS}, {MAX_DURATION_SECONDS}] "
            f"(or {UNTIL_CLOSED_DURATION} for an until-closed window)."
        )

    now = now or datetime.now(timezone.utc)
    expires_at = (
        None if until_closed
        else datetime.fromtimestamp(now.timestamp() + duration_seconds, tz=timezone.utc)
    )
    new_id = f"sov_{secrets.token_hex(6)}"

    with _conn() as con:
        # F-39 (CX B-10): supersede + insert as ONE transaction. On the
        # autocommit connection these were two independent statements — a
        # concurrent open could land between them, see "no active row", and
        # leave TWO active windows for the same tuple. BEGIN IMMEDIATE takes
        # the write lock up front; the partial unique index in _ensure_schema
        # backstops any residual path to a duplicate (the supersede UPDATE
        # closes the expired-but-open rows the WHERE used to skip, so the
        # index predicate can't trip on them either — it matches ALL
        # closed_at-IS-NULL rows, expired or not).
        con.execute("BEGIN IMMEDIATE")
        try:
            con.execute(
                """
                UPDATE sensitivity_overrides
                   SET closed_at = ?, closed_reason = 'superseded'
                 WHERE skill = ? AND workspace = ? AND provider = ?
                   AND closed_at IS NULL
                """,
                (now.isoformat(), skill, workspace, provider),
            )
            con.execute(
                """
                INSERT INTO sensitivity_overrides
                    (id, skill, workspace, provider, ceiling,
                     opened_at, expires_at, justification)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    new_id, skill, workspace, provider, ceiling,
                    now.isoformat(),
                    expires_at.isoformat() if expires_at is not None else None,
                    justification.strip(),
                ),
            )
            con.execute("COMMIT")
        except BaseException:
            con.execute("ROLLBACK")
            raise

    return Override(
        id=new_id, skill=skill, workspace=workspace, provider=provider,
        ceiling=ceiling, opened_at=now, expires_at=expires_at,
        justification=justification.strip(),
    )


def list_active_overrides(*, now: Optional[datetime] = None) -> list[Override]:
    """Return all currently-active override windows (not closed, not expired).
    Ordered by opened_at descending so newest first."""
    now = now or datetime.now(timezone.utc)
    cap_cutoff = now - timedelta(seconds=UNTIL_CLOSED_HARD_CAP_SECONDS)
    with _conn() as con:
        rows = con.execute(
            """
            SELECT * FROM sensitivity_overrides
             WHERE closed_at IS NULL AND (
                       (expires_at IS NULL AND opened_at > ?)   -- until-closed: 24h hard cap
                       OR expires_at > ?                         -- normal window: not yet expired
                   )
             ORDER BY opened_at DESC
            """,
            (cap_cutoff.isoformat(), now.isoformat()),
        ).fetchall()
    return [_row_to_override(r) for r in rows]


def find_active_override(
    *,
    skill: str,
    workspace: str,
    provider: str,
    ceiling: OverrideCeiling,
    now: Optional[datetime] = None,
) -> Optional[Override]:
    """Find an active override matching (skill, workspace, provider) with a
    ceiling >= the requested one. Returns None if no match.

    Ceiling ordering: public < internal < confidential. A 'confidential'
    override authorises 'internal' calls too (broader is OK; narrower is not).
    """
    now = now or datetime.now(timezone.utc)
    cap_cutoff = now - timedelta(seconds=UNTIL_CLOSED_HARD_CAP_SECONDS)
    rank = {"public": 0, "internal": 1, "confidential": 2}
    requested_rank = rank.get(ceiling)
    if requested_rank is None:
        return None
    with _conn() as con:
        rows = con.execute(
            """
            SELECT * FROM sensitivity_overrides
             WHERE skill = ? AND workspace = ? AND provider = ?
               AND closed_at IS NULL AND (
                       (expires_at IS NULL AND opened_at > ?)   -- until-closed: 24h hard cap
                       OR expires_at > ?                         -- normal window: not yet expired
                   )
             ORDER BY opened_at DESC
            """,
            (skill, workspace, provider, cap_cutoff.isoformat(), now.isoformat()),
        ).fetchall()
    for r in rows:
        override = _row_to_override(r)
        if rank.get(override.ceiling, -1) >= requested_rank:
            return override
    return None


def close_override(
    override_id: str,
    *,
    reason: str = "operator",
    now: Optional[datetime] = None,
) -> Override:
    """Close an active override early. Raises OverrideNotFound if the id is
    unknown OR the override is already closed/expired."""
    now = now or datetime.now(timezone.utc)
    with _conn() as con:
        row = con.execute(
            "SELECT * FROM sensitivity_overrides WHERE id = ?",
            (override_id,),
        ).fetchone()
        if row is None:
            raise OverrideNotFound(f"override {override_id!r} not found")
        if row["closed_at"] is not None:
            raise OverrideNotFound(
                f"override {override_id!r} already closed at "
                f"{row['closed_at']} (reason={row['closed_reason']})"
            )
        # An until-closed window (expires_at NULL) never auto-expires, so it is
        # always closeable while open; only a timed window can be already-expired.
        if row["expires_at"] is not None and _parse_iso(row["expires_at"]) <= now:
            raise OverrideNotFound(
                f"override {override_id!r} already expired at "
                f"{row['expires_at']}"
            )
        con.execute(
            "UPDATE sensitivity_overrides SET closed_at = ?, closed_reason = ? WHERE id = ?",
            (now.isoformat(), reason, override_id),
        )
        updated = con.execute(
            "SELECT * FROM sensitivity_overrides WHERE id = ?",
            (override_id,),
        ).fetchone()
    return _row_to_override(updated)


def _clear_all_overrides() -> None:
    """Test-only helper: wipe the table. Used by fixtures that need isolation."""
    with _conn() as con:
        con.execute("DELETE FROM sensitivity_overrides")


def _row_to_override(row: sqlite3.Row) -> Override:
    return Override(
        id=row["id"],
        skill=row["skill"],
        workspace=row["workspace"],
        provider=row["provider"],
        ceiling=row["ceiling"],
        opened_at=_parse_iso(row["opened_at"]),
        expires_at=_parse_iso(row["expires_at"]) if row["expires_at"] is not None else None,
        justification=row["justification"],
        closed_at=_parse_iso(row["closed_at"]) if row["closed_at"] else None,
        closed_reason=row["closed_reason"],
    )


def _parse_iso(s: str) -> datetime:
    """Parse an ISO 8601 string back to a timezone-aware datetime."""
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt
