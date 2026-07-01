"""Step 7 — render the 1-page results card (markdown).

Produces the section that step 8 atomically appends to ``Companies/<name>.md``,
matching the sample shape in OUTSTANDING #44:

    ## 2026-Q1 Results — reported 2026-05-07

    **Headlines:** Revenue £142m (+8% YoY, vs consensus £139m), EBITDA £28m
    (margin 19.7%), guidance reiterated.

    **KPIs:**
    - Like-for-like sales: +4.2% (vs +3.5% consensus)
    - Site count: 187 (+12 net openings)

    **Variance:** Beat on revenue (+£3m), in line on EBITDA.

    **Management commentary:** …

    **Source:** <announcement url>

A trailing hidden ``<!-- earnings-data: {...} -->`` machine line carries the
structured figures so a later run can read this period back for the prior-period
compare (invisible in Obsidian preview).

All prose is rendered deterministically from the extraction + comparison — the
model's only free text is the paraphrased ``commentary`` it already produced.
"""

from __future__ import annotations

from typing import Optional

from routines.earnings.calendar import CompanyEntry
from routines.earnings.report import Comparison, ExtractedEarnings, VarianceLine, machine_line

_CURRENCY_SYMBOLS = {"GBP": "£", "USD": "$", "EUR": "€", "JPY": "¥", "CHF": "CHF "}


def _sym(currency: str) -> str:
    cur = (currency or "").upper().strip()
    sym = _CURRENCY_SYMBOLS.get(cur, "")
    if not sym and cur:
        return f"{cur} "
    return sym


def _money(value: Optional[float], currency: str) -> str:
    """``142.0 → "£142m"``, ``1400 → "£1.4bn"``, ``None → "n/a"``."""
    if value is None:
        return "n/a"
    sym = _sym(currency)
    if abs(value) >= 1000:
        return f"{sym}{value / 1000:.1f}bn"
    return f"{sym}{value:,.0f}m"


def _pct(frac: Optional[float], *, signed: bool = True) -> str:
    if frac is None:
        return "n/a"
    return f"{frac * 100:+.1f}%" if signed else f"{frac * 100:.1f}%"


def section_heading(extracted: ExtractedEarnings) -> str:
    """``## 2026-Q1 Results — reported 2026-05-07`` (the idempotency anchor)."""
    reported = extracted.reported_date.isoformat() if extracted.reported_date else "unknown-date"
    return f"## {extracted.period_label} Results — reported {reported}"


_METRIC_LABELS = {"revenue": "revenue", "ebitda": "EBITDA", "eps": "EPS"}


def _consensus_baseline(comparison: Comparison, metric: str) -> Optional[float]:
    """The consensus value the compare step actually used for ``metric`` —
    frontmatter-curated or announcement-stated. Single source of truth for the
    headline so it never disagrees with the variance line."""
    for ln in comparison.vs_consensus:
        if ln.metric == metric:
            return ln.baseline
    return None


def _headlines(extracted: ExtractedEarnings, comparison: Comparison) -> str:
    parts: list[str] = []

    rev = _money(extracted.revenue_m, extracted.currency)
    if extracted.revenue_m is not None:
        rev_extra: list[str] = []
        if extracted.revenue_yoy is not None:
            rev_extra.append(f"{_pct(extracted.revenue_yoy)} YoY")
        # Resolved consensus (frontmatter first, else announcement-stated).
        rev_consensus = _consensus_baseline(comparison, "revenue")
        if rev_consensus is None:
            rev_consensus = extracted.consensus_revenue_m
        if rev_consensus is not None:
            rev_extra.append(f"vs consensus {_money(rev_consensus, extracted.currency)}")
        suffix = f" ({', '.join(rev_extra)})" if rev_extra else ""
        parts.append(f"Revenue {rev}{suffix}")

    if extracted.ebitda_m is not None:
        margin = (
            f" (margin {_pct(extracted.ebitda_margin, signed=False)})"
            if extracted.ebitda_margin is not None else ""
        )
        parts.append(f"EBITDA {_money(extracted.ebitda_m, extracted.currency)}{margin}")

    if extracted.eps is not None:
        parts.append(f"EPS {extracted.eps:g}")

    if extracted.guidance_change:
        parts.append(f"guidance {extracted.guidance_change}")
    elif extracted.guidance:
        parts.append(extracted.guidance.rstrip("."))

    return ", ".join(parts) if parts else "results published (figures not parsed)"


def _kpi_lines(extracted: ExtractedEarnings) -> list[str]:
    out: list[str] = []
    for kpi in extracted.kpis:
        line = f"- {kpi.name}: {kpi.value}".rstrip()
        if kpi.consensus:
            line += f" (vs {kpi.consensus} consensus)"
        out.append(line)
    return out


