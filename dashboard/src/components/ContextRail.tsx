import { useCallback, useEffect, useState, Fragment } from "react";
import type { ReactNode } from "react";
import { api, ApiError } from "../lib/api";
import { cn } from "../lib/cn";
import { BudgetBlockedBanner } from "./BudgetBlockedBanner";
import type {
  ActionItem,
  ActionsResponse,
  BudgetIncident,
  LLMBurnSummary,
  PlansResponse,
  ProjectOverview,
} from "../types";

// Day shape mirrors what App passes (a 3-day calendar stub).
interface DayEvent { text: string; flag?: boolean; }
interface Day { initial: string; date: number; today?: boolean; events: DayEvent[]; }

interface Props {
  workspace: { name: string; type: string };
  /** Right-side day range header, e.g. "Fri – Sun". */
  weekRange?: string;
  /** Same shape App feeds (a 3-day calendar stub). */
  weekDays?: Day[];
  /** Accepted for API compatibility — the agreed v2 rail has no Project card. */
  projectOverview?: ProjectOverview | null;
  /** Loaded summary from /api/telemetry/llm-burn. Null while in-flight. */
  burn: LLMBurnSummary | null;
  /** Loaded plan-cap rows from /api/usage/plans. Null while in-flight. */
  plans: PlansResponse | null;
  /** Budget incidents currently blocking the gate (brief item 3). When non-empty
   *  the COST & LIMITS section yields its slot to the red BLOCKED banner — never
   *  both. Defaults to empty so callers that don't wire it just see the panel. */
  incidents?: BudgetIncident[];
  /** Open the ack modal for a blocking incident (paired with `incidents`). */
  onAckIncident?: (incident: BudgetIncident) => void;
  /** Accepted for API compatibility — the agreed v2 rail has no Vault link. */
  onOpenVault?: () => void;
  /** Open-actions/Chat tab — controlled by App so Cmd-K /chat can switch. */
  chatTab?: "actions" | "chat";
  onChatTab?: (tab: "actions" | "chat") => void;
  /** The in-rail project chat, rendered in the Open-actions section when chatTab === "chat". */
  chatSlot?: ReactNode;
}

/**
 * Context rail — the Desk right column, matching the agreed Paper **v2** artboard
 * (node 2B6-0): ONE teal rail (var(--rail): #3F8B88 light / #142A4A navy) holding
 * THREE sections divided by solid-white hairlines — NOT a stack of cards:
 *
 *   1. THIS WEEK     — day rows (a centred initial+date cell, then dot-led events)
 *                      from the `weekDays` stub, dashed separators between days.
 *   2. OPEN ACTIONS / CHAT — a segmented toggle. "Open actions" is the live
 *                      /api/projects/<name>/actions checklist (click a box to
 *                      toggle the underlying `- [ ] ↔ - [x]`); "Chat" renders the
 *                      in-rail project chat (`chatSlot`).
 *   3. COST & LIMITS — today's £ spend against the daily cap + an EOD projection
 *                      from the GBP plan (USD telemetry fallback), a within-budget
 *                      status pill, and a real call/token volume footer.
 *
 * Light-on-rail text uses fixed light tints (correct on both the teal and the
 * deep-navy ground); structural pills use theme tokens so they still flip.
 */
