"""End-to-end pipeline: fetch -> dedupe -> score -> synthesise -> write.

Also feeds candidate M&A items into the deal tracker workbook when
`feed_deals=True` (default). The auto-feed uses the cheap
`looks_like_ma_announcement` pre-filter on title+snippet before running
the full LLM extraction, so non-M&A items don't burn tokens.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from routines.dealtracker.extract import extract_deal, looks_like_ma_announcement
from routines.dealtracker.workbook import CANONICAL_WORKBOOK_PATH, append_deal
from routines.sectornews.dedupe import dedupe
from routines.sectornews.firecrawl_client import (
    FCResult, FirecrawlClient, FirecrawlError,
)
from routines.sectornews.coverage import CoverageEntry, config_from_coverage
from routines.sectornews.search_provider import SearchClient
from routines.sectornews.score import ScoredItem, score_items
from routines.sectornews.sources import SectorConfig, load_sector_config, search_query
from routines.sectornews.synthesise import build_full_newsletter_md, synthesise_newsletter
from routines.shared import audit
from routines.shared.ollama_client import OllamaClient, OllamaError
from routines.shared.vault_writer import VaultPaths, atomic_write
from routines.guards.injection import scan_ingested_text

logger = logging.getLogger(__name__)


@dataclass
class PipelineResult:
    status: str                   # "ok" | "skipped" | "error"
    sector: str
    output_path: Path | None = None
    items_fetched: int = 0
    items_deduped: int = 0
    items_scored: int = 0
    deals_appended: int = 0       # rows added to deal tracker workbook
    deals_skipped: int = 0        # dedupe hits (already in workbook)
    deals_filtered: int = 0       # items that matched pre-filter
    duration_ms: int = 0
    error: str | None = None
    fed_urls: list[str] = field(default_factory=list)


def run_for_sector(
    sector: str,
    *,
    paths: VaultPaths,
    fc: SearchClient,                 # FirecrawlClient or TavilyClient — duck-typed
    ollama: OllamaClient,
    audit_dir: Path,
    days: int = 7,
    fetch_limit: int = 15,
    scrape_full: bool = False,
    dry_run: bool = False,
    feed_deals: bool = True,
    deals_workbook: Path | None = None,
    coverage: "CoverageEntry | None" = None,
) -> PipelineResult:
    """Run the full pipeline for one sector (or news-coverage row).

    When ``coverage`` is given (#operator-tab §3a), the config comes from
    the coverage row (sources / custom query / optional sector link)
    instead of ``Sectors/<sector>.md``; ``sector`` is then the row's
    display name.
    """
    started = time.monotonic()
    run_id = audit.new_run_id()
    logger.info("sector-news start sector=%s run_id=%s", sector, run_id)

    try:
        # 1. Load config — coverage row when given, else legacy sector note
        if coverage is not None:
            cfg = config_from_coverage(paths.root, coverage)
        else:
            cfg = load_sector_config(paths.root, sector)

        # 2. Fetch — sources URL list (if any) + Firecrawl /search query
        items = _fetch_for_sector(cfg, fc=fc, days=days, limit=fetch_limit, scrape_full=scrape_full)
        items_fetched = len(items)
        logger.info("fetched %d items for sector=%s", items_fetched, sector)

        # 3. Dedupe
        deduped = dedupe(items, threshold=0.7)
        logger.info("deduped to %d items", len(deduped))

        # 4. Score
        scored = score_items(
            deduped,
            sector_name=cfg.name,
            aliases=cfg.aliases,
            client=ollama,
        )
        non_zero = [s for s in scored if s.relevance > 0 or s.materiality > 0]
        logger.info("scored %d items (%d non-zero)", len(scored), len(non_zero))

        # 4b. Auto-feed M&A candidates into deal tracker (cheap pre-filter
        # + LLM extraction + idempotent append).
        deals_appended = 0
        deals_skipped = 0
        deals_filtered = 0
        fed_urls: list[str] = []
        if feed_deals and not dry_run:
            # Canonical precedent tracker (post 2026-06-01). Operator can
            # override via ``deals_workbook=`` (tests use ``tmp_path``).
            workbook_path = deals_workbook or CANONICAL_WORKBOOK_PATH
            # Sector slug for the lean-schema ``Subsector (slug)`` column.
            # Sector-linked coverage rows tag deals to the LINKED sector
            # (so "Hospitality weekly" still lands under hospitality);
            # standalone topics fall back to the row/run name. Slug
            # convention per ``sector-expertise.md`` is lowercase +
            # hyphen-joined.
            deal_sector = (
                coverage.sector if coverage is not None and coverage.sector
                else sector
            )
            subsector_slug = deal_sector.lower().replace(" ", "-")
            for s in scored:
                item = s.item
                if not looks_like_ma_announcement(item.title or "", item.snippet or ""):
                    continue
                deals_filtered += 1
                source_text = (item.markdown or item.snippet or item.title or "").strip()
                if not source_text:
                    continue
                # #sec-injection-guard 3a: scan the fetched web content
                # (detect-and-audit; NEVER blocks).
                scan_ingested_text(source_text, source="sectornews:auto-feed")
                try:
                    deal = extract_deal(
                        text=source_text,
                        source_url=item.url or "",
                        client=ollama,
                        subsector_slug=subsector_slug,
                        scan=False,  # already screened at the auto-feed boundary above
                    )
                except OllamaError as e:
                    logger.warning("auto-feed: extract_deal failed for %s: %s", item.url, e)
                    continue
                if not deal.target_company:
                    # Pre-filter false positive; skip silently.
                    continue
                deal.extracted_by_run_id = run_id
                try:
                    result = append_deal(workbook_path, deal)
                except Exception as e:  # noqa: BLE001 — openpyxl can raise broadly
                    logger.warning("auto-feed: append_deal failed for %s: %s", deal.target_company, e)
                    continue
                if result["status"] == "appended":
                    deals_appended += 1
                    fed_urls.append(item.url or "")
                    # #43 — best-effort vault enrichment from the auto-feed.
                    # Comps is the deliverable-level capture owner and is NOT
                    # routed here; only the news auto-feed + manual `add` emit
                    # #43 captures (SESSION-43 decision 5/6). Never fails ingest.
                    try:
                        from routines.dealtracker.capture import emit_deal_capture

                        emit_deal_capture(
                            deal, vault_root=paths.root, run_id=run_id,
                            workbook_path=workbook_path,
                        )
                    except Exception as e:  # noqa: BLE001 — capture is best-effort
                        logger.warning(
                            "auto-feed: deal-capture emit failed for %s: %s",
                            deal.target_company, e,
                        )
                else:
                    deals_skipped += 1
            logger.info(
                "auto-feed: %d pre-filtered, %d appended, %d duplicates",
                deals_filtered, deals_appended, deals_skipped,
            )

        # 5. Synthesise
        body = synthesise_newsletter(
            sector_name=cfg.name,
            scored=scored,
            client=ollama,
        )

        # 6. Build full markdown + write
        full_md = build_full_newsletter_md(
            sector_name=cfg.name,
            body=body,
            sources_used=[s.item.url for s in scored if s.item.url],
            composite_scores=[s.composite for s in scored],
            run_id=run_id,
        )
        out_path = paths.resources / "Newsletters" / f"{date.today().isoformat()}-{cfg.name.replace(' ', '-')}.md"

        if dry_run:
            logger.info("dry-run: would write %d chars to %s", len(full_md), out_path)
            duration_ms = int((time.monotonic() - started) * 1000)
            audit.write_structured(
                actor={"type": "system", "id": "routine:sectornews"},
                entity_type="vault_note",
                entity_id=str(out_path),
                action="run",
                routine="sectornews", run_id=run_id, status="ok",
                audit_dir=audit_dir,
                inputs={"sector": sector, "days": days, "dry_run": True},
                outputs={
                    "would_write_to": str(out_path),
                    "items_fetched": items_fetched,
                    "items_deduped": len(deduped),
                    "items_scored": len(scored),
                },
                duration_ms=duration_ms,
            )
            return PipelineResult(
                status="ok", sector=sector,
                items_fetched=items_fetched,
                items_deduped=len(deduped),
                items_scored=len(scored),
                duration_ms=duration_ms,
            )

        atomic_write(out_path, full_md, vault_root=paths.root)
        duration_ms = int((time.monotonic() - started) * 1000)

        audit.write_structured(
            actor={"type": "system", "id": "routine:sectornews"},
            entity_type="vault_note",
            entity_id=str(out_path),
            action="run",
            routine="sectornews", run_id=run_id, status="ok",
            audit_dir=audit_dir,
            inputs={"sector": sector, "days": days, "feed_deals": feed_deals},
            outputs={
                "output_path": str(out_path),
                "items_fetched": items_fetched,
                "items_deduped": len(deduped),
                "items_scored": len(scored),
                "deals_appended": deals_appended,
                "deals_skipped": deals_skipped,
                "deals_filtered": deals_filtered,
            },
            duration_ms=duration_ms,
        )

        return PipelineResult(
            status="ok", sector=sector, output_path=out_path,
            items_fetched=items_fetched,
            items_deduped=len(deduped),
            items_scored=len(scored),
            deals_appended=deals_appended,
            deals_skipped=deals_skipped,
            deals_filtered=deals_filtered,
            duration_ms=duration_ms,
            fed_urls=fed_urls,
        )

    except Exception as e:  # noqa: BLE001
        duration_ms = int((time.monotonic() - started) * 1000)
        logger.exception("pipeline error for sector=%s: %s", sector, e)
        audit.write_structured(
            actor={"type": "system", "id": "routine:sectornews"},
            entity_type="vault_note",
            entity_id=sector,
            action="run",
            routine="sectornews", run_id=run_id, status="error",
            audit_dir=audit_dir,
            inputs={"sector": sector, "days": days},
            duration_ms=duration_ms,
            error=str(e),
        )
        return PipelineResult(
            status="error", sector=sector, error=str(e), duration_ms=duration_ms,
        )


# ============================================================ helpers


def _fetch_for_sector(
    cfg: SectorConfig,
    *,
    fc: SearchClient,
    days: int,
    limit: int,
    scrape_full: bool,
) -> list[FCResult]:
    """Fetch items from explicit sources (if any) + provider /search."""
    items: list[FCResult] = []

    # Explicit source URLs — scrape each
    for url in cfg.sources:
        try:
            r = fc.scrape(url)
            items.append(r)
        except Exception as e:  # noqa: BLE001
            logger.warning("scrape failed for %s: %s", url, e)
            continue

    # Provider search for sector-relevant news (always, for breadth)
    time_filter = _days_to_tbs(days)
    query = search_query(cfg, days=days)
    logger.info("provider search: %r (limit=%d, time_filter=%s)", query, limit, time_filter)
    try:
        search_items = fc.search(
            query, limit=limit, scrape=scrape_full, time_filter=time_filter,
        )
        items.extend(search_items)
    except Exception as e:  # noqa: BLE001
        # If search fails, we still return scraped sources (if any). If both fail, raise.
        logger.warning("provider search failed: %s", e)
        if not items:
            raise

    return items


def _days_to_tbs(days: int) -> str:
    """Map a days window to Google's tbs= operator."""
    if days <= 1:
        return "qdr:d"
    if days <= 7:
        return "qdr:w"
    if days <= 31:
        return "qdr:m"
    return "qdr:y"