def _variance_phrase_for(lines: list[VarianceLine], currency: str) -> list[str]:
    phrases: list[str] = []
    for ln in lines:
        label = _METRIC_LABELS.get(ln.metric, ln.metric)
        if ln.verdict == "in-line":
            phrases.append(f"in line on {label}")
        elif ln.verdict in ("beat", "miss"):
            if ln.metric == "eps":
                amt = f"{ln.delta:+.2f}" if ln.delta is not None else ""
            else:
                amt = _money(ln.delta, currency) if ln.delta is not None else ""
            amt = amt.replace("£-", "-£").replace("$-", "-$").replace("€-", "-€")
            phrases.append(f"{ln.verdict} on {label} ({amt})" if amt else f"{ln.verdict} on {label}")
    return phrases


def _variance(comparison: Comparison, currency: str) -> str:
    # Render every baseline the compare step produced — consensus, prior period,
    # AND prior year. The prior-year line was previously computed but dropped on
    # the floor (#44 Codex SEV-3); a YoY read is exactly what an earnings card is
    # for, so surface it as its own clause when present.
    clauses: list[str] = []
    seen_phrases: set[str] = set()
    for lines, label in (
        (comparison.vs_consensus, "vs consensus"),
        (comparison.vs_prior, "vs prior period"),
        (comparison.vs_prior_year, "vs prior year"),
    ):
        phrases = _variance_phrase_for(lines, currency)
        if not phrases:
            continue
        body = ", ".join(phrases)
        # For an ANNUAL reporter the prior period IS the prior year (same record),
        # so the two clauses would carry identical phrasing — emit it once
        # (#44 Codex re-review SEV-3).
        if body in seen_phrases:
            continue
        seen_phrases.add(body)
        clauses.append(f"{_cap(body)} ({label})")
    if not clauses:
        return "No consensus or prior period on file to compare against."
    return "; ".join(clauses) + "."


def _cap(s: str) -> str:
    return s[:1].upper() + s[1:] if s else s


def render_card(
    entry: CompanyEntry,
    extracted: ExtractedEarnings,
    comparison: Comparison,
) -> str:
    """Render the full dated results section (heading → body → hidden machine
    line). Append-ready; ends with a single trailing newline."""
    lines: list[str] = [section_heading(extracted), ""]
    lines.append(f"**Headlines:** {_headlines(extracted, comparison)}.")
    lines.append("")

    kpis = _kpi_lines(extracted)
    if kpis:
        lines.append("**KPIs:**")
        lines.extend(kpis)
        lines.append("")

    if extracted.divisions:
        lines.append("**Divisional split:**")
        for div in extracted.divisions:
            rev = _money(div.revenue_m, extracted.currency)
            yoy = f" ({_pct(div.revenue_yoy)} YoY)" if div.revenue_yoy is not None else ""
            note = f" — {div.note}" if div.note else ""
            lines.append(f"- {div.name}: {rev}{yoy}{note}".rstrip())
        lines.append("")

    lines.append(f"**Variance:** {_variance(comparison, extracted.currency)}")
    lines.append("")

    if comparison.material and comparison.material_reasons:
        lines.append(f"**⚠ Material variance:** {'; '.join(comparison.material_reasons)}.")
        lines.append("")

    if extracted.commentary:
        lines.append(f"**Management commentary:** {extracted.commentary}")
        lines.append("")

    source = extracted.source_url or entry.source_url or entry.consensus_source or "n/a"
    if source.startswith("http"):
        lines.append(f"**Source:** [announcement]({source})")
    else:
        lines.append(f"**Source:** {source}")
    lines.append("")

    # The hidden machine record carries enough to REPLAY this capture's
    # side-effects deterministically on a later self-heal sweep (if the proposal
    # emit failed the first time) — the materiality verdict + reasons are frozen
    # here from THIS run's comparison, so a re-extraction that diverges can't
    # change them (#44 Codex).
    rec = extracted.machine_record()
    rec["guidance_change"] = extracted.guidance_change
    rec["material"] = comparison.material
    rec["material_reasons"] = list(comparison.material_reasons)
    rec["consensus_revenue_m"] = _consensus_baseline(comparison, "revenue")
    rec["consensus_ebitda_m"] = _consensus_baseline(comparison, "ebitda")
    rec["consensus_eps"] = _consensus_baseline(comparison, "eps")
    lines.append(machine_line(rec))
    lines.append("")

    return "\n".join(lines)


def render_sector_point(entry: CompanyEntry, extracted: ExtractedEarnings) -> str:
    """One-line data point for the sector page (step 9). Dated, sourced bullet
    keyed by the company + period so it's idempotent on re-append."""
    rev = _money(extracted.revenue_m, extracted.currency)
    bits = [f"Revenue {rev}"]
    if extracted.revenue_yoy is not None:
        bits.append(f"{_pct(extracted.revenue_yoy)} YoY")
    if extracted.ebitda_margin is not None:
        bits.append(f"EBITDA margin {_pct(extracted.ebitda_margin, signed=False)}")
    reported = extracted.reported_date.isoformat() if extracted.reported_date else "n/a"
    name_link = f"[[Companies/{entry.stem}|{entry.name}]]"
    return f"- **{reported}** — {name_link} {extracted.period_label}: {', '.join(bits)}."


__all__ = ["section_heading", "render_card", "render_sector_point"]