export function ContextRail({
  workspace, weekRange = "Fri – Sun", weekDays = [], projectOverview = null, burn, plans,
  incidents = [], onAckIncident,
  chatTab = "actions", onChatTab, chatSlot,
}: Props) {
  // ── Open actions — self-contained fetch (mirrors OpenActionsPanel) ──────────
  const [actions, setActions] = useState<ActionsResponse | null>(null);
  const [busy, setBusy] = useState(false);

  const refetch = useCallback(async () => {
    if (!workspace.name) return;
    try {
      setActions(await api.projectActions(workspace.name));
    } catch (e) {
      const is404 = e instanceof ApiError && e.status === 404;
      setActions((prev) => prev ?? (is404 ? EMPTY_ACTIONS(workspace.name) : prev));
    }
  }, [workspace.name]);

  useEffect(() => { void refetch(); }, [refetch]);

  const onToggle = async (a: ActionItem) => {
    if (!workspace.name || busy) return;
    setBusy(true);
    try {
      await api.toggleAction(workspace.name, {
        source_file: a.source_file,
        task_hash:   a.task_hash,
        line_hint:   a.source_line,
        to:          a.status === "done" ? "open" : "done",
      });
      await refetch();
    } catch {
      /* toast in a later pass */
    } finally {
      setBusy(false);
    }
  };

  const openRows: ActionItem[] = actions
    ? [...actions.overdue, ...actions.open, ...actions.stale].slice(0, 3)
    : [];
  const totalOpen = actions ? actions.counts.total_open : 0;

  return (
    <div className="flex w-full grow flex-col gap-[18px] overflow-y-auto min-h-0 rounded-[16px] bg-rail px-[18px] py-[20px] shadow-card">
      <ThisWeekSection weekRange={weekRange} weekDays={weekDays} />
      <RailDivider />
      <OpenActionsSection
        workspace={workspace}
        totalOpen={totalOpen}
        rows={openRows}
        loading={actions === null}
        busy={busy}
        onToggle={onToggle}
        chatTab={chatTab}
        onChatTab={onChatTab}
        chatSlot={chatSlot}
      />
      <RailDivider />
      <CostLimitsSection
        workspace={workspace}
        projectOverview={projectOverview}
        burn={burn}
        plans={plans}
        incidents={incidents}
        onAckIncident={onAckIncident}
      />
    </div>
  );
}

// ── Rail chrome ──────────────────────────────────────────────────────────────

/** Solid-white between-section rule (Paper 2B6-0): 2px, full opacity, square edges. */
function RailDivider() {
  return <div className="h-[2px] w-full shrink-0 bg-white" />;
}

/**
 * Faint dashed intra-section rule — an SVG line with rounded dash caps
 * (Paper 5VA-0: strokeWidth 1.5, linecap round, dasharray "13 11", stroke --line).
 */
function DashRule() {
  return (
    <svg width="100%" height="4" xmlns="http://www.w3.org/2000/svg" className="w-full shrink-0 overflow-visible">
      <line
        x1="0"
        y1="2"
        x2="100%"
        y2="2"
        stroke="var(--line)"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeDasharray="13 11"
      />
    </svg>
  );
}

// Light-on-rail tones — readable on both the teal (#3F8B88) and navy (#142A4A) grounds.
const RAIL_LABEL = "text-[#C8DEDC]";
const RAIL_TEXT  = "text-[#F4F8F7]";
const RAIL_SUB   = "text-[#D8EAE8]";
const RAIL_OK    = "text-[#ABDFC2]";
const RAIL_WARN  = "text-[#FF8E73]";

// ── 1 · THIS WEEK ────────────────────────────────────────────────────────────

