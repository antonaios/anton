"""Steps 5 + 6 — deterministic comparison vs consensus and prior periods.

Pure Python; no LLM. Every delta and verdict here is computed in code so the
numbers are reproducible and testable (CLAUDE.md §5.1 — the model extracts, the
code does the arithmetic).

Two baselines:

  * **Consensus** (step 5) — operator-curated figures from the company page
    frontmatter (``consensus:`` block) take precedence; the announcement's own
    stated consensus (``consensus_*`` on the extraction) is the fallback. If
    neither is present for a metric, that metric simply produces no consensus
    line (we never compare against a fabricated baseline).
  * **Prior period / prior year** (step 6) — read back from the hidden machine
    lines already on the page. "Prior period" is the most-recent earlier
    capture; "prior year" is the same ``fiscal_period`` one ``fiscal_year`` back,
    if present.

Materiality is layered on separately (:mod:`routines.earnings.materiality`),
which sets ``Comparison.material`` + ``material_reasons``.
"""

from __future__ import annotations

from typing import Any, Optional

from routines.earnings.report import (
    Comparison,
    ExtractedEarnings,
    PriorPeriod,
    VarianceLine,
)

# The verdict band: within ±this fraction of the baseline counts as "in-line".
# This is a *labelling* threshold for the card's prose, independent of the
# materiality thresholds (which decide whether to ALERT). 1.5% keeps "in line"
# honest without calling every rounding difference a beat/miss.
_INLINE_BAND = 0.015


# Frontmatter consensus keys → the metric they back. Operator authors the
# ``consensus:`` block on the company page; we accept a few spellings.
_CONSENSUS_KEYS: dict[str, tuple[str, ...]] = {
    "revenue": ("revenue_m", "revenue", "rev_m", "rev"),
    "ebitda": ("ebitda_m", "ebitda"),
    "eps": ("eps",),
}


def _consensus_value(metric: str, frontmatter_consensus: dict[str, Any]) -> Optional[float]:
    """Pull a metric's consensus from the operator-curated frontmatter block."""
    for key in _CONSENSUS_KEYS.get(metric, ()):  # noqa: B007
        if key in frontmatter_consensus:
            v = frontmatter_consensus.get(key)
            try:
                return None if v is None else float(v)
            except (TypeError, ValueError):
                return None
    return None


def _verdict(delta_pct: Optional[float]) -> str:
    if delta_pct is None:
        return "n/a"
    if abs(delta_pct) <= _INLINE_BAND:
        return "in-line"
    return "beat" if delta_pct > 0 else "miss"


def _line(metric: str, baseline_label: str, actual: Optional[float],
          baseline: Optional[float]) -> Optional[VarianceLine]:
    """Build one variance line, or ``None`` when either side is missing (we never
    compare against an absent baseline)."""
    if actual is None or baseline is None:
        return None
    delta = actual - baseline
    delta_pct = (delta / baseline) if baseline not in (0, 0.0) else None
    return VarianceLine(
        metric=metric,
        baseline_label=baseline_label,
        actual=actual,
        baseline=baseline,
        delta=delta,
        delta_pct=delta_pct,
        verdict=_verdict(delta_pct),
    )


def _resolve_consensus(
    extracted: ExtractedEarnings,
    frontmatter_consensus: dict[str, Any],
) -> dict[str, Optional[float]]:
    """Per-metric consensus: frontmatter first, announcement-stated as fallback."""
    return {
        "revenue": (
            _consensus_value("revenue", frontmatter_consensus)
            if _consensus_value("revenue", frontmatter_consensus) is not None
            else extracted.consensus_revenue_m
        ),
        "ebitda": (
            _consensus_value("ebitda", frontmatter_consensus)
            if _consensus_value("ebitda", frontmatter_consensus) is not None
            else extracted.consensus_ebitda_m
        ),
        "eps": (
            _consensus_value("eps", frontmatter_consensus)
            if _consensus_value("eps", frontmatter_consensus) is not None
            else extracted.consensus_eps
        ),
    }


def select_prior(
    extracted: ExtractedEarnings,
    priors: list[PriorPeriod],
) -> tuple[Optional[PriorPeriod], Optional[PriorPeriod]]:
    """From the page's prior captures, pick (most-recent-prior, prior-year).

    * **prior period** = the capture with the latest ``reported_date`` strictly
      before this announcement's reported date (or, if dates are missing, the
      last one on the page that isn't this same period).
    * **prior year** = the capture matching this ``fiscal_period`` exactly with
      ``fiscal_year`` one less than this one.

    Records for the SAME period as the current announcement are excluded so a
    re-run doesn't compare a quarter against itself.
    """
    this_year = extracted.fiscal_year
    this_period = extracted.fiscal_period
    this_label = extracted.period_label
    rep = extracted.reported_date

    candidates = [p for p in priors if p.period_label != this_label]

    # Prior period — prefer strict date ordering; fall back to document order.
    prior_period: Optional[PriorPeriod] = None
    dated = [p for p in candidates if p.reported_date is not None]
    if rep is not None and dated:
        earlier = [p for p in dated if p.reported_date < rep]
        if earlier:
            prior_period = max(earlier, key=lambda p: p.reported_date)
    if prior_period is None and candidates:
        # No usable dates — the machine lines are appended in chronological
        # order, so the last candidate is the most recent prior.
        prior_period = candidates[-1]

    # Prior year — same period, year-1.
    prior_year: Optional[PriorPeriod] = None
    if this_year is not None and this_period:
        for p in candidates:
            if p.fiscal_period == this_period and p.fiscal_year == this_year - 1:
                prior_year = p
                break

    return prior_period, prior_year


def compare(
    extracted: ExtractedEarnings,
    *,
    frontmatter_consensus: Optional[dict[str, Any]] = None,
    priors: Optional[list[PriorPeriod]] = None,
) -> Comparison:
    """Build the deterministic :class:`Comparison` for one announcement."""
    frontmatter_consensus = frontmatter_consensus or {}
    priors = priors or []

    consensus = _resolve_consensus(extracted, frontmatter_consensus)
    vs_consensus: list[VarianceLine] = []
    for metric, actual in (
        ("revenue", extracted.revenue_m),
        ("ebitda", extracted.ebitda_m),
        ("eps", extracted.eps),
    ):
        line = _line(metric, "consensus", actual, consensus.get(metric))
        if line is not None:
            vs_consensus.append(line)

    prior_period, prior_year = select_prior(extracted, priors)

    vs_prior: list[VarianceLine] = []
    if prior_period is not None:
        for metric, actual, base in (
            ("revenue", extracted.revenue_m, prior_period.revenue_m),
            ("ebitda", extracted.ebitda_m, prior_period.ebitda_m),
            ("eps", extracted.eps, prior_period.eps),
        ):
            line = _line(metric, "prior period", actual, base)
            if line is not None:
                vs_prior.append(line)

    vs_prior_year: list[VarianceLine] = []
    if prior_year is not None:
        for metric, actual, base in (
            ("revenue", extracted.revenue_m, prior_year.revenue_m),
            ("ebitda", extracted.ebitda_m, prior_year.ebitda_m),
            ("eps", extracted.eps, prior_year.eps),
        ):
            line = _line(metric, "prior year", actual, base)
            if line is not None:
                vs_prior_year.append(line)

    return Comparison(
        vs_consensus=vs_consensus,
        vs_prior=vs_prior,
        vs_prior_year=vs_prior_year,
        guidance_change=extracted.guidance_change,
    )


__all__ = ["compare", "select_prior"]
