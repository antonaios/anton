import type { PlanRow, PlansResponse, PlanUnit } from "../types";

interface Props {
  /** Loaded plans payload from `/api/usage/plans` (#14b). Null while in-flight. */
  plans?: PlansResponse | null;
  /** True during the first load. */
  loading?: boolean;
  /** Inline error message; rendered as a small accent chip in the header. */
  error?: string | null;
}

// Brand-tier label per provider key as it appears in /api/usage/plans.
// Sentence-case to match the Quiet-desk panel typography (Paper E6-0); the
// period window (e.g. "5h" / "daily") is appended from row.periodLabel.
// Falls through to the raw provider key for unknown providers.
const PROVIDER_LABEL: Record<string, string> = {
  anthropic: "Claude Max",
  openai:    "ChatGPT Plus",
  m27:       "MiniMax M2.7",
};

/**
 * v5 right-rail LLM Usage panel — consumes /api/usage/plans (#14b).
 *
 * Quiet-desk restyle (Paper E6-0): a mono uppercase rail label + one row per
 * plan, each a label/value baseline over a slim usage bar (structural-blue
 * fill on a bg-2 track — both themed tokens). Data wiring is unchanged —
 * including the B5 monthly $-credit row, which labels by its plan tier rather
 * than the rolling-plan brand. Loading skeleton on first load; error note in
 * the header. Matches the sibling Burn-rate / Week rail panels.
 */
export function LLMUsagePanel({ plans, loading, error }: Props) {
  return (
    <div className="flex flex-col gap-[14px]">
      {/* Section head — mono uppercase rail label, status note right-aligned */}
      <div className="flex items-baseline justify-between">
        <span className="mono text-[10px] uppercase tracking-[0.12em] text-t3">LLM usage</span>
        <div className="flex items-baseline gap-[8px] text-[10px]">
          {error && (
            <span className="mono uppercase tracking-[0.08em] text-red" title={error}>error</span>
          )}
          {loading && !plans && <span className="italic text-t4">loading…</span>}
          <span className="mono uppercase tracking-[0.08em] text-t3">Plan</span>
        </div>
      </div>

      {!plans && loading && (
        <div className="flex flex-col gap-[12px]">
          {[80, 100, 70].map((w, i) => (
            <div key={i} className="h-[16px] rounded-[3px] bg-bg-2"
              style={{ width: `${w}%`, opacity: 0.5, animation: "pulse 1.8s ease-in-out infinite", animationDelay: `${i * 0.15}s` }} />
          ))}
        </div>
      )}

      {plans && plans.plans.length === 0 && (
        <div className="text-[11.5px] leading-[150%] text-t3">No plans configured.</div>
      )}

      {plans && plans.plans.map((row, i) => (
        <PlanMeterRow key={row.provider + i} row={row} />
      ))}
    </div>
  );
}

function PlanMeterRow({ row }: { row: PlanRow }) {
  // #llm-routing-postjune15 B5 — a monthly $-credit row labels by its plan
  // tier ("Agent-SDK credit"), not the rolling-plan brand ("Claude Max");
  // the rolling rows keep their brand label.
  const isCredit = row.resetKind === "monthly";
  const brand = isCredit ? row.planTier : (PROVIDER_LABEL[row.provider] ?? row.provider);
  const pctFill = Math.min(100, Math.max(0, row.usedPct * 100));
  return (
    <div className="flex flex-col gap-[7px]">
      <div className="flex items-baseline justify-between gap-[8px]">
        <span className="truncate text-[12.5px] text-t1">
          {brand}
          {row.periodLabel && (
            <span className="mono ml-[5px] text-[10px] uppercase tracking-[0.06em] text-t3">{row.periodLabel}</span>
          )}
        </span>
        <span className="shrink-0 tabular text-[11.5px] text-t2">
          <span className="text-t1">{fmtUsage(row.used, row.unit)}</span>
          <span className="text-t3"> / {fmtCap(row.cap, row.unit)}</span>
        </span>
      </div>
      <div className="h-[5px] overflow-hidden rounded-full bg-bg-2">
        <div className="h-full rounded-full bg-ok-bright" style={{ width: `${pctFill}%` }} />
      </div>
    </div>
  );
}

// ── Helpers ────────────────────────────────────────────────────────────────

function fmtUsage(v: number, unit: PlanUnit): string {
  if (unit === "messages") return `${Math.round(v)}`;
  if (unit === "usd")      return `$${v.toFixed(2)}`;
  if (unit === "gbp")      return `£${v.toFixed(2)}`;
  return `${v.toFixed(2)}`;
}

function fmtCap(v: number, unit: PlanUnit): string {
  if (unit === "messages") return `${Math.round(v)}`;
  if (unit === "usd")      return `$${Math.round(v)}`;
  if (unit === "gbp")      return `£${Math.round(v)}`;
  return `${v}`;
}
