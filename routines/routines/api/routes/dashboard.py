"""Dashboard rollup API (#69).

Single consolidated endpoint that replaces 5+ cold-load round-trips with
one cached payload. See :mod:`routines.dashboard.overview` for the
composer + cache design, and :mod:`routines.dashboard.stale` for the
stale-items detector.

  * ``GET /api/dashboard/overview?tier=<sensitivity>``
"""

from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, Query

from routines.dashboard.overview import (
    DashboardOverview,
    build_overview,
    cache_clear,
)

router = APIRouter()
log = logging.getLogger(__name__)


Tier = Literal["public", "internal", "confidential", "MNPI"]


@router.get("/dashboard/overview", response_model=DashboardOverview)
def dashboard_overview(
    tier: Tier = Query(
        "internal",
        description=(
            "Workspace sensitivity tier of the requester. Drives both the "
            "visible-items filter AND the cache partition — a public-tier "
            "request is NEVER served a cached payload that included "
            "confidential / MNPI data. Defaults to 'internal'."
        ),
    ),
) -> DashboardOverview:
    """Return the consolidated dashboard payload.

    Compose-or-cache: first call within the 5-minute TTL window pays for
    the full multi-source fetch; subsequent calls at the same tier return
    the cached payload (response carries ``cached: true``). Tier-keyed
    so confidential data never leaks into a lower-tier cache slot.

    Stale items are included as a first-class field — top-10 oldest,
    sorted DESC, with a per-kind count breakdown and the effective
    thresholds echoed back. Detector is in
    :mod:`routines.dashboard.stale`.
    """
    return build_overview(workspace_tier=tier)


# Admin escape hatch — useful when iterating against the dashboard during
# development. Not part of the locked contract; safe to remove later.
@router.post("/dashboard/overview/cache/clear")
def clear_dashboard_cache() -> dict[str, bool]:
    """Drop every cached rollup. Forces the next GET to recompute."""
    cache_clear()
    return {"cleared": True}
