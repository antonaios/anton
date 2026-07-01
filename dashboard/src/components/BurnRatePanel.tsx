import { cn } from "../lib/cn";
import type { LLMBurnSummary } from "../types";

interface Props {
  /** Loaded summary from `/api/telemetry/llm-burn`. Null while in-flight. */
  burn?: LLMBurnSummary | null;
  /** True during the first load. */
  loading?: boolean;
  /** Inline error message; rendered as a small accent chip in the header. */
  error?: string | null;
}

/**
 * v5 right-rail Burn Rate panel — consumes /api/telemetry/llm-burn (#14).
 *
 * Quiet-desk restyle (Paper 14G-0): two headline stats — spend across the
 * telemetry window ("spent today") and a linear end-of-day projection —
 * over an empty-state line, or a compact volume summary when calls exist.
 * The per-provider + per-model breakdown now lives in the Budget tab; this
 * rail card stays at-a-glance. Value wiring is unchanged (real costUsd).
 */
export function BurnRatePanel({ burn, loading, error }: Props) {
  const hasData = !!burn && burn.totals.calls > 0;
  const spent = burn ? burn.totals.costUsd : 0;
  const projected = burn ? burn.totals.costUsd * projectionScaler(burn.window) : 0;
  const windowH = burn ? formatWindowHours(burn.window) : 24;
  const providerCount = burn ? Object.keys(burn.byProvider).length : 0;

  return (
    <div className="flex flex-col gap-[14px]">
      {/* Section head — mono uppercase rail label, status note right-aligned */}
      <div className="flex items-baseline justify-between">
        <span className="mono text-[10px] uppercase tracking-[0.12em] text-t3">Burn rate</span>
        <div className="flex items-baseline gap-[8px] text-[10px]">
          {error && (
            <span className="mono uppercase tracking-[0.08em] text-red" title={error}>error</span>
          )}
          {loading && !burn && <span className="italic text-t4">loading…</span>}
          <span className="mono uppercase tracking-[0.08em] text-t3">Projected EOD</span>
        </div>
      </div>

      {!burn && loading ? (
        /* Loading skeleton on first load */
        <div className="flex flex-col gap-[10px]">
          {[60, 80].map((w, i) => (
            <div key={i} className="h-[20px] rounded-[3px] bg-bg-2"
              style={{ width: `${w}%`, opacity: 0.5, animation: "pulse 1.8s ease-in-out infinite", animationDelay: `${i * 0.15}s` }} />
          ))}
        </div>
      ) : (
        <>
          {/* Headline stats — KpiCard label/value idiom, flat for the rail */}
          <div className="flex items-end gap-[28px]">
            <Stat value={fmtUsd(spent)} label="Spent today" muted={spent <= 0} />
            <Stat value={fmtUsd(projected)} label="Proj. EOD" muted={projected <= 0} />
          </div>

          {/* Footer — compact volume line when live, else the empty-state */}
          {hasData ? (
            <div className="text-[11.5px] leading-[150%] text-t3">
              <span className="tabular tabular-nums text-t2">{burn!.totals.calls.toLocaleString()}</span> call{burn!.totals.calls === 1 ? "" : "s"} ·{" "}
              <span className="tabular tabular-nums text-t2">{fmtTok(burn!.totals.tokensIn + burn!.totals.tokensOut)}</span> tok ·{" "}
              <span className="tabular tabular-nums text-t2">{providerCount}</span> provider{providerCount === 1 ? "" : "s"} · last {windowH}h
            </div>
          ) : (
            <div className="text-[11.5px] leading-[150%] text-t3">
              No LLM calls in the last {windowH}h — fire a chat or /skill to populate.
            </div>
          )}
        </>
      )}
    </div>
  );
}

function Stat({ value, label, muted }: { value: string; label: string; muted: boolean }) {
  return (
    <div className="flex flex-col gap-[5px]">
      <span className="mono text-[10px] uppercase tracking-[0.1em] text-t3">{label}</span>
      <span className={cn("tabular tabular-nums text-[22px] font-medium leading-none", muted ? "text-t3" : "text-t1")}>
        {value}
      </span>
    </div>
  );
}

// ── Helpers ────────────────────────────────────────────────────────────────

function fmtUsd(v: number): string {
  if (v === 0) return "$0";
  if (v < 0.01) return "<$0.01";
  if (v < 1)    return `$${v.toFixed(3)}`;
  if (v < 100)  return `$${v.toFixed(2)}`;
  return `$${Math.round(v).toLocaleString()}`;
}

function fmtTok(v: number): string {
  if (v < 1_000)     return v.toString();
  if (v < 1_000_000) return `${(v / 1_000).toFixed(1)}k`;
  return `${(v / 1_000_000).toFixed(2)}M`;
}

function formatWindowHours(window: { since: string; until: string }): number {
  try {
    const s = new Date(window.since).getTime();
    const u = new Date(window.until).getTime();
    if (!Number.isFinite(s) || !Number.isFinite(u)) return 24;
    return Math.max(1, Math.round((u - s) / 3_600_000));
  } catch { return 24; }
}

/** Multiplier to extrapolate current cost to end-of-day. Simple linear:
 *  24h / window_hours, capped at 8× so a 1-minute burst doesn't project
 *  absurd numbers. */
function projectionScaler(window: { since: string; until: string }): number {
  try {
    const s = new Date(window.since).getTime();
    const u = new Date(window.until).getTime();
    const windowH = (u - s) / 3_600_000;
    if (!Number.isFinite(windowH) || windowH <= 0) return 1;
    return Math.min(8, 24 / windowH);
  } catch { return 1; }
}
