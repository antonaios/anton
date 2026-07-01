"""SQLite storage for budgets: policies + incidents.

Single file at ``routines/state/budgets.db``. Two tables — ``policies``
(one row per ScopeRef) and ``incidents`` (one row per overrun event).
Both keyed by ``scope_id`` so the gate can look up by scope in O(1).

Module-level ``BUDGETS_DB_PATH`` is monkeypatched in tests; readers
resolve the path through ``_path()`` so a runtime override is picked up
without re-importing.

Concurrency: SQLite's per-connection mode is fine for the single-process
bridge. Each call opens a fresh connection + closes it; the OS-level
file lock handles writer contention.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from routines.api.deps import ROUTINES_REPO
from routines.budgets.policy import BudgetPolicy, ScopeRef, scope_id

logger = logging.getLogger(__name__)


BUDGETS_DB_PATH = ROUTINES_REPO / "state" / "budgets.db"


_POLICIES_SCHEMA = """
CREATE TABLE IF NOT EXISTS policies (
  id            TEXT PRIMARY KEY,
  scope_kind    TEXT NOT NULL,
  scope_a       TEXT,
  scope_b       TEXT,
  period        TEXT NOT NULL DEFAULT 'monthly_utc',
  warn_pct      REAL NOT NULL DEFAULT 80.0,
  hard_pct      REAL NOT NULL DEFAULT 100.0,
  cap_usd       REAL NOT NULL,
  cap_tokens    INTEGER,
  created       TEXT NOT NULL,
  last_modified TEXT NOT NULL
);
"""

_INCIDENTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
  id                TEXT PRIMARY KEY,
  scope_id          TEXT NOT NULL,
  scope_kind        TEXT NOT NULL,
  scope_a           TEXT,
  scope_b           TEXT,
  opened_at         TEXT NOT NULL,
  period_start      TEXT NOT NULL,
  current_pct       REAL NOT NULL,
  hard_pct          REAL NOT NULL,
  cap_usd           REAL NOT NULL,
  current_spend_usd REAL NOT NULL,
  status            TEXT NOT NULL DEFAULT 'open',
  ack_at            TEXT,
  ack_action        TEXT,
  ack_new_cap_usd   REAL,
  ack_comment       TEXT
);
"""

_INCIDENTS_INDEX_OPEN = """
CREATE INDEX IF NOT EXISTS idx_incidents_open
  ON incidents(scope_id, status, period_start);
"""


def _path() -> Path:
    """Resolve the DB path through the module so tests can monkeypatch."""
    from routines.budgets import storage as self_mod
    return self_mod.BUDGETS_DB_PATH


def _connect() -> sqlite3.Connection:
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    # REL-1 / #ops-sqlite-wal: the bridge runs route handlers in a threadpool
    # while APScheduler fires jobs on its own threads, so concurrent writers
    # can collide on budgets.db. WAL + busy_timeout turn an immediate
    # ``database is locked`` into a brief wait. isolation_level is left at the
    # sqlite3 default — this store relies on implicit transactions + the
    # explicit ``conn.commit()`` in each write — so we do NOT switch to
    # autocommit. Mirrors sessions/store.py + suspensions.py.
    # busy_timeout BEFORE journal_mode=WAL so the WAL pragma itself waits out a
    # racing connection rather than erroring immediately (codex round).
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA journal_mode=WAL")
    _ensure_schema(conn)
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_POLICIES_SCHEMA + _INCIDENTS_SCHEMA + _INCIDENTS_INDEX_OPEN)
    # First ADD COLUMN migration on the policies table — PRAGMA-guarded so
    # it's idempotent across boots and a no-op on freshly-created DBs (where
    # the CREATE above already includes the column). cap_tokens is the
    # additive track+warn budget field (#57 token-budget follow-on).
    pol_cols = {row[1] for row in conn.execute("PRAGMA table_info(policies)").fetchall()}
    if "cap_tokens" not in pol_cols:
        conn.execute("ALTER TABLE policies ADD COLUMN cap_tokens INTEGER")
    conn.commit()


# ────────────────────────────────────────────────────────────────────────────
# Policies CRUD
# ────────────────────────────────────────────────────────────────────────────


def upsert_policy(policy: BudgetPolicy) -> BudgetPolicy:
    """Insert-or-update by scope_id. Bumps last_modified to ``now`` UTC."""
    sid = scope_id(policy.scope)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    # Re-stamp last_modified so callers that pass a stale value get the
    # canonical write timestamp back.
    refreshed = policy.model_copy(update={"last_modified": _parse_iso(now)})
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO policies
              (id, scope_kind, scope_a, scope_b, period, warn_pct, hard_pct,
               cap_usd, cap_tokens, created, last_modified)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              period        = excluded.period,
              warn_pct      = excluded.warn_pct,
              hard_pct      = excluded.hard_pct,
              cap_usd       = excluded.cap_usd,
              cap_tokens    = excluded.cap_tokens,
              last_modified = excluded.last_modified
            """,
            (
                sid,
                refreshed.scope.kind,
                refreshed.scope.a,
                refreshed.scope.b,
                refreshed.period,
                refreshed.warn_pct,
                refreshed.hard_pct,
                refreshed.cap_usd,
                refreshed.cap_tokens,
                refreshed.created.isoformat(timespec="seconds"),
                refreshed.last_modified.isoformat(timespec="seconds"),
            ),
        )
        conn.commit()
    return refreshed


def get_policy(scope: ScopeRef) -> Optional[BudgetPolicy]:
    sid = scope_id(scope)
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM policies WHERE id = ?", (sid,),
        ).fetchone()
    return _row_to_policy(row) if row else None


def list_policies() -> list[BudgetPolicy]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM policies ORDER BY scope_kind, scope_a, scope_b"
        ).fetchall()
    return [_row_to_policy(r) for r in rows]


def delete_policy(scope: ScopeRef) -> bool:
    """Return True if a row was removed, False if no row existed."""
    sid = scope_id(scope)
    with _connect() as conn:
        cur = conn.execute("DELETE FROM policies WHERE id = ?", (sid,))
        conn.commit()
        return cur.rowcount > 0


# ────────────────────────────────────────────────────────────────────────────
# Row helpers
# ────────────────────────────────────────────────────────────────────────────


def _parse_iso(s: str) -> datetime:
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _row_to_policy(row: sqlite3.Row) -> BudgetPolicy:
    return BudgetPolicy(
        scope=ScopeRef(
            kind=row["scope_kind"],
            a=row["scope_a"],
            b=row["scope_b"],
        ),
        period=row["period"],
        warn_pct=row["warn_pct"],
        hard_pct=row["hard_pct"],
        cap_usd=row["cap_usd"],
        cap_tokens=(row["cap_tokens"] if "cap_tokens" in row.keys() else None),
        created=_parse_iso(row["created"]),
        last_modified=_parse_iso(row["last_modified"]),
    )


__all__ = [
    "BUDGETS_DB_PATH",
    "upsert_policy",
    "get_policy",
    "list_policies",
    "delete_policy",
    "_connect",
    "_path",
    "_parse_iso",
    "_row_to_policy",
]
