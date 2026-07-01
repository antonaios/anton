"""Suggest relevant Registers/Lessons.md entries for a project's brief.

Deterministic matcher — no LLM call. For each entry in
``Registers/Lessons.md``, derive its sector context by walking the
``First seen:`` project's ``00 Brief.md`` frontmatter, then rank
against the target project's sector / subsector / industry. Output
markdown bullets ready to paste into §6 "From prior similar deals" of
the target brief.

Used by ``lessons-learned suggest --project <X>`` and (later) by the
dashboard's project-brief-create flow.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import frontmatter

log = logging.getLogger(__name__)


REGISTER_RELPATH = "Registers/Lessons.md"
BRIEF_FILENAME = "00 Brief.md"

# Match scores — higher = more relevant.
_SCORE_SUBSECTOR = 3   # same subsector
_SCORE_SECTOR = 2      # same sector, different subsector
_SCORE_INDUSTRY = 1    # same industry, different sector
_SCORE_AGNOSTIC = 1    # lesson has no sector context (cross-cutting)


@dataclass
class LessonEntry:
    """One ## lesson-* entry from Registers/Lessons.md."""
    slug: str                       # "lesson-skill-fallback-markdown"
    title: str                      # text after the em-dash in the heading
    body: str                       # the bullet-list body
    first_seen_projects: list[str]  # projects parsed from `First seen:` wikilinks
    # Sector context derived from the first-seen project(s)
    industries: list[str] = None    # type: ignore[assignment]
    sectors: list[str] = None       # type: ignore[assignment]
    subsectors: list[str] = None    # type: ignore[assignment]

    def __post_init__(self):
        self.industries = self.industries or []
        self.sectors = self.sectors or []
        self.subsectors = self.subsectors or []


@dataclass
class Suggestion:
    lesson: LessonEntry
    score: int                      # 0–3 (see _SCORE_* constants)
    reason: str                     # human-readable why-it-matched


# ── Public API ────────────────────────────────────────────────────────────


def suggest_for_project(
    vault_root: Path, project: str, *, limit: int = 10,
) -> list[Suggestion]:
    """Return relevant lessons for ``Projects/<project>/00 Brief.md``."""
    brief_meta = _load_brief_metadata(vault_root, project)
    if brief_meta is None:
        raise FileNotFoundError(
            f"Project brief not found: Projects/{project}/{BRIEF_FILENAME}"
        )
    return suggest(vault_root, **brief_meta, limit=limit)


def suggest(
    vault_root: Path, *,
    industry: str | None = None,
    sector: str | None = None,
    subsector: str | None = None,
    limit: int = 10,
) -> list[Suggestion]:
    """Rank lessons against an explicit (industry, sector, subsector) tuple."""
    entries = _parse_register(vault_root)
    _annotate_with_sector_context(entries, vault_root)

    target_industry = _norm(industry)
    target_sector = _norm(sector)
    target_subsector = _norm(subsector)

    suggestions: list[Suggestion] = []
    for entry in entries:
        score, reason = _score_entry(
            entry,
            target_industry=target_industry,
            target_sector=target_sector,
            target_subsector=target_subsector,
        )
        if score > 0:
            suggestions.append(Suggestion(lesson=entry, score=score, reason=reason))

    suggestions.sort(key=lambda s: (-s.score, s.lesson.slug))
    return suggestions[:limit]


def render_brief_bullets(suggestions: list[Suggestion]) -> str:
    """Render suggestions as markdown bullets ready to paste into the brief
    under §6 → "From prior similar deals"."""
    if not suggestions:
        return "_(no matching lessons in `Registers/Lessons.md` yet — leave this subsection empty or populate manually as deals close)_"
    lines = []
    for s in suggestions:
        # Strip trailing punctuation from the title for a clean read.
        # Use ASCII `->` rather than `→` so the renderer's output prints
        # cleanly on Windows cp1252 terminals; Markdown is identical.
        title = s.lesson.title.rstrip(". ")
        lines.append(
            f"- {title} -> [[Registers/Lessons#{s.lesson.slug}]] "
            f"_(matched: {s.reason})_"
        )
    return "\n".join(lines)


# ── Internals — register parser ───────────────────────────────────────────


# Heading shape: "## lesson-skill-fallback-markdown — Fallback to Markdown..."
_HEADING_RE = re.compile(r"^##\s+(lesson-[A-Za-z0-9-]+)\s*(?:—|--|-)?\s*(.*?)\s*$")
_FIRST_SEEN_RE = re.compile(r"\*\*First seen:\*\*\s*(.+?)(?:\n|$)")
_PROJECT_LINK_RE = re.compile(r"\[\[Projects/([^\]\|#]+)(?:[#\|][^\]]*)?\]\]")


