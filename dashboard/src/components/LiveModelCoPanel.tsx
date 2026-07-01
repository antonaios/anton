/**
 * LiveModelCoPanel — the "Live Model" engine-preview co-panel (Paper v2 5T3-0).
 *
 * Shown ONLY at the 55" ultra-wide Desk layout, sitting as a fourth column card
 * beside sessions / chat / rail. It previews where the modelling engine feed
 * will surface once wired: a valuation football field + a sponsor-IRR
 * sensitivity grid for the base-case LBO.
 *
 * STATIC / PREVIEW — the engine feed is NOT wired yet. Every number below is
 * hardcoded sample data lifted verbatim from the artboard, and the deliberate
 * "PREVIEW · ROADMAP" / "sample data" / "not yet wired" badges MUST stay until
 * the engine is connected. No fetches, no handlers; the "Open in Excel" button
 * is intentionally inert.
 *
 * Theming: structural surfaces use the project theme tokens (bg-bg-2, bg-paper2,
 * text-t1..t4, border-line, accent, amber, shadow-card, mono) so the card flips
 * light/navy with the rest of the dashboard. The football-field bar FILLS and
 * the IRR-cell TINTS are chart colours (not theme tokens), so they keep the
 * exact hex/rgba values from the artboard.
 */

import { ChevronsRight } from "lucide-react";

// ── Sample data (artboard 5T3-0; static until the engine feed is wired) ──────

/** Football-field rows. `ml`/`w` are percentages of the 700–1000 EV (£m) axis. */
const VALUATION_ROWS: { label: string; ml: string; w: string; range: string; fill: string }[] = [
  { label: "LBO",           ml: "26.7%", w: "round(46.7%,1px)", range: "780–920", fill: "#2E6F6C" },
  { label: "DCF",           ml: "20%",   w: "round(46.7%,1px)", range: "760–900", fill: "#347E7A" },
  { label: "Trading comps", ml: "33.3%", w: "round(50%,1px)",   range: "800–950", fill: "#2F7A50" },
  { label: "Precedents",    ml: "40%",   w: "round(53.3%,1px)", range: "820–980", fill: "#3A8A5A" },
];

/** Sponsor-IRR sensitivity grid. `base: true` flags the highlighted base case. */
const IRR_EXITS = ["10.0×", "11.0×", "12.0×"];
const IRR_ROWS: { entry: string; cells: { irr: string; tint: string; base?: boolean }[] }[] = [
  {
    entry: "11.5×",
    cells: [
      { irr: "19%", tint: "#B05A5229" },
      { irr: "23%", tint: "#4E9C6E2E" },
      { irr: "27%", tint: "#4E9C6E52" },
    ],
  },
  {
    entry: "12.0×",
    cells: [
      { irr: "17%", tint: "#B05A5238" },
      { irr: "21%", tint: "#B0833C2E" },
      { irr: "24%", tint: "#4E9C6E2E" },
    ],
  },
  {
    entry: "12.5×",
    cells: [
      { irr: "15%", tint: "#B05A5247" },
      { irr: "18%", tint: "#3F8B882E", base: true },
      { irr: "22%", tint: "#B0833C2E" },
    ],
  },
];

