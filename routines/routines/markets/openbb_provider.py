"""OpenBB provider — AGPLv3 surface contained here.

Import behaviour: top-level imports OpenBB. The `adapter.get_provider()`
factory wraps this in try/except so the rest of the routines layer never
sees OpenBB unless it's installed.

Sensitivity wrap: this provider receives **public tickers only**. Input
validation happens upstream in `routes/markets.py`. Defence in depth — if
something does slip through, the symbol is sent verbatim to YFinance via
OpenBB; nothing here secretly stores or routes it elsewhere.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Any

# Eager import — if openbb isn't installed, this module raises ImportError
# at first import. adapter.get_provider() catches that.
from openbb import obb  # type: ignore[import-not-found]

from routines.markets.cache import cached
from routines.markets.types import (
    Fundamentals, FundamentalsRatios, FundamentalsYear,
    NewsItem, NewsResult, PeerItem, PeersResult, Quote,
)

log = logging.getLogger(__name__)


class OpenBBProvider:
    """Quotes via OpenBB. Defaults to YFinance — no API key needed."""

    name = "openbb"

    def __init__(self, default_provider: str = "yfinance") -> None:
        self.default_provider = default_provider

    # ── Quotes ────────────────────────────────────────────────────────────

    @cached(ttl_seconds=60)
    def get_quotes(self, symbols: list[str]) -> list[Quote]:
        """Fetch real-time(ish) quotes. Cache 60s to limit external calls.

        For each symbol we also fetch a 12-month weekly history (24h-cached
        separately) and render it as the SVG polyline `points` string for
        the dashboard's SparkTicker. If history fails, the ticker still
        renders with a fallback line shape from the seed.

        Index/futures handling: yfinance returns `open` but not `last_price`
        for ^FTSE / ^GSPC / BZ=F etc. We fall back to the latest historical
        close in those cases.
        """
        out: list[Quote] = []
        for s in symbols:
            try:
                obj = obb.equity.price.quote(symbol=s, provider=self.default_provider)
                row = self._extract_row(obj)
                if row is None:
                    log.warning("openbb: no row for %s", s)
                    continue
                # Index / futures fix: if last_price is missing — or non-finite
                # (yfinance can hand back a NaN last_price on an index's
                # incomplete bar; NaN is truthy, so a bare `not` would miss it) —
                # pull the most recent FINITE historical close instead, so we
                # show a real value rather than degrading to the "—" placeholder.
                if not _finite_positive(row.get("last_price")):
                    latest_close = self._latest_close(s)
                    if latest_close is not None:
                        row = dict(row)
                        row["last_price"] = latest_close
                q = self._to_quote(s, row)
                # Replace seed polyline with real 12m history if available.
                history_points = self.get_history_points(s)
                if history_points:
                    q = q.model_copy(update={"points": history_points})
                out.append(q)
            except Exception as e:  # noqa: BLE001 — provider can raise many things
                log.warning("openbb: quote(%s) failed: %s", s, e)
                # Skip on failure; bridge route falls back to stub for missing symbols.
                continue
        return out

    @cached(ttl_seconds=60)
    def _latest_close(self, symbol: str) -> float | None:
        """Most recent daily close. Used as a fallback for indices/futures
        where yfinance's quote() omits last_price."""
        from datetime import date, timedelta
        end = date.today()
        start = end - timedelta(days=10)
        try:
            obj = obb.equity.price.historical(
                symbol=symbol, start_date=str(start), end_date=str(end),
                interval="1d", provider=self.default_provider,
            )
            rows = self._rows(obj)
            if not rows:
                return None
            for r in reversed(rows):
                c = r.get("close")
                if c is None:
                    continue
                try:
                    fc = float(c)
                except (TypeError, ValueError):
                    continue
                # Skip yfinance's current incomplete bar, whose close is NaN:
                # float(NaN) succeeds, so without this guard the NaN would be
                # returned as the "latest" price and render as "$nan" (or crash
                # int(round(NaN)) for GBp). Fall back to the most recent FINITE
                # close. Same non-finite class as the sparkline drop in
                # get_history_points (routines 70938e0).
                if not math.isfinite(fc):
                    continue
                return fc
        except Exception as e:  # noqa: BLE001
            log.info("openbb: _latest_close(%s) failed: %s", symbol, e)
        return None

    @cached(ttl_seconds=3600)
    def market_cap_and_shares(self, symbol: str) -> tuple[float | None, float | None]:
        """Return ``(market_cap, shares_outstanding)`` in the listing currency's
        major units, from OpenBB ``equity.profile`` (YFinance-backed).

        Both best-effort — any failure yields ``(None, None)`` so the comps
        acquisition degrades to operator-paste rather than raising. Used by the
        comps skill to populate the CoCo block's Mkt Cap **directly**: the quote
        path exposes only a display-string price, and price×shares mis-scales
        pence-quoted UK stocks (e.g. PPHE quoted in GBp but reporting in GBP).
        Deliberately NOT wired into ``get_quotes`` — the dashboard ticker
        doesn't need it and an extra profile fetch per symbol would slow that
        hot path.
        """
        try:
            obj = obb.equity.profile(symbol=symbol, provider=self.default_provider)
            res = getattr(obj, "results", obj)
            d = res[0] if isinstance(res, list) else res
            dd = d.model_dump() if hasattr(d, "model_dump") else dict(d)
            return _first_num(dd.get("market_cap")), _first_num(dd.get("shares_outstanding"))
        except Exception as e:  # noqa: BLE001
            log.info("openbb: profile(%s) market data failed: %s", symbol, e)
            return None, None

    # ── Historical price polyline (sparkline) ─────────────────────────────

    @cached(ttl_seconds=86400)  # 24h
    def get_history_points(
        self, symbol: str, *, months: int = 12, target_points: int = 36,
        interval: str = "1W",
    ) -> str | None:
        """Pull closing prices over the last `months` and render as an SVG
        polyline string suitable for a 60x20 viewbox.

        Coordinate system:
            x: 0 .. 60     (chronological, oldest = 0)
            y: 2 .. 18     (price; max price -> y=2, min -> y=18)
        Padding (2px top, 2px bottom) keeps the line from kissing edges.

        Returns None on any error so the caller can fall back to the seed
        polyline shape.

        Default 12 months × weekly = ~52 bars, downsampled to 36 evenly-
        spaced points. Override `interval='1d'` for daily-resolution
        sparklines when needed (e.g. rates / macro indicators where
        weekly granularity is too coarse).
        """
        from datetime import date, timedelta

        end = date.today()
        start = end - timedelta(days=int(months * 30.4375))
        try:
            obj = obb.equity.price.historical(
                symbol=symbol,
                start_date=str(start),
                end_date=str(end),
                interval=interval,
                provider=self.default_provider,
            )
            rows = self._rows(obj)
        except Exception as e:  # noqa: BLE001
            log.warning("openbb: historical(%s) failed: %s", symbol, e)
            return None

        closes: list[float] = []
        for r in rows:
            c = r.get("close")
            if c is None:
                continue
            try:
                fc = float(c)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(fc):
                # drop NaN/inf (e.g. yfinance's current incomplete weekly bar)
                # — else the trailing ",nan" blanks the SVG sparkline
                continue
            closes.append(fc)

        if len(closes) < 6:
            log.info("openbb: history(%s) returned %d bars — too few, falling back", symbol, len(closes))
            return None

        # Downsample to target_points if we have more.
        if len(closes) > target_points:
            step = (len(closes) - 1) / (target_points - 1)
            closes = [closes[round(i * step)] for i in range(target_points)]

        cmin, cmax = min(closes), max(closes)
        n = len(closes)

        # Edge case: completely flat — render a horizontal line at y=10.
        if cmin == cmax:
            return " ".join(f"{round(i * 60 / max(n - 1, 1), 1)},10" for i in range(n))

        span = cmax - cmin
        parts: list[str] = []
        for i, c in enumerate(closes):
            x = round(i * 60 / (n - 1), 1)
            # Higher price → smaller y (SVG y=0 is top, y=20 is bottom).
            y = round(2 + (cmax - c) / span * 16, 1)
            parts.append(f"{x},{y}")
        return " ".join(parts)

    # ── Fundamentals ──────────────────────────────────────────────────────

    @cached(ttl_seconds=86400)  # 24h
    def get_fundamentals(self, symbol: str, years: int = 5) -> Fundamentals:
        """Pull income / balance / cash + ratios, fuse into Fundamentals.

        Tolerant of partial failures: if income works but balance fails,
        we still return what we have. Each subcall is independently
        try/except'd. 24h cache so daily refresh is cheap.
        """
        # Get a name + currency from a single price-quote call if possible.
        name: str | None = None
        currency: str | None = None
        try:
            q_obj = obb.equity.price.quote(symbol=symbol, provider=self.default_provider)
            q_row = self._extract_row(q_obj)
            if q_row:
                name = str(q_row.get("name") or q_row.get("long_name") or symbol)
                currency = (str(q_row.get("currency") or "").upper() or None)
        except Exception as e:  # noqa: BLE001
            log.warning("openbb: quote pre-call for %s failed: %s", symbol, e)

        income_rows = self._safe_history("income",  symbol, years)
        balance_rows = self._safe_history("balance", symbol, years)
        cash_rows = self._safe_history("cash",    symbol, years)
        ratios = self._safe_ratios(symbol)

        # Index each statement by fiscal_year for fusing.
        income_by_year   = self._index_by_year(income_rows)
        balance_by_year  = self._index_by_year(balance_rows)
        cash_by_year     = self._index_by_year(cash_rows)
        all_years = sorted(
            set(income_by_year) | set(balance_by_year) | set(cash_by_year), reverse=True
        )[:years]

        year_records: list[FundamentalsYear] = []
        for fy in all_years:
            inc = income_by_year.get(fy, {})
            bal = balance_by_year.get(fy, {})
            cf  = cash_by_year.get(fy, {})
            year_records.append(FundamentalsYear(
                fiscal_year=fy,
                period_end=_first_str(inc.get("period_ending"), bal.get("period_ending"), cf.get("period_ending")),
                revenue=_first_num(inc.get("revenue"), inc.get("total_revenue")),
                gross_profit=_first_num(inc.get("gross_profit")),
                ebitda=_first_num(inc.get("ebitda")),
                ebit=_first_num(inc.get("ebit"), inc.get("operating_income")),
                net_income=_first_num(inc.get("net_income"), inc.get("consolidated_net_income")),
                capex=_first_num(cf.get("capital_expenditure"), cf.get("capital_expenditures")),
                operating_cash_flow=_first_num(cf.get("net_cash_from_operating_activities"), cf.get("operating_cash_flow")),
                free_cash_flow=_first_num(cf.get("free_cash_flow")),
                total_debt=_first_num(bal.get("total_debt"), bal.get("long_term_debt")),
                capital_lease_obligations=_first_num(
                    bal.get("capital_lease_obligations"),
                    bal.get("long_term_capital_lease_obligation"),
                ),
                cash_and_equivalents=_first_num(bal.get("cash_and_short_term_investments"), bal.get("cash_and_cash_equivalents")),
                shareholders_equity=_first_num(bal.get("total_shareholders_equity"), bal.get("total_equity")),
            ))

        # Compute revenue CAGR + EBITDA margin from the year records.
        cagr = _revenue_cagr(year_records)
        margin = _ebitda_margin_latest(year_records)
        if ratios is None:
            ratios = FundamentalsRatios()
        if cagr is not None and ratios.revenue_growth_5y_cagr is None:
            ratios.revenue_growth_5y_cagr = cagr
        if margin is not None and ratios.ebitda_margin is None:
            ratios.ebitda_margin = margin

        return Fundamentals(
            symbol=symbol,
            name=name,
            currency=currency,
            years=year_records,
            ratios=ratios,
            provider=f"openbb-{self.default_provider}",
            error=None if year_records else "No fundamentals returned by provider.",
        )

    # ── News ──────────────────────────────────────────────────────────────

    @cached(ttl_seconds=1800)  # 30 min
    def get_news(self, symbol: str, days: int = 7, limit: int = 20) -> NewsResult:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        try:
            obj = obb.news.company(symbol=symbol, limit=limit * 2, provider=self.default_provider)
            rows = self._rows(obj)
        except Exception as e:  # noqa: BLE001
            log.warning("openbb: news(%s) failed: %s", symbol, e)
            return NewsResult(symbol=symbol, items=[], provider=f"openbb-{self.default_provider}",
                              error=f"News fetch failed: {e}")

        items: list[NewsItem] = []
        for row in rows:
            published_raw = row.get("date") or row.get("published") or row.get("timestamp")
            published_dt = _parse_iso(published_raw)
            if published_dt and published_dt < cutoff:
                continue
            items.append(NewsItem(
                title=str(row.get("title") or row.get("headline") or "").strip(),
                url=str(row.get("url") or row.get("link") or "").strip(),
                published=published_dt.isoformat() if published_dt else (str(published_raw) if published_raw else None),
                source=str(row.get("source") or row.get("publisher") or "") or None,
                summary=str(row.get("text") or row.get("summary") or "")[:500] or None,
            ))
            if len(items) >= limit:
                break
        return NewsResult(symbol=symbol, items=items, provider=f"openbb-{self.default_provider}")

    # ── Peers ─────────────────────────────────────────────────────────────

    @cached(ttl_seconds=604800)  # 7 days
    def get_peers(self, symbol: str, limit: int = 10) -> PeersResult:
        """Pull the peer universe for a target ticker.

        OpenBB providers vary on what they expose here. We try the
        canonical `equity.compare.peers` first, then fall back to the
        profile.peers field on `equity.profile`.
        """
        try:
            obj = obb.equity.compare.peers(symbol=symbol, provider=self.default_provider)
            rows = self._rows(obj)
        except Exception as e:  # noqa: BLE001
            log.info("openbb: compare.peers(%s) failed (%s) — trying profile fallback", symbol, e)
            rows = []

        peers: list[PeerItem] = []
        for r in rows[:limit]:
            sym = (r.get("symbol") or r.get("peer") or "").upper()
            if not sym or sym == symbol.upper():
                continue
            peers.append(PeerItem(
                symbol=sym,
                name=r.get("name") or r.get("company_name"),
                sector=r.get("sector"),
                industry=r.get("industry"),
                country=r.get("country"),
            ))

        if not peers:
            # Fallback: try equity.profile.peers (flat list of tickers)
            try:
                obj = obb.equity.profile(symbol=symbol, provider=self.default_provider)
                prof_rows = self._rows(obj)
                if prof_rows:
                    raw = prof_rows[0].get("peers") or prof_rows[0].get("peer_companies") or []
                    if isinstance(raw, str):
                        raw = [x.strip() for x in raw.split(",") if x.strip()]
                    for sym in raw[:limit]:
                        if not sym or sym.upper() == symbol.upper():
                            continue
                        peers.append(PeerItem(symbol=sym.upper()))
            except Exception as e:  # noqa: BLE001
                log.info("openbb: profile peers fallback(%s) failed: %s", symbol, e)

        return PeersResult(
            symbol=symbol,
            peers=peers,
            provider=f"openbb-{self.default_provider}",
            error=None if peers else "No peers returned by provider.",
        )

    # ── helpers ───────────────────────────────────────────────────────────

    @staticmethod
    def _extract_row(obj: Any) -> dict[str, Any] | None:
        """OpenBB returns OBBject with a `.results` list. Take the first row.

        Tolerant of shape drift — different providers return slightly
        different field names. We hunt the common ones.
        """
        results = getattr(obj, "results", None) or []
        if not results:
            return None
        first = results[0]
        # Newer OpenBB returns Pydantic models; convert.
        if hasattr(first, "model_dump"):
            return first.model_dump()
        if isinstance(first, dict):
            return first
        # Last-ditch attribute scrape.
        return {k: getattr(first, k, None) for k in (
            "symbol", "name", "last_price", "previous_close", "change_percent", "currency",
        )}

    @staticmethod
    def _rows(obj: Any) -> list[dict[str, Any]]:
        results = getattr(obj, "results", None) or []
        out: list[dict[str, Any]] = []
        for r in results:
            if hasattr(r, "model_dump"):
                out.append(r.model_dump())
            elif isinstance(r, dict):
                out.append(r)
        return out

    def _safe_history(self, kind: str, symbol: str, limit: int) -> list[dict[str, Any]]:
        """One of {income, balance, cash} — return all rows or []."""
        try:
            method = getattr(obb.equity.fundamental, kind)
            obj = method(symbol=symbol, provider=self.default_provider, limit=limit)
            return self._rows(obj)
        except Exception as e:  # noqa: BLE001
            log.warning("openbb: %s(%s) failed: %s", kind, symbol, e)
            return []

    def _safe_ratios(self, symbol: str) -> FundamentalsRatios | None:
        try:
            obj = obb.equity.fundamental.ratios(symbol=symbol, provider=self.default_provider, limit=1)
            rows = self._rows(obj)
            if not rows:
                return None
            r = rows[0]
            return FundamentalsRatios(
                pe=_first_num(r.get("pe_ratio"), r.get("price_to_earnings")),
                ev_ebitda=_first_num(r.get("ev_to_ebitda")),
                roe=_first_num(r.get("roe"), r.get("return_on_equity")),
                roic=_first_num(r.get("roic"), r.get("return_on_invested_capital")),
                net_debt_ebitda=_first_num(r.get("net_debt_to_ebitda")),
                dividend_yield=_first_num(r.get("dividend_yield")),
            )
        except Exception as e:  # noqa: BLE001
            log.warning("openbb: ratios(%s) failed: %s", symbol, e)
            return None

    @staticmethod
    def _index_by_year(rows: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
        out: dict[int, dict[str, Any]] = {}
        for r in rows:
            fy = r.get("fiscal_year") or _year_from_period(r.get("period_ending"))
            if fy is None:
                continue
            try:
                fy_int = int(fy)
            except (TypeError, ValueError):
                continue
            out[fy_int] = r
        return out

    @staticmethod
    def _to_quote(symbol: str, row: dict[str, Any]) -> Quote:
        """Build a Quote from a yfinance row.

        % change is computed as (last - open) / open. yfinance returns
        `open` (today's opening price) and `last_price` (current); it
        does NOT expose `change_percent` directly for most equities, so
        we compute it ourselves. Falls back to previous close if `open`
        is missing.
        """
        name = str(row.get("name") or row.get("long_name") or symbol)
        # Try a handful of common field names for price.
        price_raw = (
            row.get("last_price")
            or row.get("regular_market_price")
            or row.get("price")
            or row.get("close")
            or 0.0
        )
        currency = str(row.get("currency") or "").upper() or None
        if currency == "GBP":   # yfinance reports London listings as GBp (pence)
            currency_display = "GBP"
        else:
            currency_display = currency

        # Reference price for % change: prefer today's open, fall back to
        # previous close. Either yields a sensible "today's move" number.
        ref_raw = (
            row.get("open")
            or row.get("regular_market_open")
            or row.get("prev_close")
            or row.get("previous_close")
            or row.get("regular_market_previous_close")
        )

        try:
            price = float(price_raw)
        except (TypeError, ValueError):
            price = 0.0
        try:
            ref = float(ref_raw) if ref_raw is not None else 0.0
        except (TypeError, ValueError):
            ref = 0.0

        if ref > 0 and price > 0:
            pct = (price - ref) / ref * 100
        else:
            pct = 0.0

        direction = "up" if pct > 0.05 else "down" if pct < -0.05 else "flat"
        sign = "+" if pct >= 0 else "−"   # en-dash for negative — operator convention
        change_str = f"{sign}{abs(pct):.1f}%"

        # Format price for the display. Guard non-finite / missing prices
        # first: yfinance can return a NaN last_price (or NaN latest close) for
        # an index's incomplete bar — float(NaN) succeeds, so without this the
        # value renders as "$nan" (USD indices) or crashes int(round(NaN))
        # (GBp, e.g. ^FTSE). Coerce to a clean "—" placeholder instead. Same
        # non-finite class as the sparkline drop in get_history_points (70938e0).
        if not math.isfinite(price) or price <= 0:
            price_str = "—"
        elif currency in ("GBP", "GBp"):   # UK pence (GBp) → "Xp"
            price_str = f"{int(round(price))}p"
        elif currency == "USD":
            price_str = f"${price:,.2f}"
        elif currency == "EUR":
            price_str = f"€{price:,.2f}"
        else:
            price_str = f"{price:,.2f}"

        return Quote(
            symbol=symbol,
            name=name,
            price=price_str,
            price_numeric=price if price > 0 else None,
            change=change_str,
            direction=direction,
            # Default placeholder — get_quotes() replaces this with the
            # real 3y history polyline before returning.
            points=_SEED_POINTS.get(symbol, "0,10 10,10 20,10 30,10 40,10 50,10 60,10"),
            currency=currency_display,
            provider="openbb-yfinance",
        )


# ── module-level helpers ─────────────────────────────────────────────────


def _finite_positive(v: Any) -> bool:
    """True iff ``v`` is a finite number strictly greater than zero.

    Decides whether a quote's ``last_price`` is usable or whether to fall back
    to the latest historical close. Treats None / 0 / non-numeric / NaN / inf as
    'not usable' — note a NaN is truthy, so the old bare ``not last_price`` check
    wrongly accepted it. Mirrors the ``price <= 0`` guard in ``_to_quote``: a
    usable display price is finite and positive."""
    if v is None:
        return False
    try:
        f = float(v)
    except (TypeError, ValueError):
        return False
    return math.isfinite(f) and f > 0


def _first_num(*vs: Any) -> float | None:
    for v in vs:
        if v is None:
            continue
        try:
            f = float(v)
            if f != 0.0 or vs == (0.0,):  # explicit 0 OK if the only value
                return f
            return f
        except (TypeError, ValueError):
            continue
    return None


def _first_str(*vs: Any) -> str | None:
    for v in vs:
        if v is None:
            continue
        s = str(v).strip()
        if s:
            return s
    return None


def _parse_iso(s: Any) -> datetime | None:
    if not s:
        return None
    if isinstance(s, datetime):
        return s if s.tzinfo else s.replace(tzinfo=timezone.utc)
    try:
        if isinstance(s, str) and s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(str(s))
    except ValueError:
        return None


def _year_from_period(period: Any) -> int | None:
    if not period:
        return None
    try:
        return datetime.fromisoformat(str(period).replace("Z", "+00:00")).year
    except ValueError:
        return None


def _revenue_cagr(years: list[FundamentalsYear]) -> float | None:
    """Simple 5y revenue CAGR from oldest to newest available data point."""
    revs = [(y.fiscal_year, y.revenue) for y in years if y.revenue]
    if len(revs) < 2:
        return None
    revs.sort(key=lambda x: x[0])
    first_year, first_rev = revs[0]
    last_year, last_rev = revs[-1]
    n = last_year - first_year
    if n <= 0 or first_rev <= 0:
        return None
    return (last_rev / first_rev) ** (1 / n) - 1


def _ebitda_margin_latest(years: list[FundamentalsYear]) -> float | None:
    for y in years:  # already sorted desc
        if y.revenue and y.ebitda and y.revenue > 0:
            return y.ebitda / y.revenue
    return None


# Sparkline polylines are 5-day end-of-day trends; OpenBB historical can
# fill these later. For Phase 1 we reuse the seed shapes so the dashboard
# still has a chart for each symbol.
_SEED_POINTS = {
    "JDW.L":  "0,15 6,14 12,12 18,11 24,9 30,8 36,7 42,5 48,4 54,3 60,2",
    "IHG.L":  "0,16 6,14 12,12 18,11 24,9 30,8 36,6 42,7 48,5 54,4 60,2",
    "WTB.L":  "0,4 6,6 12,5 18,8 24,7 30,10 36,9 42,12 48,11 54,14 60,13",
    "MAB.L":  "0,12 6,11 12,9 18,10 24,7 30,8 36,5 42,6 48,4 54,3 60,5",
    "BOWL.L": "0,5 6,7 12,8 18,10 24,9 30,12 36,11 42,13 48,14 54,15 60,16",
    "SSPG.L": "0,15 6,13 12,12 18,11 24,12 30,9 36,10 42,8 48,7 54,6 60,7",
}
