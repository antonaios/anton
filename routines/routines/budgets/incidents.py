"""Incident lifecycle: record_overrun → acknowledge → list_open.

When the gate detects a scope is over its hard threshold, ``record_overrun``
writes an Incident row (status='open') if one doesn't already exist for the
current monthly period. The "one per scope per period" guard keeps the
gate idempotent — repeated blocked invocations don't spam incidents.

When an operator acks via ``POST /api/budgets/incidents/{id}/ack``:
  * action='raise_cap' → the policy's cap_usd is updated, incident
    transitions to status='acknowledged_raised'. Next gate check passes
    naturally (spend < new hard threshold).
  * action='leave_paused' → policy untouched, incident transitions to
    status='acknowledged_paused'. The gate continues to block on the
    scope until the period rolls over (1st of next month UTC) OR the
    operator manually deletes the policy.

A ``required comment`` is enforced at the route layer (422 if absent).
This is the Paperclip pattern — every governance decision leaves an
audit trail.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal, Optional

from routines.api.deps import ROUTINES_REPO
from routines.budgets import storage
from routines.budgets.policy import ScopeRef, monthly_utc_window, scope_id
from routines.budgets.storage import _connect, _parse_iso, upsert_policy
from routines.shared import audit

logger = logging.getLogger(__name__)


# AckAction is the action a /ack POST body accepts. force_clear is a
# separate endpoint, so it's not in the AckRequest Literal — but it
# DOES appear on Incident.ack_action once a paused row gets force-cleared,
# so AckActionStored carries the wider domain.
AckAction = Literal["raise_cap", "leave_paused"]
AckActionStored = Literal["raise_cap", "leave_paused", "force_clear"]


# Statuses a row can hold:
#   open                  — fresh overrun, awaiting ack
#   acknowledged_raised   — operator raised the cap OR force-cleared; gate unblocks
#   acknowledged_paused   — operator confirmed but kept paused; gate stays blocked
IncidentStatus = Literal["open", "acknowledged_raised", "acknowledged_paused"]


@dataclass
class Incident:
    id: str
    scope: ScopeRef
    opened_at: datetime
    period_start: datetime
    current_pct: float
    hard_pct: float
    cap_usd: float
    current_spend_usd: float
    status: IncidentStatus
    ack_at: Optional[datetime] = None
    ack_action: Optional[AckActionStored] = None
    ack_new_cap_usd: Optional[float] = None
    ack_comment: Optional[str] = None


# ────────────────────────────────────────────────────────────────────────────
# record_overrun
# ────────────────────────────────────────────────────────────────────────────


def _new_incident_id() -> str:
    """8-char hex id, random. Collision-free in practice for one-operator scale."""
    return secrets.token_hex(4)


def record_overrun(
    scope: ScopeRef,
    *,
    current_pct: float,
    hard_pct: float,
    cap_usd: float,
    current_spend_usd: float,
    now: Optional[datetime] = None,
) -> Incident:
    """Upsert-by-(scope, current period) — returns the existing open
    incident if one already exists for this scope in the current month.

    Side effects: writes an audit row to
    ``routines/runs/budgets.incident.jsonl`` whenever a new incident
    opens. Existing-incident returns do NOT re-audit (prevents spam).
    """
    now = now or datetime.now(timezone.utc)
    month_start, _ = monthly_utc_window(now)
    sid = scope_id(scope)

    with _connect() as conn:
        existing = conn.execute(
            """
            SELECT * FROM incidents
            WHERE scope_id = ?
              AND period_start = ?
              AND status IN ('open', 'acknowledged_paused')
            ORDER BY opened_at DESC
            LIMIT 1
            """,
            (sid, month_start.isoformat(timespec="seconds")),
        ).fetchone()

        if existing:
            return _row_to_incident(existing)

        inc = Incident(
            id=_new_incident_id(),
            scope=scope,
            opened_at=now,
            period_start=month_start,
            current_pct=round(current_pct, 4),
            hard_pct=hard_pct,
            cap_usd=cap_usd,
            current_spend_usd=round(current_spend_usd, 6),
            status="open",
        )
        conn.execute(
            """
            INSERT INTO incidents
              (id, scope_id, scope_kind, scope_a, scope_b,
               opened_at, period_start, current_pct, hard_pct, cap_usd,
               current_spend_usd, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                inc.id, sid, scope.kind, scope.a, scope.b,
                inc.opened_at.isoformat(timespec="seconds"),
                inc.period_start.isoformat(timespec="seconds"),
                inc.current_pct, inc.hard_pct, inc.cap_usd,
                inc.current_spend_usd, inc.status,
            ),
        )
        conn.commit()

    _audit_incident_event(inc, event="opened")
    return inc


# ────────────────────────────────────────────────────────────────────────────
# acknowledge
# ────────────────────────────────────────────────────────────────────────────


