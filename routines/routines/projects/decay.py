"""Actions-decay routine.

Sibling to ``routines.bd.decay``. Walks every project (vault + external trees),
runs the actions aggregator per project, and returns the set of overdue + stale
items across all projects. Used by the daily 06:45 cron and surfaced in the
morning brief.

Skips projects under Templates/ / _template/ paths (same convention as the
aggregator + orphan-link scanner).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from routines.projects import actions as actions_mod
from routines.shared import profile as profile_mod

log = logging.getLogger(__name__)


@dataclass
class StaleAction:
    """One overdue or stale action surfaced across projects."""
    project: str
    title: str
    status: str                    # 'overdue' | 'stale'
    due: str | None
    owner: str
    urgent: bool
    flag: bool
    source_file: str
    source_line: int
    task_hash: str

    @classmethod
    def from_action(cls, project: str, a: actions_mod.Action) -> "StaleAction":
        return cls(
            project=project,
            title=a.title,
            status=a.status,
            due=a.due,
            owner=a.owner,
            urgent=a.urgent,
            flag=a.flag,
            source_file=a.source_file,
            source_line=a.source_line,
            task_hash=a.task_hash,
        )


@dataclass
class DecaySweep:
    """One run's output — all decayed actions across all projects."""
    projects_scanned: list[str] = field(default_factory=list)
    overdue: list[StaleAction] = field(default_factory=list)
    stale: list[StaleAction] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.overdue) + len(self.stale)


def _discover_projects(vault: Path, profile: profile_mod.OperatorProfile) -> list[str]:
    """Enumerate project names across vault + external trees.

    Returns deduped + sorted canonical names. Uses canonical form for
    dedup; original casing of the FIRST occurrence is retained for display.
    """
    seen: dict[str, str] = {}  # canonical → original

    # Vault Projects/
    vault_projects = vault / "Projects"
    if vault_projects.is_dir():
        for child in vault_projects.iterdir():
            if not child.is_dir() or child.name.startswith("_"):
                continue
            canon = actions_mod._canonical(child.name)
            seen.setdefault(canon, child.name)

    # External project paths
    for root_str in profile.external_project_paths:
        root = Path(root_str)
        if not root.is_dir():
            continue
        for child in root.iterdir():
            if not child.is_dir() or child.name.startswith("_"):
                continue
            canon = actions_mod._canonical(child.name)
            seen.setdefault(canon, child.name)

    return sorted(seen.values(), key=lambda n: n.lower())


def scan(
    vault: Path,
    profile: profile_mod.OperatorProfile | None = None,
    today: date | None = None,
) -> DecaySweep:
    """Walk all projects; return decayed actions (overdue + stale)."""
    if profile is None:
        profile = profile_mod.load(vault)
    today = today or date.today()

    sweep = DecaySweep()
    for project in _discover_projects(vault, profile):
        sweep.projects_scanned.append(project)
        try:
            actions = actions_mod.aggregate(vault, project, profile=profile, today=today)
        except Exception as e:  # noqa: BLE001
            log.warning("actions-decay: aggregate failed for %s: %s", project, e)
            continue
        for a in actions:
            if a.status == "overdue":
                sweep.overdue.append(StaleAction.from_action(project, a))
            elif a.status == "stale":
                sweep.stale.append(StaleAction.from_action(project, a))

    log.info(
        "actions-decay: %d project(s) scanned; %d overdue, %d stale",
        len(sweep.projects_scanned), len(sweep.overdue), len(sweep.stale),
    )
    return sweep


def format_for_morning_brief(sweep: DecaySweep) -> str:
    """Render decayed actions as a morning-brief markdown section.

    Empty string when nothing to flag (so brief routine elides the section).
    """
    if not sweep.overdue and not sweep.stale:
        return ""

    lines = ["## Open actions -- decay check", ""]
    lines.append(
        f"_{len(sweep.overdue)} overdue, {len(sweep.stale)} stale_ "
        f"across {len(sweep.projects_scanned)} project(s)."
    )
    lines.append("")

    if sweep.overdue:
        lines.append("### Overdue")
        lines.append("")
        # Group by project for readability; sort each group by oldest due first
        by_project: dict[str, list[StaleAction]] = {}
        for a in sweep.overdue:
            by_project.setdefault(a.project, []).append(a)
        for project in sorted(by_project):
            items = sorted(by_project[project], key=lambda x: x.due or "")
            lines.append(f"**{project}**")
            for a in items[:8]:
                marker = " :exclamation:" if a.urgent else ""
                due_str = f"due {a.due}" if a.due else "no due"
                lines.append(f"- {a.title} ({due_str}, owner: {a.owner}){marker}")
            if len(items) > 8:
                lines.append(f"- ...and {len(items) - 8} more")
            lines.append("")

    if sweep.stale:
        lines.append("### Stale (no due, source untouched >90d)")
        lines.append("")
        by_project = {}
        for a in sweep.stale:
            by_project.setdefault(a.project, []).append(a)
        for project in sorted(by_project):
            items = by_project[project]
            lines.append(f"**{project}**")
            for a in items[:6]:
                lines.append(f"- {a.title} (owner: {a.owner})")
            if len(items) > 6:
                lines.append(f"- ...and {len(items) - 6} more")
            lines.append("")

    return "\n".join(lines)
