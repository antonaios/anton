"""Extract sector-attributable claims from closed projects.

Walks `Projects/<X>/` where:
  - `00 Brief.md` frontmatter `sector:` matches target slug (case-insensitive)
  - `00 Brief.md` frontmatter `status:` in {won, lost, dead, closed}
  - `last_sector_extract:` is missing or older than `closed_on:`

For each matching project, the routine reads:
  - `13 Lessons Learned.md` (sector-attributable lessons)
  - `09 Decision Log.md` (decision rationales)
  - `12 Outputs/` (deliverables; for now we read only file titles)

…and produces SectorExtract objects with paraphrased bullets and claim
targets inferred from content. Paraphrase rule (§6.8 retained, §6.9
restated): never name the deal codename or counterparty inline; reference
[[Projects/<X>]] wikilink for provenance but generalise the claim text.

LLM (qwen3:14b local) does the paraphrase + claim-type classification.
Without Ollama, falls back to skip-llm mode that emits raw bullets with
no classification — still useful provenance, lower-quality extracts.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date as date_cls
from pathlib import Path
from typing import Iterable

import frontmatter

from routines.sectors.schema import SectorExtract, slugify_sector

log = logging.getLogger(__name__)


# Claim-type keywords for the deterministic skip-llm fallback. The LLM
# path supersedes this; this only fires when --skip-llm is set.
_CLAIM_KEYWORDS: dict[str, tuple[str, ...]] = {
    "valuation": ("multiple", "ebitda", "ev/", "x ebitdaal", "premium", "discount"),
    "buyers": ("buyer", "bidder", "acquirer", "consolidator", "trade", "private equity"),
    "comps": ("transaction", "deal", "precedent", "announced", "closed at"),
    "issues": ("dd", "due diligence", "red flag", "snag", "spa", "completion"),
    "regulatory": ("regulator", "approval", "merger control", "spectrum", "ofcom"),
    "competitive": ("market share", "competitor", "fragmentation", "share dynamics"),
    "metrics": ("kpi", "metric", "ebitdaal", "service revenue", "arpu", "occupancy"),
    "dynamics": ("cycle", "demand", "headwind", "tailwind", "trend"),
}


def gather(
    vault_root: Path,
    sector: str,
    *,
    since: date_cls | None = None,
    skip_llm: bool = False,
) -> list[SectorExtract]:
    """Walk projects for the target sector. Emit one extract per project."""
    target_slug = slugify_sector(sector)
    extracts: list[SectorExtract] = []
    projects_dir = vault_root / "Projects"
    if not projects_dir.is_dir():
        log.info("from-projects: no Projects/ at %s", projects_dir)
        return extracts

    for proj_dir in sorted(projects_dir.iterdir()):
        if not proj_dir.is_dir() or proj_dir.name.startswith("_"):
            continue
        brief = proj_dir / "00 Brief.md"
        if not brief.exists():
            continue
        try:
            meta = frontmatter.load(brief).metadata or {}
        except Exception as e:  # noqa: BLE001
            log.warning("from-projects: failed to read %s: %s", brief, e)
            continue

        if not _matches_sector(meta.get("sector"), target_slug):
            continue

        status = str(meta.get("status") or "").lower()
        if status not in ("won", "lost", "dead", "closed"):
            continue

        # Idempotency: skip if last_sector_extract is current relative to closed_on
        last_extract = meta.get("last_sector_extract")
        closed_on = meta.get("closed_on") or meta.get("closed")
        if last_extract and closed_on and str(last_extract) >= str(closed_on):
            log.debug("from-projects: %s already extracted (%s)", proj_dir.name, last_extract)
            continue

        lessons = proj_dir / "13 Lessons Learned.md"
        decisions = proj_dir / "09 Decision Log.md"

        bullets = []
        if lessons.exists():
            bullets += _parse_bullets(lessons)
        if decisions.exists():
            bullets += _parse_bullets(decisions)

        if not bullets:
            continue

        # Source root = deal codename (one extract per project, regardless of how many bullets)
        source_root = f"project:{proj_dir.name}"

        # Paraphrase + classify: LLM in the normal path; skip-llm uses keyword fallback
        if skip_llm:
            claim_targets, paraphrased = _keyword_classify(bullets)
        else:
            # LLM call deferred to common helper; for now, return raw bullets
            # with deterministic classification. Full LLM integration in Phase 4
            # synthesizer where it's the main load-bearing call.
            claim_targets, paraphrased = _keyword_classify(bullets)

        if not paraphrased:
            continue

        extracts.append(SectorExtract(
            sector=target_slug,
            source_type="project",
            source_path=str(brief.relative_to(vault_root)).replace("\\", "/"),
            source_root=source_root,
            claim_targets=sorted(claim_targets),
            subsectors=_extract_subsectors(meta),
            bullets=paraphrased,
            sensitivity=str(meta.get("sensitivity") or "confidential"),
            extracted_on=date_cls.today(),
            extracted_by="sector-extract from-projects",
        ))

    log.info("from-projects: %d extract(s) for sector=%s", len(extracts), target_slug)
    return extracts


# ── Helpers ──────────────────────────────────────────────────────────


def _matches_sector(field_value, target_slug: str) -> bool:
    """Check whether a `sector:` frontmatter field matches the target slug.

    Accepts either:
        sector: "[[Sectors/telecoms/_Index]]"
        sector: telecoms
        sector: Telecoms
    """
    if not field_value:
        return False
    s = str(field_value).lower()
    # extract slug from wikilink form
    m = re.search(r"sectors/([a-z0-9-]+)", s)
    if m:
        return m.group(1) == target_slug
    return s.strip().replace(" ", "-") == target_slug


def _parse_bullets(path: Path) -> list[str]:
    """Pull all top-level bullets from a markdown file."""
    out: list[str] = []
    bullet_re = re.compile(r"^\s*[-*]\s+(.+?)\s*$")
    try:
        content = frontmatter.load(path).content or ""
    except Exception:
        return out
    for line in content.splitlines():
        m = bullet_re.match(line)
        if m:
            text = m.group(1).strip()
            if len(text) >= 20:  # skip short list items
                out.append(text)
    return out


def _extract_subsectors(meta: dict) -> list[str]:
    """Pull subsector list from project brief frontmatter."""
    sub = meta.get("subsector") or meta.get("subsectors")
    if not sub:
        return ["_all"]
    if isinstance(sub, str):
        return [sub.lower().replace(" ", "-")]
    if isinstance(sub, list):
        return [str(s).lower().replace(" ", "-") for s in sub]
    return ["_all"]


def _keyword_classify(bullets: Iterable[str]) -> tuple[set[str], list[str]]:
    """Deterministic classifier — used when --skip-llm or as LLM fallback.

    Returns (claim_targets, paraphrased_bullets). Paraphrase here is a no-op;
    full paraphrasing requires LLM. Routine documents this limitation in
    the proposal output so operator knows what to review.
    """
    targets: set[str] = set()
    paraphrased = list(bullets)
    for text in bullets:
        lower = text.lower()
        for claim_type, keywords in _CLAIM_KEYWORDS.items():
            if any(kw in lower for kw in keywords):
                targets.add(claim_type.capitalize())
                break
    if not targets:
        # No keyword match — still record as dynamics (catch-all)
        targets.add("Dynamics")
    return targets, paraphrased
