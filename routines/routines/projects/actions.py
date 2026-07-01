"""Action-item aggregator for a per-project Open Actions panel.

Reads inline ``- [ ] task text [tag:value]`` lines from every relevant
markdown file across the vault + Corp Finance trees and returns a deduped
list of typed Actions, ready to ship over the bridge to the dashboard.

Convention is locked per the 2026-05-23 vault-session spec (will land in
``_claude/CLAUDE.md`` §3 + ``Topics/Architecture/workspace-write-policy.md``).

Per-project source surface (∪ then deduped by (source_file, task_hash)):

    Projects/<X>/**/*.md                                  (vault tree)
    <each external_project_paths>/<X>/**/*.md             (Corp Finance tree)
    Companies/<target>.md                                 (from brief frontmatter)
    Companies/<counterparty>.md                           (from brief frontmatter)

Skip rules:
    - Files under ``Templates/**`` or ``**/_template/**``
    - Lines inside fenced code blocks (``` or ~~~)
    - Lines inside inline code spans (stripped before checkbox match)
    - Tasks with sub-5-char visible title after tags removed
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

import frontmatter

from routines.shared import profile as profile_mod

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────────
# Regex parsers
# ──────────────────────────────────────────────────────────────────────────

# Checkbox: "  - [ ] Task text" or "- [x] Task done"
CHECKBOX = re.compile(r'^\s*- \[([ x])\]\s+(.+?)\s*$')

# Inline tag: [key] (boolean) or [key:value]
# Keys are lowercase a-z; values are anything but ']'.
TAG = re.compile(r'\[([a-z]+)(?::([^\]]+))?\]')

# Inline code span — stripped before checkbox match so backtick-wrapped
# pseudo-tasks like `- [ ] not a real task` never match, and tasks
# containing inline code keep working with the code stripped from the title.
INLINE_CODE = re.compile(r'`[^`]*`')

# Fenced code-block delimiter (``` or ~~~)
FENCED = re.compile(r'^\s*(```|~~~)')

# Files to skip outright: anywhere under Templates/** or **/_template/**
SKIP_PATH = re.compile(r'[\\/](Templates|_template)([\\/]|$)', re.IGNORECASE)

# Wikilink to a Companies/<X> entry (with optional #section or |alias)
COMPANIES_LINK = re.compile(r'\[\[Companies/(.+?)(?:[#|][^\]]*)?\]\]')

# Minimum visible title length (after tags stripped)
MIN_TITLE_LEN = 5

# Stale threshold — open + no due, file mtime older than this
STALE_AFTER_DAYS = 90


# ──────────────────────────────────────────────────────────────────────────
# Result dataclasses
# ──────────────────────────────────────────────────────────────────────────

@dataclass
class Action:
    title: str
    status: str            # 'open' | 'overdue' | 'stale' | 'done'
    due: str | None        # ISO date YYYY-MM-DD
    owner: str             # slug (lowercase); defaults to operator_slug
    urgent: bool
    flag: bool
    done: str | None       # ISO date (explicit tag, or inferred from mtime if checked)
    source_file: str       # absolute path
    source_line: int       # 1-indexed (hint for fast lookup; not authoritative)
    task_hash: str         # 8-char sha1 of normalised title — task identity within file
    issue: str | None = None  # [issue:ISS-NN] — links a gating action to its issue in the project's 14 Issues & Outstanding.md (#issues-register v2)


@dataclass
class ToggleResult:
    success: bool
    line: int | None = None
    snippet: str | None = None
    error: str | None = None
    candidates: list[dict] | None = None   # populated on ambiguous match (409)


# ──────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────

def _canonical(name: str) -> str:
    """Normalised folder/project name for cross-tree matching.

    Vault session spec: ``lower().replace(' ', '-')``. Locked convention going
    forward is PascalCase-hyphenated (``DemoTarget``); aggregator
    still pairs case-insensitively so a mistyped folder name doesn't break
    aggregation — it just warns.
    """
    return name.strip().lower().replace(' ', '-')


def _parse_companies_wikilink(raw: str | None) -> str | None:
    """Extract company name from a ``[[Companies/X]]`` / ``[[Companies/X|alias]]`` link."""
    if not raw:
        return None
    m = COMPANIES_LINK.search(raw)
    return m.group(1).strip() if m else None


def _normalize_title(title: str) -> str:
    """Lowercase + collapse whitespace for stable hashing."""
    return re.sub(r'\s+', ' ', title).strip().lower()


def _hash_title(title: str) -> str:
    """8-char SHA1 of the normalised title — task identity within a file."""
    return hashlib.sha1(_normalize_title(title).encode('utf-8')).hexdigest()[:8]


def _parse_iso_date(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return date.fromisoformat(s.strip())
    except (ValueError, AttributeError):
        return None


def _file_mtime(path: Path) -> datetime:
    try:
        ts = path.stat().st_mtime
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    except OSError:
        return datetime.now(timezone.utc)


# ──────────────────────────────────────────────────────────────────────────
# Source resolution
# ──────────────────────────────────────────────────────────────────────────

def _match_subdirs(root: Path, requested: str) -> list[Path]:
    """Find subdirectories of ``root`` whose canonical name matches ``requested``.

    Warns when normalization had to do work (folder name != requested name
    but their canonical forms match) — surfaces drift between the vault tree
    and Corp Finance tree without blocking aggregation.
    """
    if not root.exists() or not root.is_dir():
        return []
    target = _canonical(requested)
    hits: list[Path] = []
    for child in root.iterdir():
        try:
            if not child.is_dir():
                continue
        except OSError:
            continue
        if _canonical(child.name) == target:
            if child.name != requested:
                logger.warning(
                    "project name normalization: requested '%s' matched folder '%s' "
                    "in %s via canonical '%s' (locked convention: PascalCase-hyphenated)",
                    requested, child.name, root, target,
                )
            hits.append(child)
    return hits


def _vault_project_dir(vault: Path, project: str) -> Path | None:
    matches = _match_subdirs(vault / "Projects", project)
    return matches[0] if matches else None


def _external_project_dirs(profile: profile_mod.OperatorProfile, project: str) -> list[Path]:
    out: list[Path] = []
    for root_str in profile.external_project_paths:
        root = Path(root_str)
        out.extend(_match_subdirs(root, project))
    return out


def _load_brief(vault_project_dir: Path | None) -> dict | None:
    """Read the project's ``00 Brief.md`` frontmatter."""
    if not vault_project_dir:
        return None
    brief_path = vault_project_dir / "00 Brief.md"
    if not brief_path.exists():
        return None
    try:
        post = frontmatter.loads(brief_path.read_text(encoding='utf-8'))
        return dict(post.metadata)
    except Exception as e:  # noqa: BLE001
        logger.warning("could not parse %s: %s", brief_path, e)
        return None


