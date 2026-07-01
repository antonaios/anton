"""SQLite sidecar for the activity log (#60).

The per-routine JSONL files at ``routines/runs/<routine>.jsonl`` stay the
primary, load-bearing audit surface (audit-rotation friendly, tail-f
friendly, grep-able). This sidecar is **additive** — every call into
``routines.shared.audit.write_structured()`` (the legacy ``write()``
adapter routes through it too) inserts one row here so the
``GET /api/activity`` endpoint can serve filtered queries without scanning
the JSONL.

Path: ``routines/state/audit_index.db`` (sibling to ``dismissals.db``,
``budgets.db``). The module-level constant ``AUDIT_DB_PATH`` is exposed
so the autouse fixture in ``tests/conftest.py`` can redirect it to a tmp
location (mirrors the ``DISMISSALS_DB_PATH`` pattern from #62).

Schema is single-table + three indexes:

  audit(ts, actor_type, actor_id, action, entity_type, entity_id, run_id,
        details_json)
  INDEX audit_entity ON audit(entity_type, entity_id, ts DESC)
  INDEX audit_actor  ON audit(actor_type, actor_id, ts DESC)
  INDEX audit_ts     ON audit(ts DESC)

JSON-as-TEXT for ``details_json`` so the row stays grep-able from the
shell when the operator needs to dump it.

#68 — every SQLite write here is best-effort: the caller wraps via
``@safe_audit`` (or the explicit try/except in ``audit.write_structured``)
so a locked / disk-full DB never crashes the user-facing skill that
triggered the write.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────────────
# Path (module-level so tests can monkeypatch — mirrors DISMISSALS_DB_PATH)
# ────────────────────────────────────────────────────────────────────────────


AUDIT_DB_PATH = (
    Path(__file__).resolve().parents[2] / "state" / "audit_index.db"
)


# ────────────────────────────────────────────────────────────────────────────
# Schema
# ────────────────────────────────────────────────────────────────────────────


_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit (
  ts TEXT NOT NULL,
  actor_type TEXT NOT NULL,
  actor_id TEXT NOT NULL,
  action TEXT NOT NULL,
  entity_type TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  run_id TEXT,
  details_json TEXT
);
CREATE INDEX IF NOT EXISTS audit_entity ON audit(entity_type, entity_id, ts DESC);
CREATE INDEX IF NOT EXISTS audit_actor  ON audit(actor_type, actor_id, ts DESC);
CREATE INDEX IF NOT EXISTS audit_ts     ON audit(ts DESC);
""".strip()


def _apply_pragmas(conn: sqlite3.Connection) -> None:
    """Harden the connection for concurrent writers (REL-1 / #ops-sqlite-wal).

    Route handlers run sync in a threadpool AND APScheduler fires jobs on
    its own threads, so two writers can hit ``audit_index.db`` at once.
    Without WAL + a busy_timeout the loser gets an immediate ``database is
    locked`` — and on this path the failure is swallowed by ``@safe_audit``,
    silently dropping an audit/telemetry row. WAL lets readers and a writer
    proceed concurrently; busy_timeout makes a contending writer wait
    instead of erroring. Mirrors ``sessions/store.py`` / ``suspensions.py``.

    ``isolation_level`` is intentionally left at the sqlite3 default here:
    the write paths in this module rely on the implicit transaction opened
    by the DML + the ``with sqlite3.connect(...) as conn:`` context manager
    committing it on exit, so we do not switch to autocommit.
    """
    # busy_timeout BEFORE journal_mode=WAL so the WAL pragma itself waits out a
    # racing connection rather than erroring immediately (codex round).
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA journal_mode=WAL")


def _resolve_path(path: Path | None) -> Path:
    """Resolve ``path`` arg, falling back to the live module-level constant.

    Re-reads the module attribute on every call so ``monkeypatch.setattr``
    in tests takes effect even if a long-lived caller cached an earlier
    value via ``from .audit_db import AUDIT_DB_PATH``.
    """
    if path is not None:
        return path
    from routines.shared import audit_db as self_mod
    return self_mod.AUDIT_DB_PATH


