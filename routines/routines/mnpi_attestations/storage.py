"""SQLite storage for per-provider MNPI cloud-attestations.

DB lives at ``routines/state/mnpi_attestations.db`` (matching the
sensitivity-overrides / budgets / sessions convention). Single table; the
find/list queries filter on ``revoked_at IS NULL AND expires_at > now`` so an
expired or revoked attestation naturally drops out of "active" without a
background sweeper. A partial unique index enforces **at most one non-revoked
row per provider** (a new grant supersedes the prior one in the same
transaction — mirrors ``sensitivity_overrides``).

Unlike an override window there is **no until-closed form**: every attestation
carries a real ``expires_at`` (a DPA/ZDR term has a validity period), and the
``grant`` refuses unless **all three** protections (dpa / zdr / no_training) are
asserted. See ``policy.py`` for the load-bearing rules.
"""

from __future__ import annotations

import logging
import os
import secrets
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .policy import (
    ATTESTABLE_PROVIDERS,
    DEFAULT_DURATION_SECONDS,
    MAX_DURATION_SECONDS,
    MIN_DURATION_SECONDS,
    Attestation,
    AttestationRefused,
    normalize_provider,
)

logger = logging.getLogger(__name__)

_DEFAULT_DB_PATH = (
    Path(__file__).resolve().parents[2] / "state" / "mnpi_attestations.db"
)


def _db_path() -> Path:
    """Allow tests + an env var to redirect the DB path."""
    override = os.environ.get("AGENTIC_MNPI_ATTESTATIONS_DB")
    if override:
        return Path(override)
    return _DEFAULT_DB_PATH


_INIT_LOCK = threading.Lock()
_INITIALISED: set[str] = set()


# ``expires_at`` is NOT NULL (every attestation expires); the three protection
# flags are CHECK-constrained to 0/1 and additionally required to be 1 at grant
# time (defence in depth — an attestation row that somehow lost a protection
# reads as inactive via ``Attestation.is_active``).
_COLUMNS_DDL = """(
                id              TEXT PRIMARY KEY,
                provider        TEXT NOT NULL,
                dpa             INTEGER NOT NULL,
                zdr             INTEGER NOT NULL,
                no_training     INTEGER NOT NULL,
                granted_by      TEXT NOT NULL,
                granted_at      TEXT NOT NULL,    -- ISO 8601 UTC
                expires_at      TEXT NOT NULL,    -- ISO 8601 UTC (always set)
                revoked_at      TEXT,
                revoked_reason  TEXT,
                CHECK (dpa IN (0,1)),
                CHECK (zdr IN (0,1)),
                CHECK (no_training IN (0,1)),
                CHECK (length(provider) > 0),
                CHECK (length(granted_by) > 0)
            )"""


def _conn() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path), isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode = WAL")
    con.execute("PRAGMA synchronous = NORMAL")
    _ensure_schema(con)
    return con


def _create_indexes(con: sqlite3.Connection) -> None:
    """Lookup index + the partial unique index ("at most ONE non-revoked row
    per provider"). Idempotent."""
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_attest_active "
        "ON mnpi_attestations (provider, revoked_at, expires_at)"
    )
    con.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_attest_one_active "
        "ON mnpi_attestations (provider) WHERE revoked_at IS NULL"
    )


def _ensure_schema(con: sqlite3.Connection) -> None:
    """Create the table + indexes if missing. Idempotent + once-per-path."""
    path = str(_db_path())
    if path in _INITIALISED:
        return
    with _INIT_LOCK:
        if path in _INITIALISED:
            return
        con.execute(f"CREATE TABLE IF NOT EXISTS mnpi_attestations {_COLUMNS_DDL}")
        _create_indexes(con)
        _INITIALISED.add(path)


# ────────────────────────────────────────────────────────────────────────────
# Public API
# ────────────────────────────────────────────────────────────────────────────


class AttestationNotFound(LookupError):
    """Raised by revoke_attestation when the id doesn't match a live row."""


