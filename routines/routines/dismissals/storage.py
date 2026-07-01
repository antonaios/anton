"""SQLite storage for the dismissals log (#62 · #58).

Single file at ``routines/state/dismissals.db``. One table:

  dismissals (
    id              TEXT PRIMARY KEY,
    proposal_id     TEXT NOT NULL,
    proposal_kind   TEXT NOT NULL,
    original_path   TEXT NOT NULL,   -- vault-relative POSIX, where the proposal lived BEFORE dismissal
    current_path    TEXT NOT NULL,   -- vault-relative POSIX, where the file is NOW (= original on skip / revision-request; = rejected/<name> on reject)
    dismissed_at    TEXT NOT NULL,   -- ISO UTC
    dismissed_by    TEXT NOT NULL,   -- "operator" | "system"
    action          TEXT NOT NULL,   -- "reject" | "skip" | "auto-expire" | "revision-request"
    reason          TEXT,            -- required for reject + revision-request; optional for skip
    reappears_at    TEXT,            -- ISO UTC; skip only
    undone_at       TEXT             -- ISO UTC; set on POST /undo to prevent double-undo
  );

Module-level ``DISMISSALS_DB_PATH`` is monkeypatched in tests; readers
resolve the path through ``_path()`` so a runtime override is picked up
without re-importing.

Concurrency mirrors ``routines.budgets.storage``: each call opens a
fresh connection and closes it; the OS-level file lock handles writer
contention. Sufficient for the single-process bridge.
"""

from __future__ import annotations

import logging
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional

from routines.api.deps import ROUTINES_REPO

logger = logging.getLogger(__name__)


DISMISSALS_DB_PATH = ROUTINES_REPO / "state" / "dismissals.db"


DismissalAction = Literal["reject", "skip", "auto-expire", "revision-request"]
DismissedBy = Literal["operator", "system"]


_SCHEMA = """
CREATE TABLE IF NOT EXISTS dismissals (
  id              TEXT PRIMARY KEY,
  proposal_id     TEXT NOT NULL,
  proposal_kind   TEXT NOT NULL,
  original_path   TEXT NOT NULL,
  current_path    TEXT NOT NULL,
  dismissed_at    TEXT NOT NULL,
  dismissed_by    TEXT NOT NULL,
  action          TEXT NOT NULL,
  reason          TEXT,
  reappears_at    TEXT,
  undone_at       TEXT
);

CREATE INDEX IF NOT EXISTS idx_dismissals_proposal_id
  ON dismissals(proposal_id);

CREATE INDEX IF NOT EXISTS idx_dismissals_dismissed_at
  ON dismissals(dismissed_at);

CREATE INDEX IF NOT EXISTS idx_dismissals_action_kind
  ON dismissals(action, proposal_kind);
"""


@dataclass
class Dismissal:
    id: str
    proposal_id: str
    proposal_kind: str
    original_path: str
    current_path: str
    dismissed_at: datetime
    dismissed_by: DismissedBy
    action: DismissalAction
    reason: Optional[str] = None
    reappears_at: Optional[datetime] = None
    undone_at: Optional[datetime] = None


class DismissalNotFound(LookupError):
    """Raised when a query / undo targets an unknown dismissal id."""


class DismissalAlreadyUndone(RuntimeError):
    """Raised when an undo targets a dismissal that's already been undone."""


# ────────────────────────────────────────────────────────────────────────────
# Connection helpers
# ────────────────────────────────────────────────────────────────────────────


def _path() -> Path:
    """Resolve the DB path through the module so tests can monkeypatch."""
    from routines.dismissals import storage as self_mod
    return self_mod.DISMISSALS_DB_PATH


def _connect() -> sqlite3.Connection:
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    # REL-1 / #ops-sqlite-wal: the bridge runs route handlers in a threadpool
    # while APScheduler fires jobs on its own threads, so concurrent writers
    # can collide on dismissals.db. WAL + busy_timeout turn an immediate
    # ``database is locked`` into a brief wait. isolation_level is left at the
    # sqlite3 default — the writers (record_dismissal / mark_undone) rely on
    # implicit transactions + explicit ``conn.commit()`` AND on the
    # sqlite3.IntegrityError surfacing from the INSERT for the id-collision
    # retry — so we do NOT switch to autocommit. Mirrors sessions/store.py.
    # busy_timeout BEFORE journal_mode=WAL so the WAL pragma itself waits out a
    # racing connection rather than erroring immediately (codex round).
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA journal_mode=WAL")
    _ensure_schema(conn)
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    conn.commit()


def _new_dismissal_id(dismissed_at: datetime) -> str:
    """Human-readable id: ``dismiss_<YYYY-MM-DD>_<6-hex>``. Operator can spot
    the date in the dismissals log without parsing JSON."""
    return f"dismiss_{dismissed_at.strftime('%Y-%m-%d')}_{secrets.token_hex(3)}"


def _parse_iso(s: Optional[str]) -> Optional[datetime]:
    if s is None:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _row_to_dismissal(row: sqlite3.Row) -> Dismissal:
    return Dismissal(
        id=row["id"],
        proposal_id=row["proposal_id"],
        proposal_kind=row["proposal_kind"],
        original_path=row["original_path"],
        current_path=row["current_path"],
        dismissed_at=_parse_iso(row["dismissed_at"]),  # type: ignore[arg-type]
        dismissed_by=row["dismissed_by"],
        action=row["action"],
        reason=row["reason"],
        reappears_at=_parse_iso(row["reappears_at"]),
        undone_at=_parse_iso(row["undone_at"]),
    )


