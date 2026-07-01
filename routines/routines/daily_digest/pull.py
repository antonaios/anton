"""Gather context for the daily digest.

Two data sources, both pure file-walks (no LLM here):

  1. **Audit JSONLs** at ``routines/runs/<routine>.jsonl`` — entries whose
     ``ts`` falls on today's date give us "what ran today".
  2. **Vault writes** — files under the vault root with mtime ≥ start-of-today,
     filtered to the directories that matter for the operator's day
     (Projects, Companies, Sectors, Daily, Templates, Registers, Resources).

The synthesise step takes the resulting bundle and asks the local LLM to
write the reflective close.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from pathlib import Path

log = logging.getLogger(__name__)


# Vault subtrees worth surfacing in "today's writes". Anything outside
# this set is structural/transient and not interesting to the operator.
INTERESTING_DIRS = (
    "Projects", "Companies", "Sectors", "People",
    "Daily", "Templates", "Registers", "Topics",
    "Resources", "Routines",
)

# Skip noise — gitignored caches, watcher staging, archive.
SKIP_DIR_NAMES = {
    ".git", ".obsidian", ".smart-env", ".recall-index", ".markets-cache",
    "_template", "_Trackers", "incoming", "processed",
    "__pycache__", ".pytest_cache",
}


@dataclass
class RoutineActivity:
    routine: str
    ok: int = 0
    error: int = 0
    skipped: int = 0
    partial: int = 0
    last_status: str = ""
    last_ts: str = ""
    last_run_id: str = ""
    last_outputs: dict = field(default_factory=dict)


@dataclass
class VaultWrite:
    path: str                  # vault-relative POSIX path
    mtime_iso: str             # "2026-05-14T18:42:11+00:00"
    bucket: str                # top-level dir, e.g. "Projects", "Companies"


@dataclass
class DigestContext:
    today: date
    routines: list[RoutineActivity] = field(default_factory=list)
    vault_writes: list[VaultWrite] = field(default_factory=list)
    profile_context: str = ""


def gather_context(
    vault_root: Path,
    runs_dir: Path,
    *,
    today: date | None = None,
    max_writes: int = 20,
) -> DigestContext:
    """Walk audit logs + vault for today's activity."""
    the_date = today or datetime.now(timezone.utc).date()
    return DigestContext(
        today=the_date,
        routines=_gather_routine_activity(runs_dir, today=the_date),
        vault_writes=_gather_vault_writes(vault_root, today=the_date, limit=max_writes),
    )


# ── Audit-log walk ────────────────────────────────────────────────────────


def _gather_routine_activity(runs_dir: Path, *, today: date) -> list[RoutineActivity]:
    """One ``RoutineActivity`` per ``runs/<routine>.jsonl`` with entries today."""
    if not runs_dir.is_dir():
        return []

    out: list[RoutineActivity] = []
    today_iso = today.isoformat()

    for log_path in sorted(runs_dir.glob("*.jsonl")):
        routine = log_path.stem
        counts: Counter[str] = Counter()
        last: dict | None = None
        try:
            with log_path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    ts = str(rec.get("ts") or "")
                    if not ts.startswith(today_iso):
                        continue
                    status = str(rec.get("status") or "ok")
                    counts[status] += 1
                    last = rec
        except OSError as e:
            log.warning("daily-digest: read %s failed: %s", log_path, e)
            continue
        if not counts:
            continue
        out.append(RoutineActivity(
            routine=routine,
            ok=counts.get("ok", 0),
            error=counts.get("error", 0),
            skipped=counts.get("skipped", 0),
            partial=counts.get("partial", 0),
            last_status=str(last.get("status") or "") if last else "",
            last_ts=str(last.get("ts") or "") if last else "",
            last_run_id=str(last.get("run_id") or "") if last else "",
            last_outputs=dict(last.get("outputs") or {}) if last else {},
        ))

    out.sort(key=lambda r: (-r.error, -(r.ok + r.skipped + r.partial), r.routine))
    return out


# ── Vault writes walk ─────────────────────────────────────────────────────


def _gather_vault_writes(
    vault_root: Path, *, today: date, limit: int,
) -> list[VaultWrite]:
    """Files under vault_root with mtime ≥ start-of-today (UTC), capped."""
    if not vault_root.is_dir():
        return []

    cutoff = datetime.combine(today, time.min, tzinfo=timezone.utc).timestamp()
    hits: list[VaultWrite] = []

    for top in INTERESTING_DIRS:
        sub = vault_root / top
        if not sub.is_dir():
            continue
        for path in sub.rglob("*.md"):
            if any(part in SKIP_DIR_NAMES for part in path.parts):
                continue
            try:
                st = path.stat()
            except OSError:
                continue
            if st.st_mtime < cutoff:
                continue
            try:
                rel = path.relative_to(vault_root).as_posix()
            except ValueError:
                continue
            mtime_iso = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(
                timespec="seconds"
            )
            hits.append(VaultWrite(path=rel, mtime_iso=mtime_iso, bucket=top))

    hits.sort(key=lambda w: w.mtime_iso, reverse=True)
    return hits[:limit]
