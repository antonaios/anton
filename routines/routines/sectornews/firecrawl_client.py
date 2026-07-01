"""Thin Firecrawl wrapper.

Centralises:
    - API key sourcing (FIRECRAWL_API_KEY env var)
    - Retry on rate limits / transient failures
    - Defensive parsing of response shapes (Firecrawl's API has shifted between
      v0/v1; the python SDK abstracts most of it but we still want to fail
      cleanly when it doesn't)

Two operations used by sector news:
    - search(query, limit, scrape=True)  -> list of {url, title, snippet, markdown}
    - scrape(url)                          -> {url, title, markdown, metadata}

Other Firecrawl ops (crawl, map, extract) not needed by sector news; add when
the deal-tracker or earnings routine needs them.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FCResult:
    """One search/scrape result. Shape we standardise on."""
    url: str
    title: str
    snippet: str         # short summary (search) or first ~500 chars (scrape)
    markdown: str        # full content if scrape=True; else empty
    metadata: dict[str, Any]


class FirecrawlError(Exception):
    """Raised on transport / auth / unrecoverable Firecrawl failure."""


class FirecrawlClient:
    """Routines should use this rather than firecrawl-py directly."""

    def __init__(self, api_key: str | None = None, timeout: int = 60) -> None:
        self.api_key = api_key or os.environ.get("FIRECRAWL_API_KEY", "").strip()
        if not self.api_key:
            raise FirecrawlError(
                "FIRECRAWL_API_KEY not set in the process environment. "
                "On Windows, set it via `setx FIRECRAWL_API_KEY \"fc-...\"` "
                "(User scope; new processes inherit it, existing processes "
                "don't), or pass api_key= explicitly. See HANDOFF.md §3."
            )
        self.timeout = timeout
        # Lazy import — firecrawl-py is heavy and we want this module
        # importable for tests that mock the client
        try:
            from firecrawl import Firecrawl  # type: ignore[import-not-found]
        except ImportError as e:
            raise FirecrawlError(
                f"firecrawl-py not installed: {e}. Run `pip install firecrawl-py`."
            ) from e
        self._client = Firecrawl(api_key=self.api_key)

    def search(
        self,
        query: str,
        *,
        limit: int = 10,
        scrape: bool = False,
        time_filter: str | None = "qdr:w",  # 'qdr:w' = past week (Google operator)
    ) -> list[FCResult]:
        """Search the web for `query`. With scrape=True, also fetches markdown
        content for each result (more API cost, but better synthesis).

        time_filter follows Google's tbs= operator: qdr:d (day), qdr:w (week),
        qdr:m (month). None disables.
        """
        kwargs: dict[str, Any] = {"limit": limit}
        if scrape:
            kwargs["scrape_options"] = {"formats": ["markdown"]}
        if time_filter:
            kwargs["tbs"] = time_filter

        last_err: Exception | None = None
        for attempt in range(3):
            try:
                resp = self._client.search(query=query, **kwargs)
                break
            except Exception as e:  # noqa: BLE001
                last_err = e
                logger.warning("firecrawl search attempt %d failed: %s", attempt + 1, e)
                if attempt < 2:
                    time.sleep(2 ** attempt)
                else:
                    raise FirecrawlError(
                        f"firecrawl search failed after retries: {e}"
                    ) from e

        return [_to_result(item, with_markdown=scrape) for item in _extract_items(resp)]

    def scrape(self, url: str) -> FCResult:
        """Scrape one URL, return markdown."""
        try:
            resp = self._client.scrape(url=url, formats=["markdown"])
        except Exception as e:  # noqa: BLE001
            raise FirecrawlError(f"firecrawl scrape failed for {url}: {e}") from e
        return _to_result(_to_dict(resp), with_markdown=True)


# ============================================================ helpers


def _to_dict(obj: Any) -> dict[str, Any]:
    """Firecrawl SDK returns pydantic models; convert defensively."""
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    if hasattr(obj, "dict"):
        return obj.dict()
    if hasattr(obj, "__dict__"):
        return dict(obj.__dict__)
    return {}


def _extract_items(resp: Any) -> list[dict[str, Any]]:
    """Search response shape varies. Try several access patterns."""
    d = _to_dict(resp)
    # Common shapes
    if "data" in d and isinstance(d["data"], dict) and "web" in d["data"]:
        return [_to_dict(x) for x in d["data"]["web"]]
    if "data" in d and isinstance(d["data"], list):
        return [_to_dict(x) for x in d["data"]]
    if "web" in d and isinstance(d["web"], list):
        return [_to_dict(x) for x in d["web"]]
    if "results" in d and isinstance(d["results"], list):
        return [_to_dict(x) for x in d["results"]]
    # Pydantic model with .web attribute
    if hasattr(resp, "web"):
        return [_to_dict(x) for x in (resp.web or [])]
    if hasattr(resp, "data") and hasattr(resp.data, "web"):
        return [_to_dict(x) for x in (resp.data.web or [])]
    logger.warning("unrecognised Firecrawl search response shape: keys=%s", list(d)[:10])
    return []


def _to_result(item: dict[str, Any], *, with_markdown: bool) -> FCResult:
    return FCResult(
        url=str(item.get("url", "")).strip(),
        title=str(item.get("title", "") or item.get("name", "")).strip(),
        snippet=str(item.get("description", "") or item.get("snippet", "")).strip(),
        markdown=str(item.get("markdown", "")).strip() if with_markdown else "",
        metadata={k: v for k, v in item.items() if k not in {"url", "title", "name", "description", "snippet", "markdown"}},
    )
