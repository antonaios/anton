import { useEffect, useMemo, useRef, useState } from "react";
import type { CSSProperties, ReactNode } from "react";
import { cn } from "../lib/cn";
import { api, ApiError } from "../lib/api";
import { useMediaQuery } from "../lib/useMediaQuery";
import { Markdown } from "./Markdown";
import type {
  PendingProposal, PendingProposalsResponse, ProposalKind, ProposalTier,
  RouteProposalBody, WorkspaceListItem, WorkspaceType,
} from "../types";

/** True when the OS "reduce motion" preference is set — gates the card
 *  slide/collapse exit (we just drop the row instead). Mirrors the same
 *  `useMediaQuery` pattern ChatCanvas uses for its status-line motion. */
function usePrefersReducedMotion(): boolean {
  return useMediaQuery("(prefers-reduced-motion: reduce)");
}

/** Card exit-animation duration — must match the CSS transition below. */
const EXIT_MS = 360;

interface Props {
  /** Latest pending-proposals snapshot from App.tsx (#7b — Session F lift).
   *  Null while the first fetch is in flight. */
  pending: PendingProposalsResponse | null;
  /** Refresh the shared proposals state. Called after every action (route /
   *  reject / skip) so the TopHeader REVIEW chip + this list stay in sync. */
  onRefresh: () => Promise<void>;
  /** Top-level fetch error from App.tsx, if any. */
  error?: string | null;
  /** True during the initial fetch — shown as a skeleton. */
  loading?: boolean;
}

type ResolvedVerdict = "routed" | "rejected" | "skipped" | "revision";
interface ResolvedEntry {
  id: string;
  verdict: ResolvedVerdict;
  title: string;
  when: string;
}

/**
 * Inbox tab — review queue for HiNotes / sector / memory proposals.
 *
 * Per-row actions hit `POST /api/proposals/{id}/route|reject|skip` (Session
 * G, locked 2026-05-25). Pending state + 2-min poll live in App.tsx (#7b);
 * actions trigger a shared refresh so the TopHeader REVIEW chip updates
 * immediately without waiting for its own poll.
 */
