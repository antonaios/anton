"""Tavily search wrapper — drop-in alternative to FirecrawlClient.

Same FCResult return shape, same SearchClient protocol, so the pipeline
doesn't care which provider it's using.

Tavily features used:
    - search(query, topic='news', days=N, max_results=N) — built-in news topic
      and date-window, cleaner than passing Google tbs=qdr:w through Firecrawl
    - extract(urls=[...]) — full markdown content per URL

Multi-key rotation: accepts TAVILY_API_KEYS as a comma-separated list of keys.
On rate-limit / auth-error on key N, rotates to key N+1. Useful when the user
has multiple keys (per-account quota) and wants to extend per-day budget.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

from routines.sectornews.firecrawl_client import FCResult

logger = logging.getLogger(__name__)


class TavilyError(Exception):
    """Raised on transport / auth / unrecoverable Tavily failure."""


class TavilyClient:
    """Drop-in alternative to FirecrawlClient. Same .search() and .scrape()
    methods, same FCResult return shape — pipeline.py doesn't care which one
    it has."""

    def __init__(self, api_keys: list[str] | None = None, timeout: int = 60) -> None:
        # Source: explicit arg > TAVILY_API_KEYS (csv) > TAVILY_API_KEY (single)
        if api_keys is None:
            csv = os.environ.get("TAVILY_API_KEYS", "").strip()
            single = os.environ.get("TAVILY_API_KEY", "").strip()
            api_keys = (
                [k.strip() for k in csv.split(",") if k.strip()] if csv
                else ([single] if single else [])
            )
        self.api_keys = api_keys
        if not self.api_keys:
            raise TavilyError(
                "No Tavily API keys found. Set TAVILY_API_KEYS (comma-sep for "
                "multiple) or TAVILY_API_KEY in env."
            )
        self.timeout = timeout
        self._key_idx = 0

        # Lazy import — keep test-importable without the package
        try:
            from tavily import TavilyClient as _TC  # type: ignore[import-not-found]
        except ImportError as e:
            raise TavilyError(
                f"tavily-python not installed: {e}. Run `pip install tavily-python`."
            ) from e
        self._TC = _TC
        self._build_client()

    def _build_client(self) -> None:
        """Re-create the underlying client with the current api key."""
        self._client = self._TC(api_key=self.api_keys[self._key_idx])

    def _rotate_key(self) -> bool:
        """Move to the next key in the list. Returns False if all keys
        exhausted (no more to try)."""
        self._key_idx += 1
        if self._key_idx >= len(self.api_keys):
            return False
        logger.warning(
            "rotating to Tavily key index %d (of %d)",
            self._key_idx, len(self.api_keys),
        )
        self._build_client()
        return True

    # ------------------------------------------------------------ search

    def search(
        self,
        query: str,
        *,
        limit: int = 10,
        scrape: bool = False,    # Tavily search returns content snippet by default
        time_filter: str | None = "qdr:w",  # ignored; we use 'days' below
    ) -> list[FCResult]:
        """Search via Tavily. Returns FCResult list. Tavily search already
        returns content snippets, so 'scrape' here means "use advanced depth"
        (longer / cleaner content) rather than a separate /scrape call."""
        days = _tbs_to_days(time_filter)
        depth = "advanced" if scrape else "basic"

        last_err: Exception | None = None
        while True:
            try:
                resp = self._client.search(
                    query=query,
                    topic="news",         # bias toward news content
                    days=days,
                    max_results=limit,
                    search_depth=depth,
                )
                break
            except Exception as e:  # noqa: BLE001
                last_err = e
                msg = str(e).lower()
                if any(t in msg for t in ("unauthorized", "invalid", "401", "403", "rate", "quota", "429")):
                    if self._rotate_key():
                        time.sleep(1)
                        continue
                # Non-auth/rate error or no more keys — fail
                raise TavilyError(f"tavily search failed: {e}") from e

        results: list[dict[str, Any]] = (
            resp.get("results", []) if isinstance(resp, dict) else []
        )
        return [_to_result(r, with_markdown=scrape) for r in results]

    # ------------------------------------------------------------ scrape

    def scrape(self, url: str) -> FCResult:
        """Single-URL extract via Tavily. Returns FCResult with markdown."""
        last_err: Exception | None = None
        while True:
            try:
                resp = self._client.extract(
                    urls=[url],
                    extract_depth="basic",
                )
                break
            except Exception as e:  # noqa: BLE001
                last_err = e
                msg = str(e).lower()
                if any(t in msg for t in ("unauthorized", "invalid", "401", "403", "rate", "quota", "429")):
                    if self._rotate_key():
                        time.sleep(1)
                        continue
                raise TavilyError(f"tavily extract failed for {url}: {e}") from e

        items = (resp.get("results", []) if isinstance(resp, dict) else [])
        if not items:
            raise TavilyError(f"tavily extract returned no content for {url}")
        item = items[0]
        return FCResult(
            url=str(item.get("url", url)).strip(),
            title=str(item.get("title", "")).strip(),
            snippet="",
            markdown=str(item.get("raw_content", "") or item.get("content", "")).strip(),
            metadata={k: v for k, v in item.items() if k not in {"url", "title", "raw_content", "content"}},
        )


# ============================================================ helpers


def _tbs_to_days(time_filter: str | None) -> int:
    """Map a Google tbs= operator (qdr:d/w/m/y) to Tavily's days parameter."""
    if time_filter is None:
        return 30
    f = time_filter.replace("qdr:", "")
    return {"d": 1, "w": 7, "m": 30, "y": 365}.get(f, 7)


def _to_result(r: dict[str, Any], *, with_markdown: bool) -> FCResult:
    """Map a Tavily search-result dict to our FCResult."""
    content = str(r.get("content", "")).strip()
    return FCResult(
        url=str(r.get("url", "")).strip(),
        title=str(r.get("title", "")).strip(),
        snippet=content[:500],   # snippet = first chunk of content
        markdown=content if with_markdown else "",
        metadata={k: v for k, v in r.items() if k not in {"url", "title", "content"}},
    )
