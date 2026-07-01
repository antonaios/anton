"""#63 phase 2b — run_id-keyed persistence for cooperatively-suspended skills.

A skill wrapped by ``@anton_skill`` can raise :class:`SkillSuspended` to pause
mid-run and ask the operator a question (the substrate #65's ``OperatorProvider``
builds on). The wrapper persists a SANITIZED checkpoint here; a later
``POST /api/skills/{run_id}/resume`` reloads it and re-invokes the body.

Why a DEDICATED store (not ``session_writes``):
  ``sessions.store.session_writes`` is keyed by ``(session_id, message_id,
  task_id, idx)`` with a hard FK into ``sessions_index`` — but skill routes are
  SESSIONLESS (lbo / recall-query take ``workspace_*`` + mint their own
  ``run_id`` via ``audit.new_run_id`` / the request-boundary id; they never
  touch a session). The resume endpoint is ``run_id``-keyed. So suspended state
  lives in its own ``run_id``-keyed table that mirrors the same
  pending → commit/discard PATTERN (a pending row that a resume claims exactly
  once) without the session coupling. Self-contained + unit-testable, like the
  sibling ``_runtime`` primitives (llm_call_counter / tool_call_cap).

Storage: a single SQLite file co-located with ``sessions.db`` (outside the
vault, gitignored per [[workspace-write-policy]] §5). The persisted ``state`` is
run through ``audit.sanitize_record`` before it lands (B6 — a paused skill never
parks a secret in cleartext).

Idempotency (B4 / the resume contract): ``claim_for_resume`` is a single atomic
``UPDATE … WHERE status='pending' AND expires_at > now`` — exactly one caller
can win, so a double-resume (network retry, double-click) can't re-run the body.
TTL: a suspension past ``expires_at`` is swept to ``expired`` and can't resume
(410), so a paused run never dangles forever.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────────────
# Types
# ────────────────────────────────────────────────────────────────────────────


@dataclass
class Suspension:
    """One cooperatively-suspended skill run, keyed by ``run_id``.

    ``state`` is the SANITIZED skill checkpoint (whatever the body stashed on
    ``SkillSuspended(state=…)``) — the resume path hands it back to the body via
    ``current_resume().state``. ``status`` is the lifecycle: ``pending``
    (awaiting input) → ``resumed`` (claimed exactly once) | ``expired`` (TTL
    lapsed) | ``discarded`` (operator cancelled).
    """

    run_id: str
    skill: str
    prompt: str
    state: dict[str, Any]
    workspace_type: str
    workspace_name: str
    sensitivity: str
    created: str            # ISO-8601 UTC (microsecond precision)
    expires_at: str         # ISO-8601 UTC = created + timeout_s
    options: Optional[list[Any]] = None
    status: str = "pending"
    resumed_at: Optional[str] = None
    # Per-suspension-INSTANCE nonce. A resume must echo it; the claim matches on
    # it so a stale resume (an old prompt's retry) can't claim a freshly
    # re-pended suspension that happens to reuse the run_id (the ABA race — a
    # multi-step skill re-suspends under the SAME run_id). Minted fresh by
    # ``put`` on every persist (codex-5.5 SEV-1).
    resume_token: str = ""

    def is_expired(self, now_iso: Optional[str] = None) -> bool:
        now = now_iso or _now_iso()
        return self.expires_at <= now

    def as_public_dict(self) -> dict[str, Any]:
        """The operator-facing view — what the awaiting response + the
        "waiting on you" list surface. DELIBERATELY omits ``state`` (an
        internal continuation checkpoint, not an API surface). Carries
        ``resume_token`` — the client MUST echo it back on resume."""
        return {
            "run_id": self.run_id,
            "skill": self.skill,
            "status": self.status,
            "prompt": self.prompt,
            "options": self.options,
            "resume_token": self.resume_token,
            "workspace_type": self.workspace_type,
            "workspace_name": self.workspace_name,
            "sensitivity": self.sensitivity,
            "created": self.created,
            "expires_at": self.expires_at,
            "resume_url": f"/api/skills/{self.run_id}/resume",
        }


# ────────────────────────────────────────────────────────────────────────────
# Storage location
# ────────────────────────────────────────────────────────────────────────────


def _default_suspensions_db() -> Path:
    """Resolve the suspensions DB path. Co-located with ``sessions.db`` so the
    suspend/resume state lives beside the other local, gitignored session state
    (outside the vault, per [[workspace-write-policy]] §5).

    Order:
      1. ``AGENTIC_SUSPENSIONS_DB`` — full file path (tests point this at tmp).
      2. ``<AGENTIC_SESSIONS_DIR>/skill_suspensions.db`` if that env is set.
      3. ``<repo>\\sessions\\skill_suspensions.db`` on Windows.
      4. ``/mnt/x/Agentic OS/sessions/skill_suspensions.db`` on WSL/Linux.
    """
    env = os.environ.get("AGENTIC_SUSPENSIONS_DB")
    if env:
        return Path(env)
    sessions_dir = os.environ.get("AGENTIC_SESSIONS_DIR")
    if sessions_dir:
        return Path(sessions_dir) / "skill_suspensions.db"
    if platform.system() == "Windows":
        return Path("<repo>/sessions/skill_suspensions.db")
    return Path("/mnt/x/Agentic OS/sessions/skill_suspensions.db")


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS skill_suspensions (
    run_id          TEXT PRIMARY KEY,
    skill           TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    prompt          TEXT NOT NULL,
    options         TEXT,                       -- JSON list, or NULL
    state           TEXT NOT NULL,              -- JSON (sanitized checkpoint)
    workspace_type  TEXT NOT NULL,
    workspace_name  TEXT NOT NULL,
    sensitivity     TEXT NOT NULL,
    created         TEXT NOT NULL,
    expires_at      TEXT NOT NULL,
    resumed_at      TEXT,
    resume_token    TEXT NOT NULL DEFAULT ''    -- per-instance nonce (anti-ABA)
);
CREATE INDEX IF NOT EXISTS skill_suspensions_pending
    ON skill_suspensions (status, expires_at);
"""


