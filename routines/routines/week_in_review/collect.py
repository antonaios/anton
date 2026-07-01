"""Mechanical aggregation for the week-in-review (#38).

Pure data extraction — no LLM in this module. Everything here is
deterministic so the bulk of the draft is reproducible from disk + git
state alone. ``render.py`` turns the resulting :class:`WeekContext` into
markdown (and adds the two LLM-synthesised sections).

Sources, all READ-ONLY:
  * per-repo ``git log`` over the window (engine + routines + dashboard +
    umbrella + vault — absolute paths);
  * the OUTSTANDING.md ARCHIVE diff (items shipped this week);
  * the audit JSONL window (``runs/*.jsonl`` — counts by routine + status;
    READ-ONLY per the #60 multi-consumer lesson);
  * the LLM telemetry roll-up (``telemetry/llm_calls.jsonl``);
  * the ``ADOPTED CONVENTIONS`` diff in CLAUDE.md / OUTSTANDING.md
    (raw material for the LLM "Decisions locked" section);
  * open OUTSTANDING items (next-week candidates).
"""

from __future__ import annotations

import logging
import re
import subprocess
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger(__name__)


# ── Repos + default source paths ───────────────────────────────────────────
#
# Absolute paths per the brief. ``synapse/`` is intentionally out of scope.
# Overridable in every collector so tests can point at tmp fixtures.

# Roots are DERIVED from this file's own location, not hard-coded, so the
# routine is consistent on both native Windows (``X:\...``) and WSL
# (``/mnt/x/...``): the collector's source repos and the CLI's output vault
# always share one base. Hard-coding ``X:/...`` here while the CLI defaulted
# the vault to ``/mnt/x/...`` would write the draft outside the repos it
# scanned on one platform and skip every repo on the other (codex #38 P2).
_REPO_ROOT = Path(__file__).resolve().parents[2]   # <base>/Agentic OS/routines
UMBRELLA = _REPO_ROOT.parent                        # <base>/Agentic OS
_BASE = UMBRELLA.parent                             # <base>  (X:/ or /mnt/x/)
VAULT = _BASE / "OS AI Vault"

DEFAULT_REPOS: list[tuple[str, Path]] = [
    ("umbrella", UMBRELLA),
    ("routines", _REPO_ROOT),
    ("dashboard", UMBRELLA / "dashboard"),
    ("engine", UMBRELLA / "engine"),
    ("vault", VAULT),
]

# ``runs/`` sits at the repo root: collect.py is routines/week_in_review/.
DEFAULT_RUNS_DIR = _REPO_ROOT / "runs"
DEFAULT_OUTSTANDING = UMBRELLA / "OUTSTANDING.md"

# Files whose ADOPTED-CONVENTIONS / Decisions additions feed the LLM
# "Decisions locked" section. Relative to each repo root; only those that
# exist are diffed.
DECISION_DOC_PATHS: list[tuple[str, str]] = [
    ("umbrella", "OUTSTANDING.md"),
    # #claudemd-restructure (2026-06-11): constitution moved to the vault root;
    # the _claude/ entry covers the redirect stub until its ~2026-09 deletion
    # (the list is only-if-exists, so the stale entry is drift-free).
    ("vault", "CLAUDE.md"),
    ("vault", "_claude/CLAUDE.md"),
    ("umbrella", "CLAUDE.md"),
    ("routines", "CLAUDE.md"),
]

COMMIT_DISPLAY_CAP = 20  # per-repo table cap; overflow shown as "+N more"

# ASCII unit separator — used as the git --pretty field delimiter so commit
# subjects containing '|' or other punctuation never confuse the parse.
_US = "\x1f"

# Effectively whole-file diff context for the decision-doc patch scan, so a
# convention bullet's section heading is always present in its hunk (see
# _in_window_commit_blocks). No tracked doc approaches this many lines.
_FULL_FILE_CONTEXT = 100000


# ── Data model ─────────────────────────────────────────────────────────────


@dataclass
class CommitRow:
    short_hash: str
    date: str            # ISO date (YYYY-MM-DD)
    subject: str


@dataclass
class RepoCommits:
    repo: str            # display name, e.g. "routines"
    path: str            # absolute path (posix)
    commits: list[CommitRow] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.commits)


@dataclass
class ShippedItem:
    number: str          # e.g. "#38"
    title: str
    commit: str | None = None
    done: bool = True    # [✓] True, [✗] False (won't-do / obsoleted)


@dataclass
class RoutineActivity:
    routine: str
    ok: int = 0
    error: int = 0
    skipped: int = 0
    partial: int = 0

    @property
    def total(self) -> int:
        return self.ok + self.error + self.skipped + self.partial


