"""Provider dispatcher: pick Firecrawl or Tavily, with optional auto-fallback.

The pipeline takes a SearchClient (a Protocol — duck-typed: anything with
`search()` and `scrape()` returning FCResult satisfies it). Both
FirecrawlClient and TavilyClient are SearchClient-compatible.

Selection:
    AGENTIC_SEARCH_PROVIDER = "firecrawl" | "tavily" | "auto" (default "auto")

Auto: try Firecrawl first if FIRECRAWL_API_KEY is set; on auth error,
      fall back to Tavily if TAVILY_API_KEY{,S} is set. If neither is
      configured, raise.

"auto" is the production-friendly mode — costs Firecrawl credits when those
work, falls through to Tavily if Firecrawl is rate-limited or out of
credits, fails clearly if neither is available.
"""

from __future__ import annotations

import logging
import os
from typing import Protocol

from routines.sectornews.firecrawl_client import FCResult, FirecrawlClient, FirecrawlError

logger = logging.getLogger(__name__)


class SearchClient(Protocol):
    """Duck-typed interface satisfied by FirecrawlClient and TavilyClient."""

    def search(
        self, query: str, *, limit: int = 10, scrape: bool = False,
        time_filter: str | None = "qdr:w",
    ) -> list[FCResult]: ...

    def scrape(self, url: str) -> FCResult: ...


class NoProviderConfiguredError(Exception):
    """Raised when no search provider's API key is set."""


def get_search_client(preference: str | None = None) -> SearchClient:
    """Return a SearchClient based on env / preference.

    Args:
        preference: "firecrawl" | "tavily" | "auto" (default: env var, or "auto")
    """
    pref = (preference or os.environ.get("AGENTIC_SEARCH_PROVIDER") or "auto").lower()

    has_firecrawl = bool(os.environ.get("FIRECRAWL_API_KEY", "").strip())
    has_tavily = bool(
        os.environ.get("TAVILY_API_KEY", "").strip()
        or os.environ.get("TAVILY_API_KEYS", "").strip()
    )

    if pref == "firecrawl":
        if not has_firecrawl:
            raise NoProviderConfiguredError("FIRECRAWL_API_KEY not set")
        return FirecrawlClient()

    if pref == "tavily":
        if not has_tavily:
            raise NoProviderConfiguredError(
                "Neither TAVILY_API_KEY nor TAVILY_API_KEYS is set"
            )
        from routines.sectornews.tavily_client import TavilyClient
        return TavilyClient()

    # auto
    if has_firecrawl:
        try:
            return FirecrawlClient()
        except FirecrawlError as e:
            logger.warning("FirecrawlClient init failed (%s); trying Tavily", e)
    if has_tavily:
        from routines.sectornews.tavily_client import TavilyClient
        return TavilyClient()
    if has_firecrawl:
        # Firecrawl was tried above and failed init; surface the original error
        return FirecrawlClient()  # raises again, caller sees the real reason
    raise NoProviderConfiguredError(
        "No search provider configured. Set FIRECRAWL_API_KEY or "
        "TAVILY_API_KEY{,S} in env."
    )
