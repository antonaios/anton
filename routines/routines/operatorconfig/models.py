"""Pydantic section models for the operator-config surface.

These are the STRICT validators applied on PUT â€” a row that fails here
never reaches the vault file. The READ side (store.read_config) is
deliberately lenient instead: it surfaces malformed rows with per-row
issues so the operator can fix them from the tab.

Validation rules mirror the live consumers (banner rules from
``routines.shared.ticker_config``; watchlist cap from
``routines.earnings.watchlist``) so a save that passes here can never be
rejected by the bar/tracker that later reads the file.
"""

from __future__ import annotations

import ipaddress
import socket
from typing import Literal, Optional
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Single source for the symbol rules â€” same objects the live banner
# loader applies (private there by convention, shared here on purpose).
from routines.shared.ticker_config import (  # noqa: PLC2701
    MAX_TICKERS_PER_BAR,
    _SYNTHETIC_SYMBOLS,
    _TICKER_PATTERN,
)

WRITABLE_SECTIONS = ("banners", "watchlist", "coverage", "sectors", "profile")

_MAX_NAME_LEN = 24          # ticker_config truncates beyond this â€” we reject instead
_MAX_WATCHLIST = 30         # earnings watchlist silently truncates beyond 30


def _clean_symbol(v: str) -> str:
    sym = str(v).strip().upper()
    if not sym:
        raise ValueError("symbol is required")
    if not _TICKER_PATTERN.fullmatch(sym):
        raise ValueError(
            f"{sym!r} is not a public-ticker-shaped symbol "
            "(A-Z 0-9 . ^ = - only, max 12 chars). Never a deal codename."
        )
    return sym


def _ip_literal(host: str):
    """Return an ``ipaddress`` object if ``host`` is ANY IP-literal form an HTTP
    stack might dial -- canonical IPv4/IPv6, OR a legacy/obfuscated IPv4 form
    (decimal-integer ``2130706433``, hex ``0x7f000001``, octal ``0177.0.0.1``,
    abbreviated ``127.1``) that ``ipaddress`` rejects but C-library
    ``inet_aton`` (and many HTTP clients) accept and normalise to a real IP.
    Returns None for a genuine hostname (DNS resolution is intentionally out of
    scope). Used to refuse non-public coverage source URLs (#sec-loopback-proxy-headers).
    """
    if not host:
        return None
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        pass
    try:
        packed = socket.inet_aton(host)
    except OSError:
        return None
    return ipaddress.ip_address(socket.inet_ntoa(packed))


