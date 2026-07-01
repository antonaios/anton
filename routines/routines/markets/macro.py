"""Macro / index / commodity / rate / indicator row builder.

The dashboard's second ticker bar (below equities) shows a mix of:
  - indices       ^FTSE, ^GSPC, ^NDX, ^DJI
  - commodities   BZ=F (Brent), GC=F (gold)
  - rates         UK 10Y gilt, UK 3M (SONIA proxy)
  - indicators    UK CPI

Each lives in a different OpenBB endpoint. This module hides that and
exposes a single `build_macro_row(symbol, kind, name)` that always
returns the same `MacroRow` shape.

Kinds:

    equity / index / commodity
        yfinance equity.price.quote + historical close fallback for the
        current value; price.historical for the 12m sparkline. Reuses the
        existing OpenBBProvider methods.

    rate
        fixedincome.government.treasury_rates(country='united_kingdom').
        Symbol selects the column:
            UK_3M  -> month_3   (SONIA proxy without FRED key)
            UK_10Y -> year_10
        12m daily series → sparkline. Change reported in basis points.

    indicator
        economy.cpi(country='united_kingdom', frequency='monthly') for
        UK_CPI. 12 monthly data points → sparkline. Change reported in
        percentage points vs prior month.

If anything fails, returns a stub MacroRow with the configured label and
an "—" value rather than dropping the entry — keeps the bar consistent.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Literal

from routines.markets.types import MacroRow

log = logging.getLogger(__name__)


# Synthetic symbol -> (country, column) for the gilt-curve path.
_RATE_COLUMNS = {
    "UK_3M":  ("united_kingdom", "month_3"),
    "UK_10Y": ("united_kingdom", "year_10"),
}

# Synthetic symbols that route to dedicated single-series endpoints.
# Distinct from _RATE_COLUMNS because the API shape is different.
_SINGLE_SERIES_RATES = {
    "UK_SONIA",   # overnight, via obb.fixedincome.rate.sonia() — needs FRED key
}

_INDICATOR_FETCHERS = {
    "UK_CPI": ("cpi", "united_kingdom"),
}


# ── public entry point ────────────────────────────────────────────────────


def build_macro_row(symbol: str, kind: str, name: str, provider_name: str = "openbb-yfinance") -> MacroRow:
    """Return one MacroRow for the dashboard's macro bar.

    Caller has already validated `symbol` and `kind` via the public-ticker
    regex (for equity/index/commodity) or a separate allowlist (for
    rate/indicator synthetics).
    """
    try:
        if kind in ("equity", "index", "commodity"):
            return _build_market_row(symbol, kind, name)   # type: ignore[arg-type]
        if kind == "rate":
            if symbol in _SINGLE_SERIES_RATES:
                return _build_single_series_rate(symbol, name)
            return _build_rate_row(symbol, name)
        if kind == "indicator":
            return _build_indicator_row(symbol, name)
    except Exception as e:  # noqa: BLE001
        log.exception("macro: build_macro_row(%s, %s) failed", symbol, kind)
        return _stub_row(symbol, name, kind, note=f"build failed: {e}")
    return _stub_row(symbol, name, kind, note=f"unknown kind: {kind}")


# ── equity/index/commodity (reuse OpenBBProvider) ─────────────────────────


def _build_market_row(symbol: str, kind: Literal["equity", "index", "commodity"], name: str) -> MacroRow:
    from routines.markets import get_provider
    provider = get_provider()

    # get_quotes handles the last_price-missing fallback for indices.
    quotes = provider.get_quotes([symbol])
    if not quotes:
        return _stub_row(symbol, name, kind, note="no quote")
    q = quotes[0]

    # For indices the equity-quote formatter appends "p" (yfinance returns
    # currency=GBP for ^FTSE and the existing formatter treats GBP/GBp the
    # same as UK pence). Strip and reformat with thousands separators.
    value = q.price
    if kind == "index" and value.endswith("p"):
        try:
            n = float(value.rstrip("p").replace(",", ""))
            value = f"{n:,.0f}"
        except ValueError:
            pass

    return MacroRow(
        symbol=symbol,
        name=name,
        kind=kind,
        value=value,
        change=q.change,
        direction=q.direction,
        points=q.points,
        note=None,
        provider=q.provider,
    )


# ── rate (UK gilt curve via fixedincome.government.treasury_rates) ────────


def _build_rate_row(symbol: str, name: str) -> MacroRow:
    if symbol not in _RATE_COLUMNS:
        return _stub_row(symbol, name, "rate", note=f"unknown rate symbol {symbol}")
    country, column = _RATE_COLUMNS[symbol]

    rows = _fetch_treasury_rates(country)
    if not rows:
        return _stub_row(symbol, name, "rate", note="no rate data")

    # Extract the requested column. Latest value last.
    series: list[tuple[date, float]] = []
    for r in rows:
        d = _to_date(r.get("date"))
        v = _finite_float(r.get(column))
        if d is None or v is None:
            continue
        series.append((d, v))
    if not series:
        return _stub_row(symbol, name, "rate", note="no rate column")

    series.sort(key=lambda x: x[0])
    latest_date, latest_val = series[-1]
    # Change vs prior trading day:
    if len(series) >= 2:
        prev_val = series[-2][1]
        change_bp = (latest_val - prev_val) * 10000   # rate is decimal (0.044 → 4.40%); 1bp = 0.0001
        sign = "+" if change_bp >= 0 else "−"
        change_str = f"{sign}{abs(change_bp):.0f}bp"
        direction = "up" if change_bp > 0.5 else "down" if change_bp < -0.5 else "flat"
    else:
        change_str = "—"
        direction = "flat"

    # 12m sparkline from the same series.
    cutoff = latest_date - timedelta(days=365)
    pts_series = [v for (d, v) in series if d >= cutoff]
    points = _series_to_polyline(pts_series, target_points=36)

    value_str = f"{latest_val * 100:.2f}%"

    return MacroRow(
        symbol=symbol,
        name=name,
        kind="rate",
        value=value_str,
        change=change_str,
        direction=direction,
        points=points,
        note=latest_date.strftime("%d %b"),
        provider="openbb-federal-reserve",
    )


def _build_single_series_rate(symbol: str, name: str) -> MacroRow:
    """Rates that come from a dedicated FRED-backed endpoint (single
    time series rather than a yield curve). SONIA is the current
    example. Distinct from _build_rate_row because the data format
    differs — SONIA's `rate` field is already in percent (3.73 vs
    0.0373 for gilt rates)."""
    from openbb import obb   # type: ignore[import-not-found]
    try:
        if symbol == "UK_SONIA":
            obj = obb.fixedincome.rate.sonia()
        else:
            return _stub_row(symbol, name, "rate", note=f"unknown series {symbol}")
        rows = [r.model_dump() if hasattr(r, "model_dump") else dict(r) for r in obj.results]
    except Exception as e:  # noqa: BLE001
        log.warning("macro: %s fetch failed: %s", symbol, e)
        return _stub_row(symbol, name, "rate", note="fetch failed")
    if not rows:
        return _stub_row(symbol, name, "rate", note="no data")

    series: list[tuple[date, float]] = []
    for r in rows:
        d = _to_date(r.get("date"))
        v = _finite_float(r.get("rate"))
        if v is None:
            v = _finite_float(r.get("value"))
        if d is None or v is None:
            continue
        series.append((d, v))
    if not series:
        return _stub_row(symbol, name, "rate", note="no values")

    series.sort(key=lambda x: x[0])
    latest_date, latest_val = series[-1]

    # Change vs prior observation, in bp. `latest_val` is already in
    # percent (e.g. 3.7292) so 1bp = 0.01 of the value.
    if len(series) >= 2:
        prev_val = series[-2][1]
        change_bp = (latest_val - prev_val) * 100
        sign = "+" if change_bp >= 0 else "−"
        change_str = f"{sign}{abs(change_bp):.0f}bp"
        direction = "up" if change_bp > 0.5 else "down" if change_bp < -0.5 else "flat"
    else:
        change_str = "—"
        direction = "flat"

    # 12m sparkline.
    cutoff = latest_date - timedelta(days=365)
    pts_series = [v for (d, v) in series if d >= cutoff]
    points = _series_to_polyline(pts_series, target_points=36)

    value_str = f"{latest_val:.2f}%"

    return MacroRow(
        symbol=symbol,
        name=name,
        kind="rate",
        value=value_str,
        change=change_str,
        direction=direction,
        points=points,
        note=latest_date.strftime("%d %b"),
        provider="openbb-fred",
    )


def _fetch_treasury_rates(country: str) -> list[dict[str, Any]]:
    """Pull recent UK gilt rates. OpenBB returns a list of daily rows
    with columns for each tenor (month_3, year_10, etc.).
    """
    from openbb import obb   # type: ignore[import-not-found]
    try:
        # `start_date` filter so we get ~12 months of history for the sparkline.
        from datetime import date as date_cls
        end = date_cls.today()
        start = end - timedelta(days=365)
        obj = obb.fixedincome.government.treasury_rates(
            country=country,
            start_date=str(start),
            end_date=str(end),
        )
        rows = []
        for r in obj.results:
            d = r.model_dump() if hasattr(r, "model_dump") else dict(r)
            rows.append(d)
        return rows
    except Exception as e:  # noqa: BLE001
        log.warning("macro: treasury_rates(%s) failed: %s", country, e)
        return []


# ── indicator (UK CPI via economy.cpi) ────────────────────────────────────


def _build_indicator_row(symbol: str, name: str) -> MacroRow:
    if symbol not in _INDICATOR_FETCHERS:
        return _stub_row(symbol, name, "indicator", note=f"unknown indicator {symbol}")
    kind_key, country = _INDICATOR_FETCHERS[symbol]
    rows = _fetch_indicator(kind_key, country)
    if not rows:
        return _stub_row(symbol, name, "indicator", note="no indicator data")

    series: list[tuple[date, float]] = []
    for r in rows:
        d = _to_date(r.get("date"))
        v = _finite_float(r.get("value"))
        if d is None or v is None:
            continue
        series.append((d, v))
    if not series:
        return _stub_row(symbol, name, "indicator", note="no values")

    series.sort(key=lambda x: x[0])
    latest_date, latest_val = series[-1]
    # CPI values are decimal YoY (e.g. 0.0345 = 3.45%).
    # Change vs prior month, in percentage points.
    if len(series) >= 2:
        prev_val = series[-2][1]
        change_pp = (latest_val - prev_val) * 100
        sign = "+" if change_pp >= 0 else "−"
        change_str = f"{sign}{abs(change_pp):.2f}pp"
        direction = "up" if change_pp > 0.01 else "down" if change_pp < -0.01 else "flat"
    else:
        change_str = "—"
        direction = "flat"

    # 12 months → 36 points is excessive; use whatever we have.
    pts_series = [v for (d, v) in series[-12:]]
    points = _series_to_polyline(pts_series, target_points=min(12, len(pts_series)))

    value_str = f"{latest_val * 100:.2f}%"
    return MacroRow(
        symbol=symbol,
        name=name,
        kind="indicator",
        value=value_str,
        change=change_str,
        direction=direction,
        points=points,
        note=latest_date.strftime("%b %Y") + " · YoY",
        provider="openbb-econdb",
    )


def _fetch_indicator(kind_key: str, country: str) -> list[dict[str, Any]]:
    from openbb import obb   # type: ignore[import-not-found]
    try:
        if kind_key == "cpi":
            obj = obb.economy.cpi(country=country, frequency="monthly")
        else:
            return []
        rows = []
        for r in obj.results:
            d = r.model_dump() if hasattr(r, "model_dump") else dict(r)
            rows.append(d)
        return rows
    except Exception as e:  # noqa: BLE001
        log.warning("macro: indicator(%s, %s) failed: %s", kind_key, country, e)
        return []


# ── helpers ───────────────────────────────────────────────────────────────


def _to_date(v: Any) -> date | None:
    if v is None:
        return None
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def _finite_float(v: Any) -> float | None:
    """Coerce to a finite float, or None for missing / non-numeric / non-finite.

    float("nan") succeeds, so a NaN observation (e.g. yfinance's current
    incomplete bar) would otherwise pass a bare float() parse and surface as a
    "nan%" / "nan bp" value. Dropping it lets the latest FINITE observation be
    shown instead — or the empty-series stub ("—") when none is finite. Same
    non-finite class as the sparkline drop in get_history_points (70938e0).
    """
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _series_to_polyline(values: list[float], *, target_points: int = 36) -> str:
    """Render a series of floats as an SVG polyline string for a 60x20
    viewbox. Returns "" on insufficient data.
    """
    # Drop NaN/inf before plotting — a single non-finite value renders as
    # ",nan" in the points string and blanks the whole SVG polyline.
    values = [v for v in values if math.isfinite(v)]
    if len(values) < 3:
        return ""
    if len(values) > target_points:
        step = (len(values) - 1) / (target_points - 1)
        values = [values[round(i * step)] for i in range(target_points)]
    vmin, vmax = min(values), max(values)
    n = len(values)
    if vmin == vmax:
        return " ".join(f"{round(i * 60 / max(n - 1, 1), 1)},10" for i in range(n))
    span = vmax - vmin
    parts: list[str] = []
    for i, v in enumerate(values):
        x = round(i * 60 / (n - 1), 1)
        y = round(2 + (vmax - v) / span * 16, 1)
        parts.append(f"{x},{y}")
    return " ".join(parts)


def _stub_row(symbol: str, name: str, kind: str, *, note: str = "") -> MacroRow:
    return MacroRow(
        symbol=symbol,
        name=name,
        kind=kind,                          # type: ignore[arg-type]
        value="—",
        change="",
        direction="flat",
        points="",
        note=note or None,
        provider="stub",
    )
