"""Shared schemas for the markets adapter."""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel


class Quote(BaseModel):
    """One row of the SparkTicker. The shape mirrors what the dashboard
    consumes; provider implementations adapt to this."""

    symbol: str                              # "JDW.L"
    name: str                                # "Greene King"
    price: str                               # "485p" / "£2,341.28" / "$82.15"
    change: str                              # "+0.3%" or "−0.4%" (note: en-dash for negative)
    direction: Literal["up", "down", "flat"]
    points: str                              # SVG polyline "0,14 6,15 12,12 ..."
    currency: Optional[str] = None           # "GBP" | "USD" — informational
    provider: Optional[str] = None           # "yfinance" | "stub" — for debug
    price_numeric: Optional[float] = None    # raw numeric price in the listing ccy
                                             # (e.g. 1990.0 GBp); `price` is the
                                             # display string. Consumers needing
                                             # arithmetic (comps) read this.


# ── Fundamentals ─────────────────────────────────────────────────────────


class FundamentalsYear(BaseModel):
    """One fiscal year of summary financials."""
    fiscal_year: int                  # 2025
    period_end: Optional[str] = None  # ISO date
    revenue: Optional[float] = None
    gross_profit: Optional[float] = None
    ebitda: Optional[float] = None
    ebit: Optional[float] = None
    net_income: Optional[float] = None
    capex: Optional[float] = None
    operating_cash_flow: Optional[float] = None
    free_cash_flow: Optional[float] = None
    total_debt: Optional[float] = None         # incl. IFRS-16 lease liabilities
    capital_lease_obligations: Optional[float] = None  # the lease component of total_debt
    cash_and_equivalents: Optional[float] = None
    shareholders_equity: Optional[float] = None


class FundamentalsRatios(BaseModel):
    pe: Optional[float] = None
    ev_ebitda: Optional[float] = None
    roe: Optional[float] = None
    roic: Optional[float] = None
    net_debt_ebitda: Optional[float] = None
    dividend_yield: Optional[float] = None
    revenue_growth_5y_cagr: Optional[float] = None
    ebitda_margin: Optional[float] = None


class Fundamentals(BaseModel):
    symbol: str
    name: Optional[str] = None
    currency: Optional[str] = None
    fiscal_year_end: Optional[str] = None  # e.g. "December"
    years: list[FundamentalsYear] = []
    ratios: Optional[FundamentalsRatios] = None
    provider: Optional[str] = None
    error: Optional[str] = None             # set if provider gave nothing


# ── News ─────────────────────────────────────────────────────────────────


class NewsItem(BaseModel):
    title: str
    url: str
    published: Optional[str] = None  # ISO date
    source: Optional[str] = None     # publisher
    summary: Optional[str] = None


class NewsResult(BaseModel):
    symbol: str
    items: list[NewsItem] = []
    provider: Optional[str] = None
    error: Optional[str] = None


# ── Peers ────────────────────────────────────────────────────────────────


class PeerItem(BaseModel):
    symbol: str
    name: Optional[str] = None
    sector: Optional[str] = None
    industry: Optional[str] = None
    country: Optional[str] = None


class PeersResult(BaseModel):
    symbol: str
    peers: list[PeerItem] = []
    provider: Optional[str] = None
    error: Optional[str] = None


# ── Comps workflow ───────────────────────────────────────────────────────


class CompRow(BaseModel):
    """One row of a comps table — the target itself first, then peers."""
    symbol: str
    name: Optional[str] = None
    currency: Optional[str] = None
    revenue: Optional[float] = None
    ebitda: Optional[float] = None
    ebitda_margin: Optional[float] = None
    revenue_growth_5y_cagr: Optional[float] = None
    pe: Optional[float] = None
    ev_ebitda: Optional[float] = None
    net_debt_ebitda: Optional[float] = None
    dividend_yield: Optional[float] = None
    fiscal_year: Optional[int] = None


class CompsResult(BaseModel):
    target_symbol: str
    target_name: Optional[str] = None
    rows: list[CompRow] = []
    note_path: Optional[str] = None    # path written to Companies/<X>.md
    provider: Optional[str] = None
    warnings: list[str] = []


# ── Equity Research workflow ─────────────────────────────────────────────


class EquityResearchSnapshot(BaseModel):
    symbol: str
    name: Optional[str] = None
    currency: Optional[str] = None
    last_price: Optional[str] = None    # display string e.g. "2,318p"
    price_change: Optional[str] = None
    direction: Optional[Literal["up", "down", "flat"]] = None
    pe: Optional[float] = None
    ev_ebitda: Optional[float] = None
    dividend_yield: Optional[float] = None
    ebitda_margin: Optional[float] = None
    revenue_growth_5y_cagr: Optional[float] = None


# ── Macro bar (second ticker row: indices, commodities, rates, indicators) ─


class MacroRow(BaseModel):
    """One cell in the macro/index/commodity/rate ticker bar.

    Fields are display-formatted strings (the dashboard renders verbatim),
    plus `points` as an SVG polyline for a 60x20 viewbox.
    """
    symbol: str                              # config-supplied (may be synthetic e.g. UK_CPI)
    name: str                                # display label
    kind: Literal["equity", "index", "commodity", "rate", "indicator"]
    value: str                               # "10,368", "3.45%", "$105.64", "4.46%"
    change: str                              # "+0.3%" / "+12bp" / "+0.2pp"
    direction: Literal["up", "down", "flat"]
    points: str                              # 12m sparkline polyline
    note: Optional[str] = None               # "12 May" / "Mar 2026 · YoY" / etc
    provider: Optional[str] = None


class MacroBarResponse(BaseModel):
    rows: list[MacroRow]
    source: str   # "config" or "fallback"


class EquityResearchResult(BaseModel):
    target_symbol: str
    snapshot: EquityResearchSnapshot
    fundamentals: Fundamentals               # 5y history
    comps: CompsResult                       # target + peers
    news: NewsResult                         # 14-day window
    note_path: Optional[str] = None
    provider: Optional[str] = None
    warnings: list[str] = []
