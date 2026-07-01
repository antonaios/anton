"""Extract sector-attributable claims from BD activity.

Walks `Companies/<X>.md` where:
  - `sector:` resolves to target slug (either directly or via wikilink)
  - `bd_state` is set to any of: watching | engaged | dormant | dead | won | lost
  - `bd_notes_updated_on:` is newer than the last extraction watermark
    (or, simpler: just take all current bd_notes content each run and
    let the synthesizer dedupe via source_root)

Source-root key = company. Multiple BD-note updates for same company
collapse to one weight=2 contribution.

Prerequisites: Phase 5 BD layer must be live (adds bd_state +
bd_last_contact + bd_notes fields to Companies/<X>.md). Until then this
extractor is dormant — emits nothing because no companies have bd_state.
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
    target_slug = slugify_sector(sector)
    extracts: list[SectorExtract] = []
    companies_dir = vault_root / "Companies"
    if not companies_dir.is_dir():
        return extracts

    for f in sorted(companies_dir.iterdir()):
        if not f.is_file() or not f.name.endswith(".md"):
            continue
        try:
            post = frontmatter.load(f)
        except Exception as e:  # noqa: BLE001
            log.warning("from-bd: failed to read %s: %s", f, e)
            continue
        meta = post.metadata or {}

        # Sector match
        if _slug_from_field(meta.get("sector")) != target_slug:
            continue

        # BD state check — Phase 5 prerequisite
        bd_state = meta.get("bd_state")
        if not bd_state:
            continue

        bd_notes = meta.get("bd_notes")
        if not bd_notes:
            continue

        # MNPI skip
        if str(meta.get("sensitivity") or "").lower() == "mnpi":
            log.debug("from-bd: skipping MNPI company %s", f.name)
            continue

        # Convert bd_notes to bullets
        if isinstance(bd_notes, str):
            bullets = [b.strip() for b in bd_notes.split("\n")
                       if b.strip() and len(b.strip()) >= 20]
        elif isinstance(bd_notes, list):
            bullets = [str(b).strip() for b in bd_notes if len(str(b).strip()) >= 20]
        else:
            continue

        if not bullets:
            continue

        company_name = f.stem
        extracts.append(SectorExtract(
            sector=target_slug,
            source_type="bd",
            source_path=str(f.relative_to(vault_root)).replace("\\", "/"),
            source_root=f"company:{company_name.lower()}",
            claim_targets=_infer_targets(bullets, bd_state),
            subsectors=["_all"],
            bullets=bullets,
            sensitivity="confidential",
            extracted_on=date_cls.today(),
            extracted_by="sector-extract from-bd",
        ))

    log.info("from-bd: %d extract(s) for sector=%s", len(extracts), target_slug)
    return extracts


def _slug_from_field(field_value) -> str | None:
    if not field_value:
        return None
    s = str(field_value).lower()
    m = re.search(r"sectors/([a-z0-9-]+)", s)
    if m:
        return m.group(1)
    # F-21: the name-fallback now uses the canonical slugifier (handles ``_``
    # → ``-`` + strip) so it agrees with the folder the sectors writer creates.
    from routines.sectors.schema import slugify_sector
    return slugify_sector(s)


def _infer_targets(bullets: list[str], bd_state: str) -> list[str]:
    targets: set[str] = set()
    # BD notes typically inform Buyers + Dynamics + occasionally Issues
    targets.add("Buyers")
    for text in bullets:
        lower = text.lower()
        if any(k in lower for k in ("multiple", "ebitda", "valuation")):
            targets.add("Valuation")
        if any(k in lower for k in ("dd", "issue", "concern", "red flag")):
            targets.add("Issues")
        if any(k in lower for k in ("market", "share", "competitor")):
            targets.add("Competitive")
    return sorted(targets)
