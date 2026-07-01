import { useEffect, useRef, useState } from "react";
import { ChevronDown, Pause, Play, Zap } from "lucide-react";
import { api, ApiError } from "../lib/api";
import { cn } from "../lib/cn";
import { useMediaQuery } from "../lib/useMediaQuery";
import type { SchedulerJob, SchedulerRunRecord } from "../types";
import { Card } from "./ui/Card";
import { Chip } from "./ui/Chip";
import { IconButton } from "./ui/IconButton";
import { StatusBadge, type StatusKind } from "./ui/StatusBadge";

const POLL_MS = 30_000;

/** True when the OS "reduce motion" preference is set — gates the live
 *  countdown tick, the amber soonest-pulse and the green fire-flash down to the
 *  static "next MMM D · HH:MM" the panel already renders. Mirrors the pattern in
 *  ChatCanvas (`usePrefersReducedMotion`). */
function usePrefersReducedMotion(): boolean {
  return useMediaQuery("(prefers-reduced-motion: reduce)");
}

/**
 * SchedulerPanel — the bridge-embedded APScheduler surface (Activity · Phase 7).
 *
 * Polls GET /api/scheduler/jobs every 30s. `running=false` means the scheduler
 * is OFFLINE/paused — we surface that as a calm amber banner and STILL list the
 * jobs so the operator can see what WOULD run. There is no per-job `paused`
 * boolean on the wire, so paused-state is derived purely from `next_run`:
 *   - `next_run` set   ⇒ ACTIVE   — show the next-fire time + offer Pause.
 *   - `next_run` null  ⇒ INACTIVE — show an amber "inactive" chip + offer Resume.
 *
 * Operator actions are audited automation controls, so "Run now" and "Pause"
 * both window.confirm() before firing; "Resume" doesn't (re-enabling a job is
 * non-destructive). The pause result's `durable` flag is surfaced (persisted vs
 * live-only) and any action error is shown inline (never crashes the panel).
 * A chevron expands an inline history drawer that lazy-fetches the last runs.
 *
 * Token-only so it flips between the LIGHT teal and DARK navy+gold themes.
 */
