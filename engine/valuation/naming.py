"""Filename construction + archive policy for engine runs.

Convention (operator-confirmed 2026-05-19):

    Template:      os-templates/Project_x_LBO_date.xlsx       (canonical, literal `_date`)
    Per-deal copy: <project_dir>/3. Financials & analysis/2. Valuation/
                   Project_<Deal>_LBO_<YYYY-MM-DD>_v<N>.xlsx

    Same-day prior versions of THIS deal's LBO move to:
                   <project_dir>/3. Financials & analysis/2. Valuation/00. OLD/

    Per-deal `00. OLD/` is the per-deal archive. The global os-templates/Archive/
    is only used when the *template itself* is superseded (e.g. v1 → v2).

Files are MOVED, never deleted. Rule from CLAUDE.md §5.9 ("safe deletion only").
"""
from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from valuation.workspace import Workspace


VALUATION_SUBPATH = Path("3. Financials & analysis") / "2. Valuation"
DEAL_ARCHIVE_SUBPATH = VALUATION_SUBPATH / "00. OLD"

# <date>_v<N>.xlsx tail shared by every run filename (the head varies by
# workspace type: ``Project_<name>_<SKILL>_`` for project/bd, ``<name>_<SKILL>_``
# for general — see valuation.workspace.Workspace.filename_prefix).
_DATE_VER_TAIL = re.compile(r"^\d{4}-\d{2}-\d{2}_v\d+\.xlsx$")

# Filename pattern: Project_<Deal>_<Skill>_<YYYY-MM-DD>_v<N>.xlsx
FILENAME_PATTERN = re.compile(
    r"^Project_(?P<deal>[^_]+)_(?P<skill>[A-Z][A-Za-z0-9]*)_"
    r"(?P<date>\d{4}-\d{2}-\d{2})_v(?P<version>\d+)\.xlsx$"
)


@dataclass(frozen=True)
class RunFilename:
    """Parsed components of an engine-run filename."""
    deal: str
    skill: str
    date: date
    version: int

    def render(self) -> str:
        return f"Project_{self.deal}_{self.skill}_{self.date.isoformat()}_v{self.version}.xlsx"


def parse_filename(name: str) -> RunFilename | None:
    """Parse a filename produced by this convention. None if it doesn't match."""
    m = FILENAME_PATTERN.match(name)
    if not m:
        return None
    return RunFilename(
        deal=m["deal"],
        skill=m["skill"],
        date=date.fromisoformat(m["date"]),
        version=int(m["version"]),
    )


def deal_valuation_dir(project_dir: Path) -> Path:
    return project_dir / VALUATION_SUBPATH


def deal_archive_dir(project_dir: Path) -> Path:
    return project_dir / DEAL_ARCHIVE_SUBPATH


def next_output_path(project_dir: Path, deal: str, skill: str, today: date | None = None) -> Path:
    """Resolve the next versioned filename for a (deal, skill) on a given day.

    Inspects `<deal>/3. Financials & analysis/2. Valuation/` for existing files
    matching the pattern; returns `Project_<deal>_<skill>_<today>_v<N+1>.xlsx`
    where N is the max existing version for the same (deal, skill, date).
    """
    if today is None:
        today = date.today()
    val_dir = deal_valuation_dir(project_dir)
    val_dir.mkdir(parents=True, exist_ok=True)

    max_v = 0
    for f in val_dir.iterdir():
        if not f.is_file():
            continue
        parsed = parse_filename(f.name)
        if parsed and parsed.deal == deal and parsed.skill == skill and parsed.date == today:
            max_v = max(max_v, parsed.version)

    rf = RunFilename(deal=deal, skill=skill, date=today, version=max_v + 1)
    return val_dir / rf.render()


