"""Walk projects + archive for ``13 Lessons Learned.md`` files.

Parser is intentionally lenient — the template only suggests sections
(``## What worked``, ``## What didn't``, ``## What we'd do differently``,
``## Patterns worth promoting``); operators may add or rename sections.
We capture every bullet under any section header and let the synthesise
step do the semantic grouping.

Bullets directly under a heading containing "pattern" or "promot" are
extracted as ``LessonPattern`` objects (Mode A — already operator-flagged
as register-worthy). All other bullets become ``LessonItem`` instances
that feed the cross-project clusterer (Mode B).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import frontmatter

from routines.lessons.schema import LessonItem, LessonPattern

log = logging.getLogger(__name__)


LESSONS_FILENAME = "13 Lessons Learned.md"


@dataclass
class LessonsBundle:
    items: list[LessonItem] = field(default_factory=list)
    patterns: list[LessonPattern] = field(default_factory=list)
    projects_scanned: list[str] = field(default_factory=list)
    closed_count: int = 0
    open_count: int = 0


def gather_lessons(vault_root: Path) -> LessonsBundle:
    """Walk ``Projects/<X>/13 Lessons Learned.md`` and ``Archive/<X>/13 Lessons Learned.md``."""
    out = LessonsBundle()

    for base, archived in [(vault_root / "Projects", False), (vault_root / "Archive", True)]:
        if not base.is_dir():
            continue
        for proj_dir in sorted(base.iterdir()):
            if not proj_dir.is_dir() or proj_dir.name.startswith("_"):
                continue
            lessons_path = proj_dir / LESSONS_FILENAME
            if not lessons_path.exists():
                continue
            items, patterns, status, sensitivity = _parse_lessons_file(
                lessons_path,
                project=proj_dir.name,
                archived=archived,
            )
            out.items.extend(items)
            out.patterns.extend(patterns)
            out.projects_scanned.append(proj_dir.name)
            if status == "closed" or archived:
                out.closed_count += 1
            else:
                out.open_count += 1
            _ = sensitivity  # surfaced on each LessonItem/Pattern; bundle-level not needed

    return out


# ── Per-file parser ───────────────────────────────────────────────────────


_HEADING_RE = re.compile(r"^\s*(#{1,6})\s+(.+?)\s*$")
_BULLET_RE = re.compile(r"^\s*[-*]\s+(.+?)\s*$")
_PATTERN_HEADING_RE = re.compile(r"pattern|promot", re.IGNORECASE)
_REGISTER_LINK_RE = re.compile(r"\[\[Registers/Lessons[^\]]*\]\]")
_SLUG_RE = re.compile(r"`(lesson-[a-z0-9-]+)`")


def _parse_lessons_file(
    path: Path, *, project: str, archived: bool,
) -> tuple[list[LessonItem], list[LessonPattern], str, str]:
    """Return (items, patterns, project_status, sensitivity)."""
    try:
        post = frontmatter.load(path)
    except Exception as e:  # noqa: BLE001
        log.warning("lessons: parse %s failed: %s", path, e)
        return [], [], "unknown", "internal"

    status = str(post.metadata.get("status") or "unknown").lower()
    sensitivity = str(post.metadata.get("sensitivity") or "internal").lower()
    body = post.content or ""

    items: list[LessonItem] = []
    patterns: list[LessonPattern] = []
    current_section: str = ""
    current_is_pattern_section: bool = False

    for raw_line in body.splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            continue

        m_head = _HEADING_RE.match(line)
        if m_head:
            heading_text = m_head.group(2).strip()
            current_section = heading_text
            current_is_pattern_section = bool(_PATTERN_HEADING_RE.search(heading_text))
            continue

        m_bullet = _BULLET_RE.match(line)
        if not m_bullet:
            continue
        text = m_bullet.group(1).strip()
        if not text or text.startswith("{") and text.endswith("}"):
            continue   # template placeholder

        if current_is_pattern_section:
            slug_match = _SLUG_RE.search(text)
            cleaned = _REGISTER_LINK_RE.sub("", text).strip().rstrip("→-:., ")
            if cleaned.startswith("{") or not cleaned:
                continue   # template placeholder, e.g. "{Pattern} →"
            patterns.append(LessonPattern(
                project=project,
                project_status="closed" if archived else status,
                text=cleaned,
                proposed_slug=slug_match.group(1) if slug_match else None,
                sensitivity=sensitivity,
            ))
        else:
            items.append(LessonItem(
                project=project,
                project_status="closed" if archived else status,
                archived=archived,
                section=current_section or "(no section)",
                text=text,
                sensitivity=sensitivity,
            ))

    return items, patterns, status, sensitivity