@dataclass
class SpendRow:
    name: str            # skill / provider label
    cost_usd: float
    calls: int


@dataclass
class TelemetryTotals:
    calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    top_spend: list[SpendRow] = field(default_factory=list)


@dataclass
class DecisionSignal:
    source: str          # repo-relative doc path
    text: str


@dataclass
class NextWeekCandidate:
    number: str
    title: str
    priority: str = ""
    owner: str = ""
    effort: str = ""


@dataclass
class WeekContext:
    start: date
    until: date          # exclusive upper bound
    label: str           # ISO "YYYY-Www"
    generated_at: str
    repos: list[RepoCommits] = field(default_factory=list)
    shipped: list[ShippedItem] = field(default_factory=list)
    routines: list[RoutineActivity] = field(default_factory=list)
    telemetry: TelemetryTotals = field(default_factory=TelemetryTotals)
    decision_signals: list[DecisionSignal] = field(default_factory=list)
    patterns_stolen: list[str] = field(default_factory=list)
    next_week: list[NextWeekCandidate] = field(default_factory=list)

    @property
    def total_commits(self) -> int:
        return sum(r.count for r in self.repos)

    @property
    def active_repos(self) -> int:
        return sum(1 for r in self.repos if r.count)

    @property
    def all_subjects(self) -> list[str]:
        return [c.subject for r in self.repos for c in r.commits]

    @property
    def total_fires(self) -> int:
        return sum(r.total for r in self.routines)

    @property
    def total_errors(self) -> int:
        return sum(r.error for r in self.routines)

    @property
    def last_included_day(self) -> date:
        return self.until - timedelta(days=1)


# ── Window resolution ──────────────────────────────────────────────────────


def resolve_window(today: date, start: date | None = None) -> tuple[date, date, str]:
    """Resolve the review window + its ISO-week label.

    Default (no ``start``): the 7 days ENDING at ``today`` — i.e.
    ``[today-7d, today)``. Fired by the Monday cron this is exactly the
    prior Mon-Sun week. With ``start`` supplied: ``[start, start+7d)`` for
    ad-hoc historical windows. ``until`` is exclusive.

    The label is the ISO year+week of ``start`` (e.g. ``"2026-W22"``), so
    a same-week re-run resolves the same output filename (idempotent).
    """
    if start is not None:
        start_date = start
        until = start + timedelta(days=7)
    else:
        until = today
        start_date = today - timedelta(days=7)
    iso_year, iso_week, _ = start_date.isocalendar()
    label = f"{iso_year}-W{iso_week:02d}"
    return start_date, until, label


# ── Commits per repo ───────────────────────────────────────────────────────