class IncidentNotFound(LookupError):
    """Raised when an ack targets an unknown incident id."""


class IncidentAlreadyAcknowledged(RuntimeError):
    """Raised when an ack targets a non-open incident.

    leave_paused is a terminal-for-the-period state — the operator can't
    re-ack a paused incident; they raise the cap on the policy directly
    or wait for period reset. raise_cap is also terminal.
    """


def acknowledge(
    incident_id: str,
    *,
    action: AckAction,
    comment: str,
    new_cap_usd: Optional[float] = None,
    now: Optional[datetime] = None,
) -> Incident:
    """Ack an open incident.

    Args:
        incident_id: 8-char id from ``record_overrun``.
        action: ``raise_cap`` updates the policy's cap to ``new_cap_usd``
            (must be > current cap); ``leave_paused`` keeps the block on.
        comment: REQUIRED (route layer 422s on empty); operator's
            audit-trail rationale.
        new_cap_usd: required when action='raise_cap'; ignored otherwise.

    Raises:
        IncidentNotFound: no row with that id.
        IncidentAlreadyAcknowledged: incident is not in status='open'.
        ValueError: action='raise_cap' but new_cap_usd missing / not greater
            than current cap.
    """
    now = now or datetime.now(timezone.utc)
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM incidents WHERE id = ?", (incident_id,),
        ).fetchone()
        if row is None:
            raise IncidentNotFound(f"incident {incident_id!r} not found")
        if row["status"] != "open":
            raise IncidentAlreadyAcknowledged(
                f"incident {incident_id!r} status={row['status']!r}; "
                "ack is only valid on open incidents"
            )

        inc = _row_to_incident(row)

        if action == "raise_cap":
            if new_cap_usd is None:
                raise ValueError("action='raise_cap' requires new_cap_usd")
            if new_cap_usd <= inc.cap_usd:
                raise ValueError(
                    f"new_cap_usd ({new_cap_usd}) must be > current cap "
                    f"({inc.cap_usd})"
                )
            _bump_policy_cap(inc.scope, new_cap_usd)
            new_status = "acknowledged_raised"
        elif action == "leave_paused":
            new_status = "acknowledged_paused"
        else:  # pragma: no cover — Literal exhausted
            raise ValueError(f"unknown action {action!r}")

        conn.execute(
            """
            UPDATE incidents SET
              status = ?,
              ack_at = ?,
              ack_action = ?,
              ack_new_cap_usd = ?,
              ack_comment = ?
            WHERE id = ?
            """,
            (
                new_status,
                now.isoformat(timespec="seconds"),
                action,
                new_cap_usd if action == "raise_cap" else None,
                comment,
                incident_id,
            ),
        )
        conn.commit()

        # Re-read for the canonical version with ack fields populated.
        refreshed = _row_to_incident(
            conn.execute(
                "SELECT * FROM incidents WHERE id = ?", (incident_id,),
            ).fetchone()
        )

    _audit_incident_event(
        refreshed,
        event="acknowledged",
        extra={"action": action, "new_cap_usd": new_cap_usd, "comment": comment},
    )
    return refreshed


def force_clear(
    incident_id: str,
    *,
    comment: str,
    now: Optional[datetime] = None,
) -> Incident:
    """Mid-period escape hatch for a paused incident.

    Operator picked ``leave_paused`` earlier, then changed their mind
    (investigation came back clean, want to flow again). This clears the
    paused incident WITHOUT changing the cap — the next gate check sees
    no blocking incident on the scope and evaluates spend vs cap fresh.
    If spend is still over cap, a NEW incident will open immediately —
    that's the right behaviour (you can't quietly bypass the gate; you
    can only un-pause).

    Only valid on ``acknowledged_paused`` incidents. Open / raised rows
    raise ``IncidentAlreadyAcknowledged`` (the state machine forbids
    force-clearing a fresh open row — operator should use ``acknowledge``).

    Required ``comment`` is the audit-trail invariant; the route layer
    422s on empty.
    """
    now = now or datetime.now(timezone.utc)
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM incidents WHERE id = ?", (incident_id,),
        ).fetchone()
        if row is None:
            raise IncidentNotFound(f"incident {incident_id!r} not found")
        if row["status"] != "acknowledged_paused":
            raise IncidentAlreadyAcknowledged(
                f"incident {incident_id!r} status={row['status']!r}; "
                "force-clear only applies to acknowledged_paused incidents"
            )

        conn.execute(
            """
            UPDATE incidents SET
              status = 'acknowledged_raised',
              ack_at = ?,
              ack_action = 'force_clear',
              ack_comment = ?
            WHERE id = ?
            """,
            (
                now.isoformat(timespec="seconds"),
                # Preserve the existing leave_paused comment alongside the
                # force-clear comment so the audit trail keeps both.
                f"[force-cleared] {comment} (prior leave_paused: "
                f"{row['ack_comment'] or '<none>'})",
                incident_id,
            ),
        )
        conn.commit()

        refreshed = _row_to_incident(
            conn.execute(
                "SELECT * FROM incidents WHERE id = ?", (incident_id,),
            ).fetchone()
        )

    _audit_incident_event(
        refreshed,
        event="force_cleared",
        extra={"comment": comment},
    )
    return refreshed


