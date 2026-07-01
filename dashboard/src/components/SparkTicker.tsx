import { useEffect, useState } from "react";
import { cn } from "../lib/cn";
import { SECTOR_COMPS } from "../data/seed";
import { api } from "../lib/api";
import { sparklineDirection, sparklineStroke } from "../lib/sparkline";
import type { Quote, SectorComp, TickerBarEntry } from "../types";

/**
 * Listed-equity ticker bar with inline SVG sparklines (12-month weekly
 * close history pulled from OpenBB via `/api/markets/quotes`).
 *
 * The list of symbols comes from `_claude/tickers.md` in the vault
 * (see `/api/markets/ticker-bar`). Operator edits that file → next
 * dashboard refresh picks up the new list. No rebuild, no restart.
 *
 * Scrolls slowly rightward. Duplicate the items so the loop wraps
 * seamlessly. Hover anywhere on the bar to pause.
 *
 * Fallback chain:
 *   1. Try /api/markets/ticker-bar for the symbol list.
 *   2. Try /api/markets/quotes for those symbols (live OpenBB).
 *   3. If either fails, fall back to the seed in src/data/seed.ts so the
 *      bar always has something to render.
 */

const FALLBACK_SYMBOLS = ["JDW.L", "IHG.L", "WTB.L", "MAB.L", "BOWL.L", "SSPG.L"];

interface DisplayRow {
  ticker: string;
  name: string;
  price: string;
  change: string;
  up: boolean;
  points: string;
  glyph: string;
}

function inferGlyph(currency?: string | null, ticker?: string): string {
  const code = (currency ?? "").toUpperCase();
  if (code === "GBP" || code === "GBp") return "£";
  if (code === "USD") return "$";
  if (code === "EUR") return "€";
  if (!ticker) return "";
  if (ticker.endsWith(".L")) return "£";
  if (ticker.endsWith(".DE") || ticker.endsWith(".PA") || ticker.endsWith(".AS") || ticker.endsWith(".MI")) return "€";
  if (ticker.endsWith(".HK")) return "HK$";
  if (ticker.endsWith(".T")) return "¥";
  if (/^[A-Z]+$/.test(ticker)) return "$";
  return "";
}

function seedRow(c: SectorComp): DisplayRow {
  return {
    ticker: c.ticker, name: c.name, price: c.price, change: c.change,
    up: c.up, points: c.points, glyph: inferGlyph(undefined, c.ticker),
  };
}

function quoteRow(q: Quote, displayName?: string): DisplayRow {
  return {
    ticker: q.symbol, name: displayName || q.name, price: q.price, change: q.change,
    up: q.direction === "up", points: q.points, glyph: inferGlyph(q.currency, q.symbol),
  };
}

function fallbackRows(symbols: string[]): DisplayRow[] {
  return symbols.map((s) => {
    const seed = SECTOR_COMPS.find((c) => c.ticker === s);
    return seed
      ? seedRow(seed)
      : { ticker: s, name: s, price: "—", change: "", up: true, points: "", glyph: inferGlyph(undefined, s) };
  });
}

export function SparkTicker() {
  const [rows, setRows] = useState<DisplayRow[]>(SECTOR_COMPS.map(seedRow));
  const [provider, setProvider] = useState<string>("seed");

  useEffect(() => {
    let cancelled = false;

    (async () => {
      let entries: TickerBarEntry[];
      try {
        const bar = await api.tickerBar();
        entries = bar.tickers;
      } catch {
        entries = FALLBACK_SYMBOLS.map((s) => ({ symbol: s, name: s }));
      }
      if (cancelled) return;

      const symbols = entries.map((e) => e.symbol);
      const nameOverride = new Map(entries.map((e) => [e.symbol, e.name] as const));

      setRows(
        fallbackRows(symbols).map((r) => ({
          ...r,
          name: nameOverride.get(r.ticker) ?? r.name,
        }))
      );

      try {
        const res = await api.marketsQuotes(symbols);
        if (cancelled) return;
        const byTicker = new Map(res.quotes.map((q) => [q.symbol, q] as const));
        const ordered = symbols.map((s) => {
          const q = byTicker.get(s);
          if (q) return quoteRow(q, nameOverride.get(s));
          const seed = SECTOR_COMPS.find((c) => c.ticker === s);
          return seed
            ? { ...seedRow(seed), name: nameOverride.get(s) ?? seed.name }
            : { ticker: s, name: nameOverride.get(s) ?? s, price: "—", change: "", up: true, points: "", glyph: inferGlyph(undefined, s) };
        });
        setRows(ordered);
        setProvider(res.provider);
      } catch {
        /* bridge offline — keep seed-priced rows */
      }
    })();

    return () => { cancelled = true; };
  }, []);

  // Render the items twice so the scrolling loop wraps seamlessly.
  const doubled = [...rows, ...rows];

  return (
    <div
      className="ticker-wrap h-[30px] overflow-hidden border-t border-b border-line bg-bg-2 antialiased [font-synthesis:none]"
      title={`Markets provider: ${provider}`}
    >
      <div className="ticker-scroll ticker-scroll-right flex h-full w-max items-center">
        {doubled.map((c, i) => (
          <TickerCell key={`${c.ticker}-${i}`} c={c} />
        ))}
      </div>
    </div>
  );
}

function TickerCell({ c }: { c: { ticker: string; name: string; price: string; change: string; up: boolean; points: string; glyph: string } }) {
  // Two distinct directional signals:
  //  - change-pct colour: today's intraday move (c.up driven by Quote.direction)
  //  - sparkline stroke:  12-month trajectory (derived from polyline endpoints)
  const sparkDir = sparklineDirection(c.points);
  return (
    <div className="flex shrink-0 items-center gap-[7px] px-[12px] min-w-[200px]">
      <span className="text-[10.5px] font-semibold leading-[14px] text-t1">{c.name}</span>
      {c.points && (
        <svg width="34" height="13" viewBox="0 -1.5 60 23" fill="none" className="shrink-0">
          <polyline
            points={c.points}
            stroke={sparklineStroke(sparkDir)}
            strokeWidth="1.3"
            strokeLinecap="round"
            strokeLinejoin="round"
            fill="none"
          />
        </svg>
      )}
      <span className="font-mono text-[10.5px] leading-[14px] text-t2 tabular-nums">
        {c.glyph}{c.price}
      </span>
      <span className={cn("font-mono text-[10.5px] leading-[14px] tabular-nums", c.up ? "text-green" : "text-red")}>{c.change}</span>
    </div>
  );
}
