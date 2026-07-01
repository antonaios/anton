"""Gather context for the morning brief — open actions + upcoming sector events.

Pulls from on-disk vault state. No LLM in this module — pure data
extraction. Pass the result to `synthesise.py` for Anton's commentary.

Action extraction is intentionally lenient: anything that looks like a
markdown checkbox or an "Action:" bullet in recent meeting notes / decision
logs counts as a candidate. The LLM trims false positives in the synthesis
step.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import frontmatter

from routines.morning_brief.schema import BriefRow

log = logging.getLogger(__name__)


# ── Action-line regex bank ────────────────────────────────────────────────
#
# Trying to be permissive across the operator's note conventions. Covers:
#   - GFM checkbox:   "- [ ] Send NDA to Heartwood Collection"
#   - Bulleted:       "- Action: Send NDA"
#   - Inline:         "Action: Send NDA"
# Date hints:
#   - "(due 2026-05-08)" / "(by Tue)" / "due: 2026-05-08"
#   - Naked ISO date anywhere in the line

_ACTION_PATTERNS = [
    re.compile(r"^\s*[-*]\s*\[\s*\]\s*(?P<text>.+?)\s*$", re.IGNORECASE),         # - [ ] ...
    re.compile(r"^\s*[-*]\s*(?:Action|TODO)\s*[:\-]\s*(?P<text>.+?)\s*$", re.IGNORECASE),
    re.compile(r"^(?:Action|TODO)\s*[:\-]\s*(?P<text>.+?)\s*$", re.IGNORECASE),
]
_DATE_RE = re.compile(r"\b(20\d{2}-\d{2}-\d{2})\b")
_DUE_KEYWORDS = ("due", "by", "deadline", "must", "before")


@dataclass
class ActionItem:
    text: str                       # cleaned action text (date hints stripped)
    source_path: str                # vault-relative path
    source_excerpt: str             # the raw line as found
    due_date: date | None = None    # parsed ISO date if present
    project: str | None = None      # inferred from path (Projects/<X>/...)
    age_days: int = 0               # days since the source note was modified


@dataclass
class ContextBundle:
    needs_you: list[ActionItem] = field(default_factory=list)
    sector_news: list[BriefRow] = field(default_factory=list)
    today: date = field(default_factory=lambda: datetime.now(timezone.utc).date())
    active_sectors: list[str] = field(default_factory=list)


# ── Skip dirs / files (same convention as recall.index) ───────────────────

SKIP_DIRS = {
    ".git", ".obsidian", ".smart-env", ".recall-index",
    "Inbox/HiNotes/incoming",
    "Templates",
    "Projects/_template",
    "Projects/_Trackers",
}


def gather_context(
    vault_root: Path,
    *,
    today: date | None = None,
    days_lookback: int = 7,
    active_sectors: list[str] | None = None,
    newsletter_lookback_days: int = 7,
) -> ContextBundle:
    """Walk the vault and produce a ContextBundle.

    Args:
        vault_root: vault root
        today: pin "today" (default: UTC now)
        days_lookback: how many days back to scan meeting notes / decision
            logs for actions. Default 7.
        active_sectors: list of sector names (from profile.md). Used to
            filter newsletter pulls. Defaults to all newsletters.
        newsletter_lookback_days: how recent a newsletter has to be to count
            as "this week". Default 7.
    """
    today = today or datetime.now(timezone.utc).date()
    cutoff = today - timedelta(days=days_lookback)

    needs_you = _gather_actions(vault_root, today=today, cutoff=cutoff)
    sector_news = _gather_sector_news(
        vault_root,
        active_sectors=active_sectors or [],
        lookback_days=newsletter_lookback_days,
    )

    return ContextBundle(
        needs_you=needs_you,
        sector_news=sector_news,
        today=today,
        active_sectors=active_sectors or [],
    )


# ── Action extraction ─────────────────────────────────────────────────────


def _gather_actions(vault_root: Path, *, today: date, cutoff: date) -> list[ActionItem]:
    """Walk projects and find action items modified within `days_lookback`.

    Sources scanned, in order:
      1. `Projects/<X>/02 Meeting Notes/*.md` (and synonyms)
      2. `Projects/<X>/09 Decision Log.md`
      3. `Registers/Actions.md` (if present)
    """
    out: list[ActionItem] = []
    projects_root = vault_root / "Projects"
    if not projects_root.exists():
        return out

    for proj_dir in projects_root.iterdir():
        if not proj_dir.is_dir() or proj_dir.name.startswith("_"):
            continue
        # Meeting notes — multiple conventions
        for subdir_name in ("02 Meeting Notes", "Meeting Notes", "meeting-notes"):
            mn = proj_dir / subdir_name
            if mn.is_dir():
                for note in mn.rglob("*.md"):
                    out.extend(_extract_from_file(
                        note, vault_root, project_name=proj_dir.name,
                        today=today, cutoff=cutoff,
                    ))
        # Decision log
        for log_name in ("09 Decision Log.md", "Decision Log.md", "decisions.md"):
            d = proj_dir / log_name
            if d.exists():
                out.extend(_extract_from_file(
                    d, vault_root, project_name=proj_dir.name,
                    today=today, cutoff=cutoff,
                ))

    # Registers/Actions.md cross-project
    registers_actions = vault_root / "Registers" / "Actions.md"
    if registers_actions.exists():
        out.extend(_extract_from_file(
            registers_actions, vault_root, project_name=None,
            today=today, cutoff=cutoff,
        ))

    # Dedupe on (text-lower-stripped, source_path).
    seen: set[tuple[str, str]] = set()
    deduped: list[ActionItem] = []
    for item in out:
        key = (item.text.lower().strip(), item.source_path)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)

    # Sort: overdue first, then due-today, then by age desc.
    def _sort_key(a: ActionItem) -> tuple[int, int]:
        if a.due_date and a.due_date < today:
            return (0, (today - a.due_date).days)
        if a.due_date == today:
            return (1, 0)
        return (2, -a.age_days)

    deduped.sort(key=_sort_key)
    return deduped[:30]   # cap so the LLM context stays manageable


def _extract_from_file(
    path: Path, vault_root: Path, *,
    project_name: str | None, today: date, cutoff: date,
) -> list[ActionItem]:
    try:
        mtime = datetime.fromtimestamp(path.stat().st_mtime).date()
    except OSError:
        return []
    if mtime < cutoff:
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    rel = str(path.relative_to(vault_root).as_posix())
    age_days = (today - mtime).days
    items: list[ActionItem] = []

    # Strip frontmatter if any.
    try:
        post = frontmatter.loads(text)
        body = post.content
    except Exception:  # noqa: BLE001
        body = text

    for line in body.splitlines():
        for pat in _ACTION_PATTERNS:
            m = pat.match(line)
            if not m:
                continue
            raw_text = m.group("text").strip()
            if not raw_text:
                continue
            # Skip already-done items.
            if "[x]" in line.lower() or line.lower().startswith("- [x]"):
                continue
            due = _parse_due(raw_text + " " + line)
            # Clean date hints out of the text.
            cleaned = _strip_date_hints(raw_text)
            items.append(ActionItem(
                text=cleaned,
                source_path=rel,
                source_excerpt=line.strip(),
                due_date=due,
                project=project_name,
                age_days=age_days,
            ))
            break
    return items


def _parse_due(text: str) -> date | None:
    """Find an ISO date in the line, preferably near a 'due'/'by' keyword."""
    matches = _DATE_RE.findall(text)
    if not matches:
        return None
    text_lc = text.lower()
    # If "due"/"by" appears, prefer the nearest date after it.
    for kw in _DUE_KEYWORDS:
        idx = text_lc.find(kw)
        if idx >= 0:
            for m in _DATE_RE.finditer(text):
                if m.start() >= idx:
                    try:
                        return date.fromisoformat(m.group(1))
                    except ValueError:
                        continue
    # Fallback: first date in the line.
    try:
        return date.fromisoformat(matches[0])
    except ValueError:
        return None


def _strip_date_hints(text: str) -> str:
    """Remove "(due 2026-05-08)" / bare ISO dates from action text."""
    cleaned = re.sub(r"\(\s*(?:due|by|deadline)[^)]*\)", "", text, flags=re.IGNORECASE)
    cleaned = _DATE_RE.sub("", cleaned)
    return re.sub(r"\s{2,}", " ", cleaned).strip().rstrip(",;:")


# ── Sector news (newsletters within lookback window) ──────────────────────


def _gather_sector_news(
    vault_root: Path, *,
    active_sectors: list[str], lookback_days: int,
) -> list[BriefRow]:
    """Pull headlines from the most recent newsletters per active sector.

    For each newsletter:
      - Use the filename date (`YYYY-MM-DD-<Sector>.md` convention).
      - Skip if older than `lookback_days`.
      - Pull the first 2-3 bullet headlines from the body.
    """
    newsletters_dir = vault_root / "Resources" / "Newsletters"
    if not newsletters_dir.exists():
        return []

    cutoff = datetime.now(timezone.utc).date() - timedelta(days=lookback_days)
    rows: list[BriefRow] = []

    files = sorted(newsletters_dir.glob("*.md"), reverse=True)
    for f in files:
        m = re.match(r"^(\d{4}-\d{2}-\d{2})-(.+)\.md$", f.name)
        if not m:
            continue
        try:
            file_date = date.fromisoformat(m.group(1))
        except ValueError:
            continue
        if file_date < cutoff:
            continue
        sector_in_filename = m.group(2).replace("-", " ")
        if active_sectors:
            if not any(s.lower() in sector_in_filename.lower() for s in active_sectors):
                continue

        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        try:
            body = frontmatter.loads(text).content
        except Exception:  # noqa: BLE001
            body = text

        headlines = _first_bullets(body, limit=3)
        for h in headlines:
            rows.append(BriefRow(
                marker="news",
                text=h,
                sub=f"{sector_in_filename} · {file_date.isoformat()}",
            ))
        if len(rows) >= 8:
            break

    return rows[:8]


def _first_bullets(body: str, limit: int = 3) -> list[str]:
    """Pull the first N bullet headlines (h2/h3 with a following paragraph,
    or short bulleted points)."""
    out: list[str] = []
    for line in body.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith(("## ", "### ")):
            heading = s.lstrip("# ").strip()
            if 10 < len(heading) < 140 and not heading.lower().startswith(("sources", "appendix", "notes")):
                out.append(heading)
        elif s.startswith(("- ", "* ")):
            bullet = s.lstrip("-* ").strip()
            if 20 < len(bullet) < 200:
                out.append(bullet)
        if len(out) >= limit:
            break
    return out
