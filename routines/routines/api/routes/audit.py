"""GET /api/audit-runs — tail one of the routine audit-log JSONL files."""

from __future__ import annotations

import json
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from routines.api.deps import RUNS_DIR

router = APIRouter()

ALLOWED_ROUTINES = {
    "hinotes",
    "sectornews",
    "memory-promote",
    "dealtracker",
    "recall",
}


class AuditRun(BaseModel):
    ts: Optional[str] = None
    run_id: Optional[str] = None
    status: Optional[str] = None
    duration_ms: Optional[int] = None
    routine: Optional[str] = None
    inputs: Optional[dict[str, Any]] = None
    outputs: Optional[dict[str, Any]] = None
    error: Optional[str] = None


class AuditRunsResponse(BaseModel):
    runs: list[AuditRun]


@router.get("/audit-runs", response_model=AuditRunsResponse)
def audit_runs(
    routine: str = Query(..., min_length=1),
    limit: int = Query(default=25, ge=1, le=200),
) -> AuditRunsResponse:
    if routine not in ALLOWED_ROUTINES:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown routine '{routine}'. Allowed: {sorted(ALLOWED_ROUTINES)}",
        )

    path = RUNS_DIR / f"{routine}.jsonl"
    if not path.exists():
        return AuditRunsResponse(runs=[])

    rows: list[AuditRun] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Audit read failed: {e}") from e

    for line in text.splitlines()[-limit:]:
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        rows.append(AuditRun(**{k: obj.get(k) for k in AuditRun.model_fields}))

    return AuditRunsResponse(runs=list(reversed(rows)))
