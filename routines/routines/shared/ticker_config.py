"""Read operator-editable dashboard config from `_claude/tickers.md`.

The dashboard's two ticker bars (equity SparkTicker + macro/index bar)
are both driven by this file. Edit the markdown frontmatter, save, and
the next dashboard refresh picks up the change — no rebuild, no restart.

File format: markdown with YAML frontmatter, so Obsidian renders the
body as documentation and the frontmatter is data:

    ---
    type: dashboard-config
    sensitivity: internal
    ticker_bar:
      - { symbol: JDW.L, name: J D Wetherspoon }
      ...
    macro_bar:
      - { symbol: ^FTSE, name: FTSE 100, kind: index }
      ...
    ---

    # Dashboard tickers
    ...explanatory body...

Backward-compat: also accepts the older `_claude/tickers.yaml` (pure
YAML, no markdown wrapper) — falls through to it if the `.md` version
doesn't exist.

Validation is defensive: bad YAML / missing file / malformed list →
hardcoded fallback. Every symbol passes the public-ticker regex (same
rule the markets route uses) except for the rate/indicator synthetic
identifiers like `UK_10Y` which are explicitly whitelisted.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from routines.shared.md_config import extract_section

log = logging.getLogger(__name__)


# Standard public-ticker regex. Same as routines/api/routes/markets.py.
_TICKER_PATTERN = re.compile(r"^[A-Z0-9.^=-]{1,12}$")

# Synthetic identifiers we accept for rate/indicator kinds (don't look like
# public tickers but the bridge route never forwards them to yfinance — the
# macro builder routes them to OpenBB economy / fixedincome endpoints).
_SYNTHETIC_SYMBOLS = {
    "UK_3M",      # 3-month gilt yield (used as SONIA proxy without FRED key)
    "UK_10Y",     # 10-year gilt yield
    "UK_SONIA",   # overnight SONIA (FRED-backed; needs FRED_API_KEY)
    "UK_CPI",     # UK CPI YoY, monthly
}

MAX_TICKERS_PER_BAR = 10


# ── Fallback defaults ────────────────────────────────────────────────────


_FALLBACK_TICKER_BAR: list[dict[str, str]] = [
    {"symbol": "JDW.L",  "name": "J D Wetherspoon"},
    {"symbol": "IHG.L",  "name": "InterContinental"},
    {"symbol": "WTB.L",  "name": "Whitbread"},
    {"symbol": "MAB.L",  "name": "Mitchells & Butlers"},
    {"symbol": "BOWL.L", "name": "Hollywood Bowl"},
    {"symbol": "SSPG.L", "name": "SSP Group"},
]

_FALLBACK_MACRO_BAR: list[dict[str, str]] = [
    {"symbol": "BZ=F",   "name": "Brent",       "kind": "commodity"},
    {"symbol": "GC=F",   "name": "Gold",        "kind": "commodity"},
    {"symbol": "^FTSE",  "name": "FTSE 100",    "kind": "index"},
    {"symbol": "^GSPC",  "name": "S&P 500",     "kind": "index"},
    {"symbol": "^NDX",   "name": "Nasdaq 100",  "kind": "index"},
    {"symbol": "^DJI",   "name": "Dow Jones",   "kind": "index"},
    {"symbol": "UK_10Y", "name": "UK 10Y",      "kind": "rate"},
    {"symbol": "UK_CPI", "name": "UK CPI",      "kind": "indicator"},
]


# Back-compat alias.
_FALLBACK = _FALLBACK_TICKER_BAR


@dataclass
class TickerEntry:
    symbol: str
    name: str


@dataclass
class MacroEntry:
    symbol: str
    name: str
    kind: str           # 'equity' | 'index' | 'commodity' | 'rate' | 'indicator'


# ── File location ────────────────────────────────────────────────────────


def config_path(vault_root: Path) -> Path:
    """Path to the preferred config file. Returns the .md version even
    if it doesn't yet exist (so the bridge logs the right location)."""
    return vault_root / "_claude" / "tickers.md"


def _load_file_text(vault_root: Path) -> str | None:
    """Read tickers.md (or legacy tickers.yaml) as text. Returns None on
    failure or if neither exists."""
    md_path = vault_root / "_claude" / "tickers.md"
    yaml_path = vault_root / "_claude" / "tickers.yaml"
    for p in (md_path, yaml_path):
        if p.exists():
            try:
                return p.read_text(encoding="utf-8")
            except OSError as e:
                log.warning("ticker_config: read %s failed (%s)", p, e)
    return None


