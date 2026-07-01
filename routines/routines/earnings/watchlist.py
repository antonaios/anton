"""Load the operator-editable earnings watchlist from
`_claude/earnings-watchlist.md`.

Same markdown-frontmatter pattern as `tickers.md` — operator edits the
list in Obsidian, the routine picks it up on the next run. No restart.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from routines.shared.md_config import extract_section

log = logging.getLogger(__name__)


_TICKER_PATTERN = re.compile(r"^[A-Z0-9.^=-]{1,12}$")

MAX_WATCHLIST = 30

_FALLBACK = [
    {"symbol": "JDW.L",  "name": "J D Wetherspoon"},
    {"symbol": "IHG.L",  "name": "InterContinental Hotels"},
    {"symbol": "WTB.L",  "name": "Whitbread"},
    {"symbol": "MAB.L",  "name": "Mitchells & Butlers"},
    {"symbol": "BOWL.L", "name": "Hollywood Bowl"},
    {"symbol": "SSPG.L", "name": "SSP Group"},
]


@dataclass
class WatchlistEntry:
    symbol: str
    name: str


def watchlist_path(vault_root: Path) -> Path:
    return vault_root / "_claude" / "earnings-watchlist.md"


def load(vault_root: Path) -> list[WatchlistEntry]:
    """Read `_claude/earnings-watchlist.md` frontmatter; fall back to
    the hardcoded default if the file is missing / unparseable / empty.
    """
    path = watchlist_path(vault_root)
    if not path.exists():
        log.info("earnings watchlist: %s not found — using fallback", path)
        return [WatchlistEntry(**r) for r in _FALLBACK]

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        log.warning("earnings watchlist: %s read failed (%s) — using fallback", path, e)
        return [WatchlistEntry(**r) for r in _FALLBACK]

    raw_list = extract_section(text, "earnings_watchlist")
    if not isinstance(raw_list, list) or not raw_list:
        log.warning("earnings watchlist: earnings_watchlist section missing or empty — using fallback")
        return [WatchlistEntry(**r) for r in _FALLBACK]

    out: list[WatchlistEntry] = []
    for row in raw_list[:MAX_WATCHLIST]:
        if not isinstance(row, dict):
            continue
        sym = str(row.get("symbol", "")).strip().upper()
        name = str(row.get("name", "")).strip() or sym
        if not sym:
            continue
        if not _TICKER_PATTERN.fullmatch(sym):
            log.warning("earnings watchlist: rejecting non-public-shaped symbol %r", sym)
            continue
        out.append(WatchlistEntry(symbol=sym, name=name))

    if not out:
        log.warning("earnings watchlist: all rows rejected — using fallback")
        return [WatchlistEntry(**r) for r in _FALLBACK]
    return out
