"""Deal tracker schema — lean 19-column precedent tracker (post 2026-06-01).

Per ``COMPS-REDESIGN-2026-06-01.md`` + ``SESSION-COMPS-IMPLEMENTATION.md`` WS2,
the canonical workbook is now the operator's precedent tracker at::

    <workspace-root>/4. Research & data/Precedent transactions tracker/
        Precedent_transactions_tracker.xlsx

A stable live filename (dates only on ``./Archive/`` snapshots), a single
``Precedent transactions`` sheet, and a 19-column lean schema designed for
``comps`` skill CoTrans population. The prior 26-column Mergermarket-shaped
schema (``Projects/_Trackers/M&A Deals.xlsx``) is SUPERSEDED — its file is
already archived to ``./Archive/``.

The ``DealRecord`` dataclass KEEPS all the prior fields (``bidder_*``,
``seller_*``, ``reported_ebit_m_y1``, ``completed_date``, etc.) so that
``routines/dealtracker/extract.py`` and its LLM SCHEMA_HINT still compile and
parse the same JSON shape — but ``to_row()`` only emits the 19 lean columns
in the new order. Dropped fields stay on the instance for the route-layer
preview surface and the audit trail; they just don't land in the workbook.

The five new lean-schema fields:

* ``acquirer_type`` — "Strategic" | "Financial" | "" (#21-comps Q5, operator
  decision 2026-06-02). Classified by ``classify_acquirer_type()`` during
  extraction / deep-research; blank when ambiguous (flagged, never guessed).
  Sits at COLUMNS index 7, immediately after ``Acquirer`` — mirrors the
  operator's CoTrans template layout so a direct paste lines up.

* ``subsector_slug`` — derived from the sector context the cron ran for
  (lowercase-hyphenated, e.g. ``hotels-full-service``); blank for non-cron
  paths (operator can fill via Excel).
* ``strategic_commentary`` — populated only by deep-research / operator
  follow-up; the cron leaves this blank intentionally.
* ``source`` — the cron's ``source_url`` (the article that announced the
  deal). Cron-fed rows are auto-sourced.
* ``deal_id`` — generated internally as ``PT-<announced-date>-<target-slug>``.
  NOT a Mergermarket ID; this is our own stable identifier so cross-sheet
  references in the comps deliverable have something to bind on.

``dedupe_key()`` is UNCHANGED — still ``(announced_date, target_company)``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any


# Lean 19-column ordering — used by openpyxl append + header creation.
# Order is load-bearing: matches the operator's CoTrans block layout so a
# direct paste into the comps template lines up column-for-column.
COLUMNS: list[str] = [
    "Announced Date",
    "Target",
    "Target Description",
    "Sector",
    "Subsector (slug)",
    "Country",
    "Acquirer",
    "Acquirer type",
    "Seller",
    "Currency",
    "EV (m)",
    "Revenue (m)",
    "EBITDA (m)",
    "EV/Revenue",
    "EV/EBITDA",
    "Deal Description",
    "Strategic Commentary",
    "Source",
    "Deal ID",
]


def _target_slug(target: str) -> str:
    """Lowercase + hyphen-joined slug of the target company name. Used inside
    ``deal_id`` so the ID is human-readable + stable across runs.

    Strips non-alphanumeric, collapses whitespace+hyphens to a single hyphen,
    trims leading/trailing hyphens. ``"DemoCo"`` -> ``"hb-leisure"``,
    ``"Hilton Worldwide Holdings"`` -> ``"hilton-worldwide-holdings"``,
    ``""`` -> ``""``.
    """
    if not target:
        return ""
    s = target.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = re.sub(r"-+", "-", s)
    return s.strip("-")


def build_deal_id(announced_date: date | None, target: str) -> str:
    """``PT-<announced-date>-<target-slug>``; ``""`` if either input missing.

    Examples::

        build_deal_id(date(2026, 5, 8), "DemoCo") -> "PT-2026-05-08-hb-leisure"
        build_deal_id(None, "X")                      -> ""
        build_deal_id(date(2026, 5, 8), "")           -> ""
    """
    slug = _target_slug(target)
    if not announced_date or not slug:
        return ""
    return f"PT-{announced_date.isoformat()}-{slug}"


# ── Acquirer-type classification (#21-comps Q5, operator decision 2026-06-02) ──
# The "Acquirer type" column carries one of two values, or blank when ambiguous:
#   "Financial"  — PE / sponsor / buyout fund / institutional financial investor
#   "Strategic"  — corporate / trade buyer operating in the target's sector
#   ""           — ambiguous; flagged for operator review, NEVER guessed
# (Consistent with the comps Iron Law: surface the gap, don't invent.)
ACQUIRER_TYPE_STRATEGIC = "Strategic"
ACQUIRER_TYPE_FINANCIAL = "Financial"

# Case-insensitive substring markers that a buyer is a FINANCIAL sponsor. Kept
# tight to avoid false positives — the LLM/deep-research hint is the primary
# signal; this is the deterministic fallback when no hint exists.
_FINANCIAL_BUYER_MARKERS: tuple[str, ...] = (
    "private equity", "venture capital", "growth equity",
    "capital partners", "capital management", "capital advisors",
    "asset management", "investment management", "investment partners",
    "equity partners", "infrastructure partners", "investment firm",
    "leveraged buyout", "buyout fund", "lbo", "financial sponsor", "sponsor",
    "family office", "pension fund", "sovereign wealth", "l.p.",
)


def classify_acquirer_type(
    name: str = "", description: str = "", *, llm_hint: str = "",
) -> str:
    """Classify an acquirer as ``"Strategic"`` | ``"Financial"`` | ``""``.

    Order of resolution (never a guess):
      1. A clean ``llm_hint`` of "strategic"/"financial" (the news extractor or
         deep-research can categorise from article context) is trusted.
      2. Else a financial-sponsor keyword in ``name``+``description`` → Financial.
      3. Else ``""`` — ambiguous, surfaced for operator review (the comps
         pipeline flags blanks rather than presuming a corporate buyer).

    This is categorisation, not maths (CLAUDE.md §5.1 permits the LLM here);
    the keyword fallback keeps the cron path deterministic without an LLM signal.
    """
    hint = (llm_hint or "").strip().lower()
    if hint == "strategic":
        return ACQUIRER_TYPE_STRATEGIC
    if hint == "financial":
        return ACQUIRER_TYPE_FINANCIAL
    blob = f"{name} {description}".lower()
    if any(m in blob for m in _FINANCIAL_BUYER_MARKERS):
        return ACQUIRER_TYPE_FINANCIAL
    return ""


@dataclass
class DealRecord:
    """One row in the precedent transactions tracker. All fields optional —
    LLM extracts are noisy and many fields legitimately come back null.

    Field set is the UNION of (a) the lean 19-col schema and (b) the legacy
    DealRecord shape consumed by ``extract.py``'s ``_normalise`` + the
    deal-tracker route's preview surface. The legacy fields (``bidder_*``,
    ``seller_*``, ``reported_ebit_*``, ``completed_date``) stay on the
    dataclass so callers compile, but they do NOT appear in ``to_row()``.
    """

    # ─── lean-schema fields (emitted by to_row in COLUMNS order) ──────────
    announced_date: date | None = None
    target_company: str = ""                # COLUMNS "Target"
    target_description: str = ""
    target_sector: str = ""                 # comma-separated allowed
    subsector_slug: str = ""                # NEW — cron sets this
    target_country: str = ""
    bidder_company: str = ""                # COLUMNS "Acquirer"
    acquirer_type: str = ""                 # NEW — COLUMNS "Acquirer type": "Strategic" | "Financial" | "" (flag)
    seller_company: str = ""
    currency: str = ""
    enterprise_value_m: float | None = None
    reported_revenue_m_y1: float | None = None
    reported_ebitda_m_y1: float | None = None
    reported_revenue_multiple_y1: float | None = None    # COLUMNS "EV/Revenue"
    reported_ebitda_multiple_y1: float | None = None     # COLUMNS "EV/EBITDA"
    deal_description: str = ""
    strategic_commentary: str = ""          # NEW — deep-research fills later
    source: str = ""                        # NEW — usually mirrors source_url
    deal_id: str = ""                       # NEW — PT-<date>-<target-slug>

    # ─── retained-but-not-emitted fields (kept so extract.py compiles) ────
    completed_date: date | None = None
    bidder_description: str = ""
    bidder_sector: str = ""
    bidder_country: str = ""
    seller_description: str = ""
    seller_sector: str = ""
    seller_country: str = ""
    target_subsector: str = ""              # legacy free-text subsector
    reported_y1_date: date | None = None
    reported_ebit_m_y1: float | None = None
    reported_ebit_multiple_y1: float | None = None
    deal_value_gbp_m: float | None = None

    # ─── provenance (not written to Excel; lives on the instance only) ────
    source_url: str = ""
    source_excerpt: str = ""
    extracted_by_run_id: str = ""

    def to_row(self) -> list[Any]:
        """Map to an 18-element row in ``COLUMNS`` order, ready for openpyxl
        append. If ``deal_id`` is empty, auto-generate it on the fly (so
        callers that forget to populate still get a stable ID — but cron path
        should set it explicitly for clarity). ``source`` falls back to
        ``source_url`` if the caller didn't set it (cron does)."""
        deal_id = self.deal_id or build_deal_id(self.announced_date, self.target_company)
        source = self.source or self.source_url
        return [
            self.announced_date,
            self.target_company,
            self.target_description,
            self.target_sector,
            self.subsector_slug,
            self.target_country,
            self.bidder_company,
            self.acquirer_type,
            self.seller_company,
            self.currency,
            self.enterprise_value_m,
            self.reported_revenue_m_y1,
            self.reported_ebitda_m_y1,
            self.reported_revenue_multiple_y1,
            self.reported_ebitda_multiple_y1,
            self.deal_description,
            self.strategic_commentary,
            source,
            deal_id,
        ]

    def dedupe_key(self) -> str:
        """Idempotency key: ``(announced_date, target_company-lower-stripped)``.

        UNCHANGED from the prior schema — the dedupe contract is load-bearing
        for the cron's idempotent re-runs. Two rows with the same key are
        treated as duplicates.
        """
        d = self.announced_date.isoformat() if self.announced_date else "?"
        t = (self.target_company or "").lower().strip()
        return f"{d}|{t}"