function ThisWeekSection({ weekRange, weekDays }: { weekRange: string; weekDays: Day[] }) {
  return (
    <div className="flex flex-col gap-[15px]">
      <div className="flex items-baseline justify-between">
        <span className="flex items-baseline gap-[6px]">
          <span className={cn("text-[10.5px] font-bold uppercase tracking-[0.11em]", RAIL_LABEL)}>Upcoming</span>
          {/* #session-ops / #17 — the calendar feed (Outlook via MS-Graph) is not
              wired yet, so these are placeholder items. Mark them honestly rather
              than present fixed sample text as a live agenda. */}
          <span
            className={cn("text-[9px] font-medium normal-case tracking-normal opacity-70", RAIL_LABEL)}
            title="Placeholder — sample items, pending Outlook calendar integration"
          >
            · sample
          </span>
        </span>
        <span className={cn("text-[11px] font-medium", RAIL_LABEL)}>{weekRange}</span>
      </div>
      {weekDays.length === 0 ? (
        <span className={cn("text-[11.5px] italic", RAIL_LABEL)}>No calendar this week.</span>
      ) : (
        weekDays.map((d, di) => (
          <Fragment key={d.initial + d.date}>
            {di > 0 && <DashRule />}
            <div className="flex gap-[14px]">
              <div className="flex w-[32px] shrink-0 flex-col items-center gap-[2px]">
                <span className={cn("text-[9.5px] font-bold uppercase tracking-[0.05em]", RAIL_SUB)}>
                  {d.initial}
                </span>
                <span className={cn("font-mono text-[18px] font-medium tabular-nums", RAIL_TEXT)}>{d.date}</span>
              </div>
              <div className="flex grow basis-0 flex-col gap-[9px] pt-[1px]">
                {d.events.length === 0 ? (
                  <span className={cn("text-[12px]", RAIL_LABEL)}>—</span>
                ) : (
                  d.events.map((ev, i) => {
                    const { label, time } = splitEventTime(ev.text);
                    return (
                      <div key={i} className="flex items-start gap-[8px]">
                        <span className={cn("mt-[6px] h-[5px] w-[5px] shrink-0 rounded-full", ev.flag ? "bg-amber" : "bg-(--mist)")} />
                        <span className={cn("grow basis-0 text-[12.5px] font-medium leading-[125%]", RAIL_TEXT)}>{label}</span>
                        {time && (
                          <span className={cn("shrink-0 font-mono text-[11px] leading-[14px]", RAIL_LABEL)}>{time}</span>
                        )}
                      </div>
                    );
                  })
                )}
              </div>
            </div>
          </Fragment>
        ))
      )}
    </div>
  );
}

// ── 2 · OPEN ACTIONS / CHAT ──────────────────────────────────────────────────

function OpenActionsSection({
  workspace, totalOpen, rows, loading, busy, onToggle, chatTab, onChatTab, chatSlot,
}: {
  workspace: { name: string; type: string };
  totalOpen: number;
  rows: ActionItem[];
  loading: boolean;
  busy: boolean;
  onToggle: (a: ActionItem) => void;
  chatTab: "actions" | "chat";
  onChatTab?: (t: "actions" | "chat") => void;
  chatSlot?: ReactNode;
}) {
  return (
    <div className="flex flex-col gap-[13px]">
      <div className="flex items-center justify-between">
        <div className="flex gap-[3px] rounded-[9px] bg-paper2 p-[3px]">
          {(["actions", "chat"] as const).map((t) => (
            <button
              key={t}
              type="button"
              onClick={() => onChatTab?.(t)}
              className={cn(
                "rounded-[7px] px-[12px] py-[5px] text-[11.5px] leading-none transition-colors",
                chatTab === t ? "bg-bg-2 font-semibold text-t1 shadow-card" : "font-medium text-t2 hover:text-t1",
              )}
            >
              {t === "actions" ? "Open actions" : "Chat"}
            </button>
          ))}
        </div>
        {chatTab === "actions" && (
          <span className={cn("text-[11px] font-medium", RAIL_LABEL)}>{totalOpen} open</span>
        )}
      </div>

      {chatTab === "chat" ? (
        chatSlot
      ) : loading ? (
        <div className={cn("text-[12px] italic", RAIL_LABEL)}>Loading…</div>
      ) : rows.length === 0 ? (
        <div className={cn("text-[12.5px] leading-[150%]", RAIL_SUB)}>
          No actions surfaced in <span className={RAIL_TEXT}>{workspace.name}</span>.
        </div>
      ) : (
        rows.map((a) => {
          const overdue = a.status === "overdue";
          return (
            <div key={`${a.source_file}#${a.task_hash}`} className="flex items-start gap-[11px]">
              <button
                type="button"
                onClick={() => onToggle(a)}
                disabled={busy}
                className="mt-[1px] flex size-[16px] shrink-0 items-center justify-center rounded-[5px] border-[1.5px] border-white/45 transition-colors disabled:opacity-50 hover:border-white"
                title="Click to toggle done"
                aria-label={`Toggle action: ${a.title}`}
              />
              <div className="flex grow basis-0 flex-col gap-[2px]">
                <div className={cn("text-[13px] font-medium leading-[130%]", RAIL_TEXT)}>{a.title}</div>
                <div className={cn("text-[11px] font-medium leading-[120%]", overdue ? RAIL_WARN : RAIL_SUB)}>
                  {actionMeta(a)}
                </div>
              </div>
            </div>
          );
        })
      )}
    </div>
  );
}

