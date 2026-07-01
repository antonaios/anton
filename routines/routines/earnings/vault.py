"""Steps 8, 9, 11 — frontmatter-aware atomic vault writes for the earnings routine.

Everything here is idempotent and append-only (CLAUDE.md §3 rule 9 — never
overwrite a prior capture). Three write surfaces plus one read:

  * :func:`write_company_capture` (steps 8 + 11) — append the dated results
    section to ``Companies/<name>.md`` AND roll ``next-reporting-date`` forward,
    in ONE atomic write. Skips entirely if the section already exists (so a
    catch-up re-fire on the same period is a no-op and does NOT re-roll the
    date).
  * :func:`append_sector_point` (step 9) — append the sector data-point bullet
    under ``## Earnings data points`` on ``Sectors/<sector>.md`` (created from a
    minimal sector note if missing), idempotent on the company+period key.
  * :func:`read_prior_periods` — parse the hidden machine lines off the company
    page so the compare step has prior quarters with no re-extraction.
  * :func:`compute_next_reporting_date` — the deterministic roll-forward.

Writes go through :func:`routines.shared.vault_writer.atomic_write` (tempfile +
rename) so Obsidian / Smart Connections never see a half-written note. Tests
exercise all of this against tmp fixtures — the real vault is never touched in
the build session.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Iterator, Optional

from routines.earnings.calendar import CompanyEntry
from routines.earnings.render import section_heading
from routines.earnings.report import ExtractedEarnings, PriorPeriod, parse_machine_lines
from routines.shared.vault_writer import atomic_write

log = logging.getLogger(__name__)

SECTOR_DATA_SECTION = "Earnings data points"

# Page-lock tunables — guard against OUR OWN overlapping writers (see _page_lock).
_LOCK_TIMEOUT_S = 5.0
_LOCK_STALE_S = 30.0


@contextlib.contextmanager
def _page_lock(path: Path) -> Iterator[None]:
    """Best-effort exclusive lock for one vault page, guarding against our own
    concurrent writers — a scheduled sweep overlapping a manually-fired
    ``/api/workflows/earnings`` run (the scheduler's ``concurrency="skip"`` does
    NOT cover a separately-launched manual subprocess). External editors
    (Obsidian) don't honour this lock; the mtime-CAS in the writer covers that
    case. A stale lock (older than ``_LOCK_STALE_S``) is stolen; on timeout we
    proceed WITHOUT the lock — a write is better than a hang."""
    lock_path = path.with_name(path.name + ".earnings.lock")
    fd: Optional[int] = None
    acquired = False
    deadline = time.monotonic() + _LOCK_TIMEOUT_S
    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            acquired = True
            break
        except FileExistsError:
            try:
                age = time.time() - lock_path.stat().st_mtime
            except OSError:
                age = 0.0
            if age > _LOCK_STALE_S:
                with contextlib.suppress(OSError):
                    lock_path.unlink()
                continue
            if time.monotonic() >= deadline:
                log.warning("earnings vault: lock %s busy after %.1fs — proceeding without it",
                            lock_path, _LOCK_TIMEOUT_S)
                break
            time.sleep(0.05)
        except OSError as e:
            log.warning("earnings vault: could not acquire lock %s (%s) — proceeding", lock_path, e)
            break
    try:
        yield
    finally:
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)
        if acquired:
            with contextlib.suppress(OSError):
                lock_path.unlink()

# Cadence → months to advance the next reporting date.
_CADENCE_MONTHS = {
    "quarterly": 3,
    "quarter": 3,
    "q": 3,
    "semi-annual": 6,
    "semiannual": 6,
    "half-year": 6,
    "interim": 6,
    "h": 6,
    "annual": 12,
    "yearly": 12,
    "fy": 12,
}


# ─────────────────────────────────────────────────────────────────────────────
# Date roll-forward
# ─────────────────────────────────────────────────────────────────────────────


def _add_months(d: date, months: int) -> date:
    """Add ``months`` calendar months to ``d``, clamping the day to the target
    month's length (so 31 Jan + 1 month → 28/29 Feb)."""
    month_index = d.month - 1 + months
    year = d.year + month_index // 12
    month = month_index % 12 + 1
    # Days in the target month.
    if month == 12:
        next_month_first = date(year + 1, 1, 1)
    else:
        next_month_first = date(year, month + 1, 1)
    last_day = (next_month_first - date(year, month, 1)).days
    return date(year, month, min(d.day, last_day))