# ────────────────────────────────────────────────────────────────────────────
# record_dismissal
# ────────────────────────────────────────────────────────────────────────────


def record_dismissal(
    *,
    proposal_id: str,
    proposal_kind: str,
    original_path: str,
    current_path: str,
    action: DismissalAction,
    dismissed_by: DismissedBy = "operator",
    reason: Optional[str] = None,
    reappears_at: Optional[datetime] = None,
    now: Optional[datetime] = None,
) -> Dismissal:
    """Insert one dismissals row. Returns the inserted ``Dismissal``.

    Never raises on duplicate IDs — the id derivation includes a random
    nonce so collisions are vanishingly improbable, but if one occurs
    the insert is retried with a fresh id.
    """
    now = now or datetime.now(timezone.utc)

    # Retry a few times if the random suffix collides (basically never).
    for _ in range(5):
        did = _new_dismissal_id(now)
        try:
            with _connect() as conn:
                conn.execute(
                    """
                    INSERT INTO dismissals
                      (id, proposal_id, proposal_kind, original_path, current_path,
                       dismissed_at, dismissed_by, action, reason, reappears_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        did,
                        proposal_id,
                        proposal_kind,
                        original_path,
                        current_path,
                        now.isoformat(timespec="seconds"),
                        dismissed_by,
                        action,
                        reason,
                        reappears_at.isoformat(timespec="seconds") if reappears_at else None,
                    ),
                )
                conn.commit()
            return Dismissal(
                id=did,
                proposal_id=proposal_id,
                proposal_kind=proposal_kind,
                original_path=original_path,
                current_path=current_path,
                dismissed_at=now,
                dismissed_by=dismissed_by,
                action=action,
                reason=reason,
                reappears_at=reappears_at,
            )
        except sqlite3.IntegrityError:
            continue  # id collision — retry with a fresh nonce

    raise RuntimeError("could not allocate a unique dismissal id after 5 attempts")


# ────────────────────────────────────────────────────────────────────────────
# get_dismissal
# ────────────────────────────────────────────────────────────────────────────


def get_dismissal(dismissal_id: str) -> Optional[Dismissal]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM dismissals WHERE id = ?", (dismissal_id,),
        ).fetchone()
    return _row_to_dismissal(row) if row else None


# ────────────────────────────────────────────────────────────────────────────
# query_dismissals
# ────────────────────────────────────────────────────────────────────────────


def query_dismissals(
    *,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
    action: Optional[DismissalAction] = None,
    kind: Optional[str] = None,
    include_undone: bool = True,
    limit: int = 500,
) -> list[Dismissal]:
    """Filter the dismissals log. Newest-first ordering.

    Args:
        since: lower-bound (inclusive) on ``dismissed_at``.
        until: upper-bound (exclusive) on ``dismissed_at``.
        action: filter to "reject" / "skip" / "auto-expire".
        kind: filter to a specific proposal_kind (e.g. "memory-promotion").
        include_undone: if False, hides rows where ``undone_at`` is set.
        limit: cap on returned rows.
    """
    clauses: list[str] = []
    params: list[object] = []
    if since is not None:
        clauses.append("dismissed_at >= ?")
        params.append(since.isoformat(timespec="seconds"))
    if until is not None:
        clauses.append("dismissed_at < ?")
        params.append(until.isoformat(timespec="seconds"))
    if action is not None:
        clauses.append("action = ?")
        params.append(action)
    if kind is not None:
        clauses.append("proposal_kind = ?")
        params.append(kind)
    if not include_undone:
        clauses.append("undone_at IS NULL")

    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    sql = (
        f"SELECT * FROM dismissals {where} "
        f"ORDER BY dismissed_at DESC LIMIT ?"
    )
    params.append(limit)

    with _connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [_row_to_dismissal(r) for r in rows]


# ────────────────────────────────────────────────────────────────────────────
# mark_undone
# ────────────────────────────────────────────────────────────────────────────


def mark_undone(
    dismissal_id: str,
    *,
    now: Optional[datetime] = None,
) -> Dismissal:
    """Mark a dismissal as undone. Idempotency: raises
    ``DismissalAlreadyUndone`` on a second call so the route can return
    409 to the caller.

    Returns the refreshed ``Dismissal`` with ``undone_at`` populated.
    """
    now = now or datetime.now(timezone.utc)
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM dismissals WHERE id = ?", (dismissal_id,),
        ).fetchone()
        if row is None:
            raise DismissalNotFound(f"dismissal {dismissal_id!r} not found")
        if row["undone_at"] is not None:
            raise DismissalAlreadyUndone(
                f"dismissal {dismissal_id!r} already undone at {row['undone_at']}"
            )
        conn.execute(
            "UPDATE dismissals SET undone_at = ? WHERE id = ?",
            (now.isoformat(timespec="seconds"), dismissal_id),
        )
        conn.commit()
        refreshed = conn.execute(
            "SELECT * FROM dismissals WHERE id = ?", (dismissal_id,),
        ).fetchone()
    return _row_to_dismissal(refreshed)


__all__ = [
    "DISMISSALS_DB_PATH",
    "Dismissal",
    "DismissalAction",
    "DismissedBy",
    "DismissalNotFound",
    "DismissalAlreadyUndone",
    "record_dismissal",
    "get_dismissal",
    "query_dismissals",
    "mark_undone",
    "_connect",
    "_path",
    "_parse_iso",
]
