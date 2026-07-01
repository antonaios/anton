"""Stale-items detector for the dashboard rollup endpoint (#69).

Surfaces three classes of "this has been sitting around too long":

  1. **Stale vault notes** — files in ``VAULT`` whose frontmatter
     ``last_reviewed`` is older than ``ANTON_STALE_NOTE_DAYS`` days
     (default 90). When the frontmatter key is absent, falls back to
     file ``mtime``. The spec called for ``last_reviewed`` but no
     existing routine writes that key — the closest existing convention
     is ``last_refreshed`` on sector-claim files (see
     ``routines/vault_health/freshness.py``). We honour both keys and
     mtime as a final fallback. The deviation is logged at module-import
     time and surfaced in the returned items' ``signal`` field
     (``"frontmatter"`` vs ``"mtime"``) so the dashboard can disclose
     which heuristic fired.

  2. **Stale proposals** — pending proposals (same source as
     ``GET /api/proposals/pending``) whose ``date`` frontmatter is
     older than ``ANTON_STALE_PROPOSAL_DAYS`` days (default 14). Reuses
     ``routines.api.routes.proposals._walk_pending`` so the source of
     truth never diverges.

  3. **Stale projects** — vault ``Projects/<name>/`` directories whose
     ``00 Brief.md`` mtime is older than ``ANTON_STALE_PROJECT_DAYS``
     days (default 30).

The top-level :func:`detect_stale` combines all three, sorts oldest-
first (DESC by ``days_stale``), truncates to the top 10, and respects
the operator's workspace tier so a non-confidential rollup never
returns notes / proposals / projects whose own ``sensitivity`` is
``confidential`` or ``MNPI``.

Spec: ``OUTSTANDING.md`` #69 lines 437-443.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, asdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Literal

import frontmatter

from routines.api.deps import VAULT

log = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────────────
# Thresholds — spec defaults overridable via env vars per the brief
# ────────────────────────────────────────────────────────────────────────────


def _env_int(name: str, default: int) -> int:
    """Read a positive int from env, fall back to ``default`` if missing /
    malformed / non-positive. We never let an env-var typo silently disable
    the detector (would mark everything stale)."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        v = int(raw.strip())
    except (ValueError, AttributeError):
        log.warning("stale: %s=%r not int — using default %d", name, raw, default)
        return default
    if v <= 0:
        log.warning("stale: %s=%d ≤ 0 — using default %d", name, v, default)
        return default
    return v


def stale_note_days() -> int:
    return _env_int("ANTON_STALE_NOTE_DAYS", 90)


def stale_proposal_days() -> int:
    return _env_int("ANTON_STALE_PROPOSAL_DAYS", 14)


def stale_project_days() -> int:
    return _env_int("ANTON_STALE_PROJECT_DAYS", 30)


# Vault notes are stale-flagged using either of two frontmatter conventions:
#   * 'last_reviewed' — hand-curated by the operator (e.g. standing-positions library)
#   * 'last_refreshed' — routine-written (e.g. sector-claim files, see vault_health/freshness.py)
# Falls back to file mtime when neither key is present. The 'signal' field on each
# returned StaleItem records which heuristic fired so the dashboard can disclose it.


# ────────────────────────────────────────────────────────────────────────────
# Dataclass
# ────────────────────────────────────────────────────────────────────────────


StaleKind = Literal["note", "proposal", "project"]
StaleSignal = Literal["frontmatter", "mtime"]
SensitivityTier = Literal["public", "internal", "confidential", "MNPI"]


@dataclass
class StaleItem:
    """One stale artefact. ``last_touched`` is ISO-8601 UTC; ``days_stale``
    is the integer days elapsed at detection time. ``signal`` records which
    heuristic fired (frontmatter key vs file mtime fallback)."""

    kind: StaleKind
    id: str                                  # path-derived stable id
    label: str                               # display label (title / name)
    last_touched: str                        # ISO-8601 UTC
    days_stale: int
    signal: StaleSignal
    sensitivity: SensitivityTier = "internal"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ────────────────────────────────────────────────────────────────────────────
# Sensitivity gating
# ────────────────────────────────────────────────────────────────────────────


# Order matters — index in this list IS the tier weight (higher = stricter).
_SENS_ORDER: tuple[SensitivityTier, ...] = (
    "public",
    "internal",
    "confidential",
    "MNPI",
)