def _next_reporting_date(
    *,
    cadence: str,
    reported_date: Optional[date],
    explicit_next: Optional[date],
    current_next: Optional[date],
    today: Optional[date],
) -> date:
    """Core roll-forward. Priority: (1) an explicit next date stated in the
    announcement; else (2) roll the just-reported date forward by cadence; else
    (3) roll the current ``next-reporting-date`` forward; else (4) roll
    ``today``."""
    base = reported_date or current_next or today or date.today()
    # Accept an announcement-stated next date ONLY if it's strictly AFTER this
    # report — a stated date on/before the base would wedge the calendar in the
    # past and re-fire fetch/LLM work forever (#44 Codex). Else advance by cadence.
    if explicit_next is not None and explicit_next > base:
        return explicit_next
    months = _CADENCE_MONTHS.get((cadence or "quarterly").strip().lower(), 3)
    return _add_months(base, months)


def compute_next_reporting_date(
    entry: CompanyEntry,
    extracted: ExtractedEarnings,
    *,
    today: Optional[date] = None,
) -> date:
    """The provisional next reporting date for ``entry`` (cadence + dates from the
    scan-time entry). The operator can always correct the provisional value in
    the page frontmatter — the cadence advance just keeps the calendar live."""
    return _next_reporting_date(
        cadence=entry.cadence,
        reported_date=extracted.reported_date,
        explicit_next=extracted.next_reporting_date,
        current_next=entry.next_reporting_date,
        today=today,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Read prior periods
# ─────────────────────────────────────────────────────────────────────────────


def read_prior_periods(company_path: Path) -> list[PriorPeriod]:
    """Parse the hidden ``earnings-data`` machine lines off the company page into
    :class:`PriorPeriod` records (document order). Empty list if the page is
    absent / has no prior captures."""
    if not company_path.is_file():
        return []
    try:
        text = company_path.read_text(encoding="utf-8")
    except OSError as e:
        log.warning("earnings vault: failed to read %s (%s)", company_path, e)
        return []
    return [PriorPeriod.from_record(rec) for rec in parse_machine_lines(text)]


# ─────────────────────────────────────────────────────────────────────────────
# Step 8 + 11 — company capture (append section + roll next-date), atomically
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class CaptureResult:
    status: str                 # "appended" | "skipped_exists" | "missing_page"
    section: str = ""
    next_reporting_date: Optional[date] = None


# Auto-populated / backlink tail sections that results history should sit ABOVE
# (so the chronological "## YYYY-QN Results" sections aren't scattered after the
# watcher-managed Mentions block).
_TAIL_SECTIONS = ("## Mentions",)


def _section_present(body: str, heading: str) -> bool:
    target = heading.strip()
    for line in body.splitlines():
        if line.strip() == target:
            return True
    return False


def _already_captured(body: str, heading: str, extracted: ExtractedEarnings) -> bool:
    """Idempotency check robust to a re-extraction that yields a slightly
    different reported_date: match the rendered heading OR any hidden machine
    record carrying the SAME stable period label. Only the machine-record path
    requires a *real* period (year + period present) so degenerate
    "unknown" labels don't over-collapse distinct captures (#44 Codex SEV-1)."""
    if _section_present(body, heading):
        return True
    if extracted.fiscal_year is not None and extracted.fiscal_period:
        period_label = extracted.period_label
        for rec in parse_machine_lines(body):
            if str(rec.get("period_label") or "").strip() == period_label:
                return True
    return False


def _insert_section(body: str, section_md: str) -> str:
    """Insert ``section_md`` BEFORE the first auto-populated tail section
    (``## Mentions``), else at end-of-body. Append-only — existing content is
    never rewritten, only repositioned-around."""
    block = section_md.rstrip("\n")
    lines = body.splitlines()
    insert_idx: Optional[int] = None
    for i, line in enumerate(lines):
        if line.strip() in _TAIL_SECTIONS:
            insert_idx = i
            break
    if insert_idx is None:
        return body.rstrip("\n") + "\n\n" + block + "\n"
    before = "\n".join(lines[:insert_idx]).rstrip("\n")
    after = "\n".join(lines[insert_idx:]).strip("\n")
    return f"{before}\n\n{block}\n\n{after}\n"


def _mtime_ns(path: Path) -> Optional[int]:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return None


def _read_stable_text(path: Path) -> tuple[Optional[str], Optional[int], bool]:
    """Stable-snapshot read of raw text → ``(text, mtime, stable)``, guaranteed not
    to have changed DURING the read (stat → read → stat, accept only when the two
    stats match). This closes the read-before-stat gap where an external edit
    would otherwise pair stale content with a fresh mtime (#44 Codex SEV-1).

    ``text=None`` with ``stable=True`` means the file is (stably) ABSENT — the
    caller may create it. ``stable=False`` means we could NOT obtain a consistent
    snapshot within the retry budget (the page is being actively rewritten); the
    caller must NOT write — there's no trustworthy CAS baseline."""
    for _ in range(5):
        if not path.is_file():
            return None, None, True   # stably absent → create path
        m1 = _mtime_ns(path)
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return None, None, False  # read error — don't write
        m2 = _mtime_ns(path)
        if m1 is not None and m1 == m2:
            return text, m2, True
    return None, None, False


# ── Raw-text frontmatter patching ────────────────────────────────────────────
# We patch the company page as RAW TEXT rather than round-tripping it through a
# YAML load/dump. That keeps the operator's frontmatter block byte-identical
# (quoting, key order, comments, multi-line scalars all preserved) — true
# append-only (§3 rule 9), and it avoids fighting a concurrent frontmatter
# relabelling pass (#41) that a full re-serialise would clobber. We touch ONLY
# the body (section append) and the single ``next-reporting-date:`` line (roll).

_FM_RE = re.compile(r"\A(---\r?\n.*?\r?\n---[ \t]*\r?\n)(.*)\Z", re.DOTALL)


def _split_frontmatter(text: str) -> tuple[str, str]:
    """Split ``text`` into ``(fm_block, body)``. ``fm_block`` includes the
    closing ``---`` and its trailing newline; ``""`` when there's no frontmatter."""
    m = _FM_RE.match(text)
    if m:
        return m.group(1), m.group(2)
    return "", text


def _fm_field(fm_block: str, key: str) -> str:
    """Read a top-level scalar ``key:`` value from a raw frontmatter block,
    canonicalised the way PyYAML would see it: a quoted scalar yields its inner
    text; an unquoted scalar has any trailing ``# comment`` stripped (YAML treats
    ` #` as a comment). This keeps the value comparable to a canonical ISO string
    so an inline comment like ``next-reporting-date: 2026-05-07 # estimate`` does
    not read as an operator edit and wedge the roll (#44 Codex). ``""`` when
    absent/empty."""
    m = re.search(rf"(?mi)^{re.escape(key)}:[ \t]*(.*?)[ \t]*$", fm_block)
    if not m:
        return ""
    val = m.group(1).strip()
    if not val:
        return ""
    # Quoted scalar — take the quoted content verbatim (a ' #' inside is literal).
    if val[0] in "\"'":
        close = val.find(val[0], 1)
        if close != -1:
            return val[1:close]
        return val[1:]
    # Unquoted — strip a YAML inline comment (whitespace + '#' onwards).
    hash_idx = val.find(" #")
    if hash_idx != -1:
        val = val[:hash_idx]
    return val.strip()


def _set_fm_field(fm_block: str, key: str, value: str) -> str:
    """Return ``fm_block`` with ``key:`` set to ``value`` (plain scalar). Replaces
    the existing line in place if present, else inserts before the closing
    ``---``. Everything else is left byte-identical."""
    pat = re.compile(rf"(?mi)^{re.escape(key)}:.*$")
    if pat.search(fm_block):
        return pat.sub(f"{key}: {value}", fm_block, count=1)
    lines = fm_block.splitlines(keepends=True)
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].strip() == "---":
            lines.insert(i, f"{key}: {value}\n")
            return "".join(lines)
    return fm_block