// ── 3 · COST & LIMITS ────────────────────────────────────────────────────────

function CostLimitsSection({
  workspace, projectOverview, burn, plans, incidents, onAckIncident,
}: {
  workspace: { name: string; type: string };
  projectOverview: ProjectOverview | null;
  burn: LLMBurnSummary | null;
  plans: PlansResponse | null;
  incidents: BudgetIncident[];
  onAckIncident?: (incident: BudgetIncident) => void;
}) {
  const blocked = incidents.length > 0;

  // ── Lifetime project spend (brief item 3) ──────────────────────────────────
  // The `burn` prop is the 24h telemetry window (drives "This window" below).
  // The lifetime "since project opened" figure needs a SECOND llm-burn call
  // scoped to this project's start date — the `created` anchor (the brief's
  // `opened` date, else the folder ctime) now on the project overview. Only for
  // project workspaces; best-effort — any miss leaves the honest "pending"
  // footer fallback.
  // Only trust the overview when it belongs to THIS workspace — during a
  // workspace switch the parent can briefly still hold the previous project's
  // overview, which would otherwise fetch this workspace's spend against the
  // wrong since-date.
  const ovForWs = projectOverview && projectOverview.name === workspace.name ? projectOverview : null;
  const sinceDate = workspace.type === "project"
    ? (ovForWs?.created ?? ovForWs?.opened ?? null)
    : null;
  const [lifetimeUsd, setLifetimeUsd] = useState<number | null>(null);
  useEffect(() => {
    if (!workspace.name || !sinceDate) { setLifetimeUsd(null); return; }
    let cancelled = false;
    api.llmBurn({ group_by: "all", since: `${sinceDate}T00:00:00Z` })
      .then((r) => {
        if (cancelled) return;
        const wb = r.byWorkspace?.[`${workspace.type}:${workspace.name}`];
        setLifetimeUsd(wb?.costUsd ?? 0);
      })
      .catch(() => { if (!cancelled) setLifetimeUsd(null); });
    return () => { cancelled = true; };
  }, [workspace.type, workspace.name, sinceDate]);

  // Prefer the £ daily-cap plan (matches the shell footer); fall back to the USD
  // telemetry total when no GBP plan is configured.
  const gbpPlan = plans?.plans.find((p) => p.unit === "gbp") ?? null;
  const todaySpent = gbpPlan ? gbpPlan.used : (burn ? burn.totals.costUsd : 0);
  const cap = gbpPlan ? gbpPlan.cap : 0;
  const fmt = gbpPlan ? fmtGbp : fmtUsd;

  const scaler = burn ? projectionScaler(burn.window) : 1;
  const todayProjected = todaySpent * scaler;
  const withinBudget = cap <= 0 || todaySpent <= cap;

  const calls = burn?.totals.calls ?? 0;
  const tok = burn ? burn.totals.tokensIn + burn.totals.tokensOut : 0;

  // ── Project row (brief item 3) ────────────────────────────────────────────
  // Per-workspace 24h burn — keyed "<type>:<name>" (the telemetry aggregator's
  // key, e.g. "project:Helix"), matching the Budget-tab matrix. (Keying on the
  // bare name silently missed every row — the aggregator never emits a name-only
  // key.) The £ plan caps GLOBAL daily spend, so project figures stay in the
  // telemetry's native USD — labelled so the unit switch is explicit. This is the
  // SAME 24h window as Today ("This window"); the LIFETIME figure comes from the
  // separate since-opened fetch above.
  const wsBurn = burn?.byWorkspace?.[`${workspace.type}:${workspace.name}`] ?? null;
  const projSpent = wsBurn ? wsBurn.costUsd : 0;
  const projProjected = projSpent * scaler;
  const projCalls = wsBurn ? wsBurn.calls : 0;

  return (
    <div className="flex flex-col gap-[12px]">
      <div className="flex items-baseline justify-between">
        <span className={cn("text-[10.5px] font-bold uppercase tracking-[0.11em]", RAIL_LABEL)}>Cost &amp; limits</span>
        <span className={cn("text-[11px] font-semibold", blocked || !withinBudget ? RAIL_WARN : RAIL_OK)}>
          {blocked ? "Cap reached" : withinBudget ? "Within budget" : "Over cap"}
        </span>
      </div>

      {blocked ? (
        // Budget-blocked yield: the enriched Today/Project cards are REPLACED by
        // the red BLOCKED banner (never both) — same slot, matching Paper 1b.
        <BudgetBlockedBanner incidents={incidents} onAckClick={(inc) => onAckIncident?.(inc)} />
      ) : (
        <div className="flex flex-col gap-[10px]">
          {/* Today — spent + projected EOD vs the daily cap */}
          <CostCard
            label="Today"
            note={`${calls.toLocaleString()} call${calls === 1 ? "" : "s"}`}
            spentLabel="Spent"
            spent={fmt(todaySpent)}
            projLabel="Proj. EOD"
            proj={fmt(todayProjected)}
            footer={cap > 0 ? `${fmt(todaySpent)} / ${fmt(cap)} daily cap` : `${fmtTok(tok)} tok today`}
            cap={cap}
            spentRaw={todaySpent}
          />

          {/* Project — per-workspace spend + projected burn (this-window). */}
          <CostCard
            accent
            label={projectLabel(workspace)}
            note={`${projCalls.toLocaleString()} call${projCalls === 1 ? "" : "s"} · 24h`}
            spentLabel="This window"
            spent={fmtUsd(projSpent)}
            projLabel="Proj. EOD"
            proj={fmtUsd(projProjected)}
            // Lifetime spend-to-date from the since-opened fetch above; falls
            // back to an honest "pending" when there's no anchor date / it missed.
            footer={lifetimeUsd != null && sinceDate
              ? `${fmtUsd(lifetimeUsd)} since ${shortSince(sinceDate)}`
              : "window spend · lifetime to-date pending"}
          />
        </div>
      )}
    </div>
  );
}

