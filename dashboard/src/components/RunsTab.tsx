import { useEffect, useState } from "react";
import { cn } from "../lib/cn";
import { api } from "../lib/api";
import type { AuditRun } from "../types";
import { Card } from "./ui/Card";
import { SegmentedToggle } from "./ui/SegmentedToggle";
import { StatusBadge, type StatusKind } from "./ui/StatusBadge";

const ROUTINES = ["hinotes", "sectornews", "memory-promote", "dealtracker", "recall"] as const;
type Routine = (typeof ROUTINES)[number];

/**
 * Runs tab — audit-log browser. Pick a routine, see the last N runs
 * with timestamp, run ID, status, duration. Click a row to expand
 * inputs/outputs/error.
 *
 * Restyled to the v5 Activity feed look (Activity · light@2x): time-stamped,
 * routine-grouped rows each carrying a RUN badge, a status dot, and
 * right-aligned metrics — wrapped in the shared Card/StatusBadge/Chip
 * primitives. Behaviour (fetch / state / expand) is preserved exactly; only
 * the presentation changed.
 */
export function RunsTab() {
  const [routine, setRoutine] = useState<Routine>("hinotes");
  const [runs, setRuns] = useState<AuditRun[]>([]);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setBusy(true);
    setError(null);
    api.auditRuns(routine, 50)
      .then((r) => { if (!cancelled) setRuns(r.runs); })
      .catch((e) => { if (!cancelled) setError(e instanceof Error ? e.message : "Unknown error"); })
      .finally(() => { if (!cancelled) setBusy(false); });
    return () => { cancelled = true; };
  }, [routine]);

  return (
    <div className="mx-auto w-full max-w-[1060px] px-[24px] py-[28px] text-t1">

      {/* No per-view H2 — the single "Activity" header lives in ActivityTab.
          This view is a bare routine selector + runs card under the toggle. */}

      {/* Routine selector — segmented filter (Activity All / Routine runs / …) */}
      <div className="mb-[20px]">
        <SegmentedToggle
          options={ROUTINES.map((r) => ({ value: r, label: r }))}
          value={routine}
          onChange={(v) => setRoutine(v as Routine)}
        />
      </div>

      {error && (
        <div className="mb-[18px] rounded-lg border border-red/40 bg-red/10 px-[12px] py-[8px] text-[12px] text-red">
          Bridge offline — {error}
        </div>
      )}

      {!busy && runs.length === 0 && !error && (
        <Card className="px-[16px] py-[14px] text-[12.5px] text-t3">
          No runs recorded for <span className="mono text-t1">{routine}</span>.
        </Card>
      )}

      {runs.length > 0 && (
        <Card padded={false}>
          {/* Group band — mirrors the Activity "TODAY · …" feed header */}
          <div className="flex items-baseline gap-[10px] border-b border-line bg-bg-1 px-[20px] py-[11px]">
            <span className="mono text-[10px] uppercase tracking-[0.14em] text-t2">{routine}</span>
            <span className="text-[11px] text-t4">{runs.length} run{runs.length === 1 ? "" : "s"}</span>
          </div>

          <ul>
            {(() => {
            // Scale each run-duration bar to the longest run currently in view.
            const maxDuration = Math.max(1, ...runs.map((x) => x.duration_ms ?? 0));
            return runs.map((r) => {
              const isOpen = expanded === r.run_id;
              const ts = r.ts ?? "—";
              const time = ts.slice(11, 16) || ts.slice(0, 16) || "—";
              const date = ts.length >= 10 ? ts.slice(5, 10) : "";
              return (
                <li key={`${r.ts}-${r.run_id}`} className="border-b border-line last:border-b-0">
                  <button
                    type="button"
                    onClick={() => setExpanded(isOpen ? null : (r.run_id ?? null))}
                    className={cn(
                      "flex w-full cursor-pointer items-center gap-[16px] px-[20px] py-[13px] text-left transition-colors",
                      isOpen ? "bg-bg-2/60" : "hover:bg-bg-2/40",
                    )}
                  >
                    {/* Time + RUN badge (Activity left cluster) */}
                    <span className="mono flex w-[46px] shrink-0 flex-col items-start leading-tight">
                      <span className="text-[12px] text-t3">{time}</span>
                      {date && <span className="text-[9.5px] text-t4">{date}</span>}
                    </span>
                    <span className="shrink-0 rounded-[5px] border border-line-2 px-[8px] text-[9px] font-bold uppercase tracking-[0.06em] text-t2">RUN</span>

                    {/* Title + run-id sub-line */}
                    <span className="flex min-w-0 flex-1 flex-col gap-[2px]">
                      <span className="truncate text-[14px] font-semibold leading-[18px] text-t1">{routine}</span>
                      <span className="mono truncate text-[11px] text-t3">{r.run_id ?? "—"}</span>
                    </span>

                    {/* Right-aligned metric + status dot (Activity trailing cluster) */}
                    <span className="flex shrink-0 items-center gap-[10px]">
                      <DurationBar ms={r.duration_ms} maxMs={maxDuration} status={r.status} />
                      <span className="mono tabular text-right text-[11.5px] text-t3">
                        {fmtDuration(r.duration_ms)}
                      </span>
                      <StatusBadge status={statusKind(r.status)} label={r.status} />
                    </span>
                  </button>

                  {isOpen && (
                    <div className="border-t border-line bg-bg-2/40 px-[20px] py-[14px] text-[11px]">
                      {r.error && (
                        <div className="mb-[10px] rounded-lg border border-red/40 bg-red/10 px-[10px] py-[6px] text-red">
                          {r.error}
                        </div>
                      )}
                      {r.inputs && Object.keys(r.inputs).length > 0 && (
                        <div className="mb-[10px]">
                          <div className="mono mb-[5px] text-[10px] uppercase tracking-[0.14em] text-t3">Inputs</div>
                          <pre className="overflow-x-auto whitespace-pre-wrap text-[11px] text-t2">{JSON.stringify(r.inputs, null, 2)}</pre>
                        </div>
                      )}
                      {r.outputs && Object.keys(r.outputs).length > 0 && (
                        <div>
                          <div className="mono mb-[5px] text-[10px] uppercase tracking-[0.14em] text-t3">Outputs</div>
                          <pre className="overflow-x-auto whitespace-pre-wrap text-[11px] text-t2">{JSON.stringify(r.outputs, null, 2)}</pre>
                        </div>
                      )}
                    </div>
                  )}
                </li>
              );
            });
            })()}
          </ul>
        </Card>
      )}
    </div>
  );
}

