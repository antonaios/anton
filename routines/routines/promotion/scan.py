"""Scanners that find promotion / compaction candidates in the vault."""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import frontmatter

from routines.shared.vault_writer import VaultPaths

logger = logging.getLogger(__name__)


# ============================================================ data classes


@dataclass
class DuplicateDecision:
    """Same decision text appearing in 2+ notes."""
    decision_text: str
    sources: list[str]              # vault-relative paths


@dataclass
class StaleAction:
    """An open `- [ ] ...` checkbox older than the threshold."""
    action_text: str
    note_path: str                   # vault-relative
    days_stale: int


@dataclass
class LessonCandidate:
    """A bullet from a project's 'Patterns worth promoting' section."""
    pattern_text: str
    note_path: str


@dataclass
class ProjectScan:
    """Aggregate scan output for one project."""
    project: str
    duplicate_decisions: list[DuplicateDecision] = field(default_factory=list)
    stale_actions: list[StaleAction] = field(default_factory=list)
    lesson_candidates: list[LessonCandidate] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (self.duplicate_decisions or self.stale_actions or self.lesson_candidates)


# ============================================================ regexes


# Match `- [ ] <text>` or `- [ ] <text> *(owner: x, due: y)*`
_OPEN_ACTION = re.compile(r"^\s*-\s*\[\s*\]\s*(.+)$", re.MULTILINE)
# Match decision bullets in either Decision Log style or meeting-note style:
#   "- **Decision:** <text>"
#   "- <text>"  under "## Decisions" section
_DECISION_BULLET = re.compile(r"^\s*-\s*\*\*Decision:\*\*\s*(.+)$", re.MULTILINE)


# ============================================================ scan one project


def scan_project(
    project: str,
    *,
    paths: VaultPaths,
    stale_threshold_days: int = 30,
) -> ProjectScan:
    """Scan one Projects/<project>/ folder for promotion/compaction candidates."""
    proj_dir = paths.projects / project
    out = ProjectScan(project=project)
    if not proj_dir.exists():
        logger.warning("project dir missing: %s", proj_dir)
        return out

    # --- duplicate decisions ---
    decision_to_sources: dict[str, list[str]] = defaultdict(list)
    today_ts = datetime.now()

    for md in proj_dir.rglob("*.md"):
        rel = str(md.relative_to(paths.root).as_posix())
        try:
            text = md.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue

        for m in _DECISION_BULLET.finditer(text):
            decision = m.group(1).strip().rstrip(".")[:200]
            decision_norm = decision.lower()
            if decision_norm and decision_norm not in {s.lower() for s in decision_to_sources.get(decision, [])}:
                decision_to_sources[decision].append(rel)

        # --- stale open actions in meeting notes ---
        if "02 Meeting Notes" in str(md):
            try:
                fm = frontmatter.loads(text)
                meeting_date_raw = fm.metadata.get("date")
                meeting_date = _parse_date(meeting_date_raw)
            except Exception:
                meeting_date = None
            if meeting_date:
                days_stale = (today_ts.date() - meeting_date).days
                if days_stale > stale_threshold_days:
                    for m in _OPEN_ACTION.finditer(text):
                        action_text = m.group(1).strip().rstrip(".")[:200]
                        out.stale_actions.append(StaleAction(
                            action_text=action_text,
                            note_path=rel,
                            days_stale=days_stale,
                        ))

    # Keep only decisions appearing 2+ times
    for decision, sources in decision_to_sources.items():
        if len(sources) >= 2:
            out.duplicate_decisions.append(DuplicateDecision(
                decision_text=decision, sources=sources,
            ))

    # --- lesson candidates from "13 Lessons Learned.md" ---
    lessons_md = proj_dir / "13 Lessons Learned.md"
    if lessons_md.exists():
        out.lesson_candidates = _extract_lesson_candidates(
            lessons_md.read_text(encoding="utf-8"),
            note_path=str(lessons_md.relative_to(paths.root).as_posix()),
        )

    return out


def _extract_lesson_candidates(text: str, *, note_path: str) -> list[LessonCandidate]:
    """Parse the 'Patterns worth promoting' section of a Lessons Learned note."""
    out: list[LessonCandidate] = []
    in_section = False
    for line in text.splitlines():
        if line.strip().startswith("## "):
            in_section = "patterns" in line.lower() and "promot" in line.lower()
            continue
        if in_section and line.strip().startswith("-"):
            bullet = line.strip().lstrip("-").strip()
            # Skip placeholder bullets
            if bullet and not bullet.startswith("*(") and not bullet.startswith("{"):
                out.append(LessonCandidate(
                    pattern_text=bullet[:300],
                    note_path=note_path,
                ))
    return out


def _parse_date(raw: Any) -> date | None:
    if isinstance(raw, date) and not isinstance(raw, datetime):
        return raw
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, str):
        try:
            return datetime.strptime(raw[:10], "%Y-%m-%d").date()
        except ValueError:
            return None
    return None
