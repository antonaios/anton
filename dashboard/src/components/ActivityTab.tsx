import { useEffect, useState } from "react";
import { SegmentedToggle } from "./ui/SegmentedToggle";
import { SchedulerPanel } from "./SchedulerPanel";
import { RunsTab } from "./RunsTab";
import { VaultTab } from "./VaultTab";
import { api } from "../lib/api";

type ActivityView = "scheduler" | "runs" | "vault";

const VIEW_OPTIONS = [
  { value: "scheduler", label: "Scheduler" },
  { value: "runs", label: "Routine runs" },
  { value: "vault", label: "Vault changes" },
] as const;

// The same routine set the RunsTab browser knows about — summed here to derive
// today's run + error totals from the live audit log (no separate "all runs"
// endpoint exists, so we aggregate per-routine, exactly like RunsTab reads).
const STAT_ROUTINES = ["hinotes", "sectornews", "memory-promote", "dealtracker", "recall"] as const;

// A metric is `null` until its fetch resolves; null renders as a graceful "—"
// (the source is unreachable / the bridge predates it) rather than a fake 0.
interface ActivityStats {
  runsToday: number | null;
  vaultChanges: number | null;
  tokens: number | null;
  errors: number | null;
}

// "142k" / "1.4M" compact token count — real total, just abbreviated like Paper.
function fmtTokens(n: number): string {
  if (n < 1000) return String(n);
  if (n < 1_000_000) return `${Math.round(n / 100) / 10}k`.replace(".0k", "k");
  return `${Math.round(n / 100_000) / 10}M`.replace(".0M", "M");
}

// A metric value cell — renders the live number or a muted em-dash when absent.
function statValue(v: number | null, fmt: (n: number) => string = String): string {
  return v === null ? "—" : fmt(v);
}

/**
 * ActivityTab — the Activity surface (Phase 7).
 *
 * Composes the three Activity sources behind a single SegmentedToggle so only
 * one shows at a time. This "Activity" H2 is the ONLY header; the three panels
 * (SchedulerPanel / RunsTab / VaultTab) render headerless under the toggle — the
 * scheduler's live/offline status shows as a StatusBadge, not a competing H2.
 *
 * The integrator renders this inside a `<main className="overflow-y-auto
 * min-h-0">`, so this is a plain flex column (toggle bar + the active view) and
 * deliberately does NOT add its own scroll container.
 */
export function ActivityTab() {
  const [view, setView] = useState<ActivityView>("scheduler");
  const [stats, setStats] = useState<ActivityStats>({
    runsToday: null, vaultChanges: null, tokens: null, errors: null,
  });

  // Derive the 4-up strip from the SAME live sources the panels read — each
  // metric resolves independently and degrades to "—" on failure (never a fake
  // 0). Behaviour-free: pure read of telemetry the dashboard already exposes.
  useEffect(() => {
    let cancelled = false;

    // RUNS TODAY + ERRORS — aggregate today's audit runs across the known
    // routines (no all-routines endpoint; mirror RunsTab's per-routine reads).
    const todayIso = new Date().toISOString().slice(0, 10);
    Promise.allSettled(STAT_ROUTINES.map((r) => api.auditRuns(r, 50)))
      .then((results) => {
        if (cancelled) return;
        let any = false, runsToday = 0, errors = 0;
        for (const res of results) {
          if (res.status !== "fulfilled") continue;
          any = true;
          for (const run of res.value.runs) {
            if (!run.ts || run.ts.slice(0, 10) !== todayIso) continue;
            runsToday += 1;
            if (run.status === "error") errors += 1;
          }
        }
        // If every per-routine fetch failed, leave both metrics absent.
        setStats((s) => any ? { ...s, runsToday, errors } : s);
      });

    // VAULT CHANGES — count of files touched in the last 24h (same source the
    // Vault changes view feeds from).
    api.vaultPulse(24, 50)
      .then((r) => { if (!cancelled) setStats((s) => ({ ...s, vaultChanges: r.items.length })); })
      .catch(() => { /* leave "—" */ });

    // TOKENS — total in+out over the default 24h telemetry window.
    api.llmBurn()
      .then((r) => { if (!cancelled) setStats((s) => ({ ...s, tokens: r.totals.tokensIn + r.totals.tokensOut })); })
      .catch(() => { /* leave "—" */ });

    return () => { cancelled = true; };
  }, []);

  return (
    <div className="flex min-h-0 flex-col text-t1">

      {/* Activity header + source switch — Paper 7ZU-0 "Activity col" header block */}
      <div className="mx-auto flex w-full max-w-[1060px] flex-col gap-[18px] px-[28px] pt-[30px] pb-[18px]">

        <div className="flex flex-col gap-[7px]">
          <div className="flex items-baseline justify-between gap-[16px]">
            <h2 className="text-[22px] font-semibold leading-[120%] tracking-[-0.01em] text-t1">Activity</h2>
            <span className="mono text-[11px] leading-[14px] text-t3">routine runs + vault changes · live</span>
          </div>
          <p className="max-w-full text-[13px] leading-[150%] text-t2">
            Every routine run and vault change in one timeline — plus the cron schedule that drives them.
          </p>
        </div>

        {/* Stats strip — Paper 7ZU-0 "Stats" (88T-0): 4 equal-grow stat cards */}
        <div className="flex gap-[12px]">
          <StatCard label="RUNS TODAY" value={statValue(stats.runsToday)} />
          <StatCard label="VAULT CHANGES" value={statValue(stats.vaultChanges)} />
          <StatCard label="TOKENS" value={statValue(stats.tokens, fmtTokens)} />
          <StatCard label="ERRORS" value={statValue(stats.errors)} valueClass="text-red" />
        </div>

        <div className="flex items-center justify-between gap-[14px]">
          <SegmentedToggle
            options={VIEW_OPTIONS.map((o) => ({ value: o.value, label: o.label }))}
            value={view}
            onChange={(v) => setView(v as ActivityView)}
          />
          <div className="flex shrink-0 items-center gap-[7px]">
            <span className="h-[7px] w-[7px] shrink-0 rounded-[4px] bg-green" />
            <span className="text-[12px] leading-[16px] text-t2">Live</span>
          </div>
        </div>
      </div>

      {/* Active view — each brings its own header (fine under the toggle) */}
      {view === "scheduler" && <SchedulerPanel />}
      {view === "runs" && <RunsTab />}
      {view === "vault" && <VaultTab />}
    </div>
  );
}

// One stat card — Paper 88T-0: a 9px mono uppercase label over a 19px/600 value.
function StatCard({ label, value, valueClass }: { label: string; value: string; valueClass?: string }) {
  return (
    <div className="flex grow basis-0 flex-col gap-[4px] rounded-[10px] border border-line bg-bg-1 px-[15px] py-[13px]">
      <span className="mono text-[9px] uppercase leading-[12px] tracking-[0.06em] text-t3">{label}</span>
      <span className={`text-[19px] font-semibold leading-[24px] text-t1${valueClass ? ` ${valueClass}` : ""}`}>{value}</span>
    </div>
  );
}