def write_company_capture(
    company_path: Path,
    entry: CompanyEntry,
    extracted: ExtractedEarnings,
    section_md: str,
    *,
    today: Optional[date] = None,
    vault_root: Optional[Path] = None,
) -> CaptureResult:
    """Append the dated results section to the company page — idempotent on the
    period, append-only. Does NOT roll ``next-reporting-date`` (that's the
    separate :func:`roll_reporting_date`, called only after the downstream
    must-succeed side-effects so a failure leaves the company DUE for retry —
    #44 Codex SEV-2).

    Returns ``status="skipped_exists"`` (no write) when the period is already
    captured; ``status="missing_page"`` if the company note doesn't exist (the
    routine never fabricates a company page — that's ``add-watch``'s job).

    Concurrency: two layers. (1) :func:`_page_lock` serialises our OWN overlapping
    writers (a scheduled sweep vs a manual ``/api`` fire). (2) Inside the lock,
    a stable-snapshot read + a pre-write mtime guard catch an external editor
    (Obsidian, which ignores the lock): we only commit the rename when the file
    is byte-for-byte the snapshot we applied to, retrying on a detected change so
    a concurrent edit is preserved, not clobbered (#44 Codex SEV-1)."""
    heading = section_heading(extracted)
    if not company_path.is_file():
        log.warning("earnings vault: company page %s missing — capture skipped", company_path)
        return CaptureResult(status="missing_page", section=heading)

    with _page_lock(company_path):
        for _ in range(4):
            text, mtime, stable = _read_stable_text(company_path)
            if not stable:
                continue   # couldn't read a consistent snapshot — retry, never write
            if text is None:
                return CaptureResult(status="missing_page", section=heading)
            if _already_captured(text, heading, extracted):
                return CaptureResult(status="skipped_exists", section=heading)
            # Patch RAW text: frontmatter block stays byte-identical; only the
            # body gains the new section (append-only, #41-safe).
            fm_block, body = _split_frontmatter(text)
            new_text = fm_block + _insert_section(body, section_md)
            # Commit ONLY if the page is byte-identical to our snapshot (mtime
            # unchanged). On a detected change we retry on fresh content. We never
            # fall back to writing a stale snapshot — if we can't win a clean
            # window we abort WITHOUT writing so an external edit is never
            # clobbered (#44 Codex SEV-1). Residual = stat→rename (irreducible).
            if mtime is not None and _mtime_ns(company_path) == mtime:
                atomic_write(company_path, new_text, vault_root=vault_root)
                log.info("earnings vault: appended %r to %s", heading, company_path)
                return CaptureResult(status="appended", section=heading)
            log.info("earnings vault: %s changed under capture — retrying on fresh content", company_path)
    log.warning("earnings vault: %s contended after retries — deferring capture to next sweep", company_path)
    return CaptureResult(status="contended", section=heading)