def grant_attestation(
    *,
    provider: str,
    dpa: bool,
    zdr: bool,
    no_training: bool,
    granted_by: str,
    duration_seconds: int = DEFAULT_DURATION_SECONDS,
    now: Optional[datetime] = None,
) -> Attestation:
    """Record a per-provider MNPI cloud-attestation. Supersedes any currently
    live (non-revoked) attestation for the same provider (closes the old with
    reason 'superseded') so there is always at most one.

    Refuses (AttestationRefused → route maps to 422):
      * any of dpa / zdr / no_training not asserted (ALL three required)
      * empty provider / granted_by
      * duration_seconds outside [MIN_DURATION_SECONDS, MAX_DURATION_SECONDS]
    """
    prov = normalize_provider(provider)
    if not prov:
        raise AttestationRefused("provider is required")
    if prov not in ATTESTABLE_PROVIDERS:
        raise AttestationRefused(
            f"provider {provider!r} (normalised {prov!r}) is not attestable for "
            f"MNPI — only {sorted(ATTESTABLE_PROVIDERS)} may carry MNPI under an "
            "enterprise attestation (MiniMax + unknown providers are excluded)."
        )
    if not (granted_by and granted_by.strip()):
        raise AttestationRefused("granted_by is required (operator identity, audit)")
    if not (dpa is True and zdr is True and no_training is True):
        # Strict identity (not truthiness): a direct caller passing 1 / "true"
        # must NOT grant a protection (the HTTP DTO is StrictBool; this is the
        # same wall for the exported helper — codex review).
        missing = [
            name for name, ok in
            (("dpa", dpa), ("zdr", zdr), ("no_training", no_training)) if ok is not True
        ]
        raise AttestationRefused(
            "an MNPI cloud-attestation requires ALL THREE protections as a strict "
            f"boolean True (dpa, zdr, no_training) — not satisfied: {', '.join(missing)} "
            "(CLAUDE.md §5.2 enterprise-MNPI gate)"
        )
    if not (MIN_DURATION_SECONDS <= duration_seconds <= MAX_DURATION_SECONDS):
        raise AttestationRefused(
            f"duration_seconds {duration_seconds} outside the allowed range "
            f"[{MIN_DURATION_SECONDS}, {MAX_DURATION_SECONDS}]."
        )

    now = now or datetime.now(timezone.utc)
    expires_at = datetime.fromtimestamp(now.timestamp() + duration_seconds, tz=timezone.utc)
    new_id = f"att_{secrets.token_hex(6)}"

    with _conn() as con:
        # Supersede + insert as ONE transaction (mirrors open_override): take the
        # write lock up front so a concurrent grant can't leave two live rows;
        # the partial unique index backstops any residual path.
        con.execute("BEGIN IMMEDIATE")
        try:
            con.execute(
                """
                UPDATE mnpi_attestations
                   SET revoked_at = ?, revoked_reason = 'superseded'
                 WHERE provider = ? AND revoked_at IS NULL
                """,
                (now.isoformat(), prov),
            )
            con.execute(
                """
                INSERT INTO mnpi_attestations
                    (id, provider, dpa, zdr, no_training,
                     granted_by, granted_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    new_id, prov, int(dpa), int(zdr), int(no_training),
                    granted_by.strip(), now.isoformat(), expires_at.isoformat(),
                ),
            )
            con.execute("COMMIT")
        except BaseException:
            con.execute("ROLLBACK")
            raise

    return Attestation(
        id=new_id, provider=prov, dpa=bool(dpa), zdr=bool(zdr),
        no_training=bool(no_training), granted_by=granted_by.strip(),
        granted_at=now, expires_at=expires_at,
    )


def find_active_attestation(
    *,
    provider: str,
    now: Optional[datetime] = None,
) -> Optional[Attestation]:
    """The active attestation for ``provider`` (not revoked, not expired, all
    three protections present), or ``None``. Fail-closed: an unknown provider or
    any read trouble yields ``None`` (callers treat that as "no attestation")."""
    now = now or datetime.now(timezone.utc)
    prov = normalize_provider(provider)
    if prov not in ATTESTABLE_PROVIDERS:
        # Defence in depth (codex review): even a manually-seeded active row for a
        # non-attestable provider (MiniMax / unknown / empty) can never satisfy
        # the gate — the lookup itself refuses to honour it.
        return None
    try:
        with _conn() as con:
            rows = con.execute(
                """
                SELECT * FROM mnpi_attestations
                 WHERE provider = ? AND revoked_at IS NULL AND expires_at > ?
                 ORDER BY granted_at DESC
                """,
                (prov, now.isoformat()),
            ).fetchall()
        for r in rows:
            att = _row_to_attestation(r)
            if att.is_active(now=now):   # re-check all three protections (defence in depth)
                return att
        return None
    except Exception as e:  # noqa: BLE001 — fail-closed AT THE SOURCE: any read
        # trouble yields "no attestation", so every caller's gate denies the
        # cloud lane rather than trusting an unverifiable store.
        logger.warning(
            "MNPI attestation lookup failed for provider=%r (treating as "
            "no-attestation — fail-closed): %s", prov, e,
        )
        return None


def list_active_attestations(*, now: Optional[datetime] = None) -> list[Attestation]:
    """All currently-active attestations (not revoked, not expired), newest
    first."""
    now = now or datetime.now(timezone.utc)
    with _conn() as con:
        rows = con.execute(
            """
            SELECT * FROM mnpi_attestations
             WHERE revoked_at IS NULL AND expires_at > ?
             ORDER BY granted_at DESC
            """,
            (now.isoformat(),),
        ).fetchall()
    return [a for a in (_row_to_attestation(r) for r in rows) if a.is_active(now=now)]


def revoke_attestation(
    attestation_id: str,
    *,
    reason: str = "operator",
    now: Optional[datetime] = None,
) -> Attestation:
    """Revoke a live attestation early. Raises AttestationNotFound if the id is
    unknown OR already revoked."""
    now = now or datetime.now(timezone.utc)
    with _conn() as con:
        # Atomic revoke (codex SEV-3): the UPDATE itself filters
        # ``revoked_at IS NULL`` inside a write-locked transaction, so two
        # concurrent revokes can't both "succeed" — exactly one updates a row;
        # the loser sees rowcount 0 and raises AttestationNotFound.
        con.execute("BEGIN IMMEDIATE")
        try:
            cur = con.execute(
                "UPDATE mnpi_attestations SET revoked_at = ?, revoked_reason = ? "
                "WHERE id = ? AND revoked_at IS NULL",
                (now.isoformat(), reason, attestation_id),
            )
            if cur.rowcount == 0:
                existing = con.execute(
                    "SELECT revoked_at FROM mnpi_attestations WHERE id = ?",
                    (attestation_id,),
                ).fetchone()
                con.execute("ROLLBACK")
                if existing is None:
                    raise AttestationNotFound(f"attestation {attestation_id!r} not found")
                raise AttestationNotFound(
                    f"attestation {attestation_id!r} already revoked at "
                    f"{existing['revoked_at']}"
                )
            updated = con.execute(
                "SELECT * FROM mnpi_attestations WHERE id = ?", (attestation_id,),
            ).fetchone()
            con.execute("COMMIT")
        except AttestationNotFound:
            raise
        except BaseException:
            con.execute("ROLLBACK")
            raise
    return _row_to_attestation(updated)


def _clear_all_attestations() -> None:
    """Test-only helper: wipe the table. Used by fixtures that need isolation."""
    with _conn() as con:
        con.execute("DELETE FROM mnpi_attestations")


def _row_to_attestation(row: sqlite3.Row) -> Attestation:
    return Attestation(
        id=row["id"],
        provider=row["provider"],
        dpa=bool(row["dpa"]),
        zdr=bool(row["zdr"]),
        no_training=bool(row["no_training"]),
        granted_by=row["granted_by"],
        granted_at=_parse_iso(row["granted_at"]),
        expires_at=_parse_iso(row["expires_at"]),
        revoked_at=_parse_iso(row["revoked_at"]) if row["revoked_at"] else None,
        revoked_reason=row["revoked_reason"],
    )


def _parse_iso(s: str) -> datetime:
    """Parse an ISO 8601 string back to a timezone-aware datetime."""
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt
