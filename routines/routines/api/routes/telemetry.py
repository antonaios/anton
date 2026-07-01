"""Telemetry endpoints — two surfaces.

  1. ``GET /api/telemetry/burn`` — ForecastPanel-shaped report from
     ``~/.claude/projects/*/*.jsonl`` + routines audit logs. Token-burn
     projection vs caps. Original surface.

  2. ``GET /api/telemetry/llm-burn`` — per-call LLM cost aggregator from
     ``routines/telemetry/llm_calls.jsonl``. Per-provider + per-session
     summary for harness #14's Burn Rate panel. Added in #13.

Both are idempotent + read-only. Safe to poll.
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from routines.api.deps import RUNS_DIR
from routines.telemetry import compute_burn
from routines.telemetry.llm_burn import GroupBy, compute_llm_burn

router = APIRouter()
log = logging.getLogger(__name__)


# OpenAPI shape — mirrors dashboard/src/types.ts ForecastData / ForecastSeries.
class BurnSeriesModel(BaseModel):
    label: str
    used: float
    projected: float
    cap: float
    unit: str
    resetIn: str
    status: Literal["ok", "warn", "crit"]


class BurnResponse(BaseModel):
    burnRate: str
    sessionsToday: int
    costToday: str
    series: list[BurnSeriesModel]


@router.get("/telemetry/burn", response_model=BurnResponse)
def telemetry_burn() -> BurnResponse:
    try:
        report = compute_burn(routines_runs_dir=RUNS_DIR)
    except Exception as e:  # noqa: BLE001
        log.exception("telemetry: compute_burn failed")
        raise HTTPException(status_code=500, detail=f"telemetry failed: {e}") from e

    return BurnResponse(
        burnRate=report.burnRate,
        sessionsToday=report.sessionsToday,
        costToday=report.costToday,
        series=[BurnSeriesModel(**asdict(s)) for s in report.series],
    )


# ────────────────────────────────────────────────────────────────────────────
# /api/telemetry/llm-burn  (#13)
# ────────────────────────────────────────────────────────────────────────────


class ModelBurnModel(BaseModel):
    calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0


class ProviderBurnModel(ModelBurnModel):
    models: dict[str, ModelBurnModel] = Field(default_factory=dict)


class SessionBurnModel(ModelBurnModel):
    workspace_type: Optional[str] = None
    workspace_name: Optional[str] = None


class WorkspaceBurnModel(ModelBurnModel):
    workspace_type: Optional[str] = None
    workspace_name: Optional[str] = None
    providers: dict[str, ModelBurnModel] = Field(default_factory=dict)


class LLMBurnResponse(BaseModel):
    window: dict[str, str]
    totals: ModelBurnModel
    by_provider: dict[str, ProviderBurnModel]
    by_session: Optional[dict[str, SessionBurnModel]] = None
    by_workspace: Optional[dict[str, WorkspaceBurnModel]] = None


def _parse_iso_param(name: str, value: Optional[str]) -> Optional[datetime]:
    if value is None:
        return None
    try:
        s = value
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError as e:
        raise HTTPException(422, f"invalid {name!r}: {e}") from e


@router.get("/telemetry/llm-burn", response_model=LLMBurnResponse)
def telemetry_llm_burn(
    since: Optional[str] = Query(None, description="ISO-8601; defaults to 24h ago"),
    until: Optional[str] = Query(None, description="ISO-8601; defaults to now"),
    group_by: GroupBy = Query(
        "provider",
        description="provider | session | both | workspace | all",
    ),
) -> LLMBurnResponse:
    """Aggregate ``llm_calls.jsonl`` into a windowed per-provider summary."""
    since_dt = _parse_iso_param("since", since)
    until_dt = _parse_iso_param("until", until)
    if since_dt and until_dt and since_dt > until_dt:
        raise HTTPException(422, "since must be before until")

    # Resolve the jsonl path lazily so tests can monkeypatch
    # ``routines.telemetry.llm_writer.LLM_CALLS_JSONL`` at runtime.
    from routines.telemetry import llm_writer as _writer_mod
    jsonl_path = _writer_mod.LLM_CALLS_JSONL

    summary = compute_llm_burn(
        jsonl_path=jsonl_path,
        since=since_dt,
        until=until_dt,
        group_by=group_by,
    )

    # Map dataclasses → Pydantic. Field-by-field is cheap + safe.
    def _mb(m) -> ModelBurnModel:
        return ModelBurnModel(
            calls=m.calls,
            tokens_in=m.tokens_in,
            tokens_out=m.tokens_out,
            cost_usd=m.cost_usd,
        )

    by_provider: dict[str, ProviderBurnModel] = {}
    for k, pb in summary.by_provider.items():
        by_provider[k] = ProviderBurnModel(
            calls=pb.calls,
            tokens_in=pb.tokens_in,
            tokens_out=pb.tokens_out,
            cost_usd=pb.cost_usd,
            models={mk: _mb(mv) for mk, mv in pb.models.items()},
        )

    by_session: Optional[dict[str, SessionBurnModel]] = None
    if summary.by_session is not None:
        by_session = {}
        for sid, sb in summary.by_session.items():
            by_session[sid] = SessionBurnModel(
                calls=sb.calls,
                tokens_in=sb.tokens_in,
                tokens_out=sb.tokens_out,
                cost_usd=sb.cost_usd,
                workspace_type=sb.workspace_type,
                workspace_name=sb.workspace_name,
            )

    by_workspace: Optional[dict[str, WorkspaceBurnModel]] = None
    if summary.by_workspace is not None:
        by_workspace = {}
        for wkey, wb in summary.by_workspace.items():
            by_workspace[wkey] = WorkspaceBurnModel(
                calls=wb.calls,
                tokens_in=wb.tokens_in,
                tokens_out=wb.tokens_out,
                cost_usd=wb.cost_usd,
                workspace_type=wb.workspace_type,
                workspace_name=wb.workspace_name,
                providers={pk: _mb(pv) for pk, pv in wb.providers.items()},
            )

    return LLMBurnResponse(
        window=summary.window,
        totals=_mb(summary.totals),
        by_provider=by_provider,
        by_session=by_session,
        by_workspace=by_workspace,
    )
