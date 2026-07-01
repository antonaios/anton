"""Comps-pull orchestration.

Given a target ticker, pulls peers + per-peer fundamentals + per-peer
quotes, fuses into a CompsResult, and (optionally) writes a markdown
comps table to Companies/<X>.md in the vault.

Reuses the markets adapter, so no extra provider plumbing required.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from routines.api.deps import VAULT, vault_paths
from routines.markets import get_provider
from routines.markets.types import (
    CompRow, CompsResult, Fundamentals,
)
from routines.shared.filename import safe_filename

log = logging.getLogger(__name__)


def build_comps(
    symbol: str,
    *,
    peers_limit: int = 8,
    years: int = 5,
    write_note: bool = False,
) -> CompsResult:
    """Pull peers + fundamentals, return a CompsResult.

    If write_note=True, append a comps-table section to Companies/<X>.md
    (creating the file with a minimal frontmatter stub if missing) and
    set CompsResult.note_path to the relative vault path.
    """
    provider = get_provider()
    warnings: list[str] = []

    peers_res = provider.get_peers(symbol, limit=peers_limit)
    if peers_res.error:
        warnings.append(peers_res.error)

    target_funda = provider.get_fundamentals(symbol, years=years)
    target_row = _to_row(symbol, target_funda)

    rows: list[CompRow] = [target_row]
    for p in peers_res.peers:
        f = provider.get_fundamentals(p.symbol, years=years)
        rows.append(_to_row(p.symbol, f, name_fallback=p.name))

    note_path: Optional[str] = None
    if write_note:
        try:
            note_path = _write_comps_note(symbol, target_funda, rows)
        except Exception as e:  # noqa: BLE001
            log.exception("comps: write_note failed")
            warnings.append(f"Failed to write note: {e}")

    # #2d: surface note_path as an ABSOLUTE path so the dashboard's
    # chip handler can compose ``window.open("file://" + path)`` directly.
    # The writer returns vault-relative for backwards compat with anything
    # still serialising via relative_to(); we resolve to absolute here.
    absolute_note_path: Optional[str] = None
    if note_path:
        absolute_note_path = str((VAULT / note_path).resolve())

    return CompsResult(
        target_symbol=symbol,
        target_name=target_funda.name,
        rows=rows,
        note_path=absolute_note_path,
        provider=provider.name,
        warnings=warnings,
    )


def _to_row(symbol: str, f: Fundamentals, name_fallback: str | None = None) -> CompRow:
    latest = f.years[0] if f.years else None
    ratios = f.ratios
    return CompRow(
        symbol=symbol,
        name=f.name or name_fallback,
        currency=f.currency,
        revenue=latest.revenue if latest else None,
        ebitda=latest.ebitda if latest else None,
        ebitda_margin=(ratios.ebitda_margin if ratios else None),
        revenue_growth_5y_cagr=(ratios.revenue_growth_5y_cagr if ratios else None),
        pe=(ratios.pe if ratios else None),
        ev_ebitda=(ratios.ev_ebitda if ratios else None),
        net_debt_ebitda=(ratios.net_debt_ebitda if ratios else None),
        dividend_yield=(ratios.dividend_yield if ratios else None),
        fiscal_year=latest.fiscal_year if latest else None,
    )


# ── Markdown writer ───────────────────────────────────────────────────────


def _fmt_num(v: float | None, *, pct: bool = False, decimals: int = 1, scale: float = 1.0) -> str:
    """Format a float, gracefully showing '—' for None."""
    if v is None:
        return "—"
    val = v * scale
    if pct:
        return f"{val * 100:.{decimals}f}%"
    if abs(val) >= 1e9:
        return f"{val / 1e9:.{decimals}f}B"
    if abs(val) >= 1e6:
        return f"{val / 1e6:.{decimals}f}M"
    if abs(val) >= 1e3:
        return f"{val / 1e3:.{decimals}f}k"
    return f"{val:.{decimals}f}"


def _comps_table_md(rows: list[CompRow]) -> str:
    """Render the rows as a GitHub-flavoured markdown table."""
    header = (
        "| # | Ticker | Company | FY | Revenue | EBITDA | EBITDA % | "
        "5y CAGR | PE | EV/EBITDA | ND/EBITDA | Div yld |"
    )
    sep = "|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|"
    lines = [header, sep]
    for i, r in enumerate(rows):
        lines.append(
            "| "
            + str(i)
            + " | " + r.symbol
            + " | " + (r.name or "")
            + " | " + (str(r.fiscal_year) if r.fiscal_year else "—")
            + " | " + _fmt_num(r.revenue)
            + " | " + _fmt_num(r.ebitda)
            + " | " + _fmt_num(r.ebitda_margin, pct=True)
            + " | " + _fmt_num(r.revenue_growth_5y_cagr, pct=True)
            + " | " + _fmt_num(r.pe, decimals=1, scale=1.0)
            + " | " + _fmt_num(r.ev_ebitda, decimals=1)
            + " | " + _fmt_num(r.net_debt_ebitda, decimals=1)
            + " | " + _fmt_num(r.dividend_yield, pct=True)
            + " |"
        )
    return "\n".join(lines)


def _write_comps_note(symbol: str, target: Fundamentals, rows: list[CompRow]) -> str:
    """Append a comps section to Companies/<safename>.md, creating the
    file with a minimal stub if it doesn't exist. Returns the path
    relative to the vault root.
    """
    name = target.name or symbol
    # #2e: shared sanitizer (was per-skill `_safe_name`). Strips Windows-
    # illegal chars + trailing punctuation so "Apple Inc." no longer
    # composes to "Apple-Inc..md". Falls back to the ticker if name
    # sanitises to empty.
    safe = safe_filename(name, fallback=symbol)
    companies_dir = VAULT / "Companies"
    companies_dir.mkdir(parents=True, exist_ok=True)
    path = companies_dir / f"{safe}.md"
    rel_path = f"Companies/{safe}.md"

    today = datetime.now(timezone.utc).date().isoformat()
    table = _comps_table_md(rows)
    section = (
        f"\n\n## Comps · {today} · auto-generated\n\n"
        f"Pulled via OpenBB for **{symbol}** ({name}) plus {len(rows) - 1} peers. "
        f"Latest fiscal-year values per provider — verify against source filings before quoting.\n\n"
        f"{table}\n\n"
        f"<small>Generated by `/api/workflows/comps` — source: {target.provider or 'unknown'}</small>\n"
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
            "tags: [company, auto-stub, comps]\n"
            "---\n\n"
            f"# {name}\n\n"
            f"Public ticker · `{symbol}`. Stub created by the Comps workflow — "
            f"replace this preamble with a deep [[Templates/company-profile]] when curating.\n"
        )
        _atomic_write(path, header + section, vault_root=VAULT)

    return rel_path


def _atomic_write(path: Path, content: str, *, vault_root: Path) -> None:
    """Tempfile + rename in the same dir, via the F-4-guarded shared helper.

    F-4 (codex r1 SEV-1 → r2 SEV-2): the legacy "helper not on this branch"
    direct-write fallback is GONE — vault_writer ships in this package, so an
    ImportError means a broken install and any fallback is a chokepoint
    bypass. Fail closed: import/policy errors propagate."""
    from routines.shared.vault_writer import atomic_write  # lazy: heavy deps

    atomic_write(path, content, vault_root=vault_root)


# Suppress unused-import warning when vault_paths is imported but the
# fallback path is taken.
_ = vault_paths