def archive_supersedes(project_dir: Path, deal: str, skill: str, *, keep_current: Path | None = None) -> list[Path]:
    """Move every prior version of `(deal, skill)` in the deal's Valuation dir
    into the per-deal `00. OLD/` archive, EXCEPT the file at `keep_current`
    (which is typically the brand-new run we just produced).

    Returns the list of files moved. Files are MOVED, never deleted.
    """
    val_dir = deal_valuation_dir(project_dir)
    archive = deal_archive_dir(project_dir)
    archive.mkdir(parents=True, exist_ok=True)

    moved: list[Path] = []
    if not val_dir.exists():
        return moved

    for f in val_dir.iterdir():
        if not f.is_file():
            continue
        if keep_current is not None and f.resolve() == keep_current.resolve():
            continue
        parsed = parse_filename(f.name)
        if parsed and parsed.deal == deal and parsed.skill == skill:
            target = archive / f.name
            # Disambiguate if the archive already has a same-named file (shouldn't,
            # but be defensive — append a numeric suffix).
            if target.exists():
                stem = target.stem
                suffix = target.suffix
                k = 1
                while True:
                    candidate = archive / f"{stem}_archived-{k}{suffix}"
                    if not candidate.exists():
                        target = candidate
                        break
                    k += 1
            shutil.move(str(f), str(target))
            moved.append(target)
    return moved


# ===================================================================
# Workspace-aware variants (#18) — work for all three workspace types.
#
# These delegate path + filename composition to the Workspace (project/bd carry
# the ``Project_`` prefix under ``3. F&A/2. Valuation``; general is flat under
# ``<name>/<SKILL>/`` with no prefix). Matching is prefix-based so workspace
# names containing underscores or spaces (e.g. "Acme Telco") are handled.
# ===================================================================

def _same_day_version(name: str, head: str) -> int | None:
    """If ``name`` is ``<head><N>.xlsx`` (N digits), return N; else None.
    ``head`` already includes the date and the trailing ``_v``."""
    if name.startswith(head) and name.endswith(".xlsx"):
        mid = name[len(head):-len(".xlsx")]
        if mid.isdigit():
            return int(mid)
    return None


def _is_run_of(name: str, prefix: str) -> bool:
    """True if ``name`` is a versioned run for ``prefix`` on ANY date —
    i.e. ``<prefix><YYYY-MM-DD>_v<N>.xlsx``."""
    if not (name.startswith(prefix) and name.endswith(".xlsx")):
        return False
    return bool(_DATE_VER_TAIL.match(name[len(prefix):]))


def next_output_path_for(ws: "Workspace", skill: str, today: date | None = None) -> Path:
    """Resolve the next versioned output path for ``(workspace, skill)`` on a
    given day. Creates the workspace's skill output dir if needed.

    project/bd → ``<name>/3. F&A/2. Valuation/Project_<name>_<SKILL>_<date>_vN.xlsx``
    general    → ``<name>/<SKILL>/<name>_<SKILL>_<date>_vN.xlsx``
    """
    if today is None:
        today = date.today()
    out_dir = ws.output_dir(skill)
    out_dir.mkdir(parents=True, exist_ok=True)

    today_iso = today.isoformat()
    head = f"{ws.filename_prefix(skill)}{today_iso}_v"
    max_v = 0
    for f in out_dir.iterdir():
        if not f.is_file():
            continue
        v = _same_day_version(f.name, head)
        if v is not None:
            max_v = max(max_v, v)
    return out_dir / ws.output_filename(skill, today_iso, max_v + 1)


def archive_supersedes_for(ws: "Workspace", skill: str, *,
                           keep_current: Path | None = None) -> list[Path]:
    """Move every prior version of ``(workspace, skill)`` into the skill's
    ``00. OLD/`` archive, EXCEPT ``keep_current``. Files are MOVED, never
    deleted (CLAUDE.md §5.9)."""
    out_dir = ws.output_dir(skill)
    archive = ws.archive_dir(skill)
    archive.mkdir(parents=True, exist_ok=True)

    prefix = ws.filename_prefix(skill)
    moved: list[Path] = []
    if not out_dir.exists():
        return moved

    for f in out_dir.iterdir():
        if not f.is_file():
            continue
        if keep_current is not None and f.resolve() == keep_current.resolve():
            continue
        if not _is_run_of(f.name, prefix):
            continue
        target = archive / f.name
        if target.exists():
            stem, suffix, k = target.stem, target.suffix, 1
            while True:
                candidate = archive / f"{stem}_archived-{k}{suffix}"
                if not candidate.exists():
                    target = candidate
                    break
                k += 1
        shutil.move(str(f), str(target))
        moved.append(target)
    return moved
