"""Schemas for the cross-project lessons routine."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class LessonItem:
    """One bullet from a project's ``13 Lessons Learned.md``."""
    project: str                           # "DemoTarget"
    project_status: str                    # "open" | "closed" | "unknown"
    archived: bool                         # True if file lives under Archive/
    section: str                           # "What worked" | "What didn't" | "What we'd do differently" | ...
    text: str
    sensitivity: str = "internal"          # propagated from project frontmatter


@dataclass
class LessonPattern:
    """An explicit "## Patterns worth promoting" entry from a project's lessons.

    Already flagged by the operator as worth promoting to
    ``Registers/Lessons.md`` — these are the high-confidence input to
    the proposal, distinct from bare bullets that need clustering."""
    project: str
    project_status: str
    text: str                              # the bullet body, sans the `[[Registers/Lessons]]` link
    proposed_slug: str | None = None       # e.g. "lesson-skill-fallback-markdown"
    sensitivity: str = "internal"


@dataclass
class LessonCluster:
    """BERTopic-derived group of LessonItems across one or more projects.

    Only meaningful once 2+ projects with lessons exist; Mode B output."""
    theme: str = "(unlabeled)"             # LLM-populated short label
    items: list[LessonItem] = field(default_factory=list)
    projects: list[str] = field(default_factory=list)
    proposed_entry_markdown: str | None = None   # ready-to-paste Registers/Lessons.md entry

    @property
    def size(self) -> int:
        return len(self.items)


@dataclass
class LessonsProposal:
    """Output of one ``lessons-learned scan`` run."""
    generated_at: datetime
    patterns: list[LessonPattern] = field(default_factory=list)
    clusters: list[LessonCluster] = field(default_factory=list)
    projects_scanned: list[str] = field(default_factory=list)
    closed_count: int = 0
    open_count: int = 0
    markdown_path: Optional[str] = None