def action_sources(
    vault: Path,
    profile: profile_mod.OperatorProfile,
    project: str,
) -> list[Path]:
    """Resolve all .md files to scan for actions on this project.

    Returns deduped paths in deterministic order (vault first, then external
    trees in profile order, then Companies/<target>, then Companies/<counterparty>).
    Templates and ``_template`` paths are filtered out.
    """
    sources: list[Path] = []

    vault_dir = _vault_project_dir(vault, project)
    if vault_dir:
        sources.extend(sorted(vault_dir.rglob("*.md")))

    for ext_dir in _external_project_dirs(profile, project):
        sources.extend(sorted(ext_dir.rglob("*.md")))

    # Companies/<target>.md + Companies/<counterparty>.md from the brief
    brief = _load_brief(vault_dir)
    if brief:
        for field_name in ("target", "counterparty"):
            company = _parse_companies_wikilink(brief.get(field_name))
            if company:
                company_path = vault / "Companies" / f"{company}.md"
                if company_path.exists():
                    sources.append(company_path)

    # Dedup by resolved path; filter Templates/_template
    seen: set[str] = set()
    out: list[Path] = []
    for p in sources:
        try:
            key = str(p.resolve())
        except OSError:
            key = str(p)
        if key in seen:
            continue
        seen.add(key)
        if SKIP_PATH.search(str(p)):
            continue
        out.append(p)
    return out


