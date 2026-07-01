import type { BudgetIncident, BudgetScope } from "../types";

interface Props {
  /** Incidents currently blocking the gate (status: open | acknowledged_paused).
   *  When non-empty, this banner REPLACES the BurnRatePanel in the right rail. */
  incidents: BudgetIncident[];
  /** Open the ack modal for a specific incident. */
  onAckClick: (incident: BudgetIncident) => void;
}

/**
 * #57 dashboard surface — red BLOCKED banner that swaps in for the BurnRate
 * panel when one or more budget incidents are open.
 *
 * Per-incident row: formatted scope · current% / hard% · cap · short id +
 * Acknowledge button. Multi-incident: stack one row per incident (rare —
 * typically just 1, but the API returns a list and we render whatever lands).
 *
 * Layout mirrors `BurnRatePanel` (pb/mb-18 + border-b border-line) so the
 * right-rail spacing is preserved across the swap.
 */
export function BudgetBlockedBanner({ incidents, onAckClick }: Props) {
  return (
    <div className="rounded-xl bg-bg-1 shadow-card p-[16px]">
      {/* Section head — red header bar matches the gate's BLOCKED semantics. */}
      <div className="border border-red bg-red/10 px-[12px] py-[8px] mb-[10px]">
        <div className="flex items-baseline justify-between">
          <span className="text-[10.5px] tracking-[0.14em] uppercase text-red font-medium">
            BUDGET GATE · LLM CALLS BLOCKED
          </span>
          <span className="text-[10px] tracking-[0.06em] text-red">
            {incidents.length === 1 ? "1 INCIDENT" : `${incidents.length} INCIDENTS`}
          </span>
        </div>
      </div>

      {/* Per-incident rows */}
      <div className="flex flex-col gap-[10px]">
        {incidents.map((inc) => (
          <IncidentRow key={inc.id} incident={inc} onAck={() => onAckClick(inc)} />
        ))}
      </div>
    </div>
  );
}

function IncidentRow({ incident, onAck }: { incident: BudgetIncident; onAck: () => void }) {
  const paused = incident.status === "acknowledged_paused";
  return (
    <div className="border border-red/40 bg-bg-1 px-[12px] py-[10px]">
      <div className="flex items-baseline justify-between mb-[6px]">
        <span className="text-[11.5px] text-t1" title={fullScopeLabel(incident.scope)}>
          {scopeLabel(incident.scope)}
        </span>
        <span className="text-[10px] tracking-[0.08em] uppercase text-red">
          {paused ? "PAUSED · ACKED" : "OPEN"}
        </span>
      </div>

      <div className="flex items-baseline justify-between text-[11px] tabular">
        <span className="text-t2">
          <span className="text-red font-medium">{fmtPct(incident.currentPct)}</span>
          <span className="text-t3"> / </span>
          <span>{fmtPct(incident.hardPct)}</span>
          <span className="text-t3"> · cap </span>
          <span className="text-t1">{fmtUsd(incident.capUsd)}</span>
        </span>
        <span className="text-t3 tracking-[0.06em]" title={`incident ${incident.id}`}>
          #{incident.id.slice(-8)}
        </span>
      </div>

      <div className="mt-[8px] flex items-center justify-between">
        <span className="text-[10px] tracking-[0.06em] text-t3">
          spend {fmtUsd(incident.currentSpendUsd)}
        </span>
        <button
          type="button"
          onClick={onAck}
          className="px-[10px] py-[4px] border border-red text-[10.5px] tracking-[0.1em] uppercase text-red hover:bg-red hover:text-bg transition-colors"
        >
          {paused ? "Re-ack" : "Acknowledge →"}
        </button>
      </div>
    </div>
  );
}

// ── Formatting helpers ─────────────────────────────────────────────────────

/** Compact scope label for the row title — matches the format the gate uses
 *  in its refusal message ("global", "anthropic/claude-opus-4-7",
 *  "project/DemoTarget"). */
function scopeLabel(scope: BudgetScope): string {
  if (scope.kind === "global") return "GLOBAL";
  return `${scope.a ?? "?"} / ${scope.b ?? "?"}`;
}

/** Long-form for the title attribute — gives the kind context on hover. */
function fullScopeLabel(scope: BudgetScope): string {
  if (scope.kind === "global") return "global scope";
  if (scope.kind === "provider")  return `provider · ${scope.a}/${scope.b}`;
  if (scope.kind === "workspace") return `workspace · ${scope.a}/${scope.b}`;
  return JSON.stringify(scope);
}

function fmtPct(pct: number): string {
  if (!Number.isFinite(pct)) return "—";
  if (pct >= 1000) return `${Math.round(pct)}%`;
  if (pct >= 100)  return `${pct.toFixed(1)}%`;
  return `${pct.toFixed(1)}%`;
}

function fmtUsd(v: number): string {
  if (v === 0) return "$0";
  if (v < 0.01) return "<$0.01";
  if (v < 1)    return `$${v.toFixed(3)}`;
  if (v < 100)  return `$${v.toFixed(2)}`;
  return `$${Math.round(v).toLocaleString()}`;
}