export function LiveModelCoPanel({ onCollapse }: { onCollapse?: () => void } = {}) {
  return (
    <div className="flex w-[520px] shrink-0 flex-col overflow-hidden rounded-[16px] bg-bg-2 shadow-card min-h-0">
      {/* Header — eyebrow + PREVIEW pill, "sample data", then the model title */}
      <div className="shrink-0 border-b border-line px-[22px] pt-[17px] pb-[15px]">
        <div className="mb-[8px] flex items-center justify-between">
          <div className="flex items-center gap-[10px]">
            <span className="text-[10px] font-bold leading-[12px] tracking-[0.11em] text-t3">
              LIVE MODEL
            </span>
            <span className="inline-flex items-center gap-[5px] rounded-[5px] border border-solid border-[#CDA25573] bg-[#CDA2551A] px-[8px] py-[2px]">
              <span className="mono text-[9.5px] leading-[12px] tracking-[0.13em] text-amber">
                PREVIEW · ROADMAP
              </span>
            </span>
          </div>
          <div className="flex items-center gap-[10px]">
            <span className="text-[10.5px] leading-[14px] text-t3">sample data</span>
            {onCollapse && (
              <button
                type="button"
                onClick={onCollapse}
                title="Collapse Live Model"
                aria-label="Collapse Live Model panel"
                className="flex size-[26px] items-center justify-center rounded-[7px] text-t3 transition-colors hover:bg-bg-1 hover:text-t1"
              >
                <ChevronsRight size={15} strokeWidth={1.8} />
              </button>
            )}
          </div>
        </div>
        <div className="text-[15px] font-semibold leading-[120%] text-t1">
          Base-case LBO · downside grid
        </div>
      </div>

      {/* Valuation range — football field over a 700–1000 EV (£m) axis */}
      <div className="px-[22px] pt-[18px] pb-[16px]">
        <div className="mb-[15px] flex items-baseline justify-between">
          <span className="text-[9.5px] font-bold leading-[12px] tracking-[0.1em] text-t3">
            VALUATION RANGE
          </span>
          <span className="text-[9.5px] leading-[12px] text-t4">EV (£m)</span>
        </div>

        {VALUATION_ROWS.map((row) => (
          <div key={row.label} className="mb-[14px] flex items-center gap-[12px]">
            <span className="w-[88px] shrink-0 text-[11.5px] font-medium leading-[14px] text-t2">
              {row.label}
            </span>
            <div className="h-[9px] grow basis-0 overflow-hidden rounded-[5px] bg-paper2">
              <div
                className="h-full rounded-[5px]"
                style={{ marginLeft: row.ml, width: row.w, backgroundColor: row.fill }}
              />
            </div>
            <span className="mono flex w-[64px] shrink-0 justify-end text-right text-[11px] leading-[14px] text-t1">
              {row.range}
            </span>
          </div>
        ))}

        {/* Axis ticks — aligned under the bar track (past the 88px + 12px label) */}
        <div className="flex justify-between pl-[100px]">
          <span className="mono text-[9px] leading-[12px] text-t1">700</span>
          <span className="mono text-[9px] leading-[12px] text-t1">850</span>
          <span className="mono text-[9px] leading-[12px] text-t1">1000</span>
        </div>
      </div>

      {/* Sponsor IRR — entry × exit sensitivity grid */}
      <div className="border-t border-line px-[22px] pt-[17px] pb-[16px]">
        <div className="mb-[13px] text-[9.5px] font-bold leading-[12px] tracking-[0.1em] text-t3">
          SPONSOR IRR — ENTRY × EXIT
        </div>

        <div className="flex flex-col gap-[5px]">
          {/* Exit header row */}
          <div className="flex gap-[5px]">
            <div className="flex w-[54px] shrink-0 items-center text-[9.5px] leading-[12px] text-t4">
              exit →
            </div>
            {IRR_EXITS.map((exit) => (
              <div
                key={exit}
                className="mono flex grow basis-0 flex-wrap justify-center text-center text-[10px] leading-[12px] text-t3"
              >
                {exit}
              </div>
            ))}
          </div>

          {/* Entry rows — each with 3 tinted IRR cells */}
          {IRR_ROWS.map((row) => (
            <div key={row.entry} className="flex gap-[5px]">
              <div className="mono flex w-[54px] shrink-0 items-center text-[10px] leading-[12px] text-t2">
                {row.entry}
              </div>
              {row.cells.map((cell, i) => (
                <div
                  key={i}
                  className="grow basis-0 rounded-[6px] py-[8px]"
                  style={{
                    backgroundColor: cell.tint,
                    ...(cell.base ? { border: "1px solid #3F8B88" } : {}),
                  }}
                >
                  <div
                    className={`mono flex flex-wrap justify-center text-center text-[12px] leading-[16px] text-t1${
                      cell.base ? " font-semibold" : ""
                    }`}
                  >
                    {cell.irr}
                  </div>
                </div>
              ))}
            </div>
          ))}
        </div>

        <div className="mt-[11px] text-[10.5px] leading-[140%] text-t2">
          At 12.5× entry / 11.0× exit, IRR holds 18% — above your 15% bar, covenant headroom 0.4×.
        </div>
      </div>

      {/* Footer — preview note + (inert) Open-in-Excel button */}
      <div className="mt-auto flex shrink-0 items-center justify-between border-t border-line px-[22px] pt-[38px] pb-[22px]">
        <span className="text-[10px] leading-[12px] text-t3">
          Preview — football field + IRR grid are roadmap (not yet wired)
        </span>
        {/* Static preview — intentionally no onClick until the engine is wired. */}
        <button
          type="button"
          className="flex h-[28px] items-center gap-[6px] rounded-[8px] bg-accent px-[12px]"
        >
          <span className="text-[11px] font-semibold leading-[14px] text-[#F2F2F2]">
            Open in Excel
          </span>
        </button>
      </div>
    </div>
  );
}
