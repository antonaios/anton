"""Operator-attended smoke test for WS-A's formatted v2-template stamp.

NOT a pytest test (intentionally underscored so pytest's default discovery
skips it). The acceptance criterion for "comps outputs my format" is the
operator eyeballing a real end-to-end stamp; this CLI helper lets the operator
fire the full Stage 0-3 pipeline against a throwaway test deal + stub provider
data and open the resulting XLSX in Excel against the v2 template.

Run (PowerShell):
    cd "<repo>/routines"
    .venv/Scripts/python.exe -m routines.skills.comps.scripts._smoke_stamp

The script:
  1. Builds a tmp deal tree under a temp directory.
  2. Synthesizes 3 approved subsectors with 2-3 sourced peers each.
  3. Calls ``_shim_xlsx_stamp`` against the REAL
     ``os-templates/Project_x_Comps_v2.xlsx`` (operator-attended only —
     this is the seam the route fires in production).
  4. Prints the output path to stdout for the operator to open.

No network, no provider call, no MNPI sensitivity check (stub provider
data; sensitivity=internal). Safe to re-run.
"""

from __future__ import annotations

import sys
import tempfile
from datetime import date
from pathlib import Path


REAL_TEMPLATE = Path("os-templates/Project_x_Comps_v2.xlsx")

