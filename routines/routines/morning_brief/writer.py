"""Atomic writer for the morning brief.

Format on disk: a markdown file with YAML frontmatter at
``Routines/morning-briefs/<date>.md``. Frontmatter carries the structured
data; the body is the human-readable rendering. The bridge endpoint
reads the frontmatter to serve JSON to the dashboard; humans browsing
the vault see the markdown rendering.
"""

from __future__ import annotations

import json
import logging
from datetime import date as date_cls
from pathlib import Path

from routines.morning_brief.schema import MorningBrief
from routines.shared.vault_writer import atomic_write

log = logging.getLogger(__name__)


def brief_path(vault_root: Path, the_date: date_cls) -> Path:
    return vault_root / "Routines" / "morning-briefs" / f"{the_date.isoformat()}.md"


def write_brief(vault_root: Path, brief: MorningBrief, the_date: date_cls) -> Path:
    """Atomic-write the brief. Returns the absolute path."""
    path = brief_path(vault_root, the_date)
    md = _render_markdown(brief, the_date)
    atomic_write(path, md, vault_root=vault_root)
    return path


def _render_markdown(brief: MorningBrief, the_date: date_cls) -> str:
    """Render the brief as markdown with frontmatter.

    Frontmatter carries the full structured payload as JSON inside a
    single `data:` key — the reader parses it back. The body is for
    human eyes.
    """
    payload_json = json.dumps(brief.model_dump(), indent=2)
    # Use a |-prefixed scalar block so YAML preserves the JSON verbatim.
    fm = (
        "---\n"
        "type: morning-brief\n"
        "sensitivity: internal\n"
        f"date: {the_date.isoformat()}\n"
        f"generated: {brief.source}\n"
        "tags: [morning-brief, routines, auto-generated]\n"
        "data: |\n"
        + _indent(payload_json, "  ")
        + "\n---\n\n"
    )

    body_lines = [
        f"# Morning Brief · {brief.date}",
        "",
        f"_Source: {brief.source}_",
        "",
    ]

    if brief.needsYou:
        body_lines += ["## Needs you", ""]
        for r in brief.needsYou:
            tag = {"ovd": "[OVERDUE]", "due": "[DUE]", "open": "[OPEN]"}.get(r.marker, "[?]")
            body_lines.append(f"- {tag} **{r.text}** — _{r.sub}_")
        body_lines.append("")

    if brief.sectorThisWeek:
        body_lines += ["## Sector this week", ""]
        for r in brief.sectorThisWeek:
            body_lines.append(f"- {r.text} — _{r.sub}_")
        body_lines.append("")

    if brief.antonSuggests:
        body_lines += [
            "## Anton suggests",
            "",
            brief.antonSuggests,
            "",
        ]

    return fm + "\n".join(body_lines)


def _indent(text: str, prefix: str) -> str:
    return "\n".join(prefix + line for line in text.splitlines())