def _coerce_sensitivity(raw: Any) -> SensitivityTier:
    """Normalise a frontmatter sensitivity value. Unknown / missing → 'internal'
    (the safe middle: never leak confidential, never gratuitously hide public)."""
    if raw is None:
        return "internal"
    text = str(raw).strip()
    if text in _SENS_ORDER:
        return text  # type: ignore[return-value]
    return "internal"


def _max_visible(workspace_tier: SensitivityTier) -> set[SensitivityTier]:
    """Return the set of sensitivities a requester at ``workspace_tier`` may
    see. A ``public`` requester sees only ``public``; a ``confidential``
    requester sees ``public`` + ``internal`` + ``confidential`` (NOT MNPI).
    MNPI is its own siloed lane — only an ``MNPI`` requester sees it."""
    if workspace_tier not in _SENS_ORDER:
        workspace_tier = "internal"
    idx = _SENS_ORDER.index(workspace_tier)
    return set(_SENS_ORDER[: idx + 1])


# ────────────────────────────────────────────────────────────────────────────
# Date helpers
# ────────────────────────────────────────────────────────────────────────────


_NOW_TZ = timezone.utc


def _now_utc() -> datetime:
    return datetime.now(_NOW_TZ)


def _parse_date(raw: Any) -> datetime | None:
    """Coerce a frontmatter date / datetime / ISO-string into a UTC-aware
    datetime. Returns None on anything unparseable so caller can fall back."""
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=_NOW_TZ)
    if isinstance(raw, date):
        return datetime(raw.year, raw.month, raw.day, tzinfo=_NOW_TZ)
    text = str(raw).strip()
    if not text:
        return None
    # Try ISO-8601 first; fall back to plain YYYY-MM-DD.
    try:
        s = text[:-1] + "+00:00" if text.endswith("Z") else text
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=_NOW_TZ)
    except ValueError:
        pass
    try:
        d = date.fromisoformat(text[:10])
        return datetime(d.year, d.month, d.day, tzinfo=_NOW_TZ)
    except ValueError:
        return None


