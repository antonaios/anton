"""Dashboard rollup composer (#69).

Consolidates the five separate dashboard cold-load round-trips into ONE
endpoint payload. Today's dashboard fans out to:

  1. ``GET /api/sessions``                 → recent sessions (sidebar)
  2. ``GET /api/proposals/pending``        → REVIEW chip total + byKind
  3. ``GET /api/projects/<n>/overview``    → active project tile (per-name)
  4. ``GET /api/telemetry/burn``           → ForecastPanel
  5. ``GET /api/scheduler/jobs``           → scheduler offline indicator

Each one does its own sensitivity check + JSON serialise + render. The
rollup runs those checks ONCE and returns the union as a single
``DashboardOverview`` payload plus the dashboard-new ``stale`` field
(:mod:`routines.dashboard.stale`).

Cache: in-process dict, 5-minute TTL, keyed by operator workspace tier.
Tier-keyed so a public-tier rollup is NEVER served confidential data
from a prior confidential request. Cache miss recomputes; cache hit
returns the cached payload bytes-for-bytes.

The composer reuses the existing handlers' DATA functions (no
duplicated query logic). Where a handler does its own audit-write,
that side-effect is preserved — the rollup is read-only and shouldn't
fabricate audit rows the operator can't trace back to a real call.

Spec: ``OUTSTANDING.md`` #69 lines 437-443.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from pydantic import BaseModel, Field

from routines.api.deps import VAULT
from routines.dashboard.stale import (
    SensitivityTier,
    StaleItem,
    detect_stale,
    stale_note_days,
    stale_project_days,
    stale_proposal_days,
)

log = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────────────
# Pydantic response shape
# ────────────────────────────────────────────────────────────────────────────


class StaleItemDTO(BaseModel):
    """Wire shape for one stale item. Mirrors :class:`StaleItem` 1-to-1."""

    kind: str
    id: str
    label: str
    last_touched: str
    days_stale: int
    signal: str
    sensitivity: str


class StaleThresholdsDTO(BaseModel):
    """Echo the effective thresholds so the dashboard can render
    "stale > 90d" / "stale > 14d" / "stale > 30d" hints without a
    second round-trip."""

    note_days: int
    proposal_days: int
    project_days: int


class StaleSummaryDTO(BaseModel):
    """The ``stale`` first-class field per spec — top-10 items, by-kind
    count breakdown, effective thresholds, and the total before truncation."""

    items: list[StaleItemDTO]
    total: int                                # PRE-truncation total
    byKind: dict[str, int]
    thresholds: StaleThresholdsDTO


class DashboardOverview(BaseModel):
    """The full rollup. Each top-level field corresponds to one of the
    five existing endpoints — the dashboard can fan-out renders against
    this single payload instead of waiting on 5+ HTTP round-trips."""

    sessions: dict[str, Any]                  # ListSessionsResponse-shaped
    proposals: dict[str, Any]                 # PendingProposalsResponse-shaped
    projects: list[dict[str, Any]] = Field(default_factory=list)
    telemetry: dict[str, Any]                 # BurnResponse-shaped
    scheduler: dict[str, Any]                 # JobsListResponse-shaped
    stale: StaleSummaryDTO
    generated_at: str                         # ISO-8601 UTC
    tier: str                                 # echoed workspace tier
    cached: bool = False                      # true → served from TTL cache


# ────────────────────────────────────────────────────────────────────────────
# Cache (in-process dict + lock, TTL'd)
# ────────────────────────────────────────────────────────────────────────────


_CACHE_TTL_SECONDS: float = 300.0   # 5 minutes per spec


@dataclass
class _CacheEntry:
    payload: dict[str, Any]
    expires_at: float


_CACHE: dict[str, _CacheEntry] = {}
_CACHE_LOCK = threading.Lock()


def _cache_get(key: str) -> Optional[dict[str, Any]]:
    """Return the cached payload for ``key`` or None if missing / expired."""
    with _CACHE_LOCK:
        entry = _CACHE.get(key)
        if entry is None:
            return None
        if entry.expires_at <= time.monotonic():
            del _CACHE[key]
            return None
        return entry.payload


def _cache_put(key: str, payload: dict[str, Any], ttl: float = _CACHE_TTL_SECONDS) -> None:
    with _CACHE_LOCK:
        _CACHE[key] = _CacheEntry(
            payload=payload,
            expires_at=time.monotonic() + ttl,
        )


def cache_clear() -> None:
    """Drop every entry. Used by tests and (potentially) an admin route."""
    with _CACHE_LOCK:
        _CACHE.clear()


# ────────────────────────────────────────────────────────────────────────────
# Composer
# ────────────────────────────────────────────────────────────────────────────


_SESSIONS_LIST_LIMIT = 50          # dashboard sidebar caps at ~50 rows today


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _gather_sessions(workspace_tier: SensitivityTier) -> dict[str, Any]:
    """Call the same store the sessions route uses. Returns a
    ListSessionsResponse-shaped dict so the dashboard can render exactly
    as it does against ``GET /api/sessions``.

    Sensitivity gating: SessionStore rows don't carry a per-row
    sensitivity, but workspace_type=='project' or 'bd' sessions live in
    workspaces whose own sensitivity is the gate. We surface ALL sessions
    here (the route does too — the bridge is loopback-only and the
    sensitivity boundary is enforced LLM-side via ``router.py``).
    The ``workspace_tier`` arg is accepted for symmetry + future tightening."""
    # Lazy import — same as the sessions route uses lru_cache; reuse it.
    from routines.api.routes.sessions import _store, _session_to_dto

    sessions = _store().list_sessions(archived=False, limit=_SESSIONS_LIST_LIMIT)
    return {"sessions": [_session_to_dto(s).model_dump() for s in sessions]}


def _gather_proposals(workspace_tier: SensitivityTier) -> dict[str, Any]:
    """Reuse ``_walk_pending`` so the rollup's pending count never diverges
    from ``GET /api/proposals/pending``, then sensitivity-gate so a
    confidential proposal NEVER leaks into a lower-tier rollup.

    ``PendingProposal`` doesn't carry a sensitivity field (``_walk_pending``
    skips it), so we re-read each proposal's frontmatter here — mirrors
    the exact pattern :func:`routines.dashboard.stale.detect_stale_proposals`
    uses. ``total`` and ``byKind`` are recomputed POST-filter so the chip
    count matches what the requester is actually allowed to see."""
    import frontmatter
    from routines.api.routes.proposals import _PROPOSAL_DIRS, _walk_pending
    from routines.dashboard.stale import _coerce_sensitivity, _max_visible

    visible = _max_visible(workspace_tier)
    raw_items = list(_walk_pending(VAULT))
    filtered = []
    for it in raw_items:
        sensitivity: SensitivityTier = "internal"
        try:
            post = frontmatter.load(VAULT / it.path)
            sensitivity = _coerce_sensitivity((post.metadata or {}).get("sensitivity"))
        except Exception:  # noqa: BLE001
            # Unreadable frontmatter → treat as default 'internal' (matches
            # detect_stale_proposals); only suppress when the requester's
            # tier doesn't include 'internal'.
            pass
        if sensitivity not in visible:
            continue
        filtered.append(it)

    by_kind: dict[str, int] = {label: 0 for label in _PROPOSAL_DIRS.values()}
    for it in filtered:
        by_kind[it.kind] = by_kind.get(it.kind, 0) + 1

    return {
        "total": len(filtered),
        "byKind": by_kind,
        "items": [it.model_dump() for it in filtered],
    }


def _gather_projects(workspace_tier: SensitivityTier) -> list[dict[str, Any]]:
    """Return a per-project overview row for every vault Project the
    requester is allowed to see.

    Calls :func:`_build_project_overview` (the pure builder) rather than
    the HTTP wrapper ``project_overview`` — same data path, but no audit
    row per call. The HTTP wrapper writes a ``projects.overview`` row
    attributed to ``operator``; with the 5-minute rollup cache + N
    projects, calling it from here would fabricate ~N audit rows every
    5 minutes the operator never initiated (FIX 2).

    Visibility is tier-gated: a public-tier rollup never echoes a
    confidential project's brief.
    """
    from routines.api.routes.projects import _build_project_overview
    from routines.dashboard.stale import _coerce_sensitivity, _max_visible

    visible = _max_visible(workspace_tier)
    projects_root = VAULT / "Projects"
    if not projects_root.is_dir():
        return []

    out: list[dict[str, Any]] = []
    for proj_dir in sorted(projects_root.iterdir(), key=lambda p: p.name):
        if not proj_dir.is_dir():
            continue
        if proj_dir.name.startswith(".") or proj_dir.name.startswith("_"):
            continue

        try:
            overview = _build_project_overview(proj_dir.name)
        except Exception as e:  # noqa: BLE001
            log.warning("rollup: _build_project_overview(%s) failed: %s", proj_dir.name, e)
            continue

        if overview is None:
            # Brief missing or path-traversal-style name; skip silently.
            continue

        # The overview's sensitivity field might be None if the brief left
        # it unset. Default to internal — matches stale-detector's default.
        sens = _coerce_sensitivity(overview.sensitivity)
        if sens not in visible:
            continue
        out.append(overview.model_dump())
    return out


def _gather_telemetry(workspace_tier: SensitivityTier) -> dict[str, Any]:
    """Call ``telemetry_burn``'s underlying ``compute_burn`` — pure
    aggregation, no sensitivity dimension. The result is the same for
    every tier today (token counts don't carry workspace metadata)."""
    from dataclasses import asdict
    from routines.api.deps import RUNS_DIR
    from routines.telemetry import compute_burn

    try:
        report = compute_burn(routines_runs_dir=RUNS_DIR)
    except Exception as e:  # noqa: BLE001
        log.warning("rollup: compute_burn failed: %s", e)
        return {
            "burnRate": "0",
            "sessionsToday": 0,
            "costToday": "0.00",
            "series": [],
            "error": f"{type(e).__name__}: {e}",
        }

    return {
        "burnRate": report.burnRate,
        "sessionsToday": report.sessionsToday,
        "costToday": report.costToday,
        "series": [asdict(s) for s in report.series],
    }


def _gather_scheduler(workspace_tier: SensitivityTier) -> dict[str, Any]:
    """Mirror ``GET /api/scheduler/jobs`` — no sensitivity surface; jobs
    are infrastructure metadata."""
    from routines.scheduler import get_scheduler

    sched = get_scheduler()
    try:
        return {
            "running": sched.running,
            "jobs": list(sched.list_jobs()),
        }
    except Exception as e:  # noqa: BLE001
        log.warning("rollup: scheduler.list_jobs failed: %s", e)
        return {"running": False, "jobs": [], "error": f"{type(e).__name__}: {e}"}


def _gather_stale(workspace_tier: SensitivityTier) -> StaleSummaryDTO:
    """Run all three stale detectors once. Top-10 truncation happens inside
    :func:`detect_stale`; we keep the pre-truncation total for the byKind
    breakdown (so the UI can say '37 stale, showing top 10')."""
    # Compute the FULL list ourselves so byKind reflects truth, then
    # truncate for the surfaced items.
    from routines.dashboard.stale import (
        detect_stale_notes,
        detect_stale_proposals,
        detect_stale_projects,
        _TOP_N,
    )

    notes = detect_stale_notes(workspace_tier=workspace_tier)
    props = detect_stale_proposals(workspace_tier=workspace_tier)
    projs = detect_stale_projects(workspace_tier=workspace_tier)
    all_items = notes + props + projs
    all_items.sort(key=lambda it: (-it.days_stale, it.kind, it.id))

    by_kind = {"note": len(notes), "proposal": len(props), "project": len(projs)}
    top = all_items[:_TOP_N]

    return StaleSummaryDTO(
        items=[StaleItemDTO(**it.to_dict()) for it in top],
        total=len(all_items),
        byKind=by_kind,
        thresholds=StaleThresholdsDTO(
            note_days=stale_note_days(),
            proposal_days=stale_proposal_days(),
            project_days=stale_project_days(),
        ),
    )


# ────────────────────────────────────────────────────────────────────────────
# Public entrypoint
# ────────────────────────────────────────────────────────────────────────────


def build_overview(workspace_tier: SensitivityTier = "internal") -> DashboardOverview:
    """Compose the full rollup. Honours the 5-minute tier-keyed cache —
    first call within TTL pays for all 5+1 data fetches; subsequent calls
    at the same tier return the cached payload until expiry.

    The ``cached`` field on the returned model tells the caller whether
    this was a fresh compute or a cache hit (useful for the dashboard's
    "last refreshed" hint)."""
    cache_key = f"tier:{workspace_tier}"
    cached_payload = _cache_get(cache_key)
    if cached_payload is not None:
        # Return a shallow copy so the caller mutating fields can't poison
        # the cache. The nested lists/dicts are still shared — fine because
        # the response is JSON-serialised before egress.
        cached = DashboardOverview.model_validate(cached_payload)
        cached.cached = True
        return cached

    # Fresh compute. Sensitivity gating runs ONCE inside each gatherer.
    sessions_block = _gather_sessions(workspace_tier)
    proposals_block = _gather_proposals(workspace_tier)
    projects_block = _gather_projects(workspace_tier)
    telemetry_block = _gather_telemetry(workspace_tier)
    scheduler_block = _gather_scheduler(workspace_tier)
    stale_block = _gather_stale(workspace_tier)

    overview = DashboardOverview(
        sessions=sessions_block,
        proposals=proposals_block,
        projects=projects_block,
        telemetry=telemetry_block,
        scheduler=scheduler_block,
        stale=stale_block,
        generated_at=_now_iso(),
        tier=workspace_tier,
        cached=False,
    )

    # Persist a serialised copy keyed by tier so the next call within
    # TTL is a free read.
    _cache_put(cache_key, overview.model_dump())
    return overview