# ────────────────────────────────────────────────────────────────────────────
# Schema-init once-guard (#eff-hotpath-batch)
# ────────────────────────────────────────────────────────────────────────────
#
# BEFORE: every ``insert_audit`` / ``query_audit`` called ``init_audit_db``,
# which opened a connection, applied 2 pragmas (incl. ``journal_mode=WAL``, a
# write), ran the full ``CREATE TABLE/INDEX IF NOT EXISTS`` script, and
# committed — a full extra connect + schema-parse on EVERY audit write (the
# highest-frequency hot path: one per LLM + tool call). ``insert_audit`` then
# opened a SECOND connection to do the actual INSERT. = 2 connects + 1 schema
# script + 2× pragma per write.
#
# AFTER: schema-init runs ONCE per resolved DB path. ``_ensure_schema`` does a
# cheap set-membership check (under a lock) and only runs the schema script the
# first time a given path is seen. The write paths then open ONE connection for
# the actual DML. Result per steady-state write: 1 connect + 1 pragma-pair +
# 1 INSERT (down from 2 connects + schema-script + 2 pragma-pairs).
#
# Keyed by the *resolved path string* (not a bare bool) so the autouse test
# fixture — which ``monkeypatch.setattr``s ``AUDIT_DB_PATH`` to a fresh tmp DB
# per test — still triggers a real schema-init for each new path. A plain
# "initialized once" flag would skip schema creation on the second test's fresh
# (empty) DB and break it.
#
# Thread-safety: route handlers run in a threadpool AND APScheduler fires on
# its own threads, so two writers can race the first insert. The lock makes the
# once-init atomic; the per-call connections stay independent (we never share
# one sqlite3 connection across threads — that is NOT thread-safe). WAL +
# busy_timeout (applied per connection) keep concurrent writers from erroring.

_initialized_paths: set[str] = set()
_init_lock = threading.Lock()