export function InboxTab({ pending, onRefresh, error, loading }: Props) {
  /** Ids currently mid-action — disables their row buttons + shows spinner. */
  const [actioning, setActioning] = useState<Set<string>>(new Set());
  /** Per-row inline error keyed by proposal id; cleared on next action / refresh. */
  const [rowErrors, setRowErrors] = useState<Record<string, string>>({});
  /** Filter chip selection — `null` = all kinds. */
  const [filterKind, setFilterKind] = useState<ProposalKind | null>(null);
  /** Workspaces for the inline Route picker. Fetched once on mount. */
  const [workspaces, setWorkspaces] = useState<WorkspaceListItem[]>([]);
  // #58-harness — inline reject panel state. Reject button now opens a
  // textarea + Submit/Cancel below the row. Server requires non-empty
  // `reason`; client mirrors with Submit disabled until trimmed length > 0.
  /** Id of the proposal whose reject panel is open. `null` = no panel open. */
  const [rejectingId, setRejectingId] = useState<string | null>(null);
  /** Textarea state for the open reject panel. Reset when rejectingId changes. */
  const [rejectReason, setRejectReason] = useState("");
  // #58-harness2 — inline revision-request panel state. Mirrors the reject
  // panel; feedback is REQUIRED non-empty after `.strip()` (server 422
  // otherwise — guard client-side too). Server 409 if a revision is already
  // pending; rendered inline + panel stays open so the operator sees why.
  const [revisingId, setRevisingId] = useState<string | null>(null);
  const [revisionFeedback, setRevisionFeedback] = useState("");
  // Session-local "resolved this session" log — drives the progress bar AND (with
  // the persistent feed below) the "Recently resolved" rail. Kept SEPARATE from
  // the feed so the progress bar counts only THIS session's actions, not history.
  const [resolved, setResolved] = useState<ResolvedEntry[]>([]);
  // #inbox-resolved-feed — persistent resolved history, seeded on mount. Feeds
  // ONLY the rail (never the session progress count).
  const [feedResolved, setFeedResolved] = useState<ResolvedEntry[]>([]);
  // #inbox-proposal-detail — the SELECTED card's fetched write-up (one at a time).
  const [detail, setDetail] = useState<{ id: string; body: string } | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  // Keyboard-triage cursor — the proposal carrying the blue selection ring.
  // `j`/`↓` + `k`/`↑` walk it over the visible (filtered, grouped) order.
  const [selectedId, setSelectedId] = useState<string | null>(null);
  // Id of the card currently playing its slide-out/collapse exit. Cleared once
  // the transition has run (or immediately under prefers-reduced-motion).
  const [exitingId, setExitingId] = useState<string | null>(null);
  // `r` triage opens the selected row's workspace picker; the picker lives in
  // the row (it owns the dropdown anchor), so we request it open by id. The
  // row clears this when it opens / dismisses, keeping mouse + keyboard in sync.
  const [pickerForId, setPickerForId] = useState<string | null>(null);

  const reduceMotion = usePrefersReducedMotion();

  // Workspaces — one fetch covers all three types via the no-arg list endpoint.
  // We only need this when a Route action fires, but pre-loading keeps the
  // picker instant.
  useEffect(() => {
    let cancelled = false;
    api.listWorkspaces()
      .then((r) => { if (!cancelled) setWorkspaces(r.workspaces); })
      .catch(() => { /* picker shows empty hint; not fatal */ });
    return () => { cancelled = true; };
  }, []);

  // #inbox-resolved-feed — seed the "Recently resolved" rail from the persistent
  // feed on mount (unions routed + dismissed, survives reloads). Best-effort: a
  // failure (or an older bridge) just leaves the rail to this session's entries.
  useEffect(() => {
    let cancelled = false;
    api.proposalsResolved(12)
      .then((r) => {
        if (cancelled) return;
        setFeedResolved(r.items.map((it) => ({
          id: it.proposal_id, verdict: it.verdict, title: it.title, when: formatResolvedWhen(it.at),
        })));
      })
      .catch(() => { /* rail falls back to this session's optimistic entries */ });
    return () => { cancelled = true; };
  }, []);

  // #inbox-proposal-detail — fetch the selected card's full write-up so it can
  // inline under the title. One at a time; cleared when nothing's selected.
  // Best-effort — the card falls back to the generic decision-context blurb.
  useEffect(() => {
    if (!selectedId) { setDetail(null); setDetailLoading(false); return; }
    let cancelled = false;
    setDetailLoading(true);
    api.proposalContent(selectedId)
      .then((r) => { if (!cancelled) { setDetail({ id: selectedId, body: r.body }); setDetailLoading(false); } })
      .catch(() => { if (!cancelled) { setDetail(null); setDetailLoading(false); } });
    return () => { cancelled = true; };
  }, [selectedId]);

  // ── Action handlers ──
  const markActioning = (id: string, on: boolean) =>
    setActioning((s) => { const n = new Set(s); on ? n.add(id) : n.delete(id); return n; });

  const setRowError = (id: string, msg: string | null) =>
    setRowErrors((m) => {
      if (msg === null) { const { [id]: _, ...rest } = m; void _; return rest; }
      return { ...m, [id]: msg };
    });

  // Push a resolved-this-session entry. Reads the item from the current pending
  // snapshot (captured before onRefresh drops it); capped at the last 8.
  const logResolved = (id: string, verdict: ResolvedVerdict) => {
    const it = pending?.items.find((p) => p.id === id);
    if (it) setResolved((prev) => [{ id, verdict, title: it.title, when: hhmm() }, ...prev].slice(0, 8));
  };

  // Visible (filtered + grouped) order — kept in a ref so the keyboard handler
  // and the cursor-advance logic can read the *current* order without being
  // re-bound on every render. Populated below where `grouped` is computed.
  const visibleOrderRef = useRef<PendingProposal[]>([]);

  // Guards the resolveWithAnim exit tail against a mid-animation unmount: the
  // ~360ms slide awaits a timer that would otherwise refresh + setState after
  // the Inbox view has been torn down.
  const mountedRef = useRef(true);
  useEffect(() => () => { mountedRef.current = false; }, []);

  // After a verdict drops `id` from the queue, move the cursor to the item that
  // visually follows it (or the new last one), so triage keeps flowing without
  // a click. Computed off the order captured BEFORE onRefresh removes the row.
  const advanceCursorPast = (id: string) => {
    const order = visibleOrderRef.current;
    const idx = order.findIndex((p) => p.id === id);
    if (idx < 0) return;
    const next = order[idx + 1] ?? order[idx - 1] ?? null;
    setSelectedId(next ? next.id : null);
  };

  // Shared resolution tail: log the verdict pill, advance the cursor, play the
  // card's slide/collapse exit (skipped under prefers-reduced-motion — the row
  // is simply dropped), then refresh the shared pending state. The title is
  // captured by `logResolved` from the pre-refresh snapshot.
  const resolveWithAnim = async (id: string, verdict: ResolvedVerdict) => {
    logResolved(id, verdict);
    advanceCursorPast(id);
    if (!reduceMotion) {
      setExitingId(id);
      // Let the slide/collapse play, then refresh while still collapsed so the
      // row never snaps back open before the queue actually drops it.
      await new Promise<void>((r) => setTimeout(r, EXIT_MS));
      if (!mountedRef.current) return;
      await onRefresh();
      if (mountedRef.current) setExitingId(null);
    } else {
      await onRefresh();
    }
  };

  const handleRoute = async (id: string, body: RouteProposalBody) => {
    markActioning(id, true);
    setRowError(id, null);
    try {
      await api.routeProposal(id, body);
      await resolveWithAnim(id, "routed");
    } catch (e) {
      setRowError(id, formatError(e, "Route failed"));
    } finally {
      markActioning(id, false);
    }
  };

  // #58-harness — open the inline reject panel for `id`. Resets the textarea
  // so switching mid-flow (proposal A → B without submitting A) starts fresh.
  const openRejectPanel = (id: string) => {
    setRejectingId(id);
    setRejectReason("");
    setRowError(id, null);
  };

  const cancelRejectPanel = () => {
    setRejectingId(null);
    setRejectReason("");
  };

  // Submit the reject. Server requires non-empty `reason` (#58); guard
  // client-side too so we never hit the 422 in the happy path.
  const submitReject = async (id: string) => {
    const trimmed = rejectReason.trim();
    if (!trimmed) return;
    markActioning(id, true);
    setRowError(id, null);
    try {
      await api.rejectProposal(id, { reason: trimmed });
      setRejectingId(null);
      setRejectReason("");
      await resolveWithAnim(id, "rejected");
    } catch (e) {
      // 422 here is defensive — client-side `trimmed` check should
      // prevent it. Surface inline + keep the panel open so the
      // operator can fix and retry.
      setRowError(id, formatError(e, "Reject failed"));
    } finally {
      markActioning(id, false);
    }
  };

  // #58-harness2 — revision panel handlers. Same shape as reject; the only
  // material difference is the 409 case (revision already pending) which is
  // surfaced inline with the panel kept open so the operator sees the why.
  const openRevisionPanel = (id: string) => {
    setRevisingId(id);
    setRevisionFeedback("");
    setRowError(id, null);
  };

  const cancelRevisionPanel = () => {
    setRevisingId(null);
    setRevisionFeedback("");
  };

  const submitRevision = async (id: string) => {
    const trimmed = revisionFeedback.trim();
    if (!trimmed) return;
    markActioning(id, true);
    setRowError(id, null);
    try {
      await api.requestRevision(id, { feedback: trimmed });
      setRevisingId(null);
      setRevisionFeedback("");
      await resolveWithAnim(id, "revision");
    } catch (e) {
      // 409 = revision already pending; 422 = empty feedback (defensive —
      // the trimmed guard above should prevent it). Either way, surface
      // inline and keep the panel open.
      setRowError(id, formatError(e, "Request revision failed"));
    } finally {
      markActioning(id, false);
    }
  };

  const handleSkip = async (id: string, defer_days: number) => {
    markActioning(id, true);
    setRowError(id, null);
    try {
      await api.skipProposal(id, { defer_days });
      await resolveWithAnim(id, "skipped");
    } catch (e) {
      setRowError(id, formatError(e, "Skip failed"));
    } finally {
      markActioning(id, false);
    }
  };

  // ── Filtering + grouping ──
  const items = pending?.items ?? [];
  const filteredItems = filterKind ? items.filter((it) => it.kind === filterKind) : items;
  const grouped = useMemo(() => groupByKind(filteredItems), [filteredItems]);
  const allKinds = useMemo(() => Object.keys(pending?.byKind ?? {}) as ProposalKind[], [pending?.byKind]);

  // Flat visible order = exactly the rendered order (grouped, date-desc within
  // each group). Drives the keyboard cursor + the progress bar. Kept in a ref
  // so the keydown handler reads the live order without re-subscribing.
  const flatItems = useMemo(() => grouped.flatMap((g) => g.items), [grouped]);
  visibleOrderRef.current = flatItems;

  // Keep the cursor valid as the queue changes (filter switch, refresh drops a
  // row, first load). Default to the first card; if the selected id vanished
  // and the cursor-advance didn't already move it, fall back to the first.
  useEffect(() => {
    if (flatItems.length === 0) {
      if (selectedId !== null) setSelectedId(null);
      return;
    }
    if (selectedId === null || !flatItems.some((p) => p.id === selectedId)) {
      setSelectedId(flatItems[0].id);
    }
  }, [flatItems, selectedId]);

  // Progress affordance — N resolved / total this session. `total` is the
  // session high-water mark (current pending + everything resolved so far).
  const pendingNow = pending?.total ?? flatItems.length;
  const resolvedCount = resolved.length;
  const sessionTotal = pendingNow + resolvedCount;
  const progressPct = sessionTotal > 0 ? (resolvedCount / sessionTotal) * 100 : 0;

  // ── Keyboard triage ──
  // Scoped to the Inbox view (this component only mounts when the Inbox tab is
  // active). Ignored while a textarea/input/select/contentEditable is focused so
  // the inline reason/feedback panels type normally (Enter submits there). j/↓ +
  // k/↑ move the cursor; r opens the workspace picker; e/x open the
  // revision/reject panel + focus its textarea; s resolves directly; o opens the
  // source in Obsidian — 1:1 with the per-row actions.
  //
  // The live handler is kept in a ref (refreshed every render) so the window
  // listener is bound once yet never reads stale state/closures (e.g. a fresh
  // `pending` snapshot for the verdict log + cursor advance).
  const moveCursor = (delta: 1 | -1) => {
    const order = visibleOrderRef.current;
    if (order.length === 0) return;
    const cur = order.findIndex((p) => p.id === selectedId);
    const base = cur < 0 ? 0 : cur;
    const next = Math.max(0, Math.min(base + delta, order.length - 1));
    setSelectedId(order[next].id);
  };

  const onKeyRef = useRef<(e: KeyboardEvent) => void>(() => {});
  onKeyRef.current = (e: KeyboardEvent) => {
    // Don't hijack typing in the inline panels / any field.
    const el = e.target as HTMLElement | null;
    const tag = el?.tagName;
    if (tag === "TEXTAREA" || tag === "INPUT" || tag === "SELECT" || el?.isContentEditable) return;
    // Leave modifier combos (copy/paste, browser shortcuts) alone.
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    // Nothing to triage / a card is mid-exit — do nothing.
    const order = visibleOrderRef.current;
    if (order.length === 0 || exitingId) return;

    const key = (e.key || "").toLowerCase();
    if (key === "j" || e.key === "ArrowDown") { e.preventDefault(); moveCursor(1); return; }
    if (key === "k" || e.key === "ArrowUp") { e.preventDefault(); moveCursor(-1); return; }

    const target = order.find((p) => p.id === selectedId) ?? order[0];
    if (!target) return;
    // Don't fire a verdict on a row that's already mid-action.
    if (actioning.has(target.id)) return;

    if (key === "r") { e.preventDefault(); setPickerForId(target.id); }
    else if (key === "e") { e.preventDefault(); openRevisionPanel(target.id); }
    else if (key === "x") { e.preventDefault(); openRejectPanel(target.id); }
    else if (key === "s") { e.preventDefault(); void handleSkip(target.id, 7); }
    else if (key === "o") { e.preventDefault(); openInObsidian(target.path); }
  };

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => onKeyRef.current(e);
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  return (
    <div className="mx-auto flex w-full max-w-[1060px] flex-col gap-[18px] px-[28px] py-[30px] text-t1">

      {/* Header — Paper 62D-0: title + subtitle, Pending pill + Refresh */}
      <div className="flex items-center justify-between">
        <div className="flex flex-col gap-[4px]">
          <h2 className="text-[23px] font-semibold leading-[28px] tracking-[-0.02em] text-t1">Inbox</h2>
          <span className="text-[13px] leading-[16px] text-t2">
            {pending === null
              ? "loading…"
              : `${pending.total} proposal${pending.total === 1 ? "" : "s"} awaiting your decision`}
            {loading && pending !== null && <span className="text-t3"> · refreshing…</span>}
          </span>
        </div>
        <div className="flex shrink-0 items-center gap-[16px]">
          {/* Keyboard-triage hint — mirrors the per-row shortcut affordances.
              Hidden when there's nothing to triage. */}
          {pending && pending.total > 0 && (
            <div className="hidden items-center gap-[6px] font-mono text-[10.5px] text-t3 md:flex">
              <Kbd>j</Kbd><Kbd>k</Kbd><span>move</span>
              <span className="mx-[2px] h-[11px] w-px bg-line-2" />
              <span className="text-accent">r</span>oute · <span className="text-accent">e</span>vise · <span className="text-accent">x</span> reject · <span className="text-accent">s</span>kip
            </div>
          )}
          <span className="flex items-center gap-[8px] rounded-[9px] border border-accent-line bg-accent-soft py-[6px] pl-[13px] pr-[8px] text-[12px] font-semibold text-t1">
            Pending
            <span className="flex h-[18px] w-[18px] items-center justify-center rounded-full bg-accent text-[11px] font-bold leading-[14px] text-[#F4F8F7]">
              {pending?.total ?? 0}
            </span>
          </span>
          <button
            type="button"
            onClick={() => void onRefresh()}
            disabled={loading}
            className="text-[13px] leading-[16px] text-t2 transition-colors hover:text-t1 disabled:cursor-default disabled:opacity-50"
            title="Re-fetch pending proposals"
          >
            Refresh
          </button>
        </div>
      </div>

      {/* Filter pills — fixed taxonomy row (Paper C2F-0): always shown whenever
          there are pending proposals; "All" + the kinds actually present (we
          don't invent zero-data kinds). Each chip is label-only, no count. */}
      {pending && pending.total > 0 && (
        <div className="flex flex-wrap items-center gap-[7px]">
          <span className="text-[11px] leading-[14px] text-t3">Kind</span>
          <FilterChip
            label="All"
            count={pending.total}
            active={filterKind === null}
            onClick={() => setFilterKind(null)}
          />
          {allKinds.filter((k) => (pending.byKind[k] ?? 0) > 0).map((k) => (
            <FilterChip
              key={k}
              label={k}
              count={pending.byKind[k] ?? 0}
              active={filterKind === k}
              onClick={() => setFilterKind(filterKind === k ? null : k)}
              kind={k}
            />
          ))}
        </div>
      )}

      {/* Progress affordance — thin "N / total resolved" bar above the queue.
          Only meaningful once there's a queue or some session progress. */}
      {sessionTotal > 0 && (
        <div className="flex items-center gap-[12px]">
          <div className="h-[4px] flex-1 overflow-hidden rounded-[2px] bg-bg-2">
            <div
              className="h-full rounded-[2px] bg-green transition-[width] duration-[400ms] ease-out"
              style={{ width: `${progressPct}%` }}
            />
          </div>
          <span className="shrink-0 font-mono text-[11px] leading-[14px] text-t3">
            {resolvedCount} / {sessionTotal} resolved
          </span>
        </div>
      )}

      {/* Error state (top-level) */}
      {error && (
        <div className="rounded-[9px] border border-red/40 bg-red/10 px-[12px] py-[8px] text-[12px] text-red">
          {error}
        </div>
      )}

      {/* Loading skeleton (first load only) */}
      {pending === null && loading && (
        <div className="flex flex-col gap-[14px]">
          {[80, 90, 70].map((w, i) => (
            <div key={i} className="rounded-[14px] border border-line bg-bg-1 shadow-card h-[96px]"
                 style={{ width: `${w}%`, opacity: 0.5, animation: "pulse 1.8s ease-in-out infinite", animationDelay: `${i * 0.15}s` }} />
          ))}
        </div>
      )}

      {/* Empty state */}
      {pending && pending.total === 0 && !loading && (
        <div className="flex flex-col items-center justify-center py-[80px] text-t3">
          <div className="mb-[10px] flex size-[46px] items-center justify-center rounded-full bg-green/15 text-[22px] font-semibold text-green">✓</div>
          <div className="text-[13px]">No proposals pending review.</div>
          <div className="mt-[6px] text-[11px] text-t4">Routines + HiNotes will surface new items as they land.</div>
        </div>
      )}

      {/* Filtered-empty state */}
      {pending && pending.total > 0 && filteredItems.length === 0 && (
        <div className="py-[40px] text-center text-[12px] italic text-t3">
          No proposals match this filter.
        </div>
      )}

      {/* Grouped item list */}
      <div className="flex flex-col gap-[20px]">
        {grouped.map((g) => (
          <section key={g.kind}>
            {grouped.length > 1 && (
              <div className="mb-[9px] text-[10px] uppercase tracking-[0.14em] text-t3">
                {g.kind} · {g.items.length}
              </div>
            )}
            <div className="flex flex-col gap-[14px]">
              {g.items.map((it) => (
                <ProposalRow
                  key={it.id}
                  item={it}
                  isActioning={actioning.has(it.id)}
                  selected={selectedId === it.id}
                  body={selectedId === it.id && detail?.id === it.id ? detail.body : undefined}
                  bodyLoading={selectedId === it.id && detailLoading}
                  exiting={exitingId === it.id}
                  reduceMotion={reduceMotion}
                  onSelect={() => setSelectedId(it.id)}
                  error={rowErrors[it.id]}
                  workspaces={workspaces}
                  pickerOpen={pickerForId === it.id}
                  onPickerOpenChange={(open) => setPickerForId(open ? it.id : null)}
                  rejectOpen={rejectingId === it.id}
                  rejectReason={rejectingId === it.id ? rejectReason : ""}
                  revisionOpen={revisingId === it.id}
                  revisionFeedback={revisingId === it.id ? revisionFeedback : ""}
                  onRoute={(body) => handleRoute(it.id, body)}
                  onRejectOpen={() => openRejectPanel(it.id)}
                  onRejectCancel={cancelRejectPanel}
                  onRejectReasonChange={setRejectReason}
                  onRejectSubmit={() => submitReject(it.id)}
                  onRevisionOpen={() => openRevisionPanel(it.id)}
                  onRevisionCancel={cancelRevisionPanel}
                  onRevisionFeedbackChange={setRevisionFeedback}
                  onRevisionSubmit={() => submitRevision(it.id)}
                  onSkip={(d) => handleSkip(it.id, d)}
                  onDismissError={() => setRowError(it.id, null)}
                />
              ))}
            </div>
          </section>
        ))}
      </div>

      {/* Recently resolved (Paper 6DD-0) — this session's actions merged with the
          persistent /proposals/resolved feed (#inbox-resolved-feed). */}
      {(resolved.length > 0 || feedResolved.length > 0) && (
        <div className="mt-[6px] flex flex-col gap-[10px] border-t border-line pt-[16px]">
          <div className="text-[9.5px] font-bold uppercase tracking-[0.1em] leading-[12px] text-t2">Recently resolved</div>
          {dedupeById([...resolved, ...feedResolved]).slice(0, 8).map((r) => (
            <div key={r.id} className="flex items-center gap-[10px]">
              <span className={cn("inline-block shrink-0 rounded-[6px] px-[8px] py-[2px] text-[10px] font-semibold leading-[12px]", verdictClass(r.verdict))}>
                {verdictLabel(r.verdict)}
              </span>
              <span className="min-w-0 flex-1 truncate text-[12.5px] leading-[16px] text-t1">{r.title}</span>
              <span className="shrink-0 text-[12.5px] leading-[16px] text-t2">you · {r.when}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ── Sub-components ─────────────────────────────────────────────────────────

function ProposalRow({
  item, isActioning, selected, body, bodyLoading, exiting, reduceMotion, onSelect, error, workspaces,
  pickerOpen, onPickerOpenChange,
  rejectOpen, rejectReason,
  revisionOpen, revisionFeedback,
  onRoute, onRejectOpen, onRejectCancel, onRejectReasonChange, onRejectSubmit,
  onRevisionOpen, onRevisionCancel, onRevisionFeedbackChange, onRevisionSubmit,
  onSkip, onDismissError,
}: {
  item: PendingProposal;
  isActioning: boolean;
  /** Carries the keyboard-triage selection ring. */
  selected: boolean;
  /** #inbox-proposal-detail — the fetched write-up for the SELECTED card (else
   *  the card shows the generic decision-context blurb). */
  body?: string;
  bodyLoading?: boolean;
  /** Mid slide-out/collapse exit (post-verdict). */
  exiting: boolean;
  /** OS reduce-motion — skip the slide/collapse, just drop the row. */
  reduceMotion: boolean;
  /** Click-to-select the row as the keyboard cursor. */
  onSelect: () => void;
  error: string | undefined;
  workspaces: WorkspaceListItem[];
  /** Controlled workspace-picker open state (so `r` can open it). */
  pickerOpen: boolean;
  onPickerOpenChange: (open: boolean) => void;
  rejectOpen: boolean;
  rejectReason: string;
  revisionOpen: boolean;
  revisionFeedback: string;
  onRoute: (body: RouteProposalBody) => void;
  onRejectOpen: () => void;
  onRejectCancel: () => void;
  onRejectReasonChange: (v: string) => void;
  onRejectSubmit: () => void;
  onRevisionOpen: () => void;
  onRevisionCancel: () => void;
  onRevisionFeedbackChange: (v: string) => void;
  onRevisionSubmit: () => void;
  onSkip: (defer_days: number) => void;
  onDismissError: () => void;
}) {
  const rejectTextareaRef = useRef<HTMLTextAreaElement | null>(null);
  const revisionTextareaRef = useRef<HTMLTextAreaElement | null>(null);

  // Focus the textarea when the panel opens so the operator can type immediately.
  useEffect(() => {
    if (rejectOpen) {
      const t = setTimeout(() => rejectTextareaRef.current?.focus(), 30);
      return () => clearTimeout(t);
    }
  }, [rejectOpen]);

  useEffect(() => {
    if (revisionOpen) {
      const t = setTimeout(() => revisionTextareaRef.current?.focus(), 30);
      return () => clearTimeout(t);
    }
  }, [revisionOpen]);

  // #58-harness — Submit disabled until trimmed length > 0. Mirrors the
  // server-side `.strip()` check so the operator never hits the 422.
  const canSubmitReject = rejectReason.trim().length > 0 && !isActioning;
  const canSubmitRevision = revisionFeedback.trim().length > 0 && !isActioning;

  // Tier-aware placeholder copy — approval-tier rejects edit the canonical
  // vault layer, so flag the elevated discipline to the operator.
  const placeholder = item.tier === "approval"
    ? "Reason (required — this is an audit-critical promotion)"
    : "Reason (required)";
  // #58-harness2 — for revision-request, the prompt is "what should be fixed",
  // not "why am I dismissing". Reflect that in the copy.
  const revisionPlaceholder = item.tier === "approval"
    ? "Feedback (required — what should the source routine fix?)"
    : "Feedback (required)";

  // Sensitivity tier-bar — approval-tier writes the canonical vault layer
  // (audit-critical) → accent; confirmation-tier (lightweight publish) → sage.
  const tierBar = item.tier === "approval" ? "bg-accent" : "bg-green";

  // Exit animation (~360ms): opacity→0, translateX 52px, collapse max-height.
  // Driven entirely by CSS transition (no @keyframes). Under reduce-motion the
  // parent skips `exiting` and unmounts the row directly, so this never runs.
  // `max-h` toggles between a generous ceiling (CSS can't transition from
  // `none`, so we clamp to a value well above any realistic card+panel height)
  // and 0; the transition does the collapse. The steady-state ceiling never
  // clips because it's ~3× the tallest card with an open inline panel.
  const exitStyle: CSSProperties = exiting
    ? { opacity: 0, transform: "translateX(52px)", maxHeight: 0, marginTop: 0, marginBottom: 0, pointerEvents: "none" }
    : { opacity: isActioning ? 0.6 : 1, transform: "translateX(0)", maxHeight: 2000 };

  return (
    <div
      onClick={onSelect}
      style={!reduceMotion ? {
        ...exitStyle,
        transitionProperty: "opacity, transform, max-height, margin",
        transitionDuration: `${EXIT_MS}ms`,
        transitionTimingFunction: "cubic-bezier(.4,0,.2,1)",
      } : (isActioning ? { opacity: 0.6 } : undefined)}
      className={cn(
        "flex overflow-hidden rounded-[14px] border bg-bg-1 shadow-card",
        // Selection ring (keyboard cursor) — blue, layered over the base border.
        selected ? "border-accent-line ring-2 ring-accent/40" : "border-line",
        error && "ring-1 ring-red/40",
      )}
    >
      {/* Tier bar — Paper 6BE-0: solid 4px left rail (accent / sage by tier) */}
      <div className={cn("w-[4px] shrink-0", tierBar)} />
      <div className="min-w-0 grow basis-0">
        {/* Row head: kind tag · tier chip · path · date — Paper 6BG-0 header band */}
        <div className="flex items-center gap-[11px] py-[13px] px-[20px]">
          <KindTag kind={item.kind} />
          <TierChip tier={item.tier} />
          <span className="min-w-0 grow truncate font-mono text-[12px] leading-[16px] text-accent" title={item.path}>{item.path}</span>
          <span className="shrink-0 font-mono text-[11.5px] leading-[14px] text-t3">{item.date || "—"}</span>
        </div>
        <div className="h-px bg-line" />

        {/* Body — Paper 6BN-0: title + decision context on the left, action stack on the right */}
        <div className="flex items-start gap-[26px] px-[20px] py-[18px]">
          <div className="flex min-w-0 grow basis-0 flex-col gap-[9px]">
            <div className="text-[15.5px] font-semibold leading-[140%] text-t1">{item.title}</div>
            {/* #inbox-proposal-detail — the selected card inlines the real write-up
                (markdown), falling back to the generic decision-context blurb. */}
            {selected && body ? (
              <div className="max-h-[300px] overflow-y-auto rounded-[9px] border border-line bg-bg-2 px-[13px] py-[11px] text-[13px] leading-[160%] text-t2">
                <Markdown>{body}</Markdown>
              </div>
            ) : selected && bodyLoading ? (
              <p className="text-[12.5px] italic leading-[155%] text-t3">Loading the write-up…</p>
            ) : (
              <p className="text-[13px] leading-[155%] text-t2">{proposalContext(item)}</p>
            )}
            {error && (
              <div className="mt-[2px] flex items-baseline justify-between gap-[8px] rounded-[9px] border border-red/40 bg-red/10 px-[10px] py-[5px] text-[11px] text-red">
                <span className="flex-1">{error}</span>
                <button type="button" onClick={onDismissError} className="text-red hover:brightness-110">×</button>
              </div>
            )}
            {isActioning && <span className="text-[10.5px] italic text-t3">…in progress</span>}
          </div>

          {/* Action stack (Paper right column) — real controls, primary Route on top */}
          <div className="flex w-[168px] shrink-0 flex-col gap-[8px]">
            <RoutePicker
              block
              open={pickerOpen}
              onOpen={() => onPickerOpenChange(true)}
              onClose={() => onPickerOpenChange(false)}
              workspaces={workspaces}
              disabled={isActioning || rejectOpen || revisionOpen}
              onPick={(ws) => {
                onPickerOpenChange(false);
                onRoute({ workspace_type: ws.type, workspace_name: ws.name });
              }}
            />
            <ActionButton block keycap="E" label="Request revision" onClick={onRevisionOpen} variant="ghost" disabled={isActioning || rejectOpen || revisionOpen} />
            <ActionButton block keycap="X" label="Reject" onClick={onRejectOpen} variant="danger" disabled={isActioning || rejectOpen || revisionOpen} />
            <ActionButton block keycap="S" label="Skip 7d" onClick={() => onSkip(7)} variant="ghost" disabled={isActioning || rejectOpen || revisionOpen} />
            <ActionButton block label="Open in Obsidian" onClick={() => openInObsidian(item.path)} variant="text" disabled={isActioning} />
          </div>
        </div>

        {/* Reject panel — full-width below the body. Submit disabled until the
            textarea has non-whitespace content (mirrors the server's `.strip()`). */}
        {rejectOpen && (
          <div className="border-t border-line bg-bg-2/40 px-[20px] py-[16px]">
            <label className="mb-[7px] block text-[10px] uppercase tracking-[0.14em] text-t3">
              Reject reason <span className="text-red normal-case tracking-normal">(required · audit trail)</span>
            </label>
            <textarea
              ref={rejectTextareaRef}
              value={rejectReason}
              onChange={(e) => onRejectReasonChange(e.target.value)}
              onKeyDown={(e) => {
                // Enter submits (Shift+Enter = newline); Esc cancels. The global
                // triage handler ignores fields, so this is the only consumer.
                if (e.key === "Enter" && !e.shiftKey) {
                  e.preventDefault();
                  if (canSubmitReject) onRejectSubmit();
                } else if (e.key === "Escape") {
                  e.preventDefault();
                  onRejectCancel();
                }
              }}
              disabled={isActioning}
              rows={3}
              placeholder={placeholder}
              className="w-full resize-y rounded-[9px] border border-line-2 bg-bg-2 px-[11px] py-[8px] text-[12.5px] text-t1 outline-none transition-colors placeholder:text-t4 focus:border-accent-line disabled:opacity-60"
            />
            <div className="mt-[10px] flex items-baseline justify-end gap-[8px]">
              <ActionButton label="Cancel" onClick={onRejectCancel} variant="ghost" disabled={isActioning} />
              <ActionButton label={isActioning ? "Rejecting…" : "Reject"} onClick={onRejectSubmit} variant="danger" disabled={!canSubmitReject} title={canSubmitReject ? undefined : "Type a reason to enable"} />
            </div>
          </div>
        )}

        {/* Request-revision panel — full-width below the body. POSTs to
            /proposals/{id}/request-revision (writes a `.revision.json` sidecar
            that hides the row until the source routine re-fires; 409 = already
            pending, surfaced via the row error region above). */}
        {revisionOpen && (
          <div className="border-t border-line bg-bg-2/40 px-[20px] py-[16px]">
            <label className="mb-[7px] block text-[10px] uppercase tracking-[0.14em] text-t3">
              Revision feedback <span className="text-accent normal-case tracking-normal">(required · sent to source routine)</span>
            </label>
            <textarea
              ref={revisionTextareaRef}
              value={revisionFeedback}
              onChange={(e) => onRevisionFeedbackChange(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey) {
                  e.preventDefault();
                  if (canSubmitRevision) onRevisionSubmit();
                } else if (e.key === "Escape") {
                  e.preventDefault();
                  onRevisionCancel();
                }
              }}
              disabled={isActioning}
              rows={3}
              placeholder={revisionPlaceholder}
              className="w-full resize-y rounded-[9px] border border-line-2 bg-bg-2 px-[11px] py-[8px] text-[12.5px] text-t1 outline-none transition-colors placeholder:text-t4 focus:border-accent-line disabled:opacity-60"
            />
            <div className="mt-[10px] flex items-baseline justify-end gap-[8px]">
              <ActionButton label="Cancel" onClick={onRevisionCancel} variant="ghost" disabled={isActioning} />
              <ActionButton label={isActioning ? "Requesting…" : "Request revision"} onClick={onRevisionSubmit} variant="primary" disabled={!canSubmitRevision} title={canSubmitRevision ? undefined : "Type feedback to enable"} />
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

/** Tiny keycap for the triage hint cluster. */
function Kbd({ children }: { children: ReactNode }) {
  return (
    <span className="rounded-[4px] border border-line-2 px-[5px] py-px text-t2">{children}</span>
  );
}

function FilterChip({
  label, count, active, onClick, kind,
}: { label: string; count: number; active: boolean; onClick: () => void; kind?: ProposalKind }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "flex h-[26px] items-center rounded-[8px] border px-[11px] text-[11.5px] leading-[14px] transition-colors",
        active
          ? "border-accent-line bg-accent-soft font-medium text-t1"
          : "border-line-2 bg-bg-1 text-t2 hover:border-accent-line hover:text-t1",
      )}
      title={kind ? `${count} ${kind} proposal${count === 1 ? "" : "s"}` : `${count} pending`}
    >
      <span>{label}</span>
    </button>
  );
}

function KindTag({ kind }: { kind: ProposalKind }) {
  const cls = kindTagClass(kind);
  return (
    <span
      className={cn("inline-block shrink-0 rounded-[5px] border px-[8px] py-[3px] text-[9.5px] font-bold uppercase leading-[12px] tracking-[0.1em]", cls)}
    >{kind}</span>
  );
}

/** Tier badge — soft pill with a leading dot. Approval (audit-critical vault
 *  promotion) → accent; confirmation (lightweight publish) → sage/green.
 *  Matches Paper 6BG-0 / 6CS-0 chips. */
function TierChip({ tier }: { tier: ProposalTier }) {
  const approval = tier === "approval";
  return (
    <span
      className={cn(
        "inline-flex shrink-0 items-center gap-[5px] rounded-[5px] border py-[3px] pl-[7px] pr-[8px] text-[9px] font-bold uppercase leading-[12px] tracking-[0.07em]",
        approval
          ? "border-accent-line bg-accent-soft text-accent"
          : "border-[#4E9C6E57] bg-[#4E9C6E1F] text-green",
      )}
    >
      <span className={cn("h-[5px] w-[5px] rounded-[3px]", approval ? "bg-accent" : "bg-green")} />
      {approval ? "Approval" : "Confirmation"}
    </span>
  );
}

function ActionButton({
  label, onClick, variant, disabled, title, block, keycap,
}: { label: string; onClick: () => void; variant: "ghost" | "primary" | "danger" | "text"; disabled?: boolean; title?: string; block?: boolean; keycap?: string }) {
  // Paper 6BN-0 action stack: text-only buttons (Open in Obsidian) are a
  // centered 26px accent link with no border; everything else is a 36px pill.
  const textVariant = variant === "text";
  // With a keycap, the 36px pill switches from centered label to a
  // space-between row (label left, mono keycap badge right) — Paper mock.
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      title={title}
      className={cn(
        "transition-colors disabled:cursor-default disabled:opacity-50",
        block
          ? textVariant
            ? "flex h-[26px] w-full items-center justify-center text-center text-[12px] leading-[16px]"
            : keycap
              ? "flex h-[36px] w-full items-center justify-between rounded-[9px] px-[12px] text-[12.5px] leading-[16px]"
              : "flex h-[36px] w-full items-center justify-center rounded-[9px] text-[12.5px] leading-[16px]"
          : "rounded-[9px] px-[12px] py-[6px] text-[11.5px] tracking-[0.02em]",
        variant === "primary" ? "border border-accent-line bg-accent-soft font-semibold text-t1 hover:brightness-[1.03]"
          : variant === "danger" ? "border border-red/70 text-red hover:bg-red/10"
          : variant === "text"   ? "text-accent hover:brightness-110"
          : "border border-line-2 text-t1 hover:border-accent-line",
      )}
    >
      {keycap ? (
        <>
          <span>{label}</span>
          <span className="rounded-[4px] border border-line-2 px-[5px] py-[1px] font-mono text-[9px] text-t3">{keycap}</span>
        </>
      ) : label}
    </button>
  );
}

/** Slim inline picker — opens on click, lists every workspace prefixed by
 *  type tag (PRJ / BD / GEN), picks fire onPick. Closes on outside click. */
function RoutePicker({
  open, onOpen, onClose, workspaces, disabled, onPick, block,
}: {
  open: boolean;
  onOpen: () => void;
  onClose: () => void;
  workspaces: WorkspaceListItem[];
  disabled?: boolean;
  onPick: (ws: { type: WorkspaceType; name: string }) => void;
  block?: boolean;
}) {
  const ref = useRef<HTMLDivElement>(null);

  // Outside-click dismissal
  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) onClose();
    };
    window.addEventListener("mousedown", onDoc);
    return () => window.removeEventListener("mousedown", onDoc);
  }, [open, onClose]);

  return (
    <div ref={ref} className={cn("relative", block ? "block" : "inline-block")}>
      <ActionButton
        block={block}
        keycap="R"
        label="Route to…"
        onClick={() => (open ? onClose() : onOpen())}
        variant="primary"
        disabled={disabled}
      />
      {open && (
        <div
          className="absolute right-0 top-full z-30 mt-[5px] max-h-[300px] min-w-[260px] overflow-y-auto rounded-[9px] border border-line-2 bg-bg-1 shadow-card"
          onClick={(e) => e.stopPropagation()}
        >
          {workspaces.length === 0 ? (
            <div className="px-[12px] py-[10px] text-[11px] italic text-t3">— no workspaces yet —</div>
          ) : (
            workspaces.map((w) => (
              <button
                key={`${w.type}/${w.name}`}
                type="button"
                onClick={() => onPick({ type: w.type, name: w.name })}
                title={`${w.sourceRoot}\\${w.name}`}
                className="block w-full px-[12px] py-[7px] text-left text-[12px] text-t2 transition-colors hover:bg-bg-2 hover:text-t1"
              >
                <div className="flex items-baseline justify-between gap-[8px]">
                  <span className="flex items-baseline gap-[7px]">
                    <span className="rounded border border-line-2 px-[4px] py-[1px] text-[9.5px] uppercase tracking-[0.1em] text-t3">
                      {w.type === "project" ? "PRJ" : w.type === "bd" ? "BD" : "GEN"}
                    </span>
                    <span>{w.name}</span>
                  </span>
                </div>
              </button>
            ))
          )}
        </div>
      )}
    </div>
  );
}

// ── Helpers ────────────────────────────────────────────────────────────────

function groupByKind(items: PendingProposal[]): { kind: ProposalKind; items: PendingProposal[] }[] {
  const out: { kind: ProposalKind; items: PendingProposal[] }[] = [];
  for (const it of items) {
    let g = out.find((x) => x.kind === it.kind);
    if (!g) { g = { kind: it.kind, items: [] }; out.push(g); }
    g.items.push(it);
  }
  // Sort each group's items by date desc (string sort works for ISO YYYY-MM-DD)
  for (const g of out) g.items.sort((a, b) => (b.date || "").localeCompare(a.date || ""));
  return out;
}

// Paper 62D-0 renders the kind tag as a neutral hairline pill for every kind —
// the colour signal lives in the tier chip + the card's left rail, so the kind
// tag stays quiet (border-line2 / slate) and the inbox scans by tier.
function kindTagClass(_kind: ProposalKind): string {
  return "border-line-2 text-t2";
}

// Format an action error compactly for inline display.
function formatError(e: unknown, prefix: string): string {
  if (e instanceof ApiError) {
    if (e.status === 404) return `${prefix} — proposal not found (may have moved)`;
    // 409 here is contextual: route → destination collision; revision →
    // already-pending. The server's `detail` carries the specific text;
    // surface it verbatim instead of asserting one cause.
    if (e.status === 409) return `${prefix} — ${e.message}`;
    if (e.status === 422) return `${prefix} — ${e.message}`;
    return `${prefix} — ${e.status}: ${e.message}`;
  }
  if (e instanceof Error) return `${prefix} — ${e.message}`;
  return prefix;
}

// Build an obsidian:// URL that opens the file in the operator's vault.
// Vault name is "OS AI Vault" per profile.md.
function openInObsidian(vaultRelativePath: string): void {
  const url = `obsidian://open?vault=OS%20AI%20Vault&file=${encodeURIComponent(vaultRelativePath)}`;
  window.open(url, "_blank", "noopener,noreferrer");
}

// Decision context composed from the fields the pending payload carries (it has
// no body). Per-kind blurb + a tier-aware note on what routing does; the full
// write-up lives in the source note (#inbox-proposal-detail tracks a real body
// endpoint to inline that text).
const KIND_BLURB: Partial<Record<ProposalKind, string>> = {
  "hinotes-unrouted":  "A meeting note (HiNotes) that hasn't been filed yet — route it to the project or BD it belongs to.",
  "memory-promotion":  "A memory candidate proposed for promotion into the canonical vault memory layer.",
  "lessons-learned":   "A lessons-learned entry extracted from recent work, proposed for the canonical record.",
  "sector-extraction": "Sector intelligence extracted from sources, proposed for the sector knowledge tree.",
  "sector-synthesis":  "A synthesised sector view, proposed for the canonical sector record.",
  "learning":          "A learning candidate surfaced from recent activity for your review.",
};

function proposalContext(item: PendingProposal): string {
  const what = KIND_BLURB[item.kind] ?? "A routine surfaced this note for your review.";
  const tier = item.tier === "approval"
    ? "Routing it writes to the canonical vault layer — audit-critical, and a reason is required to reject."
    : "This is lightweight routing — it won't touch the canonical vault layer.";
  return `${what} ${tier} Open the source for the full write-up.`;
}

// ── Recently-resolved helpers (session-local; #inbox-resolved-feed) ──────────
function hhmm(): string {
  const d = new Date();
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

/** Resolution timestamp → a compact rail label: "HH:MM" for today, else "23 Jun".
 *  (#inbox-resolved-feed spans days, unlike the session-local hh:mm.) */
function formatResolvedWhen(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const now = new Date();
  const sameDay =
    d.getFullYear() === now.getFullYear() &&
    d.getMonth() === now.getMonth() &&
    d.getDate() === now.getDate();
  if (sameDay) {
    return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
  }
  return d.toLocaleDateString("en-GB", { day: "numeric", month: "short" });
}

/** Stable first-wins de-dupe by id (feed history + this session's optimistic
 *  entries can name the same proposal). */
function dedupeById<T extends { id: string }>(list: T[]): T[] {
  const seen = new Set<string>();
  const out: T[] = [];
  for (const item of list) {
    if (seen.has(item.id)) continue;
    seen.add(item.id);
    out.push(item);
  }
  return out;
}

function verdictLabel(v: ResolvedVerdict): string {
  return v === "routed" ? "ROUTED" : v === "rejected" ? "REJECTED" : v === "revision" ? "REVISION" : "SKIPPED";
}

// Paper 6DF-0 / 6DK-0 / 6DP-0 verdict pills — translucent tone fills.
function verdictClass(v: ResolvedVerdict): string {
  return (
    v === "routed"   ? "bg-[#6FC79F24] text-green"   :
    v === "rejected" ? "bg-[#D98A7824] text-red"     :
    v === "revision" ? "bg-accent-soft text-accent"  :
    /* skipped */      "bg-[#D6B36A24] text-amber"
  );
}