def _parse_register(vault_root: Path) -> list[LessonEntry]:
    path = vault_root / REGISTER_RELPATH
    if not path.exists():
        log.info("lessons.suggest: %s does not exist; no entries to rank", path)
        return []
    try:
        post = frontmatter.load(path)
    except Exception as e:  # noqa: BLE001
        log.warning("lessons.suggest: parse %s failed: %s", path, e)
        return []
    body = post.content or ""

    # Split body by `## lesson-...` headings. Track fenced code blocks
    # (``` ... ```) so the register's own schema example (which contains
    # a `## lesson-id — {short title}` placeholder inside a code fence)
    # doesn't get treated as a real entry.
    entries: list[LessonEntry] = []
    current_slug: str | None = None
    current_title: str = ""
    current_body: list[str] = []
    in_code_block: bool = False

    for line in body.splitlines():
        # Fence toggle. Any line starting with ``` flips the state.
        if line.lstrip().startswith("```"):
            in_code_block = not in_code_block
            if current_slug is not None:
                current_body.append(line)
            continue

        if in_code_block:
            if current_slug is not None:
                current_body.append(line)
            continue

        m = _HEADING_RE.match(line)
        if m:
            if current_slug is not None:
                entries.append(_finalise_entry(current_slug, current_title, current_body))
            current_slug = m.group(1)
            current_title = m.group(2).strip() or current_slug
            current_body = []
        elif current_slug is not None:
            current_body.append(line)

    if current_slug is not None:
        entries.append(_finalise_entry(current_slug, current_title, current_body))

    return entries


def _finalise_entry(slug: str, title: str, body_lines: list[str]) -> LessonEntry:
    body_text = "\n".join(body_lines).strip()
    first_seen_projects = _extract_projects(body_text)
    return LessonEntry(
        slug=slug, title=title, body=body_text,
        first_seen_projects=first_seen_projects,
    )


def _extract_projects(body: str) -> list[str]:
    m = _FIRST_SEEN_RE.search(body)
    search_block = m.group(1) if m else body
    projects = _PROJECT_LINK_RE.findall(search_block)
    seen: set[str] = set()
    out: list[str] = []
    for p in projects:
        p = p.strip()
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return out


# ── Internals — sector context resolution ─────────────────────────────────


def _annotate_with_sector_context(entries: list[LessonEntry], vault_root: Path) -> None:
    """For each entry, walk its first-seen project(s) and union their
    industry / sector / subsector tags into the entry."""
    brief_cache: dict[str, dict[str, str | None]] = {}

    for entry in entries:
        for proj in entry.first_seen_projects:
            if proj not in brief_cache:
                brief_cache[proj] = _load_brief_metadata(vault_root, proj) or {}
            meta = brief_cache[proj]
            for key, target in (
                ("industry", entry.industries),
                ("sector", entry.sectors),
                ("subsector", entry.subsectors),
            ):
                val = _norm(meta.get(key))
                if val and val not in target:
                    target.append(val)


def _load_brief_metadata(vault_root: Path, project: str) -> Optional[dict]:
    path = vault_root / "Projects" / project / BRIEF_FILENAME
    if not path.exists():
        return None
    try:
        post = frontmatter.load(path)
    except Exception as e:  # noqa: BLE001
        log.warning("lessons.suggest: parse %s failed: %s", path, e)
        return None
    return {
        "industry": post.metadata.get("industry"),
        "sector": post.metadata.get("sector"),
        "subsector": post.metadata.get("subsector"),
    }


# ── Internals — scoring ───────────────────────────────────────────────────


def _score_entry(
    entry: LessonEntry, *,
    target_industry: str,
    target_sector: str,
    target_subsector: str,
) -> tuple[int, str]:
    if target_subsector and target_subsector in [_norm(s) for s in entry.subsectors]:
        return _SCORE_SUBSECTOR, f"same subsector ({target_subsector})"
    if target_sector and target_sector in [_norm(s) for s in entry.sectors]:
        return _SCORE_SECTOR, f"same sector ({target_sector})"
    if target_industry and target_industry in [_norm(i) for i in entry.industries]:
        return _SCORE_INDUSTRY, f"same industry ({target_industry})"
    # Sector-agnostic lessons (no sector context derived from first-seen
    # project) are always relevant at low priority.
    if not entry.sectors and not entry.subsectors and not entry.industries:
        return _SCORE_AGNOSTIC, "sector-agnostic / cross-cutting"
    return 0, ""


# ── String normalisation ──────────────────────────────────────────────────


_WIKILINK_RE = re.compile(r"\[\[(?:[^\]\|]*?/)?([^\]\|]+?)\]\]")


def _norm(s: str | None) -> str:
    """Lowercase + strip whitespace + strip Obsidian wikilink wrapper."""
    if s is None:
        return ""
    s = str(s).strip()
    # Strip [[Sectors/Telecoms]] -> "Telecoms"
    m = _WIKILINK_RE.match(s)
    if m:
        s = m.group(1).strip()
    return s.lower()