# ──────────────────────────────────────────────────────────────────────────
# Per-file scanner
# ──────────────────────────────────────────────────────────────────────────

def _parse_line(
    raw_line: str,
    *,
    path: Path,
    line_no: int,
    mtime: datetime,
    default_owner: str,
    today: date,
) -> Action | None:
    """Parse one candidate line; returns None if it's not a valid action.

    Matches CHECKBOX on the RAW line — GFM checkboxes are line-start tokens
    (after optional whitespace), so inline-code spans elsewhere in the line
    can't fake a checkbox. Matching on raw preserves backticked content in
    task titles (e.g. ``- [ ] Check the `auth_token` field``).
    """
    m = CHECKBOX.match(raw_line)
    if not m:
        return None

    checked = m.group(1) == 'x'
    body = m.group(2)

    # Extract tags + title
    tags: dict[str, str | bool] = {}
    for tm in TAG.finditer(body):
        k = tm.group(1)
        v = tm.group(2)
        tags[k] = v if v is not None else True
    title = TAG.sub('', body).strip()
    title = re.sub(r'\s+', ' ', title)

    if len(title) < MIN_TITLE_LEN:
        return None

    due_raw = tags.get('due')
    due = _parse_iso_date(due_raw) if isinstance(due_raw, str) else None
    owner_raw = tags.get('owner')
    owner = (owner_raw if isinstance(owner_raw, str) else default_owner).strip().lower()
    urgent = bool(tags.get('urgent'))
    flag_ = bool(tags.get('flag'))

    done_raw = tags.get('done')
    if isinstance(done_raw, str):
        done_iso: str | None = done_raw.strip()
    elif checked:
        done_iso = mtime.date().isoformat()
    else:
        done_iso = None

    issue_raw = tags.get('issue')
    issue = issue_raw.strip() if isinstance(issue_raw, str) and issue_raw.strip() else None

    # Status mapping
    if checked:
        status = 'done'
    elif due and due < today:
        status = 'overdue'
    elif not due and (today - mtime.date()) > timedelta(days=STALE_AFTER_DAYS):
        status = 'stale'
    else:
        status = 'open'

    return Action(
        title=title,
        status=status,
        due=due.isoformat() if due else None,
        owner=owner,
        urgent=urgent,
        flag=flag_,
        done=done_iso,
        source_file=str(path),
        source_line=line_no,
        task_hash=_hash_title(title),
        issue=issue,
    )


def _scan_file(
    path: Path,
    default_owner: str,
    today: date,
) -> Iterable[Action]:
    """Yield Action records for every parseable ``- [ ]``/``- [x]`` line in ``path``."""
    try:
        text = path.read_text(encoding='utf-8', errors='replace')
    except OSError as e:
        logger.warning("could not read %s: %s", path, e)
        return

    mtime = _file_mtime(path)
    in_fenced = False

    for line_no, raw_line in enumerate(text.splitlines(), start=1):
        if FENCED.match(raw_line):
            in_fenced = not in_fenced
            continue
        if in_fenced:
            continue

        action = _parse_line(
            raw_line,
            path=path, line_no=line_no, mtime=mtime,
            default_owner=default_owner, today=today,
        )
        if action is not None:
            yield action


# ──────────────────────────────────────────────────────────────────────────
# Aggregator entry-point
# ──────────────────────────────────────────────────────────────────────────

def aggregate(
    vault: Path,
    project: str,
    profile: profile_mod.OperatorProfile | None = None,
    today: date | None = None,
) -> list[Action]:
    """Return all parseable actions for ``project``, deduped by (source_file, task_hash).

    If ``profile`` is None, loads from ``vault/_claude/profile.md``.
    If ``today`` is None, uses local date (operator's wall clock).
    """
    if profile is None:
        profile = profile_mod.load(vault)
    today = today or date.today()
    default_owner = profile.operator_slug

    seen: set[tuple[str, str]] = set()
    out: list[Action] = []
    for src in action_sources(vault, profile, project):
        for a in _scan_file(src, default_owner=default_owner, today=today):
            try:
                resolved = str(Path(a.source_file).resolve())
            except OSError:
                resolved = a.source_file
            key = (resolved, a.task_hash)
            if key in seen:
                continue
            seen.add(key)
            out.append(a)
    return out