/**
 * One Cost & limits card (Paper 1b) — a small framed block: label + note header,
 * a Spent / Projected pair, an optional cap progress bar (Today only) and a mono
 * footer. `accent` tints the frame for the Project card.
 */
function CostCard({
  label, note, spentLabel, spent, projLabel, proj, footer, accent, cap, spentRaw,
}: {
  label: string;
  note: string;
  spentLabel: string;
  spent: string;
  projLabel: string;
  proj: string;
  footer: string;
  accent?: boolean;
  cap?: number;
  spentRaw?: number;
}) {
  const pct = cap && cap > 0 && spentRaw != null
    ? Math.min(100, Math.max(0, (spentRaw / cap) * 100))
    : null;
  const over = pct != null && pct >= 100;
  return (
    <div
      className={cn(
        "flex flex-col gap-[9px] rounded-[11px] px-[12px] py-[11px] border",
        accent ? "border-white/[0.18] bg-white/[0.05]" : "border-white/10 bg-white/[0.03]",
      )}
    >
      <div className="flex items-center justify-between">
        <span className={cn("text-[9px] font-bold uppercase tracking-[0.12em]", RAIL_LABEL)}>{label}</span>
        <span className={cn("font-mono text-[9.5px]", RAIL_LABEL)}>{note}</span>
      </div>
      <div className="flex items-end justify-between">
        <div className="flex flex-col gap-[3px]">
          <span className={cn("font-mono text-[9px] uppercase tracking-[0.08em]", RAIL_LABEL)}>{spentLabel}</span>
          <span className={cn("font-mono text-[15px] leading-none tabular-nums", RAIL_TEXT)}>{spent}</span>
        </div>
        <div className="flex flex-col items-end gap-[3px]">
          <span className={cn("font-mono text-[9px] uppercase tracking-[0.08em]", RAIL_LABEL)}>{projLabel}</span>
          <span className={cn("font-mono text-[15px] leading-none tabular-nums", RAIL_SUB)}>{proj}</span>
        </div>
      </div>
      {pct != null && (
        <div className="h-[5px] overflow-hidden rounded-[3px] bg-white/12">
          <div
            className="h-full rounded-[3px]"
            style={{ width: `${pct}%`, backgroundColor: over ? "#E8A04C" : "#6BB083" }}
          />
        </div>
      )}
      <div className={cn("font-mono text-[9.5px]", RAIL_LABEL)}>{footer}</div>
    </div>
  );
}

