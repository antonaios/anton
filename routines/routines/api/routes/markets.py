"""Endpoints for the markets adapter."""

from __future__ import annotations

import logging
import re

from fastapi import APIRouter, HTTPException, Query

from pydantic import BaseModel, Field

from routines.hooks import tool_call_hooks
from routines.markets import get_provider
from routines.markets.comps import build_comps
from routines.markets.equity_research import build_equity_research
from routines.markets.macro import build_macro_row
from routines.markets.types import (
    CompsResult, EquityResearchResult, Fundamentals, MacroBarResponse,
    NewsResult, PeersResult, Quote,
)
from routines.shared.ticker_config import (
    load_default as load_ticker_bar,
    load_macro_default as load_macro_bar,
)

router = APIRouter()
log = logging.getLogger(__name__)


# ── Public-identifier validation ──────────────────────────────────────────
# Accepts: JDW.L, IHG.L, ^FTSE, AAPL, BRK.B, MSFT, BP-A.L, ITV-A.L
# Rejects: anything containing spaces, project codenames, target/buyer
# names, or other free-form text. Defence in depth — OpenBBProvider
# ultimately passes to external providers, so we can't be careful enough.
_TICKER_PATTERN = re.compile(r"^[A-Z0-9.^=-]{1,12}$")


def _validate_single_symbol(raw: str) -> str:
    """Single-symbol variant for fundamentals/news; mirrors _validate_symbols."""
    s = (raw or "").strip().upper()
    if not s:
        raise HTTPException(status_code=400, detail="symbol query param required")
    if not _TICKER_PATTERN.fullmatch(s):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Symbol rejected as non-public identifier: {s!r}. "
                "Markets API accepts only public tickers (e.g. JDW.L, IHG.L, ^FTSE). "
                "Never pass deal codenames, target names, or buyer names."
            ),
        )
    return s


class HealthResponse:
    pass


class QuotesResponse(dict):
    """Just for OpenAPI docs — the real shape is set by FastAPI."""


def _validate_symbols(raw: str) -> list[str]:
    if not raw:
        raise HTTPException(status_code=400, detail="symbols query param required")
    symbols = [s.strip().upper() for s in raw.split(",") if s.strip()]
    if not symbols:
        raise HTTPException(status_code=400, detail="no symbols provided")
    bad = [s for s in symbols if not _TICKER_PATTERN.fullmatch(s)]
    if bad:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Symbol(s) rejected as non-public identifier: {bad!r}. "
                "Markets API accepts only public tickers (e.g. JDW.L, IHG.L, ^FTSE). "
                "Never pass deal codenames, target names, or buyer names."
            ),
        )
    if len(symbols) > 20:
        raise HTTPException(status_code=400, detail="max 20 symbols per call")
    return symbols


@router.get("/markets/quotes")
def quotes(
    symbols: str = Query(..., description="Comma-separated tickers, e.g. JDW.L,IHG.L,WTB.L"),
) -> dict:
    parsed = _validate_symbols(symbols)
    provider = get_provider()
    try:
        rows: list[Quote] = provider.get_quotes(parsed)
    except Exception as e:  # noqa: BLE001
        log.exception("markets: get_quotes failed")
        raise HTTPException(status_code=500, detail=f"Markets provider failed: {e}") from e
    return {
        "provider": provider.name,
        "requested": parsed,
        "quotes": [q.model_dump() for q in rows],
    }


@router.get("/markets/health")
def markets_health() -> dict:
    provider = get_provider()
    return {"status": "ok", "provider": provider.name}


class TickerBarEntry(BaseModel):
    symbol: str
    name: str


class TickerBarResponse(BaseModel):
    tickers: list[TickerBarEntry]
    source: str   # "config" if read from _claude/tickers.yaml, "fallback" otherwise


@router.get("/markets/ticker-bar", response_model=TickerBarResponse)
def ticker_bar() -> TickerBarResponse:
    """Return the operator-configured equity ticker list for the SparkTicker.

    Reads `_claude/tickers.md` (or legacy `tickers.yaml`) on each call —
    tiny file, no need to cache. Edit the markdown frontmatter to change
    the dashboard ticker; refresh the page.

    Public-ticker validation runs on every entry (defence in depth — even
    operator-edited config can't slip a deal codename through).
    """
    from routines.shared.ticker_config import config_path
    from routines.api.deps import VAULT

    entries = load_ticker_bar()
    source = "config" if config_path(VAULT).exists() else "fallback"
    return TickerBarResponse(
        tickers=[TickerBarEntry(symbol=e.symbol, name=e.name) for e in entries],
        source=source,
    )


