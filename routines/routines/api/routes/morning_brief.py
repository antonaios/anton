"""GET /api/morning-brief/today — load the day's brief.

Reads `Routines/morning-briefs/<date>.md` and returns the structured
payload to the dashboard's MorningBriefPanel. If no brief exists for
today, returns 404 so the panel falls back to its seed.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from routines.api.deps import VAULT
from routines.morning_brief.reader import load_for_date
from routines.morning_brief.schema import MorningBrief

router = APIRouter()
log = logging.getLogger(__name__)


@router.get("/morning-brief/today", response_model=MorningBrief)
def morning_brief_today(
    date_str: Optional[str] = Query(None, alias="date", description="ISO date (default: today UTC)"),
) -> MorningBrief:
    """Return today's morning brief, or a brief for `?date=YYYY-MM-DD`."""
    if date_str:
        try:
            the_date = date.fromisoformat(date_str)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"Bad date: {e}") from e
    else:
        the_date = datetime.now(timezone.utc).date()

    try:
        brief = load_for_date(VAULT, the_date)
    except Exception as e:  # noqa: BLE001
        log.exception("morning_brief: load failed")
        raise HTTPException(status_code=500, detail=f"Load failed: {e}") from e

    if brief is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"No morning brief for {the_date.isoformat()}. "
                "Run `morning-brief generate` or wait for the scheduled task."
            ),
        )
    return brief