def roll_reporting_date(
    company_path: Path,
    entry: CompanyEntry,
    extracted: ExtractedEarnings,
    *,
    today: Optional[date] = None,
    expected_current: Optional[date] = None,
    vault_root: Optional[Path] = None,
) -> Optional[date]:
    """Roll ``next-reporting-date`` forward on the company page. Idempotent (a
    no-op if already at the target) and concurrency-guarded like the capture.

    Called by the pipeline only AFTER the must-succeed side-effects (the material
    proposal) are durable — so if an earlier step failed, the date is NOT rolled,
    the company stays DUE, and the next sweep retries the whole capture
    idempotently (the reachable self-heal path — #44 Codex SEV-2).

    The fetch+extract window is long (Ollama), during which the operator may have
    edited the calendar in Obsidian. So we (a) recompute the target from the LIVE
    frontmatter cadence (not the stale scan-time entry), and (b) if
    ``expected_current`` is given and the page's current ``next-reporting-date``
    no longer matches the date we dispatched on, ABORT — the operator moved the
    date during our run and we must not stomp their edit (#44 Codex SEV-2 r5)."""
    if not company_path.is_file():
        return None
    expected_str = expected_current.isoformat() if expected_current else ""
    with _page_lock(company_path):
        for _ in range(4):
            text, mtime, stable = _read_stable_text(company_path)
            if not stable:
                continue
            if text is None:
                return None   # page vanished
            fm_block, body = _split_frontmatter(text)
            if not fm_block:
                # No frontmatter block → no ``next-reporting-date:`` line to patch.
                # _set_fm_field can't insert into an empty block, so a write here
                # would leave the file unchanged while we falsely report a rolled
                # date (calendar drift). Report honestly: nothing rolled. A watched
                # company always has frontmatter, so this only guards a page whose
                # frontmatter was stripped mid-run (#44 Codex extra — vault.py roll
                # no-ops on a frontmatter-less page).
                log.warning("earnings vault: %s has no frontmatter — cannot roll next-reporting-date", company_path)
                return None
            current_str = _fm_field(fm_block, "next-reporting-date")
            # Respect ANY concurrent operator edit to the calendar — including
            # CLEARING the field (current_str == ""), which is a legitimate "stop
            # tracking this date" edit we must not silently undo (#44 Codex SEV-2 r6).
            if expected_str and current_str != expected_str:
                log.info("earnings vault: %s next-reporting-date changed under us "
                         "(%r != dispatched %s) — not rolling (operator edit respected)",
                         company_path, current_str, expected_str)
                return None
            # Recompute from the LIVE cadence + current date so a cadence edit is honoured.
            cadence = _fm_field(fm_block, "reporting-cadence") or entry.cadence or "quarterly"
            current_date: Optional[date] = None
            if current_str:
                try:
                    current_date = date.fromisoformat(current_str[:10])
                except ValueError:
                    current_date = None
            nxt = _next_reporting_date(
                cadence=cadence, reported_date=extracted.reported_date,
                explicit_next=extracted.next_reporting_date,
                current_next=current_date, today=today,
            )
            target = nxt.isoformat()
            if current_str == target:
                return nxt   # already rolled — idempotent no-op
            # Patch ONLY the date line; the rest of the frontmatter + body is
            # left byte-identical (append-only, #41-safe).
            new_text = _set_fm_field(fm_block, "next-reporting-date", target) + body
            if mtime is not None and _mtime_ns(company_path) == mtime:
                atomic_write(company_path, new_text, vault_root=vault_root)
                log.info("earnings vault: rolled next-reporting-date of %s → %s", company_path, target)
                return nxt
            log.info("earnings vault: %s changed under date-roll — retrying", company_path)
    # Couldn't win a clean window — leave the date as-is (company stays due and
    # the next sweep retries). Never write a stale snapshot (#44 Codex SEV-1).
    log.warning("earnings vault: %s contended after retries — date-roll deferred", company_path)
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Step 9 — sector aggregation
# ─────────────────────────────────────────────────────────────────────────────


