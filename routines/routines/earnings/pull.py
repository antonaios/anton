"""Pull recent quarterly earnings for a ticker via OpenBB.

Uses the markets adapter's lazy provider so we get OpenBB when available
and a stub fallback otherwise. Deterministic — no LLM in the loop.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

from routines.earnings.schema import EarningsRecord
from routines.markets import get_provider

log = logging.getLogger(__name__)


def pull_earnings(symbol: str, periods: int = 4) -> list[EarningsRecord]:
    """Return up to `periods` quarterly earnings records for `symbol`.

    Notes:
      - We currently use the same Fundamentals path as the equity-research
        workflow (which is annual). True quarterly pull will route through
        the same OpenBB `equity.fundamental.income(..., period='quarter')`
        — but that call shape differs by provider and merits its own
        adapter method. For now we annualise: one row per fiscal year.
      - Period labels: "FY{year}" for annual, "FY{year} Q{n}" later.
      - Source = provider.name (e.g. "openbb-yfinance" or "stub").
    """
    provider = get_provider()
    funda = provider.get_fundamentals(symbol, years=periods)
    out: list[EarningsRecord] = []
    prior_revenue: dict[int, float] = {}
    for y in sorted(funda.years, key=lambda y: y.fiscal_year):
        prior_revenue[y.fiscal_year] = y.revenue or 0.0
    for y in funda.years[:periods]:
        prior = prior_revenue.get(y.fiscal_year - 1)
        yoy = ((y.revenue / prior) - 1) if (y.revenue and prior) else None
        margin = (y.ebitda / y.revenue) if (y.ebitda and y.revenue) else None
        period_end: date | None = None
        if y.period_end:
            try:
                period_end = date.fromisoformat(y.period_end[:10])
            except ValueError:
                period_end = None
        # Schema columns are "(m)" — millions. OpenBB returns raw values
        # (billions as e.g. 2_921_900_000), so normalise here.
        def _m(v: float | None) -> float | None:
            return None if v is None else v / 1_000_000
        out.append(EarningsRecord(
            period_end=period_end,
            period_label=f"FY{y.fiscal_year}",
            ticker=symbol,
            company=funda.name or symbol,
            currency=funda.currency or "",
            revenue_m=_m(y.revenue),
            revenue_yoy=yoy,
            ebitda_m=_m(y.ebitda),
            ebitda_margin=margin,
            ebit_m=_m(y.ebit),
            net_income_m=_m(y.net_income),
            eps=None,                  # not available in current Fundamentals
            fcf_m=_m(y.free_cash_flow),
            notes="",
            source=funda.provider or provider.name,
        ))
    return out


# Suppress unused-import warning for Any.
_ = Any
