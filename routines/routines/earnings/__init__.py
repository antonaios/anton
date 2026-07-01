"""Earnings tracker — pulls quarterly results for watchlist tickers via
OpenBB, appends to an Excel workbook with idempotency by (ticker, period).

Sibling of routines.dealtracker. Reuses the markets adapter so no extra
data-provider plumbing is required.
"""

from routines.earnings.schema import EarningsRecord, COLUMNS
from routines.earnings.workbook import append_earnings
from routines.earnings.pull import pull_earnings

__all__ = ["EarningsRecord", "COLUMNS", "append_earnings", "pull_earnings"]