export function SchedulerPanel() {
  const [jobs, setJobs] = useState<SchedulerJob[]>([]);
  const [running, setRunning] = useState<boolean>(true);
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState(false);
  // True while a "Pause all" fan-out is in flight — disables the header button.
  const [busyAll, setBusyAll] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Which job's history drawer is open (null = none). The drawer lazy-fetches.
  const [openId, setOpenId] = useState<string | null>(null);
  // Per-job inline action error keyed by job id; cleared on next action / fetch.
  const [rowErrors, setRowErrors] = useState<Record<string, string>>({});
  // Per-job action-result note (e.g. "paused · persisted"); cleared on re-fetch.
  const [rowNotes, setRowNotes] = useState<Record<string, string>>({});
  // Ids currently mid-action — disables their controls.
  const [actioning, setActioning] = useState<Set<string>>(new Set());

  // ── Live countdown (interaction-only; no extra fetch) ─────────────────────
  // A per-second client tick recomputes each active job's "in 1m 10s" delta from
  // the EXISTING next_run. Reduced-motion skips the tick entirely (the row falls
  // back to the static "next MMM D · HH:MM" the panel already renders).
  const reduceMotion = usePrefersReducedMotion();
  const [now, setNow] = useState(() => Date.now());

  // When a job's countdown crosses zero we (a) flash it green for ~1.1s and
  // (b) reschedule it CLIENT-SIDE by bumping its effective next_run forward one
  // cadence — so the row keeps counting down to its next fire without an extra
  // fetch (the 30s poll later replaces this with the server's fresh next_run).
  //   firingIds      — ids mid-flash (drives the green fire-flash + "firing…").
  //   nextRunOverride — id → ISO next_run we've rolled forward locally.
  const [firingIds, setFiringIds] = useState<Set<string>>(new Set());
  const [nextRunOverride, setNextRunOverride] = useState<Record<string, string>>({});
  // Per-id timeout handles for the green-flash auto-clear (cleaned up on unmount).
  const flashTimers = useRef<Record<string, number>>({});
  // Ids fired this snapshot — prevents a job with NO derivable cadence (so no
  // roll-forward) from re-firing every tick once its next_run is in the past.
  // Reset whenever a fresh server snapshot lands (see the override-clear effect).
  const firedThisSnapshot = useRef<Set<string>>(new Set());

  useEffect(() => {
    if (reduceMotion) return;                  // no live tick under reduced-motion
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, [reduceMotion]);

  // Clear local reschedule overrides whenever a fresh server snapshot lands
  // (jobs identity changes on every successful fetch) — the server's next_run is
  // authoritative; our client roll-forward is only a between-poll stopgap.
  useEffect(() => { setNextRunOverride({}); firedThisSnapshot.current = new Set(); }, [jobs]);

  // Tear down any pending flash timers on unmount.
  useEffect(() => () => {
    Object.values(flashTimers.current).forEach((t) => window.clearTimeout(t));
  }, []);

  // The effective next_run for a job — a local roll-forward if we've rescheduled
  // it past a fire, else the server value.
  const effectiveNextRun = (job: SchedulerJob): string | null | undefined =>
    nextRunOverride[job.id] ?? job.next_run;

  // Fire a job locally: flash green, then roll its next_run forward one cadence
  // derived from the cron trigger (so it keeps counting down). No fetch.
  const fireLocally = (job: SchedulerJob, fromIso: string) => {
    setFiringIds((s) => { const n = new Set(s); n.add(job.id); return n; });
    if (flashTimers.current[job.id]) window.clearTimeout(flashTimers.current[job.id]);
    flashTimers.current[job.id] = window.setTimeout(() => {
      setFiringIds((s) => { const n = new Set(s); n.delete(job.id); return n; });
      delete flashTimers.current[job.id];
    }, 1100);

    const cadenceSec = cadenceSecondsFromTrigger(job.trigger);
    if (cadenceSec != null) {
      // Roll next_run forward past `now` (a job idle for several cadences could
      // be many periods behind) so the countdown resumes toward a future fire.
      let nextMs = new Date(fromIso).getTime() + cadenceSec * 1000;
      const floor = Date.now();
      while (nextMs <= floor) nextMs += cadenceSec * 1000;
      setNextRunOverride((m) => ({ ...m, [job.id]: new Date(nextMs).toISOString() }));
    } else {
      // No derivable cadence ⇒ flag it fired so we don't re-flash every tick;
      // it reads "firing…" until the next 30s poll delivers the server's
      // authoritative next_run (which clears this flag via the snapshot reset).
      firedThisSnapshot.current.add(job.id);
    }
  };

  // Detect zero-crossings on each tick: any active job whose effective next_run
  // is now in the past (and isn't already flashing) fires locally. Runs in an
  // effect — never during render — so the setState chain is legal.
  useEffect(() => {
    if (reduceMotion) return;
    for (const job of jobs) {
      const iso = effectiveNextRun(job);
      if (!iso) continue;                       // inactive (no next_run) → nothing to fire
      const t = new Date(iso).getTime();
      if (Number.isNaN(t)) continue;
      // Skip a no-cadence job we've already fired this snapshot (it'd otherwise
      // re-flash every second until the poll refreshes). A rescheduled job has a
      // future override, so it isn't past here until its next cadence elapses.
      if (firedThisSnapshot.current.has(job.id)) continue;
      if (t <= now && !firingIds.has(job.id)) fireLocally(job, iso);
    }
    // `now` drives the check; jobs/overrides/firing are read fresh each tick.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [now]);

  // ── Fetch + 30s poll (cancelled-flag idiom, mirrors RunsTab) ──
  const load = (silent = false) => {
    if (!silent) setBusy(true);
    setError(null);
    return api.schedulerJobs()
      .then((r) => { setJobs(r.jobs); setRunning(r.running); setRowNotes({}); })
      .catch((e) => setError(e instanceof ApiError ? e.message : e instanceof Error ? e.message : "Unknown error"))
      .finally(() => { setBusy(false); setLoaded(true); });
  };

  useEffect(() => {
    let cancelled = false;
    setBusy(true);
    setError(null);
    api.schedulerJobs()
      .then((r) => { if (!cancelled) { setJobs(r.jobs); setRunning(r.running); } })
      .catch((e) => { if (!cancelled) setError(e instanceof ApiError ? e.message : e instanceof Error ? e.message : "Unknown error"); })
      .finally(() => { if (!cancelled) { setBusy(false); setLoaded(true); } });

    const id = window.setInterval(() => {
      api.schedulerJobs()
        .then((r) => { if (!cancelled) { setJobs(r.jobs); setRunning(r.running); } })
        .catch(() => { /* keep the last snapshot on a transient poll failure */ });
    }, POLL_MS);

    return () => { cancelled = true; window.clearInterval(id); };
  }, []);

  // ── Action helpers ──
  const markActioning = (id: string, on: boolean) =>
    setActioning((s) => { const n = new Set(s); if (on) n.add(id); else n.delete(id); return n; });

  const setRowError = (id: string, msg: string | null) =>
    setRowErrors((m) => {
      if (msg === null) { const { [id]: _drop, ...rest } = m; void _drop; return rest; }
      return { ...m, [id]: msg };
    });

  const actionError = (e: unknown, prefix: string): string => {
    if (e instanceof ApiError) return `${prefix} — ${e.message}`;
    if (e instanceof Error) return `${prefix} — ${e.message}`;
    return prefix;
  };

  const handleRunNow = async (job: SchedulerJob) => {
    if (!window.confirm(`Run "${job.name}" now?`)) return;
    markActioning(job.id, true);
    setRowError(job.id, null);
    try {
      const r = await api.runSchedulerJobNow(job.id);
      await load(true);
      // Set the result note AFTER load() — which clears rowNotes on re-fetch —
      // so the queued/durable confirmation is actually visible (codex review).
      setRowNotes((m) => ({ ...m, [job.id]: `queued · ${r.run_id}` }));
    } catch (e) {
      setRowError(job.id, actionError(e, "Run failed"));
    } finally {
      markActioning(job.id, false);
    }
  };

  const handlePause = async (job: SchedulerJob) => {
    if (!window.confirm(`Pause "${job.name}"? It won't fire until resumed.`)) return;
    markActioning(job.id, true);
    setRowError(job.id, null);
    try {
      const r = await api.pauseSchedulerJob(job.id);
      await load(true);
      setRowNotes((m) => ({ ...m, [job.id]: r.durable ? "paused · persisted" : "paused · live-only" }));
    } catch (e) {
      setRowError(job.id, actionError(e, "Pause failed"));
    } finally {
      markActioning(job.id, false);
    }
  };

  const handleResume = async (job: SchedulerJob) => {
    markActioning(job.id, true);
    setRowError(job.id, null);
    try {
      const r = await api.resumeSchedulerJob(job.id);
      await load(true);
      setRowNotes((m) => ({ ...m, [job.id]: r.durable ? "resumed · persisted" : "resumed · live-only" }));
    } catch (e) {
      setRowError(job.id, actionError(e, "Resume failed"));
    } finally {
      markActioning(job.id, false);
    }
  };

  // ── Pause all (Paper 7ZU-0 header action) ──
  // Confirms ONCE, then pauses every currently-active job through the SAME
  // per-job endpoint (api.pauseSchedulerJob → CSRF-bearing request()). Already-
  // inactive jobs are skipped. Errors are surfaced per-row (mirrors handlePause)
  // and a single re-fetch at the end syncs the list. No bulk endpoint exists, so
  // this is a fan-out over the existing audited per-job control.
  const handlePauseAll = async () => {
    const targets = jobs.filter((j) => j.next_run != null && j.next_run !== "");
    if (targets.length === 0) return;
    if (!window.confirm(`Pause all ${targets.length} active job${targets.length === 1 ? "" : "s"}? They won't fire until resumed.`)) return;
    setBusyAll(true);
    targets.forEach((j) => { markActioning(j.id, true); setRowError(j.id, null); });
    const results = await Promise.allSettled(
      targets.map((j) => api.pauseSchedulerJob(j.id).then((r) => ({ id: j.id, durable: r.durable }))),
    );
    await load(true);
    setRowNotes((m) => {
      const next = { ...m };
      results.forEach((res, i) => {
        const j = targets[i];
        if (res.status === "fulfilled") next[j.id] = res.value.durable ? "paused · persisted" : "paused · live-only";
      });
      return next;
    });
    results.forEach((res, i) => {
      const j = targets[i];
      if (res.status === "rejected") setRowError(j.id, actionError(res.reason, "Pause failed"));
      markActioning(j.id, false);
    });
    setBusyAll(false);
  };

  // The soonest-firing ACTIVE job — its countdown pulses amber (interaction
  // only; null under reduced-motion so nothing pulses). Uses the effective
  // next_run so a locally-rescheduled job is ranked by its rolled-forward time.
  const soonestId = (() => {
    if (reduceMotion) return null;
    let best: { id: string; t: number } | null = null;
    for (const job of jobs) {
      const iso = effectiveNextRun(job);
      if (!iso) continue;
      const t = new Date(iso).getTime();
      if (Number.isNaN(t)) continue;
      if (!best || t < best.t) best = { id: job.id, t };
    }
    return best?.id ?? null;
  })();

  // The soonest ACTIVE job's name for the group band's "· next: {name}" hint.
  // Falls back to the first active job (reduced-motion nulls soonestId, so the
  // band still reads "next: …" for the operator). Null when nothing is active.
  const nextJobName = (() => {
    if (!running) return null;                                 // nothing fires while offline
    const active = jobs.filter((j) => j.next_run != null && j.next_run !== "");
    if (active.length === 0) return null;
    return active.reduce((a, b) => ((a.next_run ?? "") <= (b.next_run ?? "") ? a : b)).name ?? null;
  })();

  return (
    <div className="mx-auto w-full max-w-[1060px] px-[24px] py-[28px] text-t1">

      {/* No per-view H2 — the single "Activity" header lives in ActivityTab. The
          live/offline StatusBadge is relocated into the Jobs group band below;
          only the offline banner survives up here as a calm amber notice. */}

      {/* Scheduler-offline banner — calm amber; jobs still list below */}
      {loaded && !running && (
        <div className="mb-[18px] rounded-lg border border-amber/40 bg-amber/10 px-[12px] py-[8px] text-[12px] text-amber">
          Scheduler offline — these jobs are registered but nothing will fire until the scheduler is running.
        </div>
      )}

      {/* Fetch error (the read itself failed) */}
      {error && (
        <div className="mb-[18px] rounded-lg border border-red/40 bg-red/10 px-[12px] py-[8px] text-[12px] text-red">
          Bridge offline — {error}
        </div>
      )}

      {/* Empty state */}
      {loaded && !error && jobs.length === 0 && (
        <Card className="flex items-center justify-between gap-[10px] px-[16px] py-[14px] text-[12.5px] text-t3">
          <span>No jobs registered with the scheduler.</span>
          <StatusBadge status={running ? "live" : "paused"} label={running ? "live" : "offline"} />
        </Card>
      )}

      {/* Job list */}
      {jobs.length > 0 && (
        <Card padded={false}>
          {/* Group band — mirrors the Activity feed header. Pause-all on the
              right (Paper 7ZU-0 "SCHEDULED" header action). */}
          <div className="flex items-center justify-between gap-[10px] border-b border-line bg-bg-1 px-[20px] py-[11px]">
            <div className="flex items-baseline gap-[10px]">
              <span className="mono text-[10px] uppercase tracking-[0.14em] text-t2">Jobs</span>
              <span className="text-[11px] text-t4">
                {jobs.length} job{jobs.length === 1 ? "" : "s"}
                {nextJobName && <> · next: {nextJobName}</>}
              </span>
            </div>
            <div className="flex shrink-0 items-center gap-[12px]">
              {/* Relocated live/offline status (was the removed H2's StatusBadge) */}
              <StatusBadge
                status={running ? "live" : "paused"}
                label={busy && !loaded ? "loading…" : running ? "live" : "offline"}
              />
              <button
                type="button"
                onClick={() => void handlePauseAll()}
                disabled={busyAll || jobs.every((j) => j.next_run == null || j.next_run === "")}
                className="flex shrink-0 items-center gap-[7px] rounded-[7px] border border-line-2 px-[11px] py-[5px] text-[12px] text-t2 transition-colors hover:text-t1 disabled:cursor-not-allowed disabled:opacity-50"
              >
                <Pause size={12} />
                {busyAll ? "Pausing…" : "Pause all"}
              </button>
            </div>
          </div>

          <ul>
            {jobs.map((job) => {
              // Active-vs-inactive stays derived purely from the SERVER next_run
              // (drives the Pause/Resume control + status column) — unchanged.
              const active = job.next_run != null && job.next_run !== "";
              const isOpen = openId === job.id;
              const isActioning = actioning.has(job.id);
              const rowError = rowErrors[job.id];
              const rowNote = rowNotes[job.id];
              // Live-countdown extras (reduced-motion: all inert — soonestId is
              // null, firingIds stays empty, and the countdown falls back to the
              // static next-run time the panel already rendered).
              const isSoon = soonestId === job.id;
              const isFiring = firingIds.has(job.id);
              const countdown = active && !reduceMotion
                ? fmtCountdown(effectiveNextRun(job), now)
                : null;
              return (
                <li key={job.id} className="border-b border-line last:border-b-0">
                  {/* Green fire-flash on a zero-crossing — a CSS-transitioned tint
                      that fades back out (no new @keyframes). Reduced-motion never
                      sets isFiring, so the row stays flat. */}
                  <div
                    className={cn(
                      "flex items-center gap-[16px] px-[20px] py-[13px] transition-colors duration-700",
                      isFiring ? "bg-green/15" : "bg-transparent",
                    )}
                  >

                    {/* Leading status dot (Paper): sage when active / faint when
                        inactive; the soonest active job's dot pulses amber under
                        motion. Sits beside the chevron (which opens history — an
                        additive feature we keep). */}
                    <span
                      className={cn(
                        "h-[8px] w-[8px] shrink-0 rounded-full",
                        isSoon ? "bg-amber animate-pulse" : active ? "bg-green" : "bg-t4",
                      )}
                      aria-hidden
                    />

                    {/* Expand chevron (history drawer) */}
                    <IconButton
                      icon={ChevronDown}
                      label={isOpen ? "Hide run history" : "Show run history"}
                      onClick={() => setOpenId(isOpen ? null : job.id)}
                      active={isOpen}
                      size={15}
                      title={isOpen ? "Hide run history" : "Show run history"}
                    />

                    {/* Name + mono id sub-line */}
                    <span className="flex min-w-0 flex-1 flex-col gap-[2px]">
                      <span className="truncate text-[14px] font-semibold leading-[18px] text-t1">{job.name}</span>
                      <span className="mono truncate text-[11px] text-t3" title={job.id}>{job.id}</span>
                    </span>

                    {/* Trigger + next-run / inactive cluster. The countdown is a
                        live "in 1m 10s" ticking off the existing next_run; the
                        SOONEST active job pulses amber, a firing one reads
                        "firing…". Under reduced-motion `countdown` is null and we
                        fall back to the static "next MMM D · HH:MM". */}
                    <span className="hidden min-w-0 flex-col items-end gap-[3px] sm:flex">
                      <span className="mono truncate text-[11px] text-t3" title={job.trigger}>{job.trigger}</span>
                      {active ? (
                        countdown != null ? (
                          <span
                            className={cn(
                              "mono tabular text-[11px]",
                              isFiring ? "text-green" : isSoon ? "text-amber animate-pulse" : "text-t2",
                            )}
                            title={`next ${fmtNextRun(job.next_run)}`}
                          >
                            {isFiring ? "firing…" : countdown}
                          </span>
                        ) : (
                          <span className="mono tabular text-[11px] text-t2">
                            next {fmtNextRun(job.next_run)}
                          </span>
                        )
                      ) : (
                        <Chip label="inactive" variant="amber" className="px-[7px] py-[1px] text-[9.5px] uppercase tracking-[0.08em]" />
                      )}
                    </span>

                    {/* Status column — Paper 7ZU-0: ok (sage dot) | paused (faint dot) */}
                    <span className="hidden w-[64px] shrink-0 items-center gap-[6px] sm:flex">
                      <span className={`h-[6px] w-[6px] shrink-0 rounded-[50%] ${active ? "bg-green" : "bg-t4"}`} />
                      <span className="text-[11.5px] leading-[14px] text-t2">{active ? "ok" : "paused"}</span>
                    </span>

                    {/* Action buttons — Run now always; Pause (active) | Resume (inactive) */}
                    <span className="flex shrink-0 items-center gap-[6px]">
                      <IconButton
                        icon={Zap}
                        label={`Run "${job.name}" now`}
                        onClick={() => void handleRunNow(job)}
                        variant="bordered"
                        size={14}
                        disabled={isActioning}
                        title="Run now"
                      />
                      {active ? (
                        <IconButton
                          icon={Pause}
                          label={`Pause "${job.name}"`}
                          onClick={() => void handlePause(job)}
                          variant="bordered"
                          size={14}
                          disabled={isActioning}
                          title="Pause"
                        />
                      ) : (
                        <IconButton
                          icon={Play}
                          label={`Resume "${job.name}"`}
                          onClick={() => void handleResume(job)}
                          variant="bordered"
                          size={14}
                          disabled={isActioning}
                          title="Resume"
                        />
                      )}
                    </span>
                  </div>

                  {/* Inline result note (durable flag) + action error */}
                  {(rowNote || rowError) && (
                    <div className="flex flex-col gap-[6px] px-[20px] pb-[12px]">
                      {rowNote && (
                        <span className="mono text-[10.5px] text-t3">{rowNote}</span>
                      )}
                      {rowError && (
                        <div className="flex items-baseline justify-between gap-[8px] rounded-lg border border-red/40 bg-red/10 px-[10px] py-[5px] text-[11px] text-red">
                          <span className="flex-1">{rowError}</span>
                          <button type="button" onClick={() => setRowError(job.id, null)} className="text-red hover:brightness-110">×</button>
                        </div>
                      )}
                    </div>
                  )}

                  {/* History drawer — lazy-fetches on open */}
                  {isOpen && <HistoryDrawer jobId={job.id} />}
                </li>
              );
            })}
          </ul>
        </Card>
      )}
    </div>
  );
}

// ── History drawer ──────────────────────────────────────────────────────────

/**
 * Inline run-history drawer. Lazy-fetches api.schedulerJobHistory on mount
 * (i.e. when the parent row expands) and renders the last runs latest-first:
 * time, a status dot, duration and any error_class.
 */
function HistoryDrawer({ jobId }: { jobId: string }) {
  const [runs, setRuns] = useState<SchedulerRunRecord[]>([]);
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setBusy(true);
    setError(null);
    api.schedulerJobHistory(jobId, 10)
      .then((r) => { if (!cancelled) setRuns(r.runs); })
      .catch((e) => { if (!cancelled) setError(e instanceof ApiError ? e.message : e instanceof Error ? e.message : "Unknown error"); })
      .finally(() => { if (!cancelled) setBusy(false); });
    return () => { cancelled = true; };
  }, [jobId]);

  return (
    <div className="border-t border-line bg-bg-2/40 px-[20px] py-[14px]">
      <div className="mono mb-[8px] text-[10px] uppercase tracking-[0.14em] text-t3">Recent runs</div>
      {busy ? (
        <div className="text-[11px] italic text-t3">loading history…</div>
      ) : error ? (
        <div className="text-[11px] text-red">Couldn't load history — {error}</div>
      ) : runs.length === 0 ? (
        <div className="text-[11px] italic text-t3">No runs recorded yet.</div>
      ) : (
        <ul className="flex flex-col gap-[2px]">
          {runs.map((r, i) => (
            <li
              key={`${r.ts}-${r.run_id}-${i}`}
              className="flex items-center gap-[14px] rounded-lg px-[8px] py-[6px] text-[11px] hover:bg-bg-1/60"
            >
              <span className="mono tabular w-[112px] shrink-0 text-t3">{fmtTs(r.ts)}</span>
              <StatusBadge status={statusKind(r.status)} label={r.status} className="w-[78px] shrink-0" />
              <span className="mono tabular w-[64px] shrink-0 text-right text-t3">{fmtDuration(r.duration_ms)}</span>
              <span className="mono min-w-0 flex-1 truncate text-t4" title={r.error ?? r.error_class ?? ""}>
                {r.error_class ?? ""}
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

// ── Helpers ──────────────────────────────────────────────────────────────────

// Map a scheduler-run status string onto a StatusBadge kind. The bridge writes
// free-form status strings; treat "ok"/"success" as green and anything that
// reads like a failure as red, everything else muted.
function statusKind(status: string): StatusKind {
  const s = status.toLowerCase();
  if (s === "ok" || s === "success" || s === "succeeded" || s === "completed") return "ok";
  if (s === "error" || s === "failed" || s === "failure") return "error";
  if (s === "running" || s === "queued" || s === "started") return "running";
  return "paused";
}

// Live countdown to the next fire: "in 1m 10s" / "in 2h 05m" / "in 8s".
// Returns "firing…" once the target is reached (the row's local reschedule then
// rolls next_run forward). Guards null/empty/unparseable → null (caller falls
// back to the static next-run time). Interaction-only — pure read of next_run.
function fmtCountdown(iso: string | null | undefined, nowMs: number): string | null {
  if (!iso) return null;
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return null;
  const sec = Math.round((t - nowMs) / 1000);
  if (sec <= 0) return "firing…";
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  if (h > 0) return `in ${h}h ${String(m).padStart(2, "0")}m`;
  if (m > 0) return `in ${m}m ${String(s).padStart(2, "0")}s`;
  return `in ${s}s`;
}

// Derive a cadence (seconds) from a cron `trigger` string so a fired job can be
// rolled forward client-side without an extra fetch. Handles the common forms
// the bridge registers: "*/N * * * *" (every N min), "0 */N * * *" (every N h),
// a daily "M H * * *" (86400s) and an interval "interval[…seconds=N…]". Returns
// null when no cadence is derivable — the row then reads "firing…" until the
// next 30s poll delivers the server's authoritative next_run.
function cadenceSecondsFromTrigger(trigger: string | null | undefined): number | null {
  if (!trigger) return null;
  const t = trigger.trim();

  // APScheduler IntervalTrigger string, e.g. "interval[0:15:00]" or "…seconds=900…".
  const sec = /seconds?=(\d+)/i.exec(t);
  if (sec) return Number(sec[1]) || null;
  // The bracket is a timedelta string — an optional "N day(s), " then H:M:S
  // (e.g. "interval[0:15:00]" = 900s; "interval[1 day, 0:15:00]" = 90900s).
  const td = /\[(?:(\d+)\s*days?,\s*)?(\d+):(\d{2}):(\d{2})\]/.exec(t);
  if (td) {
    const total = Number(td[1] ?? 0) * 86400 + Number(td[2]) * 3600 + Number(td[3]) * 60 + Number(td[4]);
    return total > 0 ? total : null;
  }

  // 5-field cron: "min hour dom mon dow".
  const f = t.split(/\s+/);
  if (f.length >= 5) {
    const [min, hour] = f;
    const stepMin = /^\*\/(\d+)$/.exec(min);
    if (stepMin && hour === "*") return Number(stepMin[1]) * 60 || null;     // every N minutes
    const stepHour = /^\*\/(\d+)$/.exec(hour);
    if (stepHour) return Number(stepHour[1]) * 3600 || null;                 // every N hours
    // Fixed minute + fixed hour (e.g. "0 7 * * *") ⇒ daily.
    if (/^\d+$/.test(min) && /^\d+$/.test(hour)) return 86400;
    // Fixed minute, every hour ("0 * * * *") ⇒ hourly.
    if (/^\d+$/.test(min) && hour === "*") return 3600;
  }
  return null;
}

// Format an ISO next_run into "MMM D · HH:MM" (date + 24h time). Guards
// null/empty/unparseable — we never invent a fire time.
function fmtNextRun(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const date = d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
  const time = d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", hour12: false });
  return `${date} · ${time}`;
}

// Format a history-run ISO timestamp into "MM-DD HH:MM". Guards null/unparseable.
function fmtTs(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const date = d.toLocaleDateString(undefined, { month: "2-digit", day: "2-digit" });
  const time = d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", hour12: false });
  return `${date} ${time}`;
}

// Render duration_ms as a compact "Nms" / "Ns" / "Nm Ss" string. Falls back to
// an em-dash when the field is absent (mirrors RunsTab's fmtDuration).
function fmtDuration(ms: number | null | undefined): string {
  if (ms === undefined || ms === null) return "—";
  if (ms < 1000) return `${ms}ms`;
  const totalSec = Math.round(ms / 1000);
  if (totalSec < 60) return `${totalSec}s`;
  const m = Math.floor(totalSec / 60);
  const s = totalSec % 60;
  return `${m}m ${String(s).padStart(2, "0")}s`;
}
