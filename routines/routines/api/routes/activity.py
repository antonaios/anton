"""GET /api/activity — queryable structured activity log (#60).

Reads from ``routines/state/audit_index.db`` (the SQLite sidecar populated
by every ``routines.shared.audit.write_structured()`` call; the legacy
``write()`` adapter still routes through the same pipeline, though all
call sites now use ``write_structured`` directly — #60-migrate-sites).

Filters (all optional, ANDed together):
  * entity_type — one of the closed enum strings (``proposal``,
    ``workspace``, …)
  * entity_id   — exact match
  * actor_type  — ``user`` | ``system`` | ``agent`` | ``plugin``
  * actor_id    — exact match (e.g. ``"routine:hinotes"`` for legacy
    writes, ``"Operator"`` for operator actions)
  * since / until — ISO-8601 UTC bounds (inclusive)
  * limit       — 1..1000, default 50

Always ORDER BY ts DESC. The JSONL stream at ``routines/runs/activity.jsonl``
remains the load-bearing primary write target; this endpoint is the
queryable view.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Literal, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from routines.shared import audit_db
from routines.shared.audit import ACTOR_TYPES, ENTITY_TYPES

router = APIRouter()
log = logging.getLogger(__name__)


ActorTypeLiteral = Literal["user", "system", "agent", "plugin"]
EntityTypeLiteral = Literal[
    "session",
    "skill_run",
    "vault_note",
    "proposal",
    "workspace",
    "budget",
    "credential",
    "scheduler_job",
    "composite_run",
    "crew_run",
]


class ActorRefDTO(BaseModel):
    type: str
    id: str


class ActivityRow(BaseModel):
    ts: str
    actor: ActorRefDTO
    action: str
    entity_type: str
    entity_id: str
    run_id: Optional[str] = None
    details: Optional[dict[str, Any]] = None


class ActivityResponse(BaseModel):
    rows: list[ActivityRow]
    count: int = Field(..., description="Number of rows in this response (post-filter, post-limit).")


def _coerce_iso(value: Optional[datetime], param_name: str) -> Optional[str]:
    """FastAPI parses datetime params natively; convert back to ISO for
    the SQLite comparison (rows are stored as ISO strings). Naive
    datetimes are treated as UTC."""
    if value is None:
        return None
    # No conversion needed — sqlite3 compares ISO-8601 strings
    # lexicographically, which is the intended UTC-ordering behaviour
    # when both sides include the same precision + tz suffix.
    return value.isoformat(timespec="seconds")


@router.get("/activity", response_model=ActivityResponse)
def list_activity(
    entity_type: Optional[EntityTypeLiteral] = Query(
        None, description="Filter by closed enum entity type."
    ),
    entity_id: Optional[str] = Query(None, description="Exact-match entity ID."),
    actor_type: Optional[ActorTypeLiteral] = Query(
        None, description="Filter by actor type."
    ),
    actor_id: Optional[str] = Query(None, description="Exact-match actor ID."),
    since: Optional[datetime] = Query(
        None, description="ISO-8601 UTC lower bound (inclusive)."
    ),
    until: Optional[datetime] = Query(
        None, description="ISO-8601 UTC upper bound (inclusive)."
    ),
    limit: int = Query(50, ge=1, le=1000),
) -> ActivityResponse:
    """Read filtered rows from ``audit_index.db``; ORDER BY ts DESC."""
    # Belt-and-braces enum validation — FastAPI's Literal type already
    # enforces this at parse time, but if the enums ever drift between
    # audit.py and the route signature this catches the mismatch.
    if entity_type is not None and entity_type not in ENTITY_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"entity_type must be one of {list(ENTITY_TYPES)}",
        )
    if actor_type is not None and actor_type not in ACTOR_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"actor_type must be one of {list(ACTOR_TYPES)}",
        )

    try:
        rows = audit_db.query_audit(
            entity_type=entity_type,
            entity_id=entity_id,
            actor_type=actor_type,
            actor_id=actor_id,
            since=_coerce_iso(since, "since"),
            until=_coerce_iso(until, "until"),
            limit=limit,
        )
    except Exception as e:  # noqa: BLE001 — surface the DB failure
        log.exception("activity query failed")
        raise HTTPException(status_code=500, detail=f"activity query failed: {e}") from e

    return ActivityResponse(
        rows=[ActivityRow(**row) for row in rows],
        count=len(rows),
    )
