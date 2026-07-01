"""Provider protocol + factory.

The protocol is import-free of OpenBB. The factory picks at runtime:
  - OpenBB installed → OpenBBProvider
  - otherwise        → StubProvider (deterministic seed data)

This is what keeps the AGPLv3 surface narrow: nothing outside this package
imports OpenBB, and `OpenBBProvider` is only loaded if openbb is present.
"""

from __future__ import annotations

import logging
from typing import Protocol

from routines.markets.types import Fundamentals, NewsResult, PeersResult, Quote

log = logging.getLogger(__name__)


class MarketsProvider(Protocol):
    """Implementations: StubProvider, OpenBBProvider, (later) others."""

    name: str

    def get_quotes(self, symbols: list[str]) -> list[Quote]: ...

    def get_fundamentals(self, symbol: str, years: int = 5) -> Fundamentals: ...

    def get_news(self, symbol: str, days: int = 7, limit: int = 20) -> NewsResult: ...

    def get_peers(self, symbol: str, limit: int = 10) -> PeersResult: ...


def get_provider() -> MarketsProvider:
    """Lazy provider resolution. Tries OpenBB first; falls back to stub.

    Set `MARKETS_FORCE_STUB=1` to skip OpenBB even when installed (useful
    for tests / offline / when external rate limits are biting)."""
    import os

    if os.environ.get("MARKETS_FORCE_STUB"):
        log.info("markets: MARKETS_FORCE_STUB set → using StubProvider")
        return _stub()

    try:
        # Import is lazy + scoped — keeps AGPLv3 surface contained.
        from routines.markets.openbb_provider import OpenBBProvider  # noqa: WPS433

        return OpenBBProvider()
    except ImportError as e:
        log.info(
            "markets: OpenBB not installed (%s) → using StubProvider. "
            "Install with `pip install -e .[markets]` to switch to live data.",
            e,
        )
        return _stub()


def _stub() -> MarketsProvider:
    from routines.markets.stub_provider import StubProvider
    return StubProvider()