def _create_schema(target: Path) -> None:
    """Open a connection and run the CREATE-IF-NOT-EXISTS schema script.

    Lock-free; callers serialize as needed. Does NOT touch
    ``_initialized_paths`` — that bookkeeping belongs to the caller so the
    once-guard and the public ``init_audit_db`` can share this body without
    re-entrant locking.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(target)) as conn:
        _apply_pragmas(conn)
        conn.executescript(_SCHEMA)
        conn.commit()


def init_audit_db(path: Path | None = None) -> Path:
    """Idempotently create the audit table + indexes.

    Runs the schema script unconditionally (the CREATE ... IF NOT EXISTS
    statements no-op when the schema is already present). Prefer
    ``_ensure_schema`` on hot paths — it skips the redundant connect +
    script after the first call for a given DB path. ``init_audit_db``
    remains the public entrypoint for an explicit at-startup init and is
    still safe to call repeatedly. Returns the resolved path.
    """
    target = _resolve_path(path)
    _create_schema(target)
    # Mark this path initialized so the hot-path guard can skip re-running.
    with _init_lock:
        _initialized_paths.add(str(target))
    return target


def _ensure_schema(target: Path) -> None:
    """Run schema-init ONCE per resolved DB path (hot-path guard).

    Cheap on the steady-state path: a lock-guarded set-membership check
    that opens no connection and runs no SQL once the path is known. Only
    the first call for a given path opens a connection to create the
    schema. See the module note above for the thread-safety and
    test-fixture (per-test tmp path) reasoning.
    """
    key = str(target)
    # Fast path: already initialized — no lock, no connection, no SQL.
    # (A stale miss is benign: the slow path re-checks under the lock.)
    if key in _initialized_paths:
        return
    with _init_lock:
        if key in _initialized_paths:
            return
        _create_schema(target)
        _initialized_paths.add(key)


def insert_audit(record: dict[str, Any], path: Path | None = None) -> None:
    """Insert one row into ``audit_index.db``.

    ``record`` must carry the structured shape produced by
    ``audit.write_structured()`` after sanitize+redact:

      {
        "ts": "<ISO UTC>",
        "actor": {"type": "...", "id": "..."},
        "action": "...",
        "entity_type": "...",
        "entity_id": "...",
        "run_id": "...|None",
        "details": {...},
      }

    Best-effort: caller is responsible for the try/except (the
    ``audit.write_structured`` pipeline wraps this call).
    """
    target = _resolve_path(path)
    _ensure_schema(target)  # schema-init runs ONCE per path, not per write
    actor = record.get("actor") or {}
    details = record.get("details")
    values = (
        record.get("ts") or datetime.now(timezone.utc).isoformat(
            timespec="seconds"
        ),
        str(actor.get("type") or "system"),
        str(actor.get("id") or "unknown"),
        str(record.get("action") or ""),
        str(record.get("entity_type") or "session"),
        str(record.get("entity_id") or "unknown"),
        record.get("run_id"),
        json.dumps(details, default=str) if details is not None else None,
    )
    sql = (
        "INSERT INTO audit "
        "(ts, actor_type, actor_id, action, entity_type, entity_id, "
        " run_id, details_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
    )

    def _do() -> None:
        with sqlite3.connect(str(target)) as conn:
            _apply_pragmas(conn)
            conn.execute(sql, values)
            conn.commit()

    try:
        _do()
    except sqlite3.OperationalError as e:
        # The schema-init once-guard is keyed by path; if the DB file was
        # deleted/replaced at this path mid-process the cached "initialized"
        # flag is stale and the table is gone. Re-init ONCE and retry — only
        # on the no-such-table case (do not broaden the except).
        if "no such table" not in str(e).lower():
            raise
        with _init_lock:
            _initialized_paths.discard(str(target))
        _ensure_schema(target)
        _do()


def query_audit(
    *,
    entity_type: str | None = None,
    entity_id: str | None = None,
    actor_type: str | None = None,
    actor_id: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = 50,
    path: Path | None = None,
) -> list[dict[str, Any]]:
    """SELECT rows from ``audit`` filtered by any combination of params.

    Always ORDER BY ts DESC; ``limit`` caps results (caller-side bounds
    in the endpoint layer). Returns a list of dicts shaped like the
    inserted records (``details_json`` is decoded back to a dict).
    """
    target = _resolve_path(path)
    _ensure_schema(target)  # schema-init runs ONCE per path, not per query
    where: list[str] = []
    params: list[Any] = []
    if entity_type is not None:
        where.append("entity_type = ?")
        params.append(entity_type)
    if entity_id is not None:
        where.append("entity_id = ?")
        params.append(entity_id)
    if actor_type is not None:
        where.append("actor_type = ?")
        params.append(actor_type)
    if actor_id is not None:
        where.append("actor_id = ?")
        params.append(actor_id)
    if since is not None:
        where.append("ts >= ?")
        params.append(since)
    if until is not None:
        where.append("ts <= ?")
        params.append(until)

    sql = (
        "SELECT ts, actor_type, actor_id, action, entity_type, entity_id, "
        "       run_id, details_json "
        "FROM audit"
    )
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY ts DESC LIMIT ?"
    params.append(int(limit))

    def _do() -> list[sqlite3.Row]:
        with sqlite3.connect(str(target)) as conn:
            _apply_pragmas(conn)
            conn.row_factory = sqlite3.Row
            return conn.execute(sql, params).fetchall()

    try:
        rows = _do()
    except sqlite3.OperationalError as e:
        # Stale once-guard (DB deleted/replaced at this path mid-process) →
        # the cached "initialized" flag points at a vanished table. Re-init
        # ONCE and retry — only on the no-such-table case.
        if "no such table" not in str(e).lower():
            raise
        with _init_lock:
            _initialized_paths.discard(str(target))
        _ensure_schema(target)
        rows = _do()

    out: list[dict[str, Any]] = []
    for row in rows:
        details_raw = row["details_json"]
        try:
            details = json.loads(details_raw) if details_raw else None
        except json.JSONDecodeError:
            details = {"_unparseable_details_json": details_raw}
        out.append({
            "ts": row["ts"],
            "actor": {"type": row["actor_type"], "id": row["actor_id"]},
            "action": row["action"],
            "entity_type": row["entity_type"],
            "entity_id": row["entity_id"],
            "run_id": row["run_id"],
            "details": details,
        })
    return out


__all__ = [
    "AUDIT_DB_PATH",
    "init_audit_db",
    "insert_audit",
    "query_audit",
]
