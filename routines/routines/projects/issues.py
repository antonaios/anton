"""Per-deal issues-register parser (#issues-register v2).

Reads ``Projects/<X>/14 Issues & Outstanding.md`` — the running register of
live deal issues locked as a vault convention on 2026-06-10 (v1). One
``## ISS-NN — <title>`` section per issue carrying bold-bullet metadata
(status / priority / owner / raised / affects / resolution) and gating items
as §3-rule-11 checkboxes tagged ``[issue:ISS-NN]``.

This module is read-only: it parses the register into typed records for the
``GET /api/projects/{X}/issues`` endpoint and the dashboard's issue-grouped
Open Actions panel. Writes to the register go through the operator-gated
``issue-candidate`` proposal route (v1.5) or the operator directly — never
through here.

Parsing rules mirror ``routines.projects.actions``:
  * lines inside fenced code blocks are skipped (the register template carries
    its example issue inside a fence precisely so it never parses);
  * gating checkboxes reuse the CHECKBOX/TAG regexes so titles and tags are
    read identically to the Open Actions aggregator;
  * unrecognised ``status:`` values (including template placeholders like
    ``open | monitoring | blocked | closed``) normalise to ``open`` — fail
    toward visibility, never toward silently hiding an issue.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from routines.projects.actions import CHECKBOX, FENCED, TAG

logger = logging.getLogger(__name__)

REGISTER_FILENAME = "14 Issues & Outstanding.md"

KNOWN_STATUSES = ("open", "monitoring", "blocked", "closed")

# "## ISS-03 — FDD: working-capital adjustment risk" (em/en dash or hyphen)
ISSUE_HEADING = re.compile(r"^##\s+(ISS-\d+)\s*[—–-]+\s*(.+?)\s*$")

# Bold-bullet metadata tokens; tolerant of several "**key:** value" pairs on
# one line ("- **status:** monitoring   **priority:** P1").
META_TOKEN = re.compile(r"\*\*([a-z]+):\*\*\s*(.*?)(?=\s+\*\*[a-z]+:\*\*|\s*$)")

_PRIORITY = re.compile(r"\bP([1-3])\b", re.IGNORECASE)


@dataclass
class GatingItem:
    title: str
    checked: bool
    due: str | None
    owner: str | None
    urgent: bool
    line: int              # 1-indexed in the register file


@dataclass
class Issue:
    id: str                # "ISS-03"
    title: str
    status: str            # open | monitoring | blocked | closed
    priority: str | None   # "P1" | "P2" | "P3"
    owner: str | None
    raised: str | None
    affects: str | None
    resolution: str | None
    line: int              # 1-indexed heading line
    gating: list[GatingItem] = field(default_factory=list)

    @property
    def gating_open(self) -> int:
        return sum(1 for g in self.gating if not g.checked)

    @property
    def gating_total(self) -> int:
        return len(self.gating)


def resolve_register_path(vault: Path, project: str) -> Path | None:
    """``Projects/<project>/14 Issues & Outstanding.md`` — fail-closed on path
    traversal (mirrors ``projects._resolve_brief_path``). Returns None when the
    name is unsafe or the project folder doesn't exist; the returned path may
    itself not exist yet (older deals predating the v1 template)."""
    if not project or any(sep in project for sep in ("/", "\\")) or project in (".", ".."):
        return None
    projects_root = (vault / "Projects").resolve()
    project_dir = (projects_root / project).resolve()
    try:
        project_dir.relative_to(projects_root)
    except ValueError:
        return None
    if not project_dir.is_dir():
        return None
    return project_dir / REGISTER_FILENAME


def _normalise_status(raw: str) -> str:
    value = raw.strip().lower()
    return value if value in KNOWN_STATUSES else "open"


def _normalise_priority(raw: str) -> str | None:
    m = _PRIORITY.search(raw)
    return f"P{m.group(1)}" if m else None


def _clean(raw: str) -> str | None:
    value = raw.strip()
    if not value or value.startswith("{"):  # template placeholder e.g. "{lead}"
        return None
    return value


def parse_register_text(text: str) -> list[Issue]:
    """Parse the register body into Issue records. Fenced blocks skipped."""
    issues: list[Issue] = []
    current: Issue | None = None
    in_fenced = False

    for line_no, raw in enumerate(text.splitlines(), start=1):
        if FENCED.match(raw):
            in_fenced = not in_fenced
            continue
        if in_fenced:
            continue

        m = ISSUE_HEADING.match(raw)
        if m:
            current = Issue(
                id=m.group(1), title=m.group(2).strip(), status="open",
                priority=None, owner=None, raised=None, affects=None,
                resolution=None, line=line_no,
            )
            issues.append(current)
            continue

        if raw.startswith("## "):  # any other section heading ends the issue
            current = None
            continue
        if current is None:
            continue

        cb = CHECKBOX.match(raw)
        if cb:
            checked = cb.group(1) == "x"
            body = cb.group(2)
            tags: dict[str, str | bool] = {}
            for tm in TAG.finditer(body):
                tags[tm.group(1)] = tm.group(2) if tm.group(2) is not None else True
            # Gating membership: an [issue:] tag pointing at a DIFFERENT issue
            # excludes the checkbox (it belongs there, wherever it was written);
            # an untagged checkbox inside the section still counts — operators
            # handwrite gating items and forget the tag (codex finding 5).
            tag_issue = tags.get("issue")
            if isinstance(tag_issue, str) and tag_issue.strip() and tag_issue.strip() != current.id:
                continue
            title = re.sub(r"\s+", " ", TAG.sub("", body).strip())
            if not title:
                continue
            due = tags.get("due")
            owner = tags.get("owner")
            current.gating.append(GatingItem(
                title=title,
                checked=checked,
                due=due.strip() if isinstance(due, str) else None,
                owner=owner.strip() if isinstance(owner, str) else None,
                urgent=bool(tags.get("urgent")),
                line=line_no,
            ))
            continue

        if raw.lstrip().startswith("- "):
            for mt in META_TOKEN.finditer(raw):
                key, value = mt.group(1), mt.group(2)
                if key == "status":
                    current.status = _normalise_status(value)
                elif key == "priority":
                    current.priority = _normalise_priority(value)
                elif key == "owner":
                    current.owner = _clean(value)
                elif key == "raised":
                    current.raised = _clean(value)
                elif key == "affects":
                    current.affects = _clean(value)
                elif key == "resolution":
                    current.resolution = _clean(value)

    return issues


def next_issue_id(text: str) -> str:
    """Next free ``ISS-NN`` for a register body — FENCED-AWARE: headings inside
    code fences (the template's example block) never count, so the first real
    issue on a fresh register is ISS-01, not ISS-100 (codex finding 1)."""
    nums = []
    for issue in parse_register_text(text):
        try:
            nums.append(int(issue.id.split("-", 1)[1]))
        except (IndexError, ValueError):
            continue
    return f"ISS-{(max(nums) + 1 if nums else 1):02d}"


def load_register(vault: Path, project: str) -> tuple[Path | None, list[Issue]]:
    """Resolve + parse a project's register. ``(None, [])`` on unsafe name /
    missing project; ``(path, [])`` when the project exists but the register
    file doesn't (pre-v1 deals)."""
    path = resolve_register_path(vault, project)
    if path is None:
        return None, []
    if not path.is_file():
        return path, []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        logger.warning("issues: could not read %s: %s", path, e)
        return path, []
    return path, parse_register_text(text)


__all__ = [
    "REGISTER_FILENAME",
    "KNOWN_STATUSES",
    "GatingItem",
    "Issue",
    "resolve_register_path",
    "parse_register_text",
    "next_issue_id",
    "load_register",
]
