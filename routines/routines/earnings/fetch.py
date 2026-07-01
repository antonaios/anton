"""Step 2 — fetch the results announcement via Firecrawl.

Reuses :mod:`routines.sectornews.firecrawl_client`. Resolution order:

  1. If the company page declares a source URL (``earnings-source-url`` /
     ``ir-url`` / ``rns-url`` — see :mod:`routines.earnings.calendar`), scrape
     it directly.
  2. Otherwise search for the company's latest results and scrape the top hit.

Returns ``(markdown, source_url)`` or ``(None, "")`` when nothing usable came
back. A miss is NOT an error: step 3 (catch-up) is modelled as the cron
re-firing on a later run, so "announcement not published yet" simply returns
``None`` and the pipeline leaves the company's ``next-reporting-date`` untouched
for the next sweep.

The Firecrawl client is injected so tests can pass a fake — no network in the
test suite.
"""

from __future__ import annotations

import logging
from typing import Any, Optional, Protocol

from routines.earnings.calendar import CompanyEntry

log = logging.getLogger(__name__)

# A scraped page shorter than this is treated as "not the announcement yet"
# (a holding page / cookie wall / empty IR stub) so the cron re-fires.
_MIN_USEFUL_CHARS = 400


class _FCResultLike(Protocol):
    url: str
    title: str
    markdown: str


class _FirecrawlLike(Protocol):
    def scrape(self, url: str) -> _FCResultLike: ...
    def search(self, query: str, *, limit: int = ..., scrape: bool = ...,
               time_filter: Optional[str] = ...) -> list[_FCResultLike]: ...


def _search_query(entry: CompanyEntry) -> str:
    bits = [entry.name or entry.stem]
    if entry.ticker:
        bits.append(entry.ticker)
    bits.append("results announcement")
    return " ".join(bits)


def fetch_announcement(
    entry: CompanyEntry,
    client: _FirecrawlLike,
) -> tuple[Optional[str], str, str]:
    """Fetch the announcement markdown for ``entry``. Best-effort; never raises
    into the pipeline — transport failures are logged and surface as a miss so
    the cron re-fires.

    Returns ``(markdown, source_url, via)`` where ``via`` is the ACTUAL
    provenance / disposition of the fetch — ``"pinned"`` (operator-vouched source
    URL), ``"search"`` (top web hit, NOT operator-vouched → the pipeline must
    hard-gate the issuer on it), ``"none"`` (a BENIGN miss — nothing published
    yet; markdown is None), or ``"error"`` (an operational transport/search
    EXCEPTION; markdown is None, but the caller audits it partial rather than as a
    clean "not published"). The pipeline gates the wrong-issuer check on this real
    provenance rather than inferring it from config, so a future fetch change
    can't silently bypass the gate (#44 Codex SEV-1 / re-review SEV-2)."""
    # 1. Direct URL from the company page frontmatter.
    #
    # When the operator has pinned the canonical results URL we DON'T fall back
    # to a web search on a short/failed scrape: searching could surface a stale
    # or wrong-period announcement and (worse) roll the calendar forward on it.
    # A miss here is "not published yet" — the cron re-fires next sweep, by which
    # time the pinned page has the results (#44 Codex SEV-2).
    if entry.source_url:
        try:
            res = client.scrape(entry.source_url)
            md = (getattr(res, "markdown", "") or "").strip()
            if len(md) >= _MIN_USEFUL_CHARS:
                return md, (getattr(res, "url", "") or entry.source_url), "pinned"
            log.info(
                "earnings fetch: %s scrape returned %d chars (< %d) — treating as not-yet-published",
                entry.source_url, len(md), _MIN_USEFUL_CHARS,
            )
            return None, "", "none"   # short page → benign "not published yet"
        except Exception as e:  # noqa: BLE001 — Firecrawl/IO can raise broadly
            log.warning("earnings fetch: scrape of %s failed (%s) — not falling back to search", entry.source_url, e)
            return None, "", "error"   # transport failure → operational, audit partial

    # 2. No pinned URL — fall back to a search + scrape of the top hit. NO date
    #    filter: a long catch-up (a missed/overdue announcement older than a week)
    #    must still be discoverable. Recency is validated downstream by the
    #    pipeline's reported_date staleness gate (anchored on the scheduled due
    #    date), so we don't need a time window here (#44 Codex).
    try:
        results = client.search(_search_query(entry), limit=5, scrape=True, time_filter=None)
    except Exception as e:  # noqa: BLE001
        log.warning("earnings fetch: search for %r failed (%s)", entry.name, e)
        return None, "", "error"   # transport failure → operational, audit partial

    for res in results or []:
        md = (getattr(res, "markdown", "") or "").strip()
        if len(md) >= _MIN_USEFUL_CHARS:
            return md, (getattr(res, "url", "") or ""), "search"

    log.info("earnings fetch: no usable announcement found for %r yet", entry.name)
    return None, "", "none"


# Silence unused-import lint on Any.
_ = Any

__all__ = ["fetch_announcement"]