BLOCKS = [
    {
        "subsector_slug": "hotels-full-service",
        "coco_rows": [
            {
                "ticker": "IHG.L", "name": "InterContinental Hotels", "currency": "USD",
                "ccy_px": "USD", "fs_currency": "USD",
                "shr_price": 95.50, "shares_out": 165.0,        # market_cap = 15,758
                "net_debt_m": 2000.0,
                "fye": "2025-12-31",
                "revenue_lfy_m": 4500.0,
                "ebitda_lfym1_m": 980.0, "ebitda_lfy_m": 1100.0,
                "revenue_lfy1_m": 4800.0, "ebitda_lfy1_m": 1180.0,
                "pe_ltm": 22.5,
                "source": "openbb-yfinance:2026-06-02",
                "net_debt_source": "https://www.ihgplc.com/-/media/fy25-results.pdf",
                "lfy1_source": "operator-approved:2026-06-02",
            },
            {
                "ticker": "MAR.OQ", "name": "Marriott International", "currency": "USD",
                "ccy_px": "USD", "fs_currency": "USD",
                "shr_price": 218.40, "shares_out": 310.0,        # 67,704
                "net_debt_m": 12500.0,
                "fye": "2025-12-31",
                "revenue_lfy_m": 24000.0,
                "ebitda_lfym1_m": 3950.0, "ebitda_lfy_m": 4200.0,
                "revenue_lfy1_m": 25500.0, "ebitda_lfy1_m": 4500.0,
                "pe_ltm": 24.1,
                "source": "openbb-yfinance:2026-06-02",
                "net_debt_source": "https://www.marriott.com/investor/fy25-10k.pdf",
                "lfy1_source": "operator-approved:2026-06-02",
            },
            {
                "ticker": "HLT", "name": "Hilton Worldwide", "currency": "USD",
                "ccy_px": "USD", "fs_currency": "USD",
                "shr_price": 195.20, "shares_out": 251.0,        # 48,995
                "net_debt_m": 9200.0,
                "fye": "2025-12-31",
                "revenue_lfy_m": 10500.0,
                "ebitda_lfym1_m": 2680.0, "ebitda_lfy_m": 2900.0,
                "revenue_lfy1_m": 11200.0, "ebitda_lfy1_m": 3100.0,
                "pe_ltm": 25.7,
                "source": "openbb-yfinance:2026-06-02",
                "net_debt_source": "https://ir.hilton.com/fy25-10k.pdf",
                "lfy1_source": "operator-approved:2026-06-02",
            },
        ],
        "cotrans_rows": [
            {
                "deal_id": "PT-2026-04-15-midscale",
                "announced_date": "2026-04-15",
                "target": "MidScale Hotels Group", "acquirer": "BigCo PE",
                "country": "UK", "currency": "GBP",
                "description": "120-key urban portfolio",
                "ev_m": 380.0, "revenue_m": 110.0, "ebitda_m": 28.0,
                "ev_revenue_x": 3.5, "ev_ebitda_x": 13.6,
                # Deal IS in the canonical tracker → stamper writes a
                # tracker-first hyperlink (cell displays `tracker:<deal_id>`;
                # click opens the canonical tracker xlsx at row 7 inside the
                # 'Precedent transactions' sheet, where the operator's
                # curated context + their own source links live).
                "source": "https://www.mergermarket.com/intelligence/view/intelcms-midscale-2026-04",
                "_tracker_row": 7,
                "strategic_commentary": "Cap-light scale play",
            },
        ],
    },
    {
        "subsector_slug": "hotels-limited-service",
        "coco_rows": [
            {
                "ticker": "WTB.L", "name": "Premier Inn (Whitbread)", "currency": "GBp",
                "ccy_px": "GBp", "fs_currency": "GBP",            # GBp/GBP mismatch → FX flag
                "shr_price": 3245.0, "shares_out": 195.0,
                "net_debt_m": 1200.0,
                "fye": "2026-02-28",
                "revenue_lfy_m": 2300.0,
                "ebitda_lfym1_m": 440.0, "ebitda_lfy_m": 480.0,
                "revenue_lfy1_m": 2480.0, "ebitda_lfy1_m": 520.0,
                "pe_ltm": 16.8,
                "source": "openbb-yfinance:2026-06-02",
                "net_debt_source": "https://www.whitbread.co.uk/ir/fy25-results.pdf",
                "lfy1_source": "operator-approved:2026-06-02",
            },
            {
                "ticker": "ACC.PA", "name": "Accor SA", "currency": "EUR",
                "ccy_px": "EUR", "fs_currency": "EUR",
                "shr_price": 44.20, "shares_out": 248.0,
                "net_debt_m": 2400.0,
                "fye": "2025-12-31",
                "revenue_lfy_m": 5800.0,
                "ebitda_lfym1_m": 1010.0, "ebitda_lfy_m": 1100.0,
                "revenue_lfy1_m": 6200.0, "ebitda_lfy1_m": 1180.0,
                "pe_ltm": 19.4,
                "source": "openbb-yfinance:2026-06-02",
                "net_debt_source": "https://group.accor.com/ir/fy25.pdf",
                "lfy1_source": "operator-approved:2026-06-02",
            },
        ],
        "cotrans_rows": [],
    },
    {
        "subsector_slug": "hotels-boutique",
        "coco_rows": [
            {
                "ticker": "BOU.L", "name": "Boutique Hotels plc", "currency": "GBP",
                "ccy_px": "GBP", "fs_currency": "GBP",
                "shr_price": 425.0, "shares_out": 38.0,
                "net_debt_m": 150.0,
                "fye": "2025-09-30",
                "revenue_lfy_m": 220.0,
                "ebitda_lfym1_m": 35.0, "ebitda_lfy_m": 40.0,
                "revenue_lfy1_m": 245.0, "ebitda_lfy1_m": 46.0,
                "pe_ltm": 18.2,
                "source": "openbb-yfinance:2026-06-02",
                "net_debt_source": "https://example.com/boutique-fy25.pdf",
                "lfy1_source": "operator-approved:2026-06-02",
            },
        ],
        "cotrans_rows": [
            {
                "deal_id": "PT-2026-03-01-coastal",
                "announced_date": "2026-03-01",
                "target": "Coastal Boutique Co", "acquirer": "Specialty PE",
                "country": "UK", "currency": "GBP",
                "description": "8-property lifestyle collection",
                "ev_m": 95.0, "revenue_m": 32.0, "ebitda_m": 8.0,
                "ev_revenue_x": 3.0, "ev_ebitda_x": 11.9,
                # Deal NOT in tracker AND no source URL → stamper writes
                # plain `tracker:<deal_id>` back-reference (no hyperlink).
                # Exercises priority-3 plain-text fallback.
                "source": "",
                "strategic_commentary": "Bolt-on for sponsor's lifestyle platform",
            },
            {
                "deal_id": "PT-2026-05-20-orphan",
                "announced_date": "2026-05-20",
                "target": "Orphan Resorts Ltd", "acquirer": "Mystery PE",
                "country": "UK", "currency": "GBP",
                "description": "Deal_id approved by operator but never landed in tracker",
                "ev_m": 60.0, "revenue_m": 20.0, "ebitda_m": 5.0,
                "ev_revenue_x": 3.0, "ev_ebitda_x": 12.0,
                # Deal NOT in tracker BUT source IS URL-shaped → stamper
                # falls to priority-2 (raw URL hyperlink). Exercises the
                # rare orphan-deal-with-URL fallback path.
                "source": "https://example.com/orphan-resorts-pr",
                "strategic_commentary": "Orphan operator entry with press-release URL",
            },
        ],
    },
]


