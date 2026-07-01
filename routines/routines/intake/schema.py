"""Schema for the parsed-document output.

Shape is opinionated toward the M&A intake workflow (teasers / CIMs /
expert call decks). The fields that are not in the document come back
empty — the LLM is told not to fabricate.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


DocKind = Literal["teaser", "cim", "im", "expert-deck", "research-note", "other"]


class FinancialHighlight(BaseModel):
    """One pulled-out financial datapoint (e.g. revenue, EBITDA, margin)."""

    metric: str        # e.g. "Revenue", "EBITDA", "Gross margin"
    value: str         # verbatim as in the doc, e.g. "£142m", "19.5%"
    period: str = ""   # e.g. "FY25", "LTM Mar 25", "" if not stated


class ImageNote(BaseModel):
    """One non-trivial visual the LLM observed in the doc."""

    page: int          # 1-indexed
    kind: str          # e.g. "chart", "diagram", "table-screenshot", "logo"
    summary: str       # one-sentence description of what it depicts


class ParsedDocument(BaseModel):
    """Structured extraction from a single inbound PDF."""

    doc_kind: DocKind = "other"
    target_descriptor: str = ""             # anonymised name as written, e.g. "Project Falcon"
    target_revealed_name: str = ""          # if the doc names the actual target
    industry: str = ""
    sector: str = ""
    subsector: str = ""
    geography: str = ""                     # e.g. "UK + Ireland", "EMEA"
    financials: list[FinancialHighlight] = Field(default_factory=list)
    investment_highlights: list[str] = Field(default_factory=list)
    process_notes: str = ""                 # e.g. "Phase I bids due 30 June 2026"
    advisor: str = ""                       # e.g. "Rothschild & Co"
    confidentiality: str = ""               # any confidentiality clause noted
    image_notes: list[ImageNote] = Field(default_factory=list)
    summary: str = ""                       # 2-3 sentence operator-facing precis
