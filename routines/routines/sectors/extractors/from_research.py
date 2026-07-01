"""Extract sector-attributable claims from operator research notes.

Walks:
  - `Projects/<X>/05 Research/*.md` where project sector matches target
  - `Topics/<sector-related>/*.md`
  - `Inbox/Documents/*.md` (PDF intake output) where frontmatter
    `sector:` matches target

Each research note contributes one SectorExtract. Source-root key =
publisher_id if the research is attributed to an external publisher
(e.g. "FT", "Bloomberg"); operator's own analytical work counts as
`manual_operator_note` (capped at one contribution).
"""

from __future__ import annotations

import logging
import re
from datetime import date as date_cls
from pathlib import Path

import frontmatter

from routines.sectors.schema import SectorExtract, slugify_sector

log = logging.getLogger(__name__)

_BULLET_RE = re.compile(r"^\s*[-*]\s+(.+?)\s*$")


def gather(
    vault_root: Path,
    sector: str,
    *,
    since: date_cls | None = None,
    skip_llm: bool = False,
) -> list[SectorExtract]:
    target_slug = slugify_sector(sector)
    extracts: list[SectorExtract] = []

    # 1. Projects/<X>/05 Research/*.md where project sector matches
    projects_dir = vault_root / "Projects"
    if projects_dir.is_dir():
        for proj_dir in sorted(projects_dir.iterdir()):
            if not proj_dir.is_dir() or proj_dir.name.startswith("_"):
                continue
            brief = proj_dir / "00 Brief.md"
            if not brief.exists():
                continue
            try:
                meta = frontmatter.load(brief).metadata or {}
            except Exception:
                continue
            if _slug_from_field(meta.get("sector")) != target_slug:
                continue
            research_dir = proj_dir / "05 Research"
            if not research_dir.is_dir():
                continue
            for f in sorted(research_dir.iterdir()):
                if f.is_file() and f.name.endswith(".md"):
                    extracts.extend(_extract_from_file(f, vault_root, target_slug, since))

    # 2. Inbox/Documents/*.md where frontmatter sector matches target
    inbox_dir = vault_root / "Inbox" / "Documents"
    if inbox_dir.is_dir():
        for f in sorted(inbox_dir.iterdir()):
            if f.is_file() and f.name.endswith(".md"):
                try:
                    meta = frontmatter.load(f).metadata or {}
                except Exception:
                    continue
                if _slug_from_field(meta.get("sector")) == target_slug:
                    extracts.extend(_extract_from_file(f, vault_root, target_slug, since))

    log.info("from-research: %d extract(s) for sector=%s", len(extracts), target_slug)
    return extracts


def _extract_from_file(f: Path, vault_root: Path, target_slug: str, since: date_cls | None) -> list[SectorExtract]:
    try:
        post = frontmatter.load(f)
    except Exception as e:  # noqa: BLE001
        log.warning("from-research: failed to read %s: %s", f, e)
        return []
    if since:
        file_date = date_cls.fromtimestamp(f.stat().st_mtime)
        if file_date < since:
            return []
    content = post.content or ""
    bullets = [m.group(1).strip()
               for line in content.splitlines()
               for m in [_BULLET_RE.match(line)] if m and len(m.group(1).strip()) >= 25]
    if not bullets:
        return []

    # Determine source_root: external publisher if frontmatter says so,
    # otherwise "manual_operator_note"
    publisher = post.metadata.get("publisher") or post.metadata.get("source_publisher")
    if publisher:
        source_root = f"publisher:{str(publisher).lower()}"
    else:
        source_root = "manual_operator_note"

    return [SectorExtract(
        sector=target_slug,
        source_type="research",
        source_path=str(f.relative_to(vault_root)).replace("\\", "/"),
        source_root=source_root,
        claim_targets=_infer_targets(bullets),
        subsectors=["_all"],
        bullets=bullets[:6],
        sensitivity=str(post.metadata.get("sensitivity") or "internal"),
        extracted_on=date_cls.today(),
        extracted_by="sector-extract from-research",
    )]


def _slug_from_field(field_value) -> str | None:
    if not field_value:
        return None
    s = str(field_value).lower()
    m = re.search(r"sectors/([a-z0-9-]+)", s)
    if m:
        return m.group(1)
    # F-21: canonical slugifier for the name-fallback (see from_bd._slug_from_field).
    from routines.sectors.schema import slugify_sector
    return slugify_sector(s)


def _infer_targets(bullets: list[str]) -> list[str]:
    targets: set[str] = set()
    for text in bullets:
        lower = text.lower()
        if any(k in lower for k in ("multiple", "ebitda", "valuation", "ev/")):
            targets.add("Valuation")
        if any(k in lower for k in ("buyer", "consolidat", "acquir")):
            targets.add("Buyers")
        if any(k in lower for k in ("regulat", "policy", "directive")):
            targets.add("Regulatory")
        if any(k in lower for k in ("kpi", "metric", "ratio")):
            targets.add("Metrics")
        if any(k in lower for k in ("competitor", "market share", "fragment")):
            targets.add("Competitive")
    return sorted(targets) if targets else ["Dynamics"]