def _minimal_sector_note(sector_slug: str) -> str:
    title = sector_slug.replace("-", " ").strip().title() or sector_slug
    return (
        "---\n"
        "type: sector\n"
        f"sector: {sector_slug}\n"
        "memory_kind: semantic\n"
        "sensitivity: internal\n"
        "tags: [sector]\n"
        "---\n\n"
        f"# {title}\n\n"
        f"## {SECTOR_DATA_SECTION}\n\n"
    )


def append_sector_point(
    sector_path: Path,
    sector_slug: str,
    bullet: str,
    *,
    dedup_key: str,
    vault_root: Optional[Path] = None,
) -> dict[str, Any]:
    """Append ``bullet`` under ``## {SECTOR_DATA_SECTION}`` on the sector page,
    creating a minimal sector note if absent. Idempotent: if any existing line
    under the section already contains ``dedup_key`` the append is skipped.

    ``dedup_key`` is a stable substring uniquely identifying this
    company+period (e.g. ``"Companies/Acme Plc|...2026-Q1"`` fragment).

    Concurrency-guarded (lock + stable snapshot + pre-write mtime check) like the
    company write, so overlapping writers to the SAME sector page — many
    companies in one sector, or a manual fire racing the cron — can't corrupt it
    or clobber each other's points (#44 Codex SEV-2). Best-effort by design
    (step 9): a contended write returns ``"contended"`` rather than blocking; the
    operator-facing alert (not this aggregate) is what gates the calendar."""
    with _page_lock(sector_path):
        for _ in range(4):
            text, mtime, stable = _read_stable_text(sector_path)
            if not stable:
                continue   # unstable read — retry, never write
            if text is None:
                text = _minimal_sector_note(sector_slug)
                mtime = None   # file stably ABSENT — expect it still absent at write time
            if dedup_key and dedup_key in text:
                return {"status": "skipped_duplicate"}
            new_text = _append_under_heading(text, f"## {SECTOR_DATA_SECTION}", bullet.rstrip("\n"))
            # Commit only if the page is still in the state we read: absent→still
            # absent (None == None → create), or present→unchanged mtime. A
            # detected change (incl. another writer creating the file) → retry.
            if _mtime_ns(sector_path) == mtime:
                atomic_write(sector_path, new_text, vault_root=vault_root)
                log.info("earnings vault: appended sector point to %s", sector_path)
                return {"status": "appended"}
            log.info("earnings vault: sector %s changed under append — retrying", sector_path)
    log.warning("earnings vault: sector %s contended after retries — point deferred", sector_path)
    return {"status": "contended"}