@router.get("/markets/macro-bar", response_model=MacroBarResponse)
def macro_bar() -> MacroBarResponse:
    """Return the operator-configured macro/index/commodity/rate row.

    Each entry hits a kind-appropriate OpenBB endpoint (equity.price.quote
    for indices and futures, fixedincome.government.treasury_rates for
    UK gilt rates, economy.cpi for the inflation indicator). Per-row
    failures degrade gracefully — the bar still renders with placeholder
    cells rather than dropping entries.
    """
    from routines.shared.ticker_config import config_path
    from routines.api.deps import VAULT

    entries = load_macro_bar()
    source = "config" if config_path(VAULT).exists() else "fallback"
    rows = [
        build_macro_row(symbol=e.symbol, kind=e.kind, name=e.name)
        for e in entries
    ]
    return MacroBarResponse(rows=rows, source=source)


@router.get("/markets/fundamentals", response_model=Fundamentals)
def fundamentals(
    symbol: str = Query(..., description="Public ticker, e.g. JDW.L"),
    years: int = Query(5, ge=1, le=10, description="Number of fiscal years to fetch"),
) -> Fundamentals:
    sym = _validate_single_symbol(symbol)
    provider = get_provider()
    try:
        return provider.get_fundamentals(sym, years=years)
    except Exception as e:  # noqa: BLE001
        log.exception("markets: fundamentals failed")
        raise HTTPException(status_code=500, detail=f"Fundamentals failed: {e}") from e


@router.get("/markets/news", response_model=NewsResult)
def news(
    symbol: str = Query(..., description="Public ticker, e.g. JDW.L"),
    days: int = Query(7, ge=1, le=90, description="Look-back window in days"),
    limit: int = Query(20, ge=1, le=100, description="Max number of items"),
) -> NewsResult:
    sym = _validate_single_symbol(symbol)
    provider = get_provider()
    try:
        return provider.get_news(sym, days=days, limit=limit)
    except Exception as e:  # noqa: BLE001
        log.exception("markets: news failed")
        raise HTTPException(status_code=500, detail=f"News failed: {e}") from e


@router.get("/markets/peers", response_model=PeersResult)
def peers(
    symbol: str = Query(..., description="Public ticker, e.g. JDW.L"),
    limit: int = Query(10, ge=1, le=25, description="Max peers to return"),
) -> PeersResult:
    sym = _validate_single_symbol(symbol)
    provider = get_provider()
    try:
        return provider.get_peers(sym, limit=limit)
    except Exception as e:  # noqa: BLE001
        log.exception("markets: peers failed")
        raise HTTPException(status_code=500, detail=f"Peers failed: {e}") from e


# ── Comps workflow ────────────────────────────────────────────────────────


class CompsRequest(BaseModel):
    symbol: str = Field(..., description="Target ticker, e.g. WTB.L")
    peers_limit: int = Field(8, ge=1, le=20)
    years: int = Field(5, ge=1, le=10)
    write_note: bool = Field(False, description="Append a table to Companies/<X>.md")


@router.post("/workflows/comps", response_model=CompsResult)
def workflow_comps(req: CompsRequest) -> CompsResult:
    """Comps-pull workflow: target + peers + fundamentals → CompsResult.

    Optionally writes a markdown table to Companies/<X>.md in the vault.
    Sensitivity-bounded by the ticker validator — only public identifiers
    are accepted.
    """
    sym = _validate_single_symbol(req.symbol)
    with tool_call_hooks(
        tool_name="comps_pull",
        sensitivity="public",
        tool_input=req.model_dump(),
    ) as ctx:
        try:
            result = build_comps(
                sym,
                peers_limit=req.peers_limit,
                years=req.years,
                write_note=req.write_note,
            )
        except Exception as e:  # noqa: BLE001
            log.exception("workflows: comps failed")
            raise HTTPException(status_code=500, detail=f"Comps workflow failed: {e}") from e
        ctx.result = result.model_dump()
        return result


class EquityResearchRequest(BaseModel):
    symbol: str = Field(..., description="Public ticker, e.g. WTB.L")
    years: int = Field(5, ge=1, le=10)
    peers_limit: int = Field(6, ge=0, le=15)
    news_days: int = Field(14, ge=1, le=90)
    news_limit: int = Field(12, ge=1, le=50)
    write_note: bool = Field(True)


@router.post("/workflows/equity-research", response_model=EquityResearchResult)
def workflow_equity_research(req: EquityResearchRequest) -> EquityResearchResult:
    """Full equity-research pull: snapshot + 5y financials + comps + news.

    Deterministic data assembly only — no LLM. Writes a structured
    section to Companies/<safename>.md (append-only) with analyst-
    commentary slots left blank for the operator to fill.
    """
    sym = _validate_single_symbol(req.symbol)
    with tool_call_hooks(
        tool_name="equity_research",
        sensitivity="public",
        tool_input=req.model_dump(),
    ) as ctx:
        try:
            result = build_equity_research(
                sym,
                years=req.years,
                peers_limit=req.peers_limit,
                news_days=req.news_days,
                news_limit=req.news_limit,
                write_note=req.write_note,
            )
        except Exception as e:  # noqa: BLE001
            log.exception("workflows: equity-research failed")
            raise HTTPException(status_code=500, detail=f"Equity research failed: {e}") from e
        ctx.result = result.model_dump()
        return result
