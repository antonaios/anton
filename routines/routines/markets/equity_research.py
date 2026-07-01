"""Equity-research workflow orchestrator.

Phase 4 of the OpenBB integration. Given a public ticker:
  1. Fetch a price snapshot (60s cached quote)
  2. Fetch 5y fundamentals + ratios (24h cached)
  3. Build a comps table — target + 6 peers (reuses comps.py)
  4. Fetch the last 14 days of news (30m cached)
  5. Optionally write a structured Equity Research section to
     Companies/<safename>.md (append; create file with stub if missing).

No LLM in the loop — deterministic data assembly + markdown rendering.
Analyst commentary slots are left empty for the operator to fill.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from routines.api.deps import VAULT
from routines.markets import get_provider
from routines.markets.comps import build_comps, _comps_table_md, _fmt_num, _atomic_write
from routines.markets.types import (
    EquityResearchResult, EquityResearchSnapshot, Fundamentals,
)
from routines.shared.filename import safe_filename

log = logging.getLogger(__name__)


def build_equity_research(
    symbol: str,
    *,
    years: int = 5,
    peers_limit: int = 6,
    news_days: int = 14,
    news_limit: int = 12,
    write_note: bool = True,
) -> EquityResearchResult:
    provider = get_provider()
    warnings: list[str] = []

    # ── 1. Snapshot ──────────────────────────────────────────────────────
    snap = EquityResearchSnapshot(symbol=symbol)
    try:
        quotes = provider.get_quotes([symbol])
        if quotes:
            q = quotes[0]
            snap.name = q.name
            snap.currency = q.currency
            snap.last_price = q.price
            snap.price_change = q.change
            snap.direction = q.direction
    except Exception as e:  # noqa: BLE001
        warnings.append(f"Quote fetch failed: {e}")

    # ── 2. Fundamentals ───────────────────────────────────────────────────
    funda: Fundamentals = provider.get_fundamentals(symbol, years=years)
    if funda.error:
        warnings.append(f"Fundamentals: {funda.error}")
    if funda.ratios:
        snap.pe = funda.ratios.pe
        snap.ev_ebitda = funda.ratios.ev_ebitda
        snap.dividend_yield = funda.ratios.dividend_yield
        snap.ebitda_margin = funda.ratios.ebitda_margin
        snap.revenue_growth_5y_cagr = funda.ratios.revenue_growth_5y_cagr

    # ── 3. Comps (target + peers) ─────────────────────────────────────────
    comps = build_comps(symbol, peers_limit=peers_limit, years=years, write_note=False)
    warnings.extend(comps.warnings)

    # ── 4. News ───────────────────────────────────────────────────────────
    news = provider.get_news(symbol, days=news_days, limit=news_limit)
    if news.error:
        warnings.append(f"News: {news.error}")

    # ── 5. Write the structured section ───────────────────────────────────
    note_path: str | None = None
    if write_note:
        try:
            note_path = _write_equity_research_note(symbol, snap, funda, comps, news)
        except Exception as e:  # noqa: BLE001
            log.exception("equity_research: write_note failed")
            warnings.append(f"Note write failed: {e}")

    # #2d: surface note_path as ABSOLUTE for the dashboard's file:// chip.
    absolute_note_path: str | None = None
    if note_path:
        absolute_note_path = str((VAULT / note_path).resolve())

    return EquityResearchResult(
        target_symbol=symbol,
        snapshot=snap,
        fundamentals=funda,
        comps=comps,
        news=news,
        note_path=absolute_note_path,
        provider=provider.name,
        warnings=warnings,
    )


# ── Markdown writer ───────────────────────────────────────────────────────


def _five_year_md(funda: Fundamentals) -> str:
    if not funda.years:
        return "_No 5y financials returned by provider._"
    header = "| FY | Revenue | EBITDA | EBITDA % | EBIT | Net income | FCF | Total debt | Cash | Equity |"
    sep    = "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|"
    lines = [header, sep]
    for y in funda.years:
        margin = (y.ebitda / y.revenue) if (y.ebitda and y.revenue) else None
        lines.append(
            "| " + str(y.fiscal_year)
            + " | " + _fmt_num(y.revenue)
            + " | " + _fmt_num(y.ebitda)
            + " | " + (f"{margin * 100:.1f}%" if margin is not None else "—")
            + " | " + _fmt_num(y.ebit)
            + " | " + _fmt_num(y.net_income)
            + " | " + _fmt_num(y.free_cash_flow)
            + " | " + _fmt_num(y.total_debt)
            + " | " + _fmt_num(y.cash_and_equivalents)
            + " | " + _fmt_num(y.shareholders_equity)
            + " |"
        )
    return "\n".join(lines)


def _news_md(news) -> str:
    if not news.items:
        return "_No news in the look-back window._"
    out = []
    for it in news.items:
        date = (it.published or "")[:10]
        out.append(f"- **{date}** — [{it.title}]({it.url})" + (f" · _{it.source}_" if it.source else ""))
    return "\n".join(out)


def _snapshot_md(snap: EquityResearchSnapshot) -> str:
    rows = [
        ("Ticker", snap.symbol),
        ("Name", snap.name or "—"),
        ("Last price", (f"{snap.last_price} ({snap.price_change})" if snap.last_price else "—")),
        ("PE", f"{snap.pe:.1f}x" if snap.pe else "—"),
        ("EV/EBITDA", f"{snap.ev_ebitda:.1f}x" if snap.ev_ebitda else "—"),
        ("Div yield", f"{snap.dividend_yield * 100:.1f}%" if snap.dividend_yield else "—"),
        ("EBITDA margin", f"{snap.ebitda_margin * 100:.1f}%" if snap.ebitda_margin else "—"),
        ("Revenue 5y CAGR", f"{snap.revenue_growth_5y_cagr * 100:.1f}%" if snap.revenue_growth_5y_cagr else "—"),
    ]
    return "\n".join(f"- **{k}** · {v}" for k, v in rows)


def _write_equity_research_note(
    symbol: str,
    snap: EquityResearchSnapshot,
    funda: Fundamentals,
    comps,
    news,
) -> str:
    name = funda.name or snap.name or symbol
    # #2e: shared sanitizer (was per-skill `_safe_name`).
    safe = safe_filename(name, fallback=symbol)
    companies_dir = VAULT / "Companies"
    companies_dir.mkdir(parents=True, exist_ok=True)
    path = companies_dir / f"{safe}.md"
    rel_path = f"Companies/{safe}.md"

    today = datetime.now(timezone.utc).date().isoformat()

    section = (
        f"\n\n## Equity research · {today} · auto-generated\n\n"
        f"Pulled via OpenBB for **{symbol}** ({name}). Combines snapshot + 5y financials + "
        f"comps (target + {len(comps.rows) - 1} peers) + last {14}-day news. "
        f"**No LLM synthesis** — figures are direct from the data provider; verify before quoting.\n\n"

        f"### Snapshot\n\n"
        f"{_snapshot_md(snap)}\n\n"

        f"### 5-year financials\n\n"
        f"{_five_year_md(funda)}\n\n"

        f"### Comps\n\n"
        f"{_comps_table_md(comps.rows)}\n\n"

        f"### News · last 14 days\n\n"
        f"{_news_md(news)}\n\n"

        f"### Analyst commentary\n\n"
        f"_Operator-written. Use the data above. Cite sources._\n\n"
        f"- **Thesis**: \n"
        f"- **Risks**: \n"
        f"- **Catalysts**: \n\n"

        f"<small>Generated by `/api/workflows/equity-research` — provider: {comps.provider or 'unknown'}</small>\n"
    )

    if path.exists():
        existing = path.read_text(encoding="utf-8")
        new = existing.rstrip() + section
        _atomic_write(path, new, vault_root=VAULT)
    else:
        header = (
            "---\n"
            f"type: company-profile\n"
            "sensitivity: public\n"
            f"ticker: {symbol}\n"
            f"created: {today}\n"
            "tags: [company, auto-stub, equity-research]\n"
            "---\n\n"
            f"# {name}\n\n"
            f"Public ticker · `{symbol}`. Stub created by the Equity Research workflow — "
            f"upgrade to a deep [[Templates/company-profile]] when curating.\n"
        )
        _atomic_write(path, header + section, vault_root=VAULT)

    return rel_path


# Suppress unused-import warnings
_ = Path