def _extract_list(vault_root: Path, section: str) -> list[Any] | None:
    """Try to extract a YAML list under `## <section>` (body code block)
    or `<section>:` (frontmatter back-compat). Returns the parsed list
    or None if not found / invalid."""
    text = _load_file_text(vault_root)
    if text is None:
        return None
    return extract_section(text, section)


# ── Equity ticker bar (the original `load` function) ─────────────────────


def load(vault_root: Path) -> list[TickerEntry]:
    """Read the `ticker_bar` list from the operator config.

    Falls back to the hardcoded default if the file is missing, the YAML
    is bad, or all entries are rejected by the validator.
    """
    raw_list = _extract_list(vault_root, "ticker_bar")
    if not isinstance(raw_list, list) or not raw_list:
        log.info("ticker_config: ticker_bar not found / empty — using fallback")
        return [TickerEntry(**r) for r in _FALLBACK_TICKER_BAR]

    out: list[TickerEntry] = []
    for row in raw_list[:MAX_TICKERS_PER_BAR]:
        if not isinstance(row, dict):
            continue
        sym = str(row.get("symbol", "")).strip().upper()
        name = str(row.get("name", "")).strip()
        if not sym:
            continue
        if not _TICKER_PATTERN.fullmatch(sym):
            log.warning(
                "ticker_config: rejecting non-public-shaped symbol %r — "
                "must match %s",
                sym, _TICKER_PATTERN.pattern,
            )
            continue
        if not name:
            name = sym
        if len(name) > 24:
            name = name[:22] + "…"
        out.append(TickerEntry(symbol=sym, name=name))

    if not out:
        log.warning("ticker_config: all ticker_bar rows rejected — using fallback")
        return [TickerEntry(**r) for r in _FALLBACK_TICKER_BAR]
    return out


# ── Macro bar (new) ──────────────────────────────────────────────────────


_VALID_MACRO_KINDS = {"equity", "index", "commodity", "rate", "indicator"}


def load_macro(vault_root: Path) -> list[MacroEntry]:
    """Read the `macro_bar` list from the operator config.

    Each entry needs `symbol`, `name`, and `kind`. `kind` must be one
    of equity / index / commodity / rate / indicator.

    For rate/indicator kinds the symbol must be in the synthetic
    allowlist (UK_3M, UK_10Y, UK_CPI) — these route to OpenBB economy /
    fixedincome endpoints. For the market kinds (equity, index,
    commodity) the symbol must pass the standard public-ticker regex.
    """
    raw_list = _extract_list(vault_root, "macro_bar")
    if not isinstance(raw_list, list) or not raw_list:
        log.info("ticker_config: macro_bar not found / empty — using fallback")
        return [MacroEntry(**r) for r in _FALLBACK_MACRO_BAR]

    out: list[MacroEntry] = []
    for row in raw_list[:MAX_TICKERS_PER_BAR]:
        if not isinstance(row, dict):
            continue
        sym = str(row.get("symbol", "")).strip().upper()
        name = str(row.get("name", "")).strip()
        kind = str(row.get("kind", "")).strip().lower()
        if not sym or not kind:
            continue
        if kind not in _VALID_MACRO_KINDS:
            log.warning("ticker_config: macro kind %r not in %s — skipping %s",
                        kind, sorted(_VALID_MACRO_KINDS), sym)
            continue
        # Validate the symbol against kind-appropriate rules.
        if kind in ("rate", "indicator"):
            if sym not in _SYNTHETIC_SYMBOLS:
                log.warning("ticker_config: %s kind=%s but symbol not in synthetic allowlist %s",
                            sym, kind, sorted(_SYNTHETIC_SYMBOLS))
                continue
        else:
            if not _TICKER_PATTERN.fullmatch(sym):
                log.warning("ticker_config: macro %s kind=%s — symbol rejected by ticker regex",
                            sym, kind)
                continue
        if not name:
            name = sym
        if len(name) > 24:
            name = name[:22] + "…"
        out.append(MacroEntry(symbol=sym, name=name, kind=kind))

    if not out:
        log.warning("ticker_config: all macro_bar rows rejected — using fallback")
        return [MacroEntry(**r) for r in _FALLBACK_MACRO_BAR]
    return out


# ── Convenience: load from the bridge's vault path ───────────────────────


def load_default() -> list[TickerEntry]:
    from routines.api.deps import VAULT
    return load(VAULT)


def load_macro_default() -> list[MacroEntry]:
    from routines.api.deps import VAULT
    return load_macro(VAULT)