def main(argv: list[str] | None = None) -> int:
    argv = argv or sys.argv[1:]
    template = REAL_TEMPLATE
    if "--template" in argv:
        i = argv.index("--template")
        template = Path(argv[i + 1])

    if not template.is_file():
        print(f"ERROR: template not found at {template}", file=sys.stderr)
        print(
            "Pass --template <path> to override (e.g. an in-repo fixture).",
            file=sys.stderr,
        )
        return 2

    print(f"Smoke-stamp: using template {template}")
    print(f"  Blocks: {[b['subsector_slug'] for b in BLOCKS]}")
    print(f"  Peers per block: {[len(b['coco_rows']) for b in BLOCKS]}")
    print(f"  Deals per block: {[len(b['cotrans_rows']) for b in BLOCKS]}")

    # Import the stamper. We import inside main() so the smoke-test script
    # can be invoked from anywhere on the path even if openpyxl import
    # surfaces a stack trace.
    from routines.skills.comps.scripts.comps import _shim_xlsx_stamp

    out_dir = Path(tempfile.mkdtemp(prefix="comps_smoke_"))
    out_path = (
        out_dir / "TestDeal" / "3. Financials & analysis" / "2. Valuation"
        / "01. COMPS" / f"Project_TestDeal_COMPS_{date.today().isoformat()}_v1.xlsx"
    )
    _shim_xlsx_stamp(template, out_path, BLOCKS, sensitivity="internal")

    print()
    print("=" * 70)
    print("DONE — open in Excel to eyeball the formatted v2 deliverable:")
    print(f"  {out_path}")
    print("=" * 70)
    print()
    print("Operator checklist:")
    print("  [ ] One CoCo block per subsector, each with the operator's banner fill")
    print("  [ ] Block-N data rows show populated peer rows")
    print("  [ ] Stats footer (Mean/Median/75th/25th/Min/Max) per block, re-based")
    print("  [ ] CoTrans sheet mirrors the block structure")
    print("  [ ] Notes/methodology preserved at the bottom (once)")
    print("  [ ] Formulas live (Excel recalculates), not converted to values")
    print("  [ ] CoTrans Source cell for the MidScale deal is a CLICKABLE")
    print("      hyperlink (blue/underlined), opens the CANONICAL TRACKER")
    print("      xlsx (NOT mergermarket) at 'Precedent transactions'!A7")
    print("  [ ] CoTrans Source cell for the Coastal deal is PLAIN TEXT")
    print("      `tracker:PT-2026-03-01-coastal` (no hyperlink — no URL)")
    print("  [ ] CoTrans Source cell for the Orphan deal IS a clickable")
    print("      hyperlink, opens the example.com URL (priority-2 fallback")
    print("      — deal_id not in tracker so we fall through to raw URL)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