def _git_log(repo: Path, start: date, until: date) -> list[CommitRow] | None:
    """Return commit rows in ``[start, until)`` for one repo, or None if the
    path is not a readable git repo.

    Deliberately does NOT use git's ``--since``/``--until``: those prune the
    walk as soon as a commit older than ``--since`` is reached, assuming
    monotonic history, and silently drop in-window commits when history is
    non-monotonic (rebases, cherry-picks, imported dates). We read the full
    log and filter on the (author) date in Python so the window is exact."""
    if not (repo / ".git").exists():
        return None
    cmd = [
        "git", "-C", str(repo), "log", "--no-merges",
        f"--pretty=format:%h{_US}%ad{_US}%s",
        "--date=short",
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, encoding="utf-8", errors="replace", timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("week-in-review: git log failed for %s: %s", repo, e)
        return None
    if proc.returncode != 0:
        log.warning("week-in-review: git log rc=%s for %s: %s",
                    proc.returncode, repo, (proc.stderr or "").strip()[:200])
        return None

    rows: list[CommitRow] = []
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split(_US)
        if len(parts) != 3:
            continue
        short_hash, dstr, subject = parts
        try:
            cdate = date.fromisoformat(dstr)
        except ValueError:
            continue
        if not (start <= cdate < until):
            continue
        rows.append(CommitRow(short_hash=short_hash, date=dstr, subject=subject))
    return rows


def collect_commits(
    repos: list[tuple[str, Path]], start: date, until: date,
) -> list[RepoCommits]:
    """One :class:`RepoCommits` per repo that is a readable git repo. Repos
    that don't exist / aren't git repos are skipped silently (not every
    machine has every repo cloned)."""
    out: list[RepoCommits] = []
    for name, path in repos:
        rows = _git_log(path, start, until)
        if rows is None:
            continue
        out.append(RepoCommits(repo=name, path=path.as_posix(), commits=rows))
    return out


# ── OUTSTANDING ARCHIVE diff — items shipped this week ──────────────────────

_SHIPPED_RE = re.compile(
    r"^\+#{2,4}\s*\[(?P<mark>[✓✗x])\]\s*(?P<num>#[\w.\-]+)\s*[·:\-]\s*(?P<title>.+?)\s*$"
)
_HASH_RE = re.compile(r"`([0-9a-f]{7,40})`")
_COMMIT_KW_RE = re.compile(r"commit\s+([0-9a-f]{7,40})", re.IGNORECASE)


def _in_window_commit_blocks(
    repo: Path, rel: str, start: date, until: date,
) -> list[str]:
    """Return one unified-diff string per commit (over ``rel``) whose author
    date falls in ``[start, until)``.

    Like :func:`_git_log`, this avoids ``--since``/``--until`` (which would
    prune the walk on non-monotonic history) — it reads the file's full
    ``-p`` log, splits on a per-commit ``%ad`` sentinel, and keeps only the
    in-window blocks. The file's history is bounded, so reading it all is
    cheap. READ-ONLY git."""
    if not (repo / ".git").exists() or not (repo / rel).exists():
        return []
    # Whole-file diff context (``--unified``). The default 3 lines would omit
    # the section heading whenever a convention bullet is added more than 3
    # lines below its ``## ADOPTED CONVENTIONS`` / ``Decisions`` heading,
    # leaving _scan_convention_additions with an empty heading and silently
    # dropping the signal (codex #38 r3 P2). These are single bounded docs,
    # so reading them whole per in-window commit is cheap and makes the
    # heading state-machine reliable regardless of where the bullet lands.
    cmd = [
        "git", "-C", str(repo), "log", "--no-merges", "-p", "--date=short",
        f"--unified={_FULL_FILE_CONTEXT}",
        f"--pretty=format:{_US}%ad", "--", rel,
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, encoding="utf-8", errors="replace", timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("week-in-review: patch log failed for %s @ %s: %s", rel, repo, e)
        return []
    if proc.returncode != 0:
        return []

    blocks: list[str] = []
    cur: list[str] | None = None
    keep = False
    for line in proc.stdout.splitlines():
        if line.startswith(_US):
            if cur is not None and keep:
                blocks.append("\n".join(cur))
            try:
                d = date.fromisoformat(line[1:11])
                keep = start <= d < until
            except ValueError:
                keep = False
            cur = []
            continue
        if cur is not None:
            cur.append(line)
    if cur is not None and keep:
        blocks.append("\n".join(cur))
    return blocks


def collect_shipped(
    umbrella_repo: Path, start: date, until: date,
    *, outstanding_rel: str = "OUTSTANDING.md",
) -> list[ShippedItem]:
    """Parse the OUTSTANDING.md diff over the window for newly-ARCHIVED
    items (lines added as ``### [✓] #N · ...`` / ``[✗]``). READ-ONLY git."""
    seen: set[str] = set()
    out: list[ShippedItem] = []
    for block in _in_window_commit_blocks(umbrella_repo, outstanding_rel, start, until):
        for line in block.splitlines():
            m = _SHIPPED_RE.match(line)
            if not m:
                continue
            num = m.group("num")
            if num in seen:
                continue
            seen.add(num)
            title = m.group("title").strip()
            commit = None
            hm = _COMMIT_KW_RE.search(title) or _HASH_RE.search(title)
            if hm:
                commit = hm.group(1)
            out.append(ShippedItem(
                number=num,
                title=title,
                commit=commit,
                done=m.group("mark") in ("✓",),
            ))
    return out


# ── Audit JSONL window — routine activity ──────────────────────────────────


# ``runs/`` also holds aggregate / non-per-routine streams that must NOT be
# counted as routine fires: ``activity.jsonl`` is the #60 unified structured
# stream (one row per activity across the whole platform, no top-level
# ``status``) — counting it would dominate the totals (codex #38 P2).
_AGGREGATE_STREAMS = {"activity"}


def collect_routine_activity(
    runs_dir: Path, start: date, until: date,
) -> list[RoutineActivity]:
    """Per-``runs/<routine>.jsonl`` counts for rows whose ``ts`` date falls
    in ``[start, until)``. READ-ONLY (the #60 multi-consumer lesson).

    Counts ONLY genuine per-routine audit rows — those carrying a top-level
    ``status`` (the legacy ``audit.write`` contract). Aggregate streams and
    structured-activity rows (which carry ``status`` only nested under
    ``details``) are skipped so they don't inflate the fire counts."""
    if not runs_dir.is_dir():
        return []
    import json

    out: list[RoutineActivity] = []
    for log_path in sorted(runs_dir.glob("*.jsonl")):
        if log_path.stem in _AGGREGATE_STREAMS:
            continue
        counts: Counter[str] = Counter()
        try:
            with log_path.open("r", encoding="utf-8") as f:
                for raw in f:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        rec = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    status = rec.get("status")
                    if status is None:          # not a per-routine audit row
                        continue
                    if not _ts_in_window(rec.get("ts"), start, until):
                        continue
                    counts[str(status)] += 1
        except OSError as e:
            log.warning("week-in-review: read %s failed: %s", log_path, e)
            continue
        if not counts:
            continue
        out.append(RoutineActivity(
            routine=log_path.stem,
            ok=counts.get("ok", 0),
            error=counts.get("error", 0),
            skipped=counts.get("skipped", 0),
            partial=counts.get("partial", 0),
        ))
    # Errors first, then busiest.
    out.sort(key=lambda r: (-r.error, -r.total, r.routine))
    return out


def _ts_in_window(ts: object, start: date, until: date) -> bool:
    if not isinstance(ts, str) or len(ts) < 10:
        return False
    try:
        d = date.fromisoformat(ts[:10])
    except ValueError:
        return False
    return start <= d < until


# ── Telemetry roll-up ──────────────────────────────────────────────────────


def collect_telemetry(
    telemetry_path: Path, start: date, until: date, *, top_n: int = 5,
) -> TelemetryTotals:
    """Aggregate ``telemetry/llm_calls.jsonl`` rows over the window. Groups
    spend by ``skill`` (falling back to ``provider`` / ``model``)."""
    totals = TelemetryTotals()
    if not telemetry_path.is_file():
        return totals
    import json

    cost_by: Counter[str] = Counter()
    calls_by: Counter[str] = Counter()
    try:
        with telemetry_path.open("r", encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    rec = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not _ts_in_window(rec.get("ts"), start, until):
                    continue
                totals.calls += 1
                totals.tokens_in += int(rec.get("tokens_in") or 0)
                totals.tokens_out += int(rec.get("tokens_out") or 0)
                cost = float(rec.get("cost_usd") or 0.0)
                totals.cost_usd += cost
                label = (
                    rec.get("skill")
                    or rec.get("provider")
                    or rec.get("model")
                    or "raw"
                )
                cost_by[str(label)] += cost
                calls_by[str(label)] += 1
    except OSError as e:
        log.warning("week-in-review: telemetry read failed: %s", e)
        return totals

    totals.cost_usd = round(totals.cost_usd, 4)
    totals.top_spend = [
        SpendRow(name=name, cost_usd=round(cost, 4), calls=calls_by[name])
        for name, cost in cost_by.most_common(top_n)
    ]
    return totals


# ── Decisions-locked raw material (ADOPTED CONVENTIONS diff) ────────────────


def collect_decision_signals(
    repos: dict[str, Path], start: date, until: date, *, cap: int = 15,
) -> list[DecisionSignal]:
    """Added bullet lines that landed under an ``ADOPTED CONVENTIONS`` /
    ``Decisions`` heading in the tracked docs over the window. Raw material
    for the LLM "Decisions locked" section — never fatal if empty."""
    out: list[DecisionSignal] = []
    for repo_name, rel in DECISION_DOC_PATHS:
        repo = repos.get(repo_name)
        if repo is None:
            continue
        for block in _in_window_commit_blocks(repo, rel, start, until):
            out.extend(_scan_convention_additions(block, source=rel))
            if len(out) >= cap:
                return out[:cap]
    return out[:cap]


def _scan_convention_additions(diff_text: str, *, source: str) -> list[DecisionSignal]:
    """Walk a unified diff, tracking the current heading from BOTH context
    and added lines, and record ADDED bullets sitting under a heading that
    mentions ADOPTED CONVENTIONS / DECISIONS."""
    out: list[DecisionSignal] = []
    heading = ""
    for line in diff_text.splitlines():
        if line.startswith(("+++", "---", "@@", "diff ", "index ")):
            continue
        sign = line[:1]
        if sign not in ("+", " "):
            continue
        content = line[1:]
        stripped = content.lstrip()
        if stripped.startswith("#"):
            heading = stripped.strip("# ").strip()
            continue
        if sign == "+" and stripped[:2] in ("- ", "* "):
            up = heading.upper()
            if "ADOPTED CONVENTION" in up or "DECISION" in up:
                text = stripped[2:].strip()
                if text:
                    out.append(DecisionSignal(source=source, text=text))
    return out


# ── Patterns stolen (commit-message grep) ──────────────────────────────────

_PATTERN_RE = re.compile(
    r"\b(pattern from|stolen from|borrowed from|per (?:the )?\w+ eval|"
    r"eval(?:s|uation)?[:/]|inspired by)\b",
    re.IGNORECASE,
)


def collect_patterns_stolen(subjects: list[str], *, cap: int = 10) -> list[str]:
    """Commit subjects that reference borrowing a pattern from an eval /
    another project. Best-effort signal; the section omits cleanly when
    empty."""
    out: list[str] = []
    seen: set[str] = set()
    for s in subjects:
        if _PATTERN_RE.search(s) and s not in seen:
            seen.add(s)
            out.append(s)
        if len(out) >= cap:
            break
    return out


# ── Next-week candidates (open OUTSTANDING items) ──────────────────────────

_OPEN_ITEM_RE = re.compile(
    r"^#{2,4}\s*\[ \]\s*(?P<num>#[\w.\-]+)\s*[·:\-]\s*(?P<title>.+?)\s*$"
)
_HEADER_RE = re.compile(r"^#{1,6}\s")
_PRIORITY_RE = re.compile(r"Priority:\**\s*\**\s*(HIGH|MEDIUM|LOW)", re.IGNORECASE)
_OWNER_RE = re.compile(r"Owner:\**\s*\**\s*([^\n·•|]+?)(?:\s*[·•|]|\*\*|$)")
_EFFORT_RE = re.compile(r"Effort:\**\s*\**\s*([^\n·•|]+?)(?:\s*[·•|]|\*\*|$)")

_PRIORITY_RANK = {"HIGH": 0, "MEDIUM": 1, "LOW": 2, "": 3}


def collect_next_week(
    outstanding_path: Path, *, top_n: int = 5,
) -> list[NextWeekCandidate]:
    """Parse open ``### [ ] #N · title`` items + their Priority/Owner/Effort
    from OUTSTANDING.md. Ranked HIGH-first then by file order (oldest);
    top N. Read directly from the file (never written)."""
    try:
        text = outstanding_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    lines = text.splitlines()
    cands: list[NextWeekCandidate] = []
    i = 0
    n = len(lines)
    while i < n:
        m = _OPEN_ITEM_RE.match(lines[i])
        if not m:
            i += 1
            continue
        cand = NextWeekCandidate(number=m.group("num"), title=m.group("title").strip())
        j = i + 1
        while j < n and not _HEADER_RE.match(lines[j]):
            blk = lines[j]
            if not cand.priority:
                pm = _PRIORITY_RE.search(blk)
                if pm:
                    cand.priority = pm.group(1).upper()
            if not cand.owner:
                om = _OWNER_RE.search(blk)
                if om:
                    cand.owner = om.group(1).strip()
            if not cand.effort:
                em = _EFFORT_RE.search(blk)
                if em:
                    cand.effort = em.group(1).strip()
            j += 1
        cands.append(cand)
        i = j

    cands.sort(key=lambda c: _PRIORITY_RANK.get(c.priority, 3))
    return cands[:top_n]


# ── Orchestrator ───────────────────────────────────────────────────────────


def collect_week(
    *,
    start: date,
    until: date,
    label: str,
    repos: list[tuple[str, Path]] | None = None,
    umbrella_repo: Path | None = None,
    runs_dir: Path | None = None,
    telemetry_path: Path | None = None,
    outstanding_path: Path | None = None,
    generated_at: str | None = None,
) -> WeekContext:
    """Run every mechanical collector for the window and assemble a
    :class:`WeekContext`. All sources default to the real absolute paths and
    are individually overridable for tests."""
    repos = repos if repos is not None else DEFAULT_REPOS
    umbrella_repo = umbrella_repo or UMBRELLA
    runs_dir = runs_dir or DEFAULT_RUNS_DIR
    outstanding_path = outstanding_path or DEFAULT_OUTSTANDING
    if telemetry_path is None:
        from routines.telemetry import llm_writer
        telemetry_path = llm_writer.LLM_CALLS_JSONL
    if generated_at is None:
        generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    repo_commits = collect_commits(repos, start, until)
    ctx = WeekContext(
        start=start, until=until, label=label, generated_at=generated_at,
        repos=repo_commits,
        shipped=collect_shipped(umbrella_repo, start, until),
        routines=collect_routine_activity(runs_dir, start, until),
        telemetry=collect_telemetry(telemetry_path, start, until),
        decision_signals=collect_decision_signals(dict(repos), start, until),
        next_week=collect_next_week(outstanding_path),
    )
    ctx.patterns_stolen = collect_patterns_stolen(ctx.all_subjects)
    return ctx
