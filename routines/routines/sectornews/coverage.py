"""News-coverage config (#operator-tab §3a) — decouples WHAT the daily
newsletter covers from the operator's expertise sectors.

``_claude/news-coverage.md`` (dashboard-config family, editable in
Obsidian AND from the dashboard's OPERATOR tab) carries a ``coverage``
YAML block::

    ## coverage

    ```yaml
    - name: Hospitality
      sector: Hospitality          # linked → feeds the expertise waterfall
      sources: []                  # explicit URLs; empty → search fallback
    - name: UK macro & rates       # standalone topic — no expertise tree
      query: "Bank of England rate decision UK economy"
      sources:
        - "https://www.bankofengland.co.uk/news"
    ```

Absent file / section / empty list → a default coverage list is
synthesised from profile.md ``active_sectors`` — exactly the
pre-decoupling behaviour, so the 07:00 job can never regress on a
missing file.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from routines.shared.md_config import extract_section
from routines.shared.profile import load as load_profile

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CoverageEntry:
    """One newsletter run per morning."""

    name: str
    sector: str | None = None        # link to an expertise sector, if any
    sources: list[str] = field(default_factory=list)
    query: str | None = None         # custom search query (else sector-derived)
    enabled: bool = True


def coverage_path(vault_root: Path) -> Path:
    return vault_root / "_claude" / "news-coverage.md"


def _parse_row(row: object, idx: int) -> CoverageEntry | None:
    """Lenient row parse — the scheduler must not die on one bad row."""
    if not isinstance(row, dict):
        logger.warning("news-coverage: row %d is not a mapping — skipped", idx + 1)
        return None
    name = str(row.get("name", "")).strip()
    if not name:
        logger.warning("news-coverage: row %d has no name — skipped", idx + 1)
        return None
    sector = str(row.get("sector", "")).strip() or None
    query = str(row.get("query", "")).strip() or None
    raw_sources = row.get("sources") or []
    sources: list[str] = []
    if isinstance(raw_sources, list):
        for s in raw_sources:
            url = str(s.get("url", "") if isinstance(s, dict) else s).strip()
            if url.startswith(("http://", "https://")):
                sources.append(url)
            elif url:
                logger.warning(
                    "news-coverage: %s: source %r is not http(s) — skipped",
                    name, url,
                )
    enabled = _parse_enabled(row.get("enabled", True), name)
    return CoverageEntry(
        name=name, sector=sector, sources=sources, query=query, enabled=enabled,
    )


def _parse_enabled(value: object, name: str) -> bool:
    """Explicit bool parse (codex SEV-1): ``bool("false")`` is True, so a
    quoted ``enabled: "false"`` would re-enable a paused row in the live
    07:00 cron. Accept real bools + the common string forms; anything
    unrecognised warns and defaults to enabled (visible beats silent)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("false", "no", "0", "off"):
            return False
        if v in ("true", "yes", "1", "on", ""):
            return True
    logger.warning(
        "news-coverage: %s: unrecognised enabled=%r — treating as enabled",
        name, value,
    )
    return True


def load_coverage(vault_root: Path) -> tuple[list[CoverageEntry], str]:
    """Return ``(entries, source)`` where source is ``"config"`` when the
    coverage file supplied the list and ``"synthesised"`` when it was
    derived from ``active_sectors`` (file/section absent or empty).
    """
    path = coverage_path(vault_root)
    if path.exists():
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as e:
            logger.warning("news-coverage: read %s failed (%s) — synthesising", path, e)
            text = None
        if text is not None:
            raw = extract_section(text, "coverage")
            if isinstance(raw, list) and raw:
                entries = [
                    e for i, r in enumerate(raw)
                    if (e := _parse_row(r, i)) is not None
                ]
                if entries:
                    return entries, "config"
                logger.warning(
                    "news-coverage: every row malformed — synthesising from "
                    "active_sectors"
                )

    profile = load_profile(vault_root)
    entries = [
        CoverageEntry(name=s, sector=s) for s in profile.active_sectors
    ]
    return entries, "synthesised"


def config_from_coverage(vault_root: Path, entry: CoverageEntry):
    """Build the pipeline's ``SectorConfig`` from a coverage row.

    Sector-linked rows still read the linked ``Sectors/<X>.md`` for
    alias enrichment (sub-sector names sharpen relevance scoring);
    standalone topics run on the row alone.
    """
    from routines.sectornews.sources import SectorConfig, load_sector_config

    aliases: list[str] = []
    raw_metadata: dict = {}
    if entry.sector:
        # Non-fatal (codex SEV-1): a broken/renamed sector link must not
        # fail the row's morning run — enrichment is a bonus, not a
        # dependency. (A MISSING note already degrades inside
        # load_sector_config; this guards the unparseable-note case.)
        try:
            linked = load_sector_config(vault_root, entry.sector)
            aliases = linked.aliases
            raw_metadata = linked.raw_metadata
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "news-coverage: %s: sector link %r unreadable (%s) — "
                "running without alias enrichment",
                entry.name, entry.sector, e,
            )

    return SectorConfig(
        name=entry.name,
        sources=list(entry.sources),
        aliases=aliases,
        raw_metadata=raw_metadata,
        query=entry.query,
    )