def _bump_policy_cap(scope: ScopeRef, new_cap_usd: float) -> None:
    """Raise the existing policy's cap_usd. The policy must already exist."""
    from routines.budgets.storage import get_policy
    policy = get_policy(scope)
    if policy is None:
        raise ValueError(
            f"cannot raise cap on scope {scope_id(scope)!r}: policy not found"
        )
    upsert_policy(policy.model_copy(update={
        "cap_usd": new_cap_usd,
        "last_modified": datetime.now(timezone.utc),
    }))


# ────────────────────────────────────────────────────────────────────────────
# list_open
# ────────────────────────────────────────────────────────────────────────────


def list_open_incidents() -> list[Incident]:
    """All incidents currently blocking the gate.

    Returns rows with ``status IN ('open', 'acknowledged_paused')``.
    Sorted newest-first so the dashboard banner picks up the most
    recent overrun deterministically.
    """
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM incidents
            WHERE status IN ('open', 'acknowledged_paused')
            ORDER BY opened_at DESC
            """,
        ).fetchall()
    return [_row_to_incident(r) for r in rows]


def list_all_incidents(*, limit: int = 100) -> list[Incident]:
    """All incidents, any status. For audit / dashboard history view."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM incidents ORDER BY opened_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [_row_to_incident(r) for r in rows]


def find_blocking_incident(scope: ScopeRef, *, now: Optional[datetime] = None) -> Optional[Incident]:
    """Return an unresolved incident for the scope in the CURRENT period.

    ``acknowledged_paused`` incidents block until the period rolls over,
    so the gate consults this on every call. A row from a prior month
    does NOT block the current month — the period_start filter handles
    that automatically.
    """
    now = now or datetime.now(timezone.utc)
    month_start, _ = monthly_utc_window(now)
    sid = scope_id(scope)
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT * FROM incidents
            WHERE scope_id = ?
              AND period_start = ?
              AND status IN ('open', 'acknowledged_paused')
            ORDER BY opened_at DESC
            LIMIT 1
            """,
            (sid, month_start.isoformat(timespec="seconds")),
        ).fetchone()
    return _row_to_incident(row) if row else None


# ────────────────────────────────────────────────────────────────────────────
# Audit helpers
# ────────────────────────────────────────────────────────────────────────────


def _audit_incident_event(
    inc: Incident,
    *,
    event: str,
    extra: Optional[dict] = None,
) -> None:
    """Write one row to ``runs/budgets.incident.jsonl``.

    Never raises — observability must not break the gate. Errors are
    logged at WARNING.
    """
    try:
        audit.write_structured(
            actor={"type": "system", "id": "routine:budgets.incident"},
            entity_type="budget",
            entity_id=inc.id,
            action=event,
            routine="budgets.incident",
            run_id=inc.id,
            status="ok",
            audit_dir=ROUTINES_REPO / "runs",
            inputs={
                "event": event,
                "scope_kind": inc.scope.kind,
                "scope_a": inc.scope.a,
                "scope_b": inc.scope.b,
                "current_pct": inc.current_pct,
                "hard_pct": inc.hard_pct,
                "cap_usd": inc.cap_usd,
                "current_spend_usd": inc.current_spend_usd,
                "status": inc.status,
            },
            outputs=extra or None,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("budgets audit write failed: %s", e)


# ────────────────────────────────────────────────────────────────────────────
# Row → dataclass
# ────────────────────────────────────────────────────────────────────────────


def _row_to_incident(row) -> Incident:  # row is sqlite3.Row
    return Incident(
        id=row["id"],
        scope=ScopeRef(
            kind=row["scope_kind"],
            a=row["scope_a"],
            b=row["scope_b"],
        ),
        opened_at=_parse_iso(row["opened_at"]),
        period_start=_parse_iso(row["period_start"]),
        current_pct=row["current_pct"],
        hard_pct=row["hard_pct"],
        cap_usd=row["cap_usd"],
        current_spend_usd=row["current_spend_usd"],
        status=row["status"],
        ack_at=_parse_iso(row["ack_at"]) if row["ack_at"] else None,
        ack_action=row["ack_action"],
        ack_new_cap_usd=row["ack_new_cap_usd"],
        ack_comment=row["ack_comment"],
    )


__all__ = [
    "AckAction",
    "IncidentStatus",
    "Incident",
    "IncidentNotFound",
    "IncidentAlreadyAcknowledged",
    "record_overrun",
    "acknowledge",
    "force_clear",
    "list_open_incidents",
    "list_all_incidents",
    "find_blocking_incident",
]