def _days_between(then: datetime, now: datetime | None = None) -> int:
    """Return integer days elapsed from ``then`` until ``now`` (default
    :func:`_now_utc`). Negative values clipped to 0 — we never report
    'negative days stale' for clock-skewed files."""
    delta = (now or _now_utc()) - then
    return max(0, int(delta.total_seconds() // 86400))


def _iso_utc(dt: datetime) -> str:
    return dt.astimezone(_NOW_TZ).isoformat(timespec="seconds")


# ────────────────────────────────────────────────────────────────────────────
# Skip rules for vault walk
# ────────────────────────────────────────────────────────────────────────────


# Directories under VAULT that should never count toward note-staleness:
# either machine-managed (.git, .obsidian), proposal queues (handled by the
# proposal detector), or working dirs that turn over fast on purpose.
_SKIP_DIRS: frozenset[str] = frozenset({
    ".git",
    ".obsidian",
    ".smart-env",
    ".recall-index",
    ".trash",
    "Routines",          # proposal queues + system runs — own detector
    "_processing",       # rejected / applied — already retired
    "Daily",             # daily notes are MEANT to age; not stale
    "Inbox",             # transient capture zone
    "Templates",         # stencils — staleness is meaningless
})


def _should_skip(path: Path, vault_root: Path) -> bool:
    """True if ``path`` lives under any skip-dir or is itself a sentinel."""
    try:
        rel = path.relative_to(vault_root)
    except ValueError:
        return True
    for part in rel.parts:
        if part in _SKIP_DIRS:
            return True
        # Underscore-prefixed sentinels (templates, indexes).
        if part.startswith("_") and part != rel.parts[-1]:
            return True
    return False


# ────────────────────────────────────────────────────────────────────────────
# Detector 1 — stale vault notes
# ────────────────────────────────────────────────────────────────────────────


def detect_stale_notes(
    vault_root: Path | None = None,
    workspace_tier: SensitivityTier = "internal",
    *,
    now: datetime | None = None,
    threshold_days: int | None = None,
) -> list[StaleItem]:
    """Walk ``vault_root`` and yield notes whose review timestamp is older
    than ``threshold_days`` (default :func:`stale_note_days`).

    Sensitivity gating: only notes whose own ``sensitivity`` is at or below
    ``workspace_tier`` are returned. A non-confidential request will never
    surface a confidential note's title."""
    root = vault_root if vault_root is not None else VAULT
    if not root.is_dir():
        return []

    threshold = threshold_days if threshold_days is not None else stale_note_days()
    visible = _max_visible(workspace_tier)
    now_dt = now or _now_utc()
    out: list[StaleItem] = []

    for md in root.rglob("*.md"):
        if _should_skip(md, root):
            continue
        if not md.is_file():
            continue

        # Parse frontmatter once. On any error, fall back to mtime-only
        # path so unreadable YAML doesn't disqualify a file from being
        # flagged stale (the goal is to surface neglected notes; a
        # corrupt one is the most neglected of all).
        last_touched_dt: datetime | None = None
        signal: StaleSignal = "mtime"
        sensitivity: SensitivityTier = "internal"
        try:
            post = frontmatter.load(md)
            meta = post.metadata or {}
            sensitivity = _coerce_sensitivity(meta.get("sensitivity"))
            # Honour either key — see module docstring deviation note.
            for key in ("last_reviewed", "last_refreshed"):
                if key in meta:
                    parsed = _parse_date(meta.get(key))
                    if parsed is not None:
                        last_touched_dt = parsed
                        signal = "frontmatter"
                        break
        except Exception as e:  # noqa: BLE001
            log.debug("stale: frontmatter parse failed for %s: %s", md, e)

        if last_touched_dt is None:
            try:
                last_touched_dt = datetime.fromtimestamp(md.stat().st_mtime, tz=_NOW_TZ)
            except OSError:
                continue
            signal = "mtime"

        if sensitivity not in visible:
            continue

        days = _days_between(last_touched_dt, now_dt)
        if days < threshold:
            continue

        rel = md.relative_to(root).as_posix()
        out.append(StaleItem(
            kind="note",
            id=f"note:{rel}",
            label=md.stem,
            last_touched=_iso_utc(last_touched_dt),
            days_stale=days,
            signal=signal,
            sensitivity=sensitivity,
        ))

    return out


# ────────────────────────────────────────────────────────────────────────────
# Detector 2 — stale pending proposals
# ────────────────────────────────────────────────────────────────────────────


def detect_stale_proposals(
    vault_root: Path | None = None,
    workspace_tier: SensitivityTier = "internal",
    *,
    now: datetime | None = None,
    threshold_days: int | None = None,
) -> list[StaleItem]:
    """Read the SAME pending-proposals source as
    ``GET /api/proposals/pending`` (``_walk_pending``) and flag any whose
    ``date`` frontmatter is older than ``threshold_days`` (default
    :func:`stale_proposal_days`).

    Proposal sensitivity is read from the file's own frontmatter when
    present; defaults to ``internal`` otherwise. Visibility is gated by
    ``workspace_tier`` as per :func:`_max_visible`."""
    # Import here to avoid a circular at module-load (proposals route imports
    # nothing from dashboard, but stale runs inside the api routes layer).
    from routines.api.routes.proposals import _walk_pending

    root = vault_root if vault_root is not None else VAULT
    if not root.is_dir():
        return []

    threshold = threshold_days if threshold_days is not None else stale_proposal_days()
    visible = _max_visible(workspace_tier)
    now_dt = now or _now_utc()
    out: list[StaleItem] = []

    for prop in _walk_pending(root):
        last_touched_dt = _parse_date(prop.date)
        signal: StaleSignal = "frontmatter"

        if last_touched_dt is None:
            # Fall back to file mtime when proposal carries no parseable date.
            try:
                last_touched_dt = datetime.fromtimestamp(
                    (root / prop.path).stat().st_mtime, tz=_NOW_TZ,
                )
            except OSError:
                continue
            signal = "mtime"

        # Re-read sensitivity from the proposal's own frontmatter (PendingProposal
        # doesn't carry it — _walk_pending skips that field).
        sensitivity: SensitivityTier = "internal"
        try:
            post = frontmatter.load(root / prop.path)
            sensitivity = _coerce_sensitivity((post.metadata or {}).get("sensitivity"))
        except Exception:  # noqa: BLE001
            pass

        if sensitivity not in visible:
            continue

        days = _days_between(last_touched_dt, now_dt)
        if days < threshold:
            continue

        out.append(StaleItem(
            kind="proposal",
            id=f"proposal:{prop.id}",
            label=prop.title or prop.path,
            last_touched=_iso_utc(last_touched_dt),
            days_stale=days,
            signal=signal,
            sensitivity=sensitivity,
        ))

    return out


# ────────────────────────────────────────────────────────────────────────────
# Detector 3 — stale projects (no activity in 30d)
# ────────────────────────────────────────────────────────────────────────────


def detect_stale_projects(
    vault_root: Path | None = None,
    workspace_tier: SensitivityTier = "internal",
    *,
    now: datetime | None = None,
    threshold_days: int | None = None,
) -> list[StaleItem]:
    """Walk ``Projects/<name>/`` and yield those whose newest file mtime
    is older than ``threshold_days`` (default :func:`stale_project_days`).

    Project sensitivity is read from ``00 Brief.md`` frontmatter; defaults
    to ``internal`` when absent or unreadable. ``Projects/_template`` and
    underscore-prefixed sentinels are skipped."""
    root = vault_root if vault_root is not None else VAULT
    projects_root = root / "Projects"
    if not projects_root.is_dir():
        return []

    threshold = threshold_days if threshold_days is not None else stale_project_days()
    visible = _max_visible(workspace_tier)
    now_dt = now or _now_utc()
    out: list[StaleItem] = []

    for proj_dir in projects_root.iterdir():
        if not proj_dir.is_dir():
            continue
        if proj_dir.name.startswith(".") or proj_dir.name.startswith("_"):
            continue

        # Sensitivity from brief, default internal.
        sensitivity: SensitivityTier = "internal"
        brief = proj_dir / "00 Brief.md"
        if brief.is_file():
            try:
                post = frontmatter.load(brief)
                sensitivity = _coerce_sensitivity((post.metadata or {}).get("sensitivity"))
            except Exception:  # noqa: BLE001
                pass

        if sensitivity not in visible:
            continue

        # Newest mtime across all .md files (cheap proxy for "activity").
        # We deliberately ignore the project directory's own mtime — it
        # changes whenever ANY child file is created/touched (including
        # during fresh project scaffolding), which would mask a project
        # whose actual content is stale. If a project tree has no .md
        # files at all, fall back to the dir mtime so brand-new empty
        # projects don't immediately register as "stale day 0".
        md_files = list(proj_dir.rglob("*.md"))
        if not md_files:
            newest = proj_dir.stat().st_mtime
        else:
            newest = 0.0
            for f in md_files:
                try:
                    m = f.stat().st_mtime
                except OSError:
                    continue
                if m > newest:
                    newest = m
            if newest == 0.0:
                # Every .md was unreadable — fall back to dir mtime.
                newest = proj_dir.stat().st_mtime

        last_touched_dt = datetime.fromtimestamp(newest, tz=_NOW_TZ)
        days = _days_between(last_touched_dt, now_dt)
        if days < threshold:
            continue

        out.append(StaleItem(
            kind="project",
            id=f"project:{proj_dir.name}",
            label=proj_dir.name,
            last_touched=_iso_utc(last_touched_dt),
            days_stale=days,
            signal="mtime",
            sensitivity=sensitivity,
        ))

    return out


# ────────────────────────────────────────────────────────────────────────────
# Top-level combiner
# ────────────────────────────────────────────────────────────────────────────


_TOP_N: int = 10


def detect_stale(
    workspace_tier: SensitivityTier = "internal",
    vault_root: Path | None = None,
    *,
    now: datetime | None = None,
    top_n: int = _TOP_N,
) -> list[StaleItem]:
    """Run all three detectors, sort by ``days_stale`` DESC (oldest first),
    truncate to ``top_n`` (default 10).

    The three detectors each open the vault independently; this stays
    deliberate so a partial failure in one detector (e.g. permissions on
    ``Projects/``) doesn't disqualify the other two."""
    items: list[StaleItem] = []
    for detector in (detect_stale_notes, detect_stale_proposals, detect_stale_projects):
        try:
            items.extend(detector(vault_root=vault_root, workspace_tier=workspace_tier, now=now))
        except Exception as e:  # noqa: BLE001
            log.warning("stale: %s failed: %s", detector.__name__, e)

    # Sort DESC: oldest first. Tie-break on kind then id for determinism.
    items.sort(key=lambda it: (-it.days_stale, it.kind, it.id))
    return items[:top_n]
