"""Extract sector-attributable claims from meeting notes (HiNotes).

Walks `Projects/<X>/02 Meeting Notes/*.md` where:
  - frontmatter `sector:` matches target slug OR project brief sector matches
  - file mtime is within extraction window (default: last 24h)

For each matching meeting note, parses operator-flagged sections —
typically "Key takeaways", "Decisions", "Open questions" — and emits
one SectorExtract per attendee/person source.

Source-root key = person_name (one extract per person per run; multiple
meetings with the same person collapse to one weight=2 contribution).
"""

from __future__ import annotations

import logging
import re
from datetime import date as date_cls
from datetime import datetime
from pathlib import Path

import frontmatter

from routines.sectors.schema import SectorExtract, slugify_sector

log = logging.getLogger(__name__)


# Sections to extract from (case-insensitive substring match on heading)
_TARGET_SECTIONS = (
    "key takeaways",
    "decisions",
    "open questions",
    "actions",
    "highlights",
)

_HEADING_RE = re.compile(r"^\s*#{2,4}\s+(.+?)\s*$")
_BULLET_RE = re.compile(r"^\s*[-*]\s+(.+?)\s*$")
_WIKILINK_RE = re.compile(r"\[\[People/([^\]]+?)\]\]")


def gather(
    vault_root: Path,
    sector: str,
    *,
    since: date_cls | None = None,
    skip_llm: bool = False,
) -> list[SectorExtract]:
    """Walk meeting notes; emit extracts grouped by person source root."""
    target_slug = slugify_sector(sector)
    extracts: list[SectorExtract] = []
    projects_dir = vault_root / "Projects"
    if not projects_dir.is_dir():
        return extracts

    by_person: dict[str, list[str]] = {}
    person_meeting_paths: dict[str, str] = {}

    for proj_dir in sorted(projects_dir.iterdir()):
        if not proj_dir.is_dir() or proj_dir.name.startswith("_"):
            continue

        # Resolve project's default sector from 00 Brief.md
        brief = proj_dir / "00 Brief.md"
        project_sector = _project_sector(brief) if brief.exists() else None
        if project_sector and project_sector != target_slug:
            # Project sector mismatch — skip unless individual meeting overrides
            project_match = False
        else:
            project_match = project_sector == target_slug

        meeting_dir = proj_dir / "02 Meeting Notes"
        if not meeting_dir.is_dir():
            continue

        for f in sorted(meeting_dir.iterdir()):
            if not f.is_file() or not f.name.endswith(".md"):
                continue

            try:
                post = frontmatter.load(f)
            except Exception as e:  # noqa: BLE001
                log.warning("from-meetings: failed to read %s: %s", f, e)
                continue

            meeting_sector = _slug_from_field(post.metadata.get("sector"))
            if meeting_sector and meeting_sector != target_slug:
                continue
            if not meeting_sector and not project_match:
                continue

            # Date filter via file mtime (HiNotes uses filenames not frontmatter dates always)
            if since:
                file_date = date_cls.fromtimestamp(f.stat().st_mtime)
                if file_date < since:
                    continue

            # MNPI skip
            if str(post.metadata.get("sensitivity") or "").lower() == "mnpi":
                log.debug("from-meetings: skipping MNPI file %s", f.name)
                continue

            content = post.content or ""
            bullets_with_attendees = _extract_section_bullets(content)
            attendees = _extract_attendees(post.metadata, content)

            # If no attendees found, attribute to the meeting's filename as fallback root
            if not attendees:
                attendees = [f.stem]

            for person in attendees:
                key = person.strip()
                by_person.setdefault(key, []).extend(bullets_with_attendees)
                person_meeting_paths.setdefault(key,
                    str(f.relative_to(vault_root)).replace("\\", "/"))

    for person, bullets in by_person.items():
        if not bullets:
            continue
        extracts.append(SectorExtract(
            sector=target_slug,
            source_type="meeting",
            source_path=person_meeting_paths[person],
            source_root=f"person:{person.lower()}",
            claim_targets=_infer_targets(bullets),
            subsectors=["_all"],
            bullets=bullets[:8],
            sensitivity="confidential",
            extracted_on=date_cls.today(),
            extracted_by="sector-extract from-meetings",
        ))

    log.info("from-meetings: %d extract(s) for sector=%s across %d people",
             len(extracts), target_slug, len(by_person))
    return extracts


# ── Helpers ──────────────────────────────────────────────────────────


def _project_sector(brief: Path) -> str | None:
    try:
        meta = frontmatter.load(brief).metadata or {}
    except Exception:
        return None
    return _slug_from_field(meta.get("sector"))


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


def _extract_section_bullets(content: str) -> list[str]:
    """Pull bullets from sections whose heading matches target patterns."""
    out: list[str] = []
    in_target_section = False
    for line in content.splitlines():
        hm = _HEADING_RE.match(line)
        if hm:
            heading_lower = hm.group(1).lower()
            in_target_section = any(t in heading_lower for t in _TARGET_SECTIONS)
            continue
        if not in_target_section:
            continue
        bm = _BULLET_RE.match(line)
        if bm:
            text = bm.group(1).strip()
            if len(text) >= 20:
                out.append(text)
    return out


def _extract_attendees(meta: dict, content: str) -> list[str]:
    """Find people via frontmatter `attendees:` or `[[People/X]]` wikilinks."""
    out: list[str] = []
    attendees = meta.get("attendees") or meta.get("participants")
    if isinstance(attendees, list):
        out.extend(str(a) for a in attendees)
    elif isinstance(attendees, str):
        out.append(attendees)
    # Also pull wikilinks
    for m in _WIKILINK_RE.finditer(content):
        person = m.group(1).strip()
        if person not in out:
            out.append(person)
    return out


def _infer_targets(bullets: list[str]) -> list[str]:
    targets: set[str] = set()
    for text in bullets:
        lower = text.lower()
        if any(k in lower for k in ("buyer", "bidder", "interest", "appetite")):
            targets.add("Buyers")
        if any(k in lower for k in ("multiple", "ebitda", "valuation")):
            targets.add("Valuation")
        if any(k in lower for k in ("dd", "diligence", "red flag", "issue")):
            targets.add("Issues")
        if any(k in lower for k in ("market", "competitor", "share")):
            targets.add("Competitive")
        if any(k in lower for k in ("regulat", "approval", "ofcom")):
            targets.add("Regulatory")
    return sorted(targets) if targets else ["Dynamics"]