class TickerRow(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    symbol: str
    name: str = ""

    @field_validator("symbol")
    @classmethod
    def _symbol(cls, v: str) -> str:
        return _clean_symbol(v)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        name = str(v).strip()
        if len(name) > _MAX_NAME_LEN:
            raise ValueError(
                f"name {name!r} is longer than {_MAX_NAME_LEN} chars "
                "(the bar would truncate it â€” shorten it here instead)"
            )
        return name

    @model_validator(mode="after")
    def _default_name(self) -> "TickerRow":
        if not self.name:
            self.name = self.symbol
        return self


class MacroRow(TickerRow):
    kind: Literal["equity", "index", "commodity", "rate", "indicator"]

    @model_validator(mode="after")
    def _kind_rules(self) -> "MacroRow":
        if self.kind in ("rate", "indicator"):
            if self.symbol not in _SYNTHETIC_SYMBOLS:
                raise ValueError(
                    f"kind={self.kind} requires a synthetic symbol from "
                    f"{sorted(_SYNTHETIC_SYMBOLS)}, got {self.symbol!r}"
                )
        return self


class BannersData(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    ticker_bar: list[TickerRow] = Field(..., max_length=MAX_TICKERS_PER_BAR)
    macro_bar: list[MacroRow] = Field(..., max_length=MAX_TICKERS_PER_BAR)


class WatchlistRow(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    symbol: str
    name: str = ""

    @field_validator("symbol")
    @classmethod
    def _symbol(cls, v: str) -> str:
        return _clean_symbol(v)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        return str(v).strip()


class WatchlistData(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    earnings_watchlist: list[WatchlistRow] = Field(..., max_length=_MAX_WATCHLIST)


class CoverageRow(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str = Field(..., min_length=1, max_length=60)
    sector: Optional[str] = None
    sources: list[str] = Field(default_factory=list)
    query: Optional[str] = None
    enabled: bool = True

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        name = str(v).strip()
        if not name:
            raise ValueError("coverage row needs a name")
        return name

    @field_validator("sector", "query")
    @classmethod
    def _optional_str(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        s = str(v).strip()
        return s or None

    @field_validator("sources")
    @classmethod
    def _sources(cls, v: list[str]) -> list[str]:
        out: list[str] = []
        for url in v:
            u = str(url).strip()
            if not u:
                continue
            if not u.startswith(("http://", "https://")):
                raise ValueError(f"source {u!r} must be an http(s) URL")
            # Reject internal / non-public hosts before the URL is handed to the
            # sector-news SaaS fetchers (Tavily/Firecrawl). _ip_literal also
            # canonicalises legacy/obfuscated IPv4 forms (decimal/hex/octal/
            # abbreviated) that some fetch stacks normalise to internal IPs;
            # ``not is_global`` covers loopback/private/link-local/reserved/CGNAT/
            # metadata/unspecified/multicast in one posture. A hostname that only
            # resolves internally via DNS is out of scope (no DNS in a validator).
            host = (urlparse(u).hostname or "").strip()
            ip = _ip_literal(host)
            if ip is not None:
                # Evaluate an IPv4-mapped IPv6 literal (::ffff:a.b.c.d) on its
                # mapped v4. ``not is_global`` covers loopback/private/link-local/
                # reserved/CGNAT/unspecified -- but NOT multicast (224.0.0.0/4 and
                # ff00::/8 are global-but-not-unicast), so reject that explicitly.
                effective = getattr(ip, "ipv4_mapped", None) or ip
                if not effective.is_global or effective.is_multicast:
                    raise ValueError(
                        f"source {u!r} points at a non-public address - refused"
                    )
            out.append(u)
        return out


class CoverageData(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    coverage: list[CoverageRow]

    @model_validator(mode="after")
    def _unique_names(self) -> "CoverageData":
        seen: set[str] = set()
        for row in self.coverage:
            key = row.name.lower()
            if key in seen:
                raise ValueError(
                    f"duplicate coverage name {row.name!r} â€” newsletter "
                    "filenames are derived from the name, so it must be unique"
                )
            seen.add(key)
        return self


class SectorsData(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    active_sectors: list[str] = Field(..., min_length=1)

    @field_validator("active_sectors")
    @classmethod
    def _sectors(cls, v: list[str]) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for s in v:
            name = str(s).strip()
            if not name:
                raise ValueError("sector names must be non-empty")
            if len(name) > 40:
                raise ValueError(f"sector name {name!r} is too long (max 40)")
            if name.lower() in seen:
                raise ValueError(f"duplicate sector {name!r}")
            seen.add(name.lower())
            out.append(name)
        return out


class ProfileData(BaseModel):
    """The agreed v1 editable scalar set â€” everything else in profile.md
    stays Obsidian-edited (the card deep-links to the file)."""

    model_config = ConfigDict(extra="forbid", strict=True)
    operator: str = Field(..., min_length=1, max_length=80)
    operator_slug: str = Field(..., pattern=r"^[a-z0-9][a-z0-9-]{0,23}$")
    qualifications: list[str] = Field(default_factory=list)
    role_title: str = Field("", max_length=80)
    role_firm: str = Field("", max_length=120)

    @field_validator("operator", "role_title", "role_firm")
    @classmethod
    def _strip(cls, v: str) -> str:
        return str(v).strip()

    @field_validator("qualifications")
    @classmethod
    def _quals(cls, v: list[str]) -> list[str]:
        out = [str(q).strip() for q in v if str(q).strip()]
        for q in out:
            if len(q) > 20:
                raise ValueError(f"qualification {q!r} is too long (max 20)")
        return out


SECTION_MODELS = {
    "banners": BannersData,
    "watchlist": WatchlistData,
    "coverage": CoverageData,
    "sectors": SectorsData,
    "profile": ProfileData,
}