# ────────────────────────────────────────────────────────────────────────────
# Store
# ────────────────────────────────────────────────────────────────────────────


class SuspensionStore:
    """SQLite-backed store for cooperatively-suspended skill runs.

    One SQLite file (WAL mode). Concurrent reads + serial writes from the
    FastAPI bridge are guarded by a per-instance ``threading.Lock`` around the
    write paths (mirrors ``SessionStore``).
    """

    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = (db_path or _default_suspensions_db()).resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_schema()

    # ── connection ────────────────────────────────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, isolation_level=None)  # autocommit
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA_SQL)

    # ── writes ────────────────────────────────────────────────────────────

    def put(self, susp: Suspension) -> str:
        """Persist (or replace) a pending suspension. Mints + returns a FRESH
        ``resume_token`` for this instance (and sets it on ``susp``) so the
        caller can hand it to the operator.

        ``INSERT OR REPLACE`` so a multi-step skill that re-suspends the SAME
        ``run_id`` (suspend → resume → suspend again) overwrites with fresh
        state, a fresh ``pending`` status (``resumed_at`` → NULL), AND a fresh
        token — which is what makes a stale resume for the PRIOR prompt fail to
        claim the new one (anti-ABA). The double-resume guard is in
        ``claim_for_resume``, not here.
        """
        # F-32 (HR INV5-G1): full 128 bits. The old ``hex[:16]`` truncation
        # left a 64-bit nonce — fine vs accidental ABA, thin as a guessing
        # margin for a loopback-local brute force against a long-lived
        # suspension. uuid4 is already 128 bits of CSPRNG state; keep it all.
        token = uuid.uuid4().hex
        susp.resume_token = token
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO skill_suspensions
                  (run_id, skill, status, prompt, options, state,
                   workspace_type, workspace_name, sensitivity,
                   created, expires_at, resumed_at, resume_token)
                VALUES (?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
                """,
                (
                    susp.run_id, susp.skill, susp.prompt,
                    json.dumps(susp.options, default=str) if susp.options is not None else None,
                    json.dumps(susp.state, default=str),
                    susp.workspace_type, susp.workspace_name, susp.sensitivity,
                    susp.created, susp.expires_at, token,
                ),
            )
        return token

    def claim_for_resume(
        self, run_id: str, resume_token: str, *, now_iso: Optional[str] = None,
    ) -> bool:
        """Atomically transition ``pending → resumed`` for a NON-expired row
        whose ``resume_token`` matches.

        Returns ``True`` iff this call won the claim (exactly one caller can —
        the ``UPDATE`` is the critical section). ``False`` means the row is
        missing, already resumed/discarded, expired, OR the token doesn't match
        (a stale resume for a prior prompt instance) — the caller re-fetches +
        maps to 404/410/409. This is the idempotency keystone: a double-resume
        (or an ABA re-pend) can't re-run the body.
        """
        now = now_iso or _now_iso()
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE skill_suspensions
                   SET status = 'resumed', resumed_at = ?
                 WHERE run_id = ? AND resume_token = ?
                   AND status = 'pending' AND expires_at > ?
                """,
                (now, run_id, resume_token, now),
            )
            return cur.rowcount == 1

    def release_claim(self, run_id: str) -> bool:
        """Roll a ``resumed`` row back to ``pending`` (preserving its token) so
        it stays resumable. Used when a resume CLAIMED the suspension but the
        body was never admitted — e.g. a readiness precondition lapsed between
        suspend and resume and the central guard refused in ``__enter__``
        (codex-5.5 SEV-2). Returns True iff a resumed row was released."""
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE skill_suspensions SET status = 'pending', resumed_at = NULL
                 WHERE run_id = ? AND status = 'resumed'
                """,
                (run_id,),
            )
            return cur.rowcount == 1

    def discard(self, run_id: str) -> bool:
        """Operator-cancel a pending suspension. Returns True if a pending row
        was discarded."""
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE skill_suspensions SET status = 'discarded'
                 WHERE run_id = ? AND status = 'pending'
                """,
                (run_id,),
            )
            return cur.rowcount == 1

    def sweep_expired(self, *, now_iso: Optional[str] = None) -> int:
        """Flip every lapsed ``pending`` row to ``expired``. Returns the count
        swept. Called lazily on read (resume / list) so a never-resumed run
        can't masquerade as pending; safe to also call from a periodic job."""
        now = now_iso or _now_iso()
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE skill_suspensions SET status = 'expired'
                 WHERE status = 'pending' AND expires_at <= ?
                """,
                (now,),
            )
            return cur.rowcount

    # ── reads ─────────────────────────────────────────────────────────────

    def get(self, run_id: str) -> Optional[Suspension]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM skill_suspensions WHERE run_id = ?", (run_id,),
            ).fetchone()
        return _row_to_suspension(row) if row else None

    def list_pending(
        self,
        *,
        workspace_type: Optional[str] = None,
        limit: int = 50,
        now_iso: Optional[str] = None,
    ) -> list[Suspension]:
        """The "waiting on you" list — non-expired pending suspensions, newest
        first. Expiry is computed live (``expires_at > now``) so the list is
        correct even between sweeps."""
        now = now_iso or _now_iso()
        sql = (
            "SELECT * FROM skill_suspensions "
            "WHERE status = 'pending' AND expires_at > ?"
        )
        params: list[Any] = [now]
        if workspace_type is not None:
            sql += " AND workspace_type = ?"
            params.append(workspace_type)
        sql += " ORDER BY created DESC LIMIT ?"
        params.append(int(limit))
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_row_to_suspension(r) for r in rows]


# ────────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def expiry_from(created_iso: str, timeout_s: int) -> str:
    """``created + timeout_s`` as an ISO-8601 UTC string in the SAME format as
    ``_now_iso`` so string comparison in SQL (``expires_at > now``) stays
    monotonic with time."""
    base = datetime.fromisoformat(created_iso)
    return (base + timedelta(seconds=max(0, int(timeout_s)))).isoformat(timespec="microseconds")


def now_iso() -> str:
    """Public stamp (the wrapper uses this for ``created``)."""
    return _now_iso()


def _row_to_suspension(row: sqlite3.Row) -> Suspension:
    return Suspension(
        run_id=row["run_id"],
        skill=row["skill"],
        prompt=row["prompt"],
        state=json.loads(row["state"] or "{}"),
        workspace_type=row["workspace_type"],
        workspace_name=row["workspace_name"],
        sensitivity=row["sensitivity"],
        created=row["created"],
        expires_at=row["expires_at"],
        options=json.loads(row["options"]) if row["options"] else None,
        status=row["status"],
        resumed_at=row["resumed_at"],
        resume_token=row["resume_token"] if "resume_token" in row.keys() else "",
    )


# ────────────────────────────────────────────────────────────────────────────
# Process-wide singleton
# ────────────────────────────────────────────────────────────────────────────


_STORE: Optional[SuspensionStore] = None
_STORE_LOCK = threading.Lock()


def get_suspension_store() -> SuspensionStore:
    """The process-wide store (lazy singleton). The wrapper + the resume route
    both go through this so they share one DB."""
    global _STORE
    if _STORE is None:
        with _STORE_LOCK:
            if _STORE is None:
                _STORE = SuspensionStore()
    return _STORE


def reset_suspension_store_for_tests(store: Optional[SuspensionStore] = None) -> None:
    """Swap (or clear) the singleton — test isolation only."""
    global _STORE
    _STORE = store


__all__ = [
    "Suspension",
    "SuspensionStore",
    "get_suspension_store",
    "reset_suspension_store_for_tests",
    "expiry_from",
    "now_iso",
]
