import { useEffect, useState } from "react";
import { cn } from "../lib/cn";
import { api } from "../lib/api";
import { sparklineDirection, sparklineStroke } from "../lib/sparkline";
import type { MacroRow } from "../types";

/**
 * Macro/index/commodity/rate/indicator ticker bar — second row below
 * the equity SparkTicker. Sourced from `_claude/tickers.md` →
 * /api/markets/macro-bar.
 *
 * Scrolls slowly leftward (opposite direction from the equity bar above).
 * Names use the same weight/color as the equity bar for visual parity.
 * Hover to pause.
 */
export function MacroTicker() {
  const [rows, setRows] = useState<MacroRow[] | null>(null);

  useEffect(() => {
    let cancelled = false;
    api.macroBar()
      .then((res) => { if (!cancelled) setRows(res.rows); })
      .catch(() => { /* silent — leave empty */ });
    return () => { cancelled = true; };
  }, []);

  if (!rows || rows.length === 0) return null;

  const doubled = [...rows, ...rows];

  return (
    <div className="ticker-wrap h-[30px] overflow-hidden border-b border-line bg-bg-2 antialiased [font-synthesis:none]">
      <div className="ticker-scroll ticker-scroll-left flex h-full w-max items-center">
        {doubled.map((r, i) => (
          <MacroCell key={`${r.symbol}-${i}`} r={r} />
        ))}
      </div>
    </div>
  );
}

function MacroCell({ r }: { r: MacroRow }) {
  // Two distinct directional signals:
  //  - change colour:    today's intraday / daily move (r.direction)
  //  - sparkline stroke: 12-month trajectory (derived from the polyline)
  const up = r.direction === "up";
  const down = r.direction === "down";
  const sparkDir = sparklineDirection(r.points);
  return (
    <div
      className="flex shrink-0 items-center gap-[7px] px-[12px] min-w-[200px]"
      title={r.note ?? ""}
    >
      <span className="text-[10.5px] font-semibold leading-[14px] text-t2">{r.name}</span>
      {r.points && (
        <svg width="34" height="13" viewBox="0 -1.5 60 23" fill="none" className="shrink-0">
          <polyline
            points={r.points}
            stroke={sparklineStroke(sparkDir)}
            strokeWidth="1.3"
            strokeLinecap="round"
            strokeLinejoin="round"
            fill="none"
          />
        </svg>
      )}
      <span className="font-mono text-[10.5px] leading-[14px] text-t1 tabular-nums">{r.value}</span>
      <span className={cn(
        "font-mono text-[10.5px] leading-[14px] tabular-nums",
        up && "text-green",
        down && "text-red",
        r.direction === "flat" && "text-t3",
      )}>
        {r.change || "—"}
      </span>
    </div>
  );
}