# ──────────────────────────────────────────────────────────────────────────
# Write-back: toggle a checkbox between open and done
# ──────────────────────────────────────────────────────────────────────────

def _line_hash(raw_line: str) -> str | None:
    """Hash for a candidate line, or None if it's not a parseable task.

    Matches CHECKBOX on raw (same rationale as _parse_line — see there).
    """
    m = CHECKBOX.match(raw_line)
    if not m:
        return None
    body = m.group(2)
    title = re.sub(r'\s+', ' ', TAG.sub('', body).strip())
    if len(title) < MIN_TITLE_LEN:
        return None
    return _hash_title(title)


def toggle(
    source_file: Path,
    task_hash: str,
    to: str,
    line_hint: int | None = None,
    today: date | None = None,
) -> ToggleResult:
    """Toggle a task's checkbox state between ``- [ ]`` and ``- [x]``.

    Lookup order:
      1. ``line_hint`` (if provided and hash matches)
      2. Fall back to scanning the entire file
      3. Return 409-style error if ≥ 2 distinct lines match

    On 'done': swaps ``- [ ]`` → ``- [x]`` and appends ``[done:YYYY-MM-DD]``
    (preserving any other tags). On 'open': reverse swap and strips the
    ``[done:...]`` tag.
    """
    if to not in ('open', 'done'):
        return ToggleResult(success=False, error=f"invalid target state: {to!r}")
    today = today or date.today()
    if not source_file.exists():
        return ToggleResult(success=False, error=f"source file not found: {source_file}")

    try:
        text = source_file.read_text(encoding='utf-8')
    except OSError as e:
        return ToggleResult(success=False, error=f"read failed: {e}")

    # Split preserving line endings so we can write back byte-faithfully.
    # `splitlines(keepends=True)` returns each line with its terminator.
    lines = text.splitlines(keepends=True)

    # Try the line hint first
    hit_index: int | None = None
    if line_hint is not None and 1 <= line_hint <= len(lines):
        if _line_hash(lines[line_hint - 1]) == task_hash:
            hit_index = line_hint - 1

    # If line_hint missed, scan whole file
    all_hits: list[int] = []
    if hit_index is None:
        for i, raw in enumerate(lines):
            if _line_hash(raw) == task_hash:
                all_hits.append(i)
        if not all_hits:
            return ToggleResult(success=False, error="task not found in source file (hash miss)")
        if len(all_hits) > 1:
            return ToggleResult(
                success=False,
                error="ambiguous: multiple lines match this task hash",
                candidates=[
                    {"line": idx + 1, "snippet": lines[idx].rstrip("\r\n")}
                    for idx in all_hits
                ],
            )
        hit_index = all_hits[0]

    # Rewrite the line (preserve original line ending)
    raw = lines[hit_index]
    if raw.endswith("\r\n"):
        newline = "\r\n"; body = raw[:-2]
    elif raw.endswith("\n"):
        newline = "\n";   body = raw[:-1]
    elif raw.endswith("\r"):
        newline = "\r";   body = raw[:-1]
    else:
        newline = "";     body = raw

    if to == 'done':
        new_body = body.replace('- [ ]', '- [x]', 1)
        if '[done:' not in new_body:
            new_body = new_body.rstrip() + f' [done:{today.isoformat()}]'
    else:  # 'open'
        new_body = body.replace('- [x]', '- [ ]', 1)
        new_body = re.sub(r'\s*\[done:[^\]]+\]', '', new_body)

    lines[hit_index] = new_body + newline
    try:
        source_file.write_text(''.join(lines), encoding='utf-8')
    except OSError as e:
        return ToggleResult(success=False, error=f"write failed: {e}")

    return ToggleResult(success=True, line=hit_index + 1, snippet=new_body.strip())
