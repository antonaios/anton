"""Markets adapter — quotes, fundamentals, peers, news.

This package is the **only** place the OpenBB SDK is imported. Everything
else in the routines layer reaches the data via the `MarketsProvider`
protocol in `adapter.py`, so the OpenBB-AGPLv3 surface is contained here.

Install OpenBB to switch from stub to live data:

    pip install -e ".[markets]"   # from the routines repo

Without it, the StubProvider returns deterministic seed data so the
dashboard ticker remains populated.

Sensitivity wrap (NON-NEGOTIABLE):
    Public identifiers only. NEVER deal codenames, target names from
    confidential mandates, or buyer names from active sell-sides. The
    bridge route in `routes/markets.py` enforces validation.
"""

from routines.markets.adapter import get_provider

__all__ = ["get_provider"]
