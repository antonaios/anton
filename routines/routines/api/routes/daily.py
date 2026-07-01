"""GET /api/daily/today — fetch today's daily note (path + content)."""

from __future__ import annotations

import re
from datetime import date as _date
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Query
from pydantic import BaseModel

from routines.api.deps import VAULT

router = APIRouter()


class DailyResponse(BaseModel):
    date: str               # ISO date
    path: str               # relative to vault root
    exists: bool
    content: Optional[str] = None    # markdown content if exists
    size_bytes: Optional[int] = None


class RecentDailyItem(BaseModel):
    date: str               # ISO date (the note's filename stem)
    path: str               # relative to vault root, e.g. "Daily/2026-06-30.md"
    size_bytes: int


class RecentDailyResponse(BaseModel):
    items: list[RecentDailyItem]   # most-recent-first, capped at `limit`
    total: int                     # total daily notes in the vault


@router.get("/daily/today", response_model=DailyResponse)
def daily_today() -> DailyResponse:
    """Returns today's Daily/<YYYY-MM-DD>.md note.

    If the note doesn't exist we return exists=false so the dashboard can
    offer a "create note" affordance. We do NOT create it here — that
    would be a side effect.
    """
    today = datetime.now(timezone.utc).date().isoformat()
    rel = f"Daily/{today}.md"
    path = VAULT / "Daily" / f"{today}.md"

    if not path.exists():
        return DailyResponse(date=today, path=rel, exists=False)

    try:
        content = path.read_text(encoding="utf-8")
        size = path.stat().st_size
    except OSError as e:
        return DailyResponse(
            date=today, path=rel, exists=True,
            content=f"_Could not read note: {e}_", size_bytes=None,
        )

    return DailyResponse(date=today, path=rel, exists=True, content=content, size_bytes=size)


def _is_iso_date_stem(stem: str) -> bool:
    """True iff ``stem`` is a canonical ``YYYY-MM-DD`` daily-note name.

    The strict regex is FIRST and load-bearing: Python 3.11+ ``date.fromisoformat``
    also accepts non-canonical ISO forms (basic ``20260630``, week dates
    ``2026-W27``) whose stems would violate the ``date`` contract AND break the
    lexicographic-==-chronological sort invariant. The ``fromisoformat`` check
    then rejects impossible dates (e.g. ``2026-06-99``)."""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", stem):
        return False
    try:
        _date.fromisoformat(stem)
        return True
    except ValueError:
        return False


@router.get("/daily/recent", response_model=RecentDailyResponse)
def daily_recent(limit: int = Query(6, ge=1, le=30)) -> RecentDailyResponse:
    """Return the most-recent daily notes (newest first), metadata only.

    Powers the Notes-page "recent" rail. Reads no file content — only the
    filename (which IS the ISO date) + size. Non-date-named files in Daily/
    are ignored. Missing Daily/ dir → empty list, never a 500.

    Daily notes are non-confidential episodic memory (surfaced as "General"),
    so there is no sensitivity gate here — same posture as ``/daily/today``.
    """
    daily_dir = VAULT / "Daily"
    if not daily_dir.is_dir():
        return RecentDailyResponse(items=[], total=0)

    # Names are ISO dates → lexicographic stem sort IS chronological. Newest first.
    notes = sorted(
        (p for p in daily_dir.glob("*.md") if p.is_file() and _is_iso_date_stem(p.stem)),
        key=lambda p: p.stem,
        reverse=True,
    )

    # Scan the FULL sorted list (not notes[:limit]) so an unreadable note is
    # skipped without cutting the list short — we still return up to `limit`
    # readable notes when enough exist.
    items: list[RecentDailyItem] = []
    for p in notes:
        if len(items) >= limit:
            break
        try:
            size = p.stat().st_size
        except OSError:
            continue
        items.append(RecentDailyItem(date=p.stem, path=f"Daily/{p.name}", size_bytes=size))

    return RecentDailyResponse(items=items, total=len(notes))