// ── Helpers ────────────────────────────────────────────────────────────────

// Map an audit-run status onto a StatusBadge kind: ok → green dot,
// error → red dot, skipped → muted (paused) dot.
function statusKind(status: AuditRun["status"]): StatusKind {
  switch (status) {
    case "ok":      return "ok";
    case "error":   return "error";
    case "skipped": return "paused";
    default:        return "paused";
  }
}

// Render duration_ms as a compact "Ns" / "Nm Ss" / "Nms" string for the
// right-aligned metric. Falls back to an em-dash when the field is absent.
function fmtDuration(ms: number | undefined): string {
  if (ms === undefined || ms === null) return "—";
  if (ms < 1000) return `${ms}ms`;
  const totalSec = Math.round(ms / 1000);
  if (totalSec < 60) return `${totalSec}s`;
  const m = Math.floor(totalSec / 60);
  const s = totalSec % 60;
  return `${m}m ${String(s).padStart(2, "0")}s`;
}

// Run-duration bar — a fixed-width track with a fill ∝ duration_ms relative to
// the longest run in view, colored by status (ok=sage / error=oxblood /
// skipped=muted). Pure presentation of the audit-run data already shown; no
// motion, so nothing to gate on prefers-reduced-motion.
function DurationBar({ ms, maxMs, status }: { ms: number | undefined; maxMs: number; status: AuditRun["status"] }) {
  if (ms === undefined || ms === null) return <span className="w-[180px] shrink-0" aria-hidden />;
  const pct = Math.max(2, Math.min(100, (ms / maxMs) * 100));
  const fill = status === "ok" ? "bg-green" : status === "error" ? "bg-red" : "bg-t4";
  return (
    <span className="h-[4px] w-[180px] shrink-0 overflow-hidden rounded-full bg-line" title={fmtDuration(ms)}>
      <span className={cn("block h-full rounded-full", fill)} style={{ width: `${pct}%` }} />
    </span>
  );
}