/** Header label for the project Cost card — "Project Helix" for a deal, else the
 *  workspace name with its type tag so a BD/general rail reads honestly. */
function projectLabel(workspace: { name: string; type: string }): string {
  if (workspace.type === "project") return `Project ${workspace.name}`;
  return workspace.name;
}

// ── Helpers ──────────────────────────────────────────────────────────────────

function EMPTY_ACTIONS(project: string): ActionsResponse {
  return {
    project,
    overdue: [], open: [], stale: [], done: [],
    counts: { overdue: 0, open: 0, stale: 0, done: 0, total_open: 0 },
  };
}

/**
 * Split a trailing HH:MM clock time off an event label so it can render as a
 * right-aligned mono column (Paper 2GY-0). "Client meeting 11:00" → { label:
 * "Client meeting", time: "11:00" }. No trailing time → { label, time: "" }.
 */
function splitEventTime(text: string): { label: string; time: string } {
  const m = text.match(/^(.*?)\s+(\d{1,2}:\d{2})\s*$/);
  return m ? { label: m[1], time: m[2] } : { label: text, time: "" };
}

function actionMeta(a: ActionItem): string {
  if (a.status === "overdue") return a.due ? `Overdue · due ${a.due}` : "Overdue";
  const due = a.due ? `Due ${a.due}` : "No due date";
  return a.owner ? `${due} · → ${a.owner}` : due;
}

/** 24h / window_hours, capped at 8× so a 1-minute burst doesn't project absurdly. */
function projectionScaler(window: { since: string; until: string }): number {
  try {
    const s = new Date(window.since).getTime();
    const u = new Date(window.until).getTime();
    const h = (u - s) / 3_600_000;
    if (!Number.isFinite(h) || h <= 0) return 1;
    return Math.min(8, 24 / h);
  } catch { return 1; }
}

function fmtUsd(v: number): string {
  if (v === 0) return "$0";
  if (v < 0.01) return "<$0.01";
  if (v < 100)  return `$${v.toFixed(2)}`;
  return `$${Math.round(v).toLocaleString()}`;
}

function fmtGbp(v: number): string {
  if (v === 0) return "£0.00";
  if (v < 100) return `£${v.toFixed(2)}`;
  return `£${Math.round(v).toLocaleString()}`;
}

function fmtTok(v: number): string {
  if (v < 1_000)     return v.toString();
  if (v < 1_000_000) return `${(v / 1_000).toFixed(1)}k`;
  return `${(v / 1_000_000).toFixed(2)}M`;
}

/** ISO date → compact "Mar 2026" for the lifetime-spend footer. Falls back to
 *  the raw string if it can't be parsed (never throws in render). */
function shortSince(iso: string): string {
  const d = new Date(`${iso}T00:00:00Z`);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleDateString("en-GB", { month: "short", year: "numeric", timeZone: "UTC" });
}
