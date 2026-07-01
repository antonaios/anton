"""Read a stored morning brief back into a MorningBrief object."""

from __future__ import annotations

import json
import logging
from datetime import date as date_cls
from datetime import datetime, timezone
from pathlib import Path

import frontmatter

from routines.morning_brief.schema import BriefRow, MorningBrief
from routines.morning_brief.writer import brief_path

log = logging.getLogger(__name__)


def load_today(vault_root: Path) -> MorningBrief | None:
    """Load today's brief if it exists; None otherwise."""
    today = datetime.now(timezone.utc).date()
    return load_for_date(vault_root, today)


def load_for_date(vault_root: Path, the_date: date_cls) -> MorningBrief | None:
    path = brief_path(vault_root, the_date)
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        log.warning("morning-brief: read %s failed: %s", path, e)
        return None
    try:
        post = frontmatter.loads(text)
    except Exception as e:  # noqa: BLE001
        log.warning("morning-brief: parse %s failed: %s", path, e)
        return None

    raw_data = post.metadata.get("data")
    if not raw_data:
        return None
    try:
        payload = json.loads(raw_data) if isinstance(raw_data, str) else raw_data
    except json.JSONDecodeError as e:
        log.warning("morning-brief: payload JSON parse failed for %s: %s", path, e)
        return None
    if not isinstance(payload, dict):
        return None

    return MorningBrief(
        date=str(payload.get("date", "")),
        source=str(payload.get("source", "")),
        needsYou=[BriefRow(**r) for r in payload.get("needsYou", []) if isinstance(r, dict)],
        sectorThisWeek=[BriefRow(**r) for r in payload.get("sectorThisWeek", []) if isinstance(r, dict)],
        antonSuggests=str(payload.get("antonSuggests", "")),
    )
