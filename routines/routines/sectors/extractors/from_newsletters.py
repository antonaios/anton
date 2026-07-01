"""Extract sector-attributable claims from sector newsletters.

Walks `Resources/Newsletters/<date>-<sector>.md` files written in the
extraction window. For each newsletter matching the target sector, parses
top-N items (typically 3-7 per newsletter) and emits one SectorExtract
per distinct publisher mentioned across the items.

Source-root key = publisher_id (one extract per publisher per run, capping
weight per Plan v3 §6.9 B5 deduplication rule).
"""

from __future__ import annotations

import logging
import re
from datetime import date as date_cls
from pathlib import Path

import frontmatter

from routines.sectors.schema import SectorExtract, slugify_sector

log = logging.getLogger(__name__)


def gather(
    vault_root: Path,
    sector: str,
    *,
    since: date_cls | None = None,
    skip_llm: bool = False,
) -> list[SectorExtract]:
    """Walk sector newsletters and emit extracts grouped by publisher."""
    target_slug = slugify_sector(sector)
    extracts: list[SectorExtract] = []
    newsletters_dir = vault_root / "Resources" / "Newsletters"
    if not newsletters_dir.is_dir():
        log.info("from-newsletters: no Resources/Newsletters/ at %s", newsletters_dir)
        return extracts

    # Match filenames like "2026-05-20-telecoms.md" or "2026-05-20-Telecoms.md"
    filename_re = re.compile(rf"^(\d{{4}}-\d{{2}}-\d{{2}})-{re.escape(target_slug)}\.md$", re.IGNORECASE)

    # Group bullets by publisher (source_root)
    by_publisher: dict[str, list[str]] = {}
    publisher_sources: dict[str, str] = {}  # publisher_id → file path

    for f in sorted(newsletters_dir.iterdir()):
        if not f.is_file():
            continue
        m = filename_re.match(f.name)
        if not m:
            continue

        # Date filter
        file_date = date_cls.fromisoformat(m.group(1))
        if since and file_date < since:
            continue

        try:
            content = frontmatter.load(f).content or ""
        except Exception as e:  # noqa: BLE001
            log.warning("from-newsletters: failed to read %s: %s", f, e)
            continue

        for publisher, bullet in _extract_items(content):
            by_publisher.setdefault(publisher, []).append(bullet)
            publisher_sources[publisher] = str(f.relative_to(vault_root)).replace("\\", "/")

    for publisher, bullets in by_publisher.items():
        extracts.append(SectorExtract(
            sector=target_slug,
            source_type="newsletter",
            source_path=publisher_sources[publisher],
            source_root=f"publisher:{publisher.lower()}",
            claim_targets=_infer_targets(bullets),
            subsectors=["_all"],
            bullets=bullets[:5],  # cap bullets per publisher
            sensitivity="public",  # newsletters are public-source
            extracted_on=date_cls.today(),
            extracted_by="sector-extract from-newsletters",
        ))

    log.info("from-newsletters: %d extract(s) for sector=%s across %d publishers",
             len(extracts), target_slug, len(by_publisher))
    return extracts


# ── Helpers ──────────────────────────────────────────────────────────


_PUBLISHER_RE = re.compile(r"\(([^)]+),\s*\d{4}", re.IGNORECASE)
_BULLET_RE = re.compile(r"^\s*[-*]\s+(.+?)\s*$")


def _extract_items(content: str) -> list[tuple[str, str]]:
    """Pull (publisher, bullet) pairs from a newsletter body.

    Heuristic: any bullet that contains a "(<Publisher>, <year>" citation
    is attributed to that publisher. Bullets without an inline citation
    default to publisher='unknown' and are collapsed under that root.
    """
    pairs: list[tuple[str, str]] = []
    for line in content.splitlines():
        m = _BULLET_RE.match(line)
        if not m:
            continue
        bullet = m.group(1).strip()
        if len(bullet) < 25:
            continue
        pub_m = _PUBLISHER_RE.search(bullet)
        publisher = pub_m.group(1).strip() if pub_m else "unknown"
        pairs.append((publisher, bullet))
    return pairs


def _infer_targets(bullets: list[str]) -> list[str]:
    """Map bullets to likely claim files. Heuristic; LLM path supersedes."""
    targets: set[str] = set()
    for text in bullets:
        lower = text.lower()
        if any(k in lower for k in ("multiple", "ebitda", "ev/")):
            targets.add("Valuation")
        if any(k in lower for k in ("buyer", "acquir", "consolidat")):
            targets.add("Buyers")
        if any(k in lower for k in ("deal", "transaction", "announced")):
            targets.add("Comps")
        if any(k in lower for k in ("regulat", "approval", "ofcom")):
            targets.add("Regulatory")
    return sorted(targets) if targets else ["Dynamics"]
