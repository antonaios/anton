"""Stub provider — deterministic seed quotes for offline / unlicensed use.

Mirrors the data in dashboard/src/data/seed.ts so the SparkTicker shows
the same values whether or not OpenBB is installed.
"""

from __future__ import annotations

from routines.markets.types import (
    Fundamentals, FundamentalsYear, FundamentalsRatios,
    NewsResult, NewsItem, PeerItem, PeersResult, Quote,
)

# Seed data — kept in sync with dashboard/src/data/seed.ts SECTOR_COMPS.
_SEED: list[Quote] = [
    Quote(symbol="JDW.L",  name="JD Wetherspoon",       price="604p",   change="+0.4%", direction="up",
          points="0,15 6,14 12,12 18,11 24,9 30,8 36,7 42,5 48,4 54,3 60,2",
          currency="GBP", provider="stub"),
    Quote(symbol="IHG.L",  name="IHG",                  price="7,420p", change="+1.2%", direction="up",
          points="0,16 6,14 12,12 18,11 24,9 30,8 36,6 42,7 48,5 54,4 60,2",
          currency="GBP", provider="stub"),
    Quote(symbol="WTB.L",  name="Whitbread",            price="2,940p", change="−0.4%", direction="down",
          points="0,4 6,6 12,5 18,8 24,7 30,10 36,9 42,12 48,11 54,14 60,13",
          currency="GBP", provider="stub"),
    Quote(symbol="MAB.L",  name="Mitchells & Butlers",  price="285p",   change="+0.8%", direction="up",
          points="0,12 6,11 12,9 18,10 24,7 30,8 36,5 42,6 48,4 54,3 60,5",
          currency="GBP", provider="stub"),
    Quote(symbol="BOWL.L", name="Hollywood Bowl",       price="312p",   change="−1.1%", direction="down",
          points="0,5 6,7 12,8 18,10 24,9 30,12 36,11 42,13 48,14 54,15 60,16",
          currency="GBP", provider="stub"),
    Quote(symbol="SSPG.L", name="SSP Group",            price="157p",   change="+1.3%", direction="up",
          points="0,15 6,13 12,12 18,11 24,12 30,9 36,10 42,8 48,7 54,6 60,7",
          currency="GBP", provider="stub"),
]

_BY_SYMBOL = {q.symbol: q for q in _SEED}


class StubProvider:
    name = "stub"

    def get_quotes(self, symbols: list[str]) -> list[Quote]:
        out: list[Quote] = []
        for s in symbols:
            if s in _BY_SYMBOL:
                out.append(_BY_SYMBOL[s])
        return out

    def get_fundamentals(self, symbol: str, years: int = 5) -> Fundamentals:
        """Stub returns an empty Fundamentals with an explanatory `error`.

        The bridge route surfaces this as a 200 with `error` set so the
        dashboard can render a "stub provider — install OpenBB" banner
        rather than a generic failure.
        """
        return Fundamentals(
            symbol=symbol,
            name=_BY_SYMBOL.get(symbol).name if symbol in _BY_SYMBOL else None,
            currency="GBP" if symbol.endswith(".L") else None,
            years=[],
            ratios=FundamentalsRatios(),
            provider="stub",
            error=(
                "Fundamentals not available on the stub provider. "
                "Install OpenBB (`pip install -e .[markets]`) to enable."
            ),
        )

    def get_news(self, symbol: str, days: int = 7, limit: int = 20) -> NewsResult:
        return NewsResult(
            symbol=symbol,
            items=[],
            provider="stub",
            error=(
                "News not available on the stub provider. "
                "Install OpenBB (`pip install -e .[markets]`) to enable."
            ),
        )

    def get_peers(self, symbol: str, limit: int = 10) -> PeersResult:
        """Seed-static peer set for the T&L sector. The same six tickers
        we ship in SECTOR_COMPS — minus the target itself, capped at limit.
        """
        siblings = [s for s in _BY_SYMBOL if s != symbol][:limit]
        peers = [
            PeerItem(
                symbol=s,
                name=_BY_SYMBOL[s].name,
                sector="Consumer Discretionary",
                industry="Travel & Leisure",
                country="GB" if s.endswith(".L") else None,
            )
            for s in siblings
        ]
        return PeersResult(
            symbol=symbol,
            peers=peers,
            provider="stub",
        )


# Suppress noisy hint when the year/limit args aren't used in the stub.
_ = FundamentalsYear, NewsItem
