"""Data model for the calendar-driven earnings pipeline (#44).

This is the *announcement* side of the earnings module — distinct from the
OpenBB-backed quarterly workbook (:mod:`routines.earnings.schema`). Where the
workbook stores deterministic provider rows, this side carries what the LLM
extracted from a results announcement (RNS / IR page) plus the deterministic
comparison the routine computes against consensus + prior periods.

Three groups of types:

  * :class:`ExtractedEarnings` — the structured fields the local model lifts
    from one announcement (revenue / EBITDA / margins / EPS / guidance / KPIs /
    divisional split). Empty fields mean "not stated in the source", never
    "zero" — the Iron Law: never fabricate a figure the announcement didn't
    give (CLAUDE.md §5.1).
  * :class:`PriorPeriod` — a thin record reconstructed from a previously
    appended results section on the company page (read back from the hidden
    ``<!-- earnings-data: {...} -->`` machine line). Used for the prior-period
    / prior-year trend compare with no re-extraction.
  * :class:`Comparison` / :class:`VarianceLine` — the deterministic output of
    the compare step: per-metric beat/miss/in-line verdicts vs consensus and
    vs the prior period, plus the material-variance assessment.

The hidden machine line is the mechanism that keeps step 6 (compare vs prior)
deterministic and testable: each appended section embeds a compact JSON blob
that a later run parses back, rather than re-scraping or re-extracting old
quarters.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Optional


# ─────────────────────────────────────────────────────────────────────────────
# KPI + divisional line items
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class KPILine:
    """One operating KPI lifted from the announcement.

    ``value`` / ``consensus`` / ``unit`` are free-form because KPIs vary by
    sector (like-for-like %, site count, occupancy, RevPAR …). ``value`` is a
    string so we never lose the announcement's own phrasing ("+4.2%", "187").
    """

    name: str = ""
    value: str = ""
    consensus: str = ""   # consensus for this KPI if stated, else ""
    unit: str = ""        # optional unit hint ("%", "sites", "£m")


@dataclass
class DivisionLine:
    """One divisional / segment revenue split line."""

    name: str = ""
    revenue_m: Optional[float] = None
    revenue_yoy: Optional[float] = None   # fractional (0.08 = +8%)
    note: str = ""


# ─────────────────────────────────────────────────────────────────────────────
# Extracted announcement
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class ExtractedEarnings:
    """Structured fields the local model lifted from one results announcement.

    All numeric monetary fields are in millions of ``currency``. ``None`` means
    the announcement did not state the figure — downstream code must treat that
    as "unknown", never zero.
    """

    # Period identity.
    fiscal_year: Optional[int] = None
    fiscal_period: str = ""        # "Q1" | "H1" | "Q3" | "FY" | "9M" …
    reported_date: Optional[date] = None

    # Headline P&L (millions of `currency`).
    currency: str = ""
    revenue_m: Optional[float] = None
    revenue_yoy: Optional[float] = None     # fractional, vs same period prior year
    ebitda_m: Optional[float] = None
    ebitda_margin: Optional[float] = None   # fractional (0.197 = 19.7%)
    ebit_m: Optional[float] = None
    net_income_m: Optional[float] = None
    eps: Optional[float] = None

    # Guidance.
    guidance: str = ""              # short paraphrase of the guidance statement
    guidance_change: str = ""       # "reiterated" | "raised" | "lowered" | "" (unknown)

    # Consensus figures STATED IN THE ANNOUNCEMENT itself (e.g. "ahead of
    # consensus of £139m"). Operator-curated consensus in the page frontmatter
    # takes precedence in the compare step; these are the fallback.
    consensus_revenue_m: Optional[float] = None
    consensus_ebitda_m: Optional[float] = None
    consensus_eps: Optional[float] = None

    # Narrative + structure.
    kpis: list[KPILine] = field(default_factory=list)
    divisions: list[DivisionLine] = field(default_factory=list)
    commentary: str = ""            # paraphrased management commentary

    # Forward calendar hint, if the announcement states the next reporting date.
    next_reporting_date: Optional[date] = None

    # Provenance.
    source_url: str = ""

    # Issuer identity — lifted from the announcement so the pipeline can HARD-GATE
    # a search-fallback hit against the watched ``CompanyEntry`` before any write
    # (a same-day wrong-company RNS must never be captured — #44 Codex SEV-1).
    # Appended at the END so positional ``ExtractedEarnings(...)`` calls are never
    # silently shifted (#44 Codex re-review).
    company_name: str = ""
    ticker: str = ""

    # ---- derived ------------------------------------------------------------

    @property
    def period_label(self) -> str:
        """``"2026-Q1"`` style label. Falls back gracefully when the model
        couldn't pin the period (``"unknown-period"``) so a section still gets
        a stable-ish heading rather than crashing the render."""
        year = f"{self.fiscal_year:04d}" if self.fiscal_year else "unknown"
        period = (self.fiscal_period or "").strip() or "period"
        return f"{year}-{period}"

    def has_headline_numbers(self) -> bool:
        """True when the extraction produced at least one headline P&L figure —
        the bar for "the announcement has actually published" (vs an empty page
        the cron should re-fire on). Revenue OR EBITDA OR EPS is enough."""
        return any(
            v is not None for v in (self.revenue_m, self.ebitda_m, self.eps, self.net_income_m)
        )

    def machine_record(self) -> dict[str, Any]:
        """Compact JSON-serialisable record embedded in the appended section as
        a hidden ``<!-- earnings-data: {...} -->`` line, so a later run can read
        this period back for prior-period comparison without re-extracting."""
        return {
            "fiscal_year": self.fiscal_year,
            "fiscal_period": self.fiscal_period,
            "period_label": self.period_label,
            "reported_date": self.reported_date.isoformat() if self.reported_date else None,
            "currency": self.currency,
            "revenue_m": self.revenue_m,
            "revenue_yoy": self.revenue_yoy,
            "ebitda_m": self.ebitda_m,
            "ebitda_margin": self.ebitda_margin,
            "ebit_m": self.ebit_m,
            "net_income_m": self.net_income_m,
            "eps": self.eps,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Prior period (read back from a previously appended section)
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class PriorPeriod:
    """One previously captured period, reconstructed from the hidden machine
    line on the company page. Only the fields needed for trend compare."""

    fiscal_year: Optional[int] = None
    fiscal_period: str = ""
    period_label: str = ""
    reported_date: Optional[date] = None
    currency: str = ""
    revenue_m: Optional[float] = None
    revenue_yoy: Optional[float] = None
    ebitda_m: Optional[float] = None
    ebitda_margin: Optional[float] = None
    eps: Optional[float] = None
    # Replay fields (frozen at capture) — let a self-heal sweep re-emit the
    # captured period's side-effects deterministically without re-extracting.
    guidance_change: str = ""
    material: bool = False
    material_reasons: list[str] = field(default_factory=list)
    consensus_revenue_m: Optional[float] = None
    consensus_ebitda_m: Optional[float] = None
    consensus_eps: Optional[float] = None
    # True only when the record was written with the frozen replay fields (post
    # the #44 self-heal fix). A legacy/partial record is NOT replay-complete, so
    # a self-heal must fall back rather than trust a defaulted material=False.
    replay_complete: bool = False

    @classmethod
    def from_record(cls, rec: dict[str, Any]) -> "PriorPeriod":
        def _f(key: str) -> Optional[float]:
            v = rec.get(key)
            try:
                return None if v is None else float(v)
            except (TypeError, ValueError):
                return None

        rd = rec.get("reported_date")
        reported: Optional[date] = None
        if isinstance(rd, str) and rd.strip():
            try:
                reported = date.fromisoformat(rd[:10])
            except ValueError:
                reported = None

        reasons = rec.get("material_reasons")
        if not isinstance(reasons, list):
            reasons = []

        fy = rec.get("fiscal_year")
        try:
            fy_int = int(fy) if fy is not None else None
        except (TypeError, ValueError):
            fy_int = None

        return cls(
            fiscal_year=fy_int,
            fiscal_period=str(rec.get("fiscal_period") or "").strip(),
            period_label=str(rec.get("period_label") or "").strip(),
            reported_date=reported,
            currency=str(rec.get("currency") or "").strip(),
            revenue_m=_f("revenue_m"),
            revenue_yoy=_f("revenue_yoy"),
            ebitda_m=_f("ebitda_m"),
            ebitda_margin=_f("ebitda_margin"),
            eps=_f("eps"),
            guidance_change=str(rec.get("guidance_change") or "").strip(),
            material=bool(rec.get("material")),
            material_reasons=[str(r) for r in reasons],
            consensus_revenue_m=_f("consensus_revenue_m"),
            consensus_ebitda_m=_f("consensus_ebitda_m"),
            consensus_eps=_f("consensus_eps"),
            replay_complete="material" in rec,
        )


_MACHINE_LINE_RE = re.compile(r"<!--\s*earnings-data:\s*(\{.*?\})\s*-->", re.DOTALL)


def machine_line(rec: dict[str, Any]) -> str:
    """Render the hidden machine line. Single-line JSON so the regex read-back
    is robust and the line stays invisible in Obsidian preview."""
    return f"<!-- earnings-data: {json.dumps(rec, separators=(',', ':'), default=str)} -->"


def parse_machine_lines(text: str) -> list[dict[str, Any]]:
    """Return every ``earnings-data`` record embedded in ``text``, in document
    order. Malformed blobs are skipped (best-effort — a hand-edit shouldn't
    crash the next run)."""
    out: list[dict[str, Any]] = []
    for m in _MACHINE_LINE_RE.finditer(text):
        try:
            obj = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Comparison output
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class VarianceLine:
    """One metric's verdict against a baseline (consensus or prior period)."""

    metric: str = ""              # "revenue" | "ebitda" | "eps" | "<kpi name>"
    baseline_label: str = ""      # "consensus" | "prior period" | "prior year"
    actual: Optional[float] = None
    baseline: Optional[float] = None
    delta: Optional[float] = None         # actual - baseline (same units)
    delta_pct: Optional[float] = None     # fractional (0.022 = +2.2%)
    verdict: str = ""             # "beat" | "miss" | "in-line" | "n/a"


@dataclass
class Comparison:
    """Deterministic compare output for one announcement.

    ``vs_consensus`` / ``vs_prior`` / ``vs_prior_year`` hold the per-metric
    variance lines. ``material`` + ``material_reasons`` are filled by the
    materiality assessment (see :mod:`routines.earnings.materiality`)."""

    vs_consensus: list[VarianceLine] = field(default_factory=list)
    vs_prior: list[VarianceLine] = field(default_factory=list)
    vs_prior_year: list[VarianceLine] = field(default_factory=list)
    guidance_change: str = ""     # mirrored from the extraction for convenience
    material: bool = False
    material_reasons: list[str] = field(default_factory=list)

    def all_lines(self) -> list[VarianceLine]:
        return [*self.vs_consensus, *self.vs_prior, *self.vs_prior_year]


__all__ = [
    "KPILine",
    "DivisionLine",
    "ExtractedEarnings",
    "PriorPeriod",
    "VarianceLine",
    "Comparison",
    "machine_line",
    "parse_machine_lines",
]
