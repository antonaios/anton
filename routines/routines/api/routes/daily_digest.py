"""GET /api/daily-digest/today — load the day's digest.

Sibling endpoint to /api/morning-brief/today. Reads
``Routines/daily-digests/<date>.md`` and returns the structured payload.
404 if no digest exists for the date.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from routines.api.deps import VAULT
from routines.daily_digest.reader import load_for_date
from routines.daily_digest.schema import DailyDigest

router = APIRouter()
log = logging.getLogger(__name__)


@router.get("/daily-digest/today", response_model=DailyDigest)
def daily_digest_today(
    date_str: Optional[str] = Query(None, alias="date", description="ISO date (default: today UTC)"),
) -> DailyDigest:
    """Return today's daily digest, or the digest for `?date=YYYY-MM-DD`."""
    if date_str:
        try:
            the_date = date.fromisoformat(date_str)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"Bad date: {e}") from e
    else:
        the_date = datetime.now(timezone.utc).date()

    try:
        digest = load_for_date(VAULT, the_date)
    except Exception as e:  # noqa: BLE001
        log.exception("daily_digest: load failed")
        raise HTTPException(status_code=500, detail=f"Load failed: {e}") from e

    if digest is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"No daily digest for {the_date.isoformat()}. "
                "Run `daily-digest generate` or wait for the scheduled task."
            ),
        )
    return digest
