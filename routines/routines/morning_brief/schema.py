"""Schema for the morning brief.

Mirrors the dashboard's MorningBriefData / BriefRow types so the JSON
served by /api/morning-brief/today is consumed verbatim — no transform.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


BriefMarker = Literal["ovd", "due", "open", "news"]


class BriefRow(BaseModel):
    marker: BriefMarker
    text: str
    sub: str


class MorningBrief(BaseModel):
    date: str                       # e.g. "Wed · 14 May 2026 · 09:24 UTC"
    source: str                     # provenance string
    needsYou: list[BriefRow] = []
    sectorThisWeek: list[BriefRow] = []
    antonSuggests: str = ""
