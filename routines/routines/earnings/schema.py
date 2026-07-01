"""Earnings tracker schema — quarterly results per watchlist ticker."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any


COLUMNS: list[str] = [
    "Period End",
    "Period Label",      # "FY2025 Q2" — convenience for filtering
    "Ticker",
    "Company",
    "Currency",
    "Revenue (m)",
    "Revenue YoY",       # % change vs same period prior year
    "EBITDA (m)",
    "EBITDA Margin",
    "EBIT (m)",
    "Net Income (m)",
    "EPS",
    "FCF (m)",
    "Notes",             # free-text — operator-filled
    "Source",            # provider attribution
]


@dataclass
class EarningsRecord:
    """One quarterly row. Mirrors openpyxl append shape."""
    period_end: date | None = None
    period_label: str = ""
    ticker: str = ""
    company: str = ""
    currency: str = ""
    revenue_m: float | None = None
    revenue_yoy: float | None = None
    ebitda_m: float | None = None
    ebitda_margin: float | None = None
    ebit_m: float | None = None
    net_income_m: float | None = None
    eps: float | None = None
    fcf_m: float | None = None
    notes: str = ""
    source: str = ""

    def to_row(self) -> list[Any]:
        return [
            self.period_end,
            self.period_label,
            self.ticker,
            self.company,
            self.currency,
            self.revenue_m,
            self.revenue_yoy,
            self.ebitda_m,
            self.ebitda_margin,
            self.ebit_m,
            self.net_income_m,
            self.eps,
            self.fcf_m,
            self.notes,
            self.source,
        ]

    def dedupe_key(self) -> str:
        """Idempotency key: (ticker, period_label) — re-pulling same
        quarter twice should not duplicate the row."""
        return f"{(self.ticker or '').upper()}|{(self.period_label or '').strip()}"