def _append_under_heading(text: str, heading: str, bullet: str) -> str:
    """Append ``bullet`` under ``heading`` (creating the section at EOF if
    absent). Append-only; existing content untouched. Inserts before the next
    ``##`` heading so the bullet stays inside its section."""
    lines = text.splitlines()
    head_idx: Optional[int] = None
    for i, line in enumerate(lines):
        if line.strip() == heading.strip():
            head_idx = i
            break

    if head_idx is None:
        body = text.rstrip("\n")
        return f"{body}\n\n{heading}\n\n{bullet}\n"

    # Find the end of the section: next top-level heading, else EOF.
    end_idx = len(lines)
    for j in range(head_idx + 1, len(lines)):
        if lines[j].startswith("## "):
            end_idx = j
            break

    # Back up over trailing blank lines so the bullet sits flush.
    insert_at = end_idx
    while insert_at - 1 > head_idx and lines[insert_at - 1].strip() == "":
        insert_at -= 1

    new_lines = lines[:insert_at] + [bullet] + lines[insert_at:]
    out = "\n".join(new_lines)
    if not out.endswith("\n"):
        out += "\n"
    return out


__all__ = [
    "SECTOR_DATA_SECTION",
    "CaptureResult",
    "compute_next_reporting_date",
    "read_prior_periods",
    "write_company_capture",
    "roll_reporting_date",
    "append_sector_point",
]
