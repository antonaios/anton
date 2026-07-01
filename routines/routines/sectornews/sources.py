"""Read sector source lists from `Sectors/<X>.md` frontmatter.

LEGACY as a NEWS INPUT (#operator-tab §3a, 2026-06-10): news source URLs
now live in `_claude/news-coverage.md` (see `routines.sectornews.coverage`).
This module remains the loader for sector ALIAS enrichment (sub-sectors →
relevance scoring) and the fallback path when no coverage file exists.

Schema (in the Sectors file):
    ---
    type: sector
    name: Travel
    sources:
      - "https://www.example.com/feed.rss"           # plain URL = used as-is
      - { url: "https://...", note: "RSS — daily" }   # dict form = with metadata
    ---

We don't enforce the dict form; both shapes are accepted. RSS feeds and HTML
news index pages both work — Firecrawl's /scrape returns markdown for either.

If `sources:` is empty (typical for newly-stubbed sectors), the routine falls
back to Firecrawl /search with a sector-derived query (see search_query()).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

import frontmatter

logger = logging.getLogger(__name__)

# A sector is a plain ASCII name/slug ("Travel", "oil-gas", "Consumer
# Discretionary"). Explicit ranges only — no ``\w`` — so Unicode lookalikes
# never match. ``.`` is deliberately absent: it kills ``..`` traversal AND
# the Win32 trailing-dot trap in one rule.
_SECTOR_SLUG_RE = re.compile(r"[A-Za-z0-9 _-]+")


def validate_sector_slug(sector: str) -> str:
    """Validate ``sector`` as a plain name/slug — never a path.

    #sec-read-path-policy (Shannon DAST 2026-06-12, codex-adjudicated
    defense-in-depth): ``load_sector_config`` interpolates the sector name
    into ``Sectors/<sector>.md``, so a value like ``../Projects/X/deal_memo``
    used to traverse out of ``Sectors/`` and read an arbitrary vault note.
    Validating HERE makes the chokepoint cover every caller — API
    (subprocess CLI), cron run-all, and direct CLI alike; the API layer
    additionally rejects early with a 422 (``api/routes/sectornews.py``).

    Refused: empty/blank values, leading/trailing whitespace (Win32 strips
    trailing spaces at open), and any char outside ``[A-Za-z0-9 _-]`` —
    which kills separators, ``..``, drive/ADS colons, NULs, trailing dots
    and non-ASCII lookalikes in one rule. Returns ``sector`` unchanged;
    raises ``ValueError`` otherwise (pipeline → failed run; API → 422).
    """
    if not sector or not sector.strip():
        raise ValueError("sector name must not be empty")
    if sector != sector.strip():
        raise ValueError(
            f"invalid sector name {sector!r}: leading/trailing whitespace"
        )
    if not _SECTOR_SLUG_RE.fullmatch(sector):
        raise ValueError(
            f"invalid sector name {sector!r}: a sector is a name, not a "
            "path — only ASCII letters, digits, spaces, hyphens and "
            "underscores are allowed"
        )
    return sector


@dataclass(frozen=True)
class SectorConfig:
    """Loaded sector-news config for one sector / coverage topic."""
    name: str                    # sector name, e.g. "Travel"
    sources: list[str]           # explicit URLs from Sectors/<X>.md
    aliases: list[str]           # alternate names / sub-sectors for relevance scoring
    raw_metadata: dict           # full frontmatter for downstream use
    query: str | None = None     # custom search query (news-coverage rows; #operator-tab §3a)


def load_sector_config(vault_root: Path, sector: str) -> SectorConfig:
    """Read Sectors/<sector>.md and return a SectorConfig.

    Raises ``ValueError`` when ``sector`` is not a plain name/slug
    (#sec-read-path-policy — see :func:`validate_sector_slug`).
    """
    sector = validate_sector_slug(sector)
    sector_path = vault_root / "Sectors" / f"{sector}.md"
    if not sector_path.exists():
        logger.warning("Sectors/%s.md missing; using minimal default config", sector)
        return SectorConfig(name=sector, sources=[], aliases=[], raw_metadata={})

    post = frontmatter.loads(sector_path.read_text(encoding="utf-8"))
    meta = dict(post.metadata)

    sources_raw = meta.get("sources", []) or []
    sources: list[str] = []
    for s in sources_raw:
        if isinstance(s, str):
            sources.append(s.strip())
        elif isinstance(s, dict) and "url" in s:
            sources.append(str(s["url"]).strip())

    sub_sectors = meta.get("sub_sectors") or meta.get("sub-sectors") or []
    aliases = [str(x).strip() for x in sub_sectors if x] if isinstance(sub_sectors, list) else []

    return SectorConfig(
        name=meta.get("name", sector),
        sources=[s for s in sources if s.startswith(("http://", "https://"))],
        aliases=aliases,
        raw_metadata=meta,
    )


def search_query(sector: SectorConfig, *, days: int = 7) -> str:
    """Build a default Firecrawl /search query for a sector when no explicit
    sources are configured. Includes M&A / corporate-finance framing because
    that's the operator's lens.

    A coverage row's custom ``query:`` (news-coverage.md) overrides the
    sector-derived default entirely.
    """
    if sector.query:
        return sector.query

    base = sector.name
    if sector.aliases:
        # Use first 2-3 aliases for breadth (e.g. "Pubs", "Hotels", "Restaurants")
        base += " (" + " OR ".join(sector.aliases[:3]) + ")"

    return f"{base} M&A deals UK news"
