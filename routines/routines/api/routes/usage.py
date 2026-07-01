"""Plan-cap endpoint for the dashboard's LLMUsagePanel — OUTSTANDING #14b.

The Burn Rate panel (#13) shows per-call telemetry; this endpoint adds the
subscription-tier ceilings the panel needs to render "X% of plan used,
resets in Y" badges. Three provider rows are returned today:

  * **Anthropic** Claude Max — ~50 messages per 5h rolling block
  * **OpenAI** Plus — ~40 messages per 3h rolling block
  * **M2.7** Standard — £10 daily ceiling

``used_pct`` is computed from ``compute_llm_burn`` over each provider's
plan window. ``reset_in_sec`` is the seconds until the rolling window
closes (since-window-start + window-length − now), clamped to ≥0.

v1 hardcodes the cap values + window lengths — operator iterates on
real numbers once provider APIs surface official quotas. Endpoint shape
is locked so the dashboard can wire today and rotate caps later without
a contract change.

Wraps `tool_call_hooks` for uniform audit + sensitivity surface (this
endpoint reads telemetry — public lane, internal sensitivity is overkill
but matches the rest of the route discipline).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional

from fastapi import APIRouter
from pydantic import BaseModel, Field

from routines.hooks import tool_call_hooks

router = APIRouter()
log = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────────────
# Provider plan-cap table — hardcoded v1
# ────────────────────────────────────────────────────────────────────────────
#
# Each entry declares: which provider name shows up in llm_calls.jsonl,
# the plan-tier label the panel renders, the rolling window length, the
# cap (with unit) we're measuring against, and how the panel labels the
# window.
#
# The cap unit drives how `used` aggregates from telemetry:
#   * unit="messages"  → use `calls` count from the burn summary
#   * unit="usd"       → use `cost_usd` from the burn summary
#   * unit="gbp"       → use `cost_usd * gbp_per_usd` (cheap conversion;
#                        accurate enough for "where am I in the plan window")


_GBP_PER_USD = 0.79  # rough — will rotate as FX moves; cosmetic for v1


class _PlanSpec:
    __slots__ = (
        "provider", "plan_tier", "period_label", "window_seconds", "cap",
        "unit", "match_providers",
    )

    def __init__(
        self,
        provider: str,
        plan_tier: str,
        period_label: str,
        window_seconds: int,
        cap: float,
        unit: str,
        match_providers: frozenset[str] | None = None,
    ) -> None:
        self.provider = provider
        self.plan_tier = plan_tier
        self.period_label = period_label
        self.window_seconds = window_seconds
        self.cap = cap
        self.unit = unit
        # The set of telemetry ``provider`` keys this plan aggregates. The
        # normal path stamps ``provider_for(model)`` (anthropic / openai /
        # ollama / m27), but the #75 cloud paths set ``provider_override``
        # (claude-subprocess / claude-api), so a plan must match ALL of its
        # provider's aliases or real cloud usage reads 0. Defaults to just
        # ``{provider}`` for back-compat.
        self.match_providers = match_providers or frozenset({provider})


# Three rows, ordered per the v5 mock's LLMUsagePanel column.
_PLAN_TABLE: tuple[_PlanSpec, ...] = (
    _PlanSpec(
        provider="anthropic",
        plan_tier="Max",
        period_label="5h block",
        window_seconds=5 * 3600,
        cap=50.0,
        unit="messages",
        # provider_for(claude-*) = "anthropic"; #75 subprocess/API paths
        # override to claude-subprocess / claude-api.
        match_providers=frozenset({"anthropic", "claude", "claude-subprocess", "claude-api"}),
    ),
    _PlanSpec(
        provider="openai",
        plan_tier="Plus",
        period_label="3h block",
        window_seconds=3 * 3600,
        cap=40.0,
        unit="messages",
        # provider_for(gpt-*/o1/o3) = "openai"; codex overrides are forward-looking.
        match_providers=frozenset({"openai", "codex", "codex-subprocess", "codex-api"}),
    ),
    _PlanSpec(
        provider="m27",
        plan_tier="Standard",
        period_label="daily £",
        window_seconds=24 * 3600,
        cap=10.0,
        unit="gbp",
        match_providers=frozenset({"m27", "minimax"}),
    ),
)


# ────────────────────────────────────────────────────────────────────────────
# Pydantic wire shape
# ────────────────────────────────────────────────────────────────────────────


class PlanRow(BaseModel):
    provider: str = Field(..., description="provider key as it appears in llm_calls.jsonl")
    plan_tier: str
    period_label: str
    used_pct: float = Field(
        ..., ge=0.0, description="fraction of cap used in the current window (0.0-1.0+); may exceed 1.0 on overrun"
    )
    used: float = Field(..., ge=0.0, description="raw used amount in `unit`")
    cap: float = Field(..., ge=0.0, description="plan cap in `unit`")
    unit: Literal["messages", "usd", "gbp"]
    reset_in_sec: int = Field(..., ge=0, description="seconds until the window resets")
    # #llm-routing-postjune15 B5 — distinguishes a rolling plan-cap window from a
    # monthly UTC $-credit row (the Agent-SDK credit). Additive + defaulted, so
    # the existing rolling rows + any pre-B5 consumer are unaffected.
    reset_kind: Literal["rolling", "monthly"] = Field(
        "rolling",
        description="rolling = a sliding plan-cap window; monthly = a UTC-month $ budget credit",
    )


class PlansResponse(BaseModel):
    plans: list[PlanRow]


# ────────────────────────────────────────────────────────────────────────────
# Per-plan aggregation
# ────────────────────────────────────────────────────────────────────────────


def _used_for(
    spec: _PlanSpec, *, jsonl_path, now: datetime,
) -> tuple[float, int]:
    """Return (used_amount_in_spec_unit, reset_in_sec) for one plan.

    Window is ``[now - window_seconds, now]``. ``compute_llm_burn`` does
    the heavy lifting — we just project its output to the spec's unit
    and compute the seconds-until-reset.
    """
    from routines.telemetry.llm_burn import compute_llm_burn

    since = now - timedelta(seconds=spec.window_seconds)
    summary = compute_llm_burn(
        jsonl_path=jsonl_path,
        since=since,
        until=now,
        group_by="provider",
        now=now,
    )
    # Aggregate across every provider bucket this plan matches (handles the
    # #75 claude-subprocess / claude-api override keys, not just the bare
    # provider_for() name).
    matched = [
        pb for key, pb in summary.by_provider.items()
        if key in spec.match_providers
    ]
    calls = sum(pb.calls for pb in matched)
    cost = sum(pb.cost_usd for pb in matched)
    if not matched:
        used = 0.0
    elif spec.unit == "messages":
        used = float(calls)
    elif spec.unit == "usd":
        used = float(cost)
    elif spec.unit == "gbp":
        used = float(cost) * _GBP_PER_USD
    else:
        used = 0.0

    # Rolling-window reset: the oldest in-window event ages out at
    # `oldest_ts + window_seconds`. We don't track the oldest event here
    # (would require re-reading jsonl) — v1 approximates with
    # `since + window_seconds = now`, i.e. window_seconds remaining
    # always. Operator iterates on the precise math later. We clamp to 0
    # so the dashboard never renders a negative.
    reset_in_sec = max(0, int(spec.window_seconds))
    return used, reset_in_sec


# ────────────────────────────────────────────────────────────────────────────
# Monthly $-credit rows (#llm-routing-postjune15 B5) — read-side of B3
# ────────────────────────────────────────────────────────────────────────────


def _seconds_to_next_month(now: datetime) -> int:
    """Seconds from ``now`` to the start of next month (UTC) — the monthly
    budget window reset that a $-credit row counts down to."""
    if now.month == 12:
        nxt = now.replace(year=now.year + 1, month=1, day=1,
                          hour=0, minute=0, second=0, microsecond=0)
    else:
        nxt = now.replace(month=now.month + 1, day=1,
                          hour=0, minute=0, second=0, microsecond=0)
    return max(0, int((nxt - now).total_seconds()))


def _credit_rows(now: datetime) -> list[PlanRow]:
    """Monthly $-credit rows from provider-scope BudgetPolicies with a ``b="*"``
    (all-models) USD cap — e.g. the Agent-SDK monthly credit (B3). Distinct from
    the rolling plan-cap rows: unit=usd, monthly reset, plan_tier "Agent-SDK
    credit". Best-effort — a budgets read failure logs and yields no credit rows
    so the rolling plan rows are never affected."""
    try:
        from routines.budgets import list_policies
        from routines.budgets.gate import _current_spend_by_scope
        from routines.telemetry.cost_table import normalize_provider
    except Exception as e:  # noqa: BLE001
        log.warning("usage/plans: budgets import failed, no credit rows: %s", e)
        return []
    try:
        policies = [
            p for p in list_policies()
            if p.scope.kind == "provider" and p.scope.b == "*" and p.cap_usd > 0
        ]
        if not policies:
            return []
        spend_by_scope = _current_spend_by_scope([p.scope for p in policies], now=now)
        reset_in_sec = _seconds_to_next_month(now)
        rows: list[PlanRow] = []
        for p in policies:
            spend = spend_by_scope.get(p.scope.id(), 0.0)
            used_pct = (spend / p.cap_usd) if p.cap_usd > 0 else 0.0
            provider = normalize_provider(p.scope.a) or str(p.scope.a)
            rows.append(PlanRow(
                provider=provider,
                plan_tier="Agent-SDK credit",
                period_label="monthly $",
                used_pct=round(used_pct, 4),
                used=round(spend, 4),
                cap=p.cap_usd,
                unit="usd",
                reset_in_sec=reset_in_sec,
                reset_kind="monthly",
            ))
        return rows
    except Exception as e:  # noqa: BLE001
        log.warning("usage/plans: credit-row build failed: %s", e)
        return []


# ────────────────────────────────────────────────────────────────────────────
# Route
# ────────────────────────────────────────────────────────────────────────────


@router.get("/usage/plans", response_model=PlansResponse)
def get_plans() -> PlansResponse:
    """Per-provider plan-cap rows for the LLMUsagePanel.

    Hardcoded cap table for v1; pulls used from `/api/telemetry/llm-burn`
    aggregation (same data source the panel already reads for per-call
    burn). Operator iterates on real cap values + precise rolling-window
    math later — the wire shape stays stable.
    """
    with tool_call_hooks(
        tool_name="usage_plans",
        sensitivity="internal",
        tool_input={},
    ) as ctx:
        # Resolve telemetry path lazily so tests can monkeypatch the writer.
        from routines.telemetry import llm_writer as _writer_mod
        jsonl_path = _writer_mod.LLM_CALLS_JSONL
        now = datetime.now(timezone.utc)

        rows: list[PlanRow] = []
        for spec in _PLAN_TABLE:
            used, reset_in_sec = _used_for(spec, jsonl_path=jsonl_path, now=now)
            used_pct = (used / spec.cap) if spec.cap > 0 else 0.0
            rows.append(PlanRow(
                provider=spec.provider,
                plan_tier=spec.plan_tier,
                period_label=spec.period_label,
                used_pct=round(used_pct, 4),
                used=round(used, 4),
                cap=spec.cap,
                unit=spec.unit,  # type: ignore[arg-type]
                reset_in_sec=reset_in_sec,
            ))

        # #llm-routing-postjune15 B5 — append per-provider monthly $-credit rows
        # (the read-side of the B3 provider-scope credit policy).
        rows.extend(_credit_rows(now))

        result = PlansResponse(plans=rows)
        ctx.result = result.model_dump()
        return result


__all__ = ["router", "PlanRow", "PlansResponse"]
