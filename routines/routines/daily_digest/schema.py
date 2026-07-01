"""Schema for the daily digest.

Mirrors the morning-brief shape so the dashboard can render either with
the same `BriefRow`-style component (marker, text, sub).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


DigestMarker = Literal["routine", "vault", "session", "info"]


class DigestRow(BaseModel):
    marker: DigestMarker
    text: str
    sub: str


class DailyDigest(BaseModel):
    date: str                       # e.g. "Thu · 14 May 2026 · UTC"
    source: str                     # provenance string
    activity: list[DigestRow] = []     # routines that fired today
    vaultChanges: list[DigestRow] = []  # files written/modified today
    antonCloses: str = ""              # 1-3 sentence reflective close
