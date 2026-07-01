import { useCallback, useEffect, useRef, useState } from "react";
import { Crosshair, RotateCw, ExternalLink, ArrowUp, ChevronRight, ChevronDown } from "lucide-react";
import { cn } from "../lib/cn";
import { api, ApiError, StreamUnavailableError } from "../lib/api";
import { Chip } from "./ui/Chip";
import type { ChatSource, ChatTurn, WorkspaceType } from "../types";

/**
 * Project chat — per-deal conversational memory (OUTSTANDING #42 / plan §6.6).
 *
 * Right-rail panel scoped STRICTLY to the currently-selected project/deal.
 * Shares the panel-shell visual grammar of MorningBriefPanel / DailyDigestPanel
 * (panel box + bordered header + amber accent edge; IBM-Plex-Mono v5 build) but
 * renders a live conversation rather than a static 3-column digest.
 *
 * Behaviour:
 *   • On mount / project change: GET /api/projects/{code}/chat/history → render
 *     the stored thread. History is best-effort — a 404 (or fail-soft empty log)
 *     degrades to an empty thread, not a loud error.
 *   • Composer STREAMS via POST /api/projects/{code}/chat/stream (#42 v2). The
 *     user turn is appended optimistically; the assistant answer fills a live
 *     in-progress bubble token-by-token as `delta` events arrive; the terminal
 *     `done` event swaps it for the canonical persisted turn + sources footer +
 *     meta line. A `done`/`error` is terminal — the backend appends both turns
 *     ATOMICALLY only on success, so on any `error` event (or stream fault)
 *     nothing was persisted: roll the optimistic turn back + restore the draft.
 *     If streaming isn't available on this bridge (old build → non-SSE / 404),
 *     fall back to the one-shot POST /api/projects/{code}/chat.
 *   • An in-flight stream is ABANDONED on a mid-stream deal switch / unmount
 *     (AbortController fired from the deal-switch effect cleanup) and ignored if
 *     it resolves after the deal changed (projectRef + mountedRef + reqSeq
 *     guards, same as the history loader).
 *
 * v1 operator decisions (locked):
 *   • Citations: expandable FOOTER only (no inline superscripts) — click
 *     "N sources" to reveal each source's wikilink path + score + excerpt.
 *   • No token cap (Ollama is local/free).
 *
 * v2 fast-follows (#42 v2, landed 2026-06-04):
 *   • Cross-projects toggle (off by default). When ON, the turn sends
 *     `cross_projects: true` and recall widens to the WHOLE vault; out-of-deal
 *     content is capped at ≤ internal sensitivity SERVER-side (the current deal
 *     stays full tier; confidential/MNPI from other deals never surface). A
 *     cross-scope answer is marked on its assistant bubble so the operator sees
 *     when it drew on other deals / the general vault.
 *   • Cmd-K `/chat` mode routes here + auto-focuses the composer (`focusSignal`
 *     bumps from App when the command fires — see CommandModal `/chat`).
 *
 * Header buttons: refresh history; open `_chat.md` in Obsidian.
 *
 * NOTE: these endpoints go live only after the operator restarts the bridge
 * (§10 — bridge lifecycle is operator-owned). Until then the panel shows its
 * quiet offline/empty state.
 */

interface Props {
  /** Selected deal/project code (== workspace.name). Chat is scoped to this. */
  project: string;
  /** Workspace type — chat is project-only; BD/general show a scope notice. */
  workspaceType?: WorkspaceType;
  /** #42 v2 — Cmd-K `/chat` focus signal. A monotonic counter from App; each
   *  bump (a `/chat` command) focuses the composer. 0/undefined → no auto-focus
   *  (a manual Chat-tab click must NOT steal focus). */
  focusSignal?: number;
}

/** A rendered turn — a ChatTurn plus a client-only flag marking whether the turn
 *  ran under cross-project scope. History-loaded turns carry no flag (the scope
 *  isn't persisted in `_chat.md`); only turns produced this session are marked. */
type DisplayTurn = ChatTurn & { crossProjects?: boolean };

export function ProjectChatPanel({ project, workspaceType, focusSignal }: Props) {
  const isProject = workspaceType === undefined || workspaceType === "project";

  const [turns, setTurns]               = useState<DisplayTurn[]>([]);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [historyError, setHistoryError] = useState<string | null>(null);

  const [input, setInput]               = useState("");
  const [sending, setSending]           = useState(false);
  const [sendError, setSendError]       = useState<string | null>(null);
  // Live streaming state (#42 v2). `streaming` flips true once the first delta
  // (or `start`) lands; `streamingText` is the in-progress assistant answer
  // rendered into a transient bubble until the terminal `done` swaps in the
  // canonical persisted turn. Both reset on a deal switch + each new send.
  const [streaming, setStreaming]       = useState(false);
  const [streamingText, setStreamingText] = useState("");
  // Response-only stats for the most recent turn (recall_hits + duration_ms are
  // not carried on a ChatTurn, so they live here and reset on history reload).
  const [lastMeta, setLastMeta] = useState<{ recall_hits: number; duration_ms: number } | null>(null);
  // #42 v2 — relaxed-scope toggle. OFF by default (strict project scope); resets
  // to OFF on every deal switch so cross-project reach is an explicit, per-deal
  // opt-in (conservative default for a confidentiality boundary). Captured at
  // send-time so an in-flight turn keeps the scope it was sent with.
  const [crossProjects, setCrossProjects] = useState(false);

  const threadRef = useRef<HTMLDivElement>(null);
  // Composer textarea — focused by the Cmd-K `/chat` focusSignal effect below.
  const composerRef = useRef<HTMLTextAreaElement>(null);

  // Monotonic request id + mounted flag. A newer history load OR a send bumps
  // the id, so any earlier-started history GET discards its (now stale) result
  // instead of clobbering newer state. Guards both codex SEV-2 races:
  //   (1) a refresh that lands around an in-flight send dropping/duplicating a
  //       turn, and (2) a stale/after-unmount manual refresh writing state.
  const reqSeqRef  = useRef(0);
  const mountedRef = useRef(true);
  // Set true on setup AND false on cleanup. StrictMode (dev) runs the effect
  // setup → cleanup → setup; a cleanup-ONLY effect would leave this false forever
  // after the simulated unmount, making every guard bail (history/send/409 all
  // swallowed, composer stuck) — codex R4 SEV-2.
  useEffect(() => {
    mountedRef.current = true;
    return () => { mountedRef.current = false; };
  }, []);

  // Latest rendered deal, readable inside async send closures. The panel stays
  // mounted across tab swaps AND deal switches (App keeps it alive + CSS-hidden),
  // so a send that resolves after the operator switches deals must be abandoned
  // rather than bleed A's response into B's thread (codex SEV-2). Assigned
  // SYNCHRONOUSLY during render — NOT in a passive effect — so there is no
  // commit-to-effect window where an async POST could observe the prior deal
  // (codex R3 SEV-2). This is a read-only "latest value" mirror (we never read
  // it during render), the canonical use-ref-during-render exception.
  const projectRef = useRef(project);
  projectRef.current = project;

  // Controller for the in-flight SSE stream. A deal switch / unmount aborts it
  // (see the deal-switch effect cleanup) so an abandoned stream stops reading
  // and never bleeds into the new deal's thread. Null when no stream is open.
  const streamAbortRef = useRef<AbortController | null>(null);

  // ── History load — the SINGLE guarded loader shared by the mount effect AND
  //    the Refresh button (codex SEV-2 #2: one guarded path, not two). ─────────
  const loadHistory = useCallback(async () => {
    if (!project || !isProject) { setTurns([]); return; }
    const seq = ++reqSeqRef.current;
    setHistoryLoading(true);
    setHistoryError(null);
    try {
      const res = await api.projectChatHistory(project);
      if (!mountedRef.current || seq !== reqSeqRef.current) return;  // superseded by a newer load or a send
      setTurns(res.turns);
    } catch (e) {
      if (!mountedRef.current || seq !== reqSeqRef.current) return;
      // 404 = no project / no log yet → quiet empty thread, not an error chip.
      if (e instanceof ApiError && e.status === 404) setTurns([]);
      else setHistoryError(formatErr(e));
    } finally {
      // Always clear this request's spinner (a superseding request manages its
      // own), so a send-during-load can never leave it stuck.
      if (mountedRef.current) setHistoryLoading(false);
    }
  }, [project, isProject]);

  // Load on mount + when the selected deal changes. `loadHistory` is memoised on
  // [project, isProject], so this fires on a deal switch but NOT on a tab swap
  // (which keeps a typed draft intact). Clear the prior deal's thread/draft/
  // transient state first so deal A's content never shows under deal B's header.
  useEffect(() => {
    setTurns([]);
    setInput("");
    setSendError(null);
    setLastMeta(null);
    setStreaming(false);
    setStreamingText("");
    setCrossProjects(false);   // re-opt-in per deal (conservative scope default)
    void loadHistory();
    // Cleanup runs before the next deal's effect AND on unmount: abandon any
    // in-flight stream so a mid-stream deal switch stops reading immediately
    // (defence-in-depth on top of the projectRef/mountedRef guards in `send`).
    return () => { streamAbortRef.current?.abort(); };
  }, [loadHistory]);

  // ── Cmd-K `/chat` auto-focus (#42 v2 Feature A) ─────────────────────────────
  // App bumps `focusSignal` when the `/chat` command routes here; focus the
  // composer after the next paint — by then the command modal has torn down and
  // the (keep-mounted) chat tab is visible, so the composer is focusable even
  // when `/chat <project>` switched the deal in the same commit. A 0/undefined
  // signal is a normal mount / manual tab open — do NOT grab focus then.
  // Independent of the history/send guards above.
  useEffect(() => {
    if (!focusSignal || !isProject) return;
    const raf = requestAnimationFrame(() => composerRef.current?.focus());
    return () => cancelAnimationFrame(raf);
  }, [focusSignal, isProject]);

  // Auto-scroll the thread to the newest turn (after history load, each send,
  // and as streaming deltas grow the in-progress bubble).
  useEffect(() => {
    const el = threadRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [turns, sending, streaming, streamingText]);

  // ── Send a turn (streaming, with non-stream fallback) ───────────────────────
  const send = async () => {
    const message = input.trim();
    if (!message || sending || !isProject) return;
    const sentProject = project;   // the deal this turn belongs to
    const sentCross = crossProjects;   // scope captured at send-time (the toggle
    // may flip before this turn resolves; the in-flight turn keeps its scope).

    // A send mutates `turns` authoritatively — bump the request id so any history
    // GET still in flight discards its result (codex SEV-2 #1), and clear the
    // history spinner so a slow/hung superseded GET can't leave it stuck
    // (codex SEV-3). Refresh is also disabled while sending.
    ++reqSeqRef.current;
    setHistoryLoading(false);

    // Abort any prior stream (shouldn't be one — `sending` gates re-entry — but
    // belt-and-braces) and open a fresh controller for this send.
    streamAbortRef.current?.abort();
    const ac = new AbortController();
    streamAbortRef.current = ac;

    // Optimistic user turn (object identity used for rollback on error).
    const optimistic: ChatTurn = {
      timestamp: new Date().toISOString(),
      role: "user",
      text: message,
      sources: [],
    };
    setTurns((prev) => [...prev, optimistic]);
    setInput("");
    setSendError(null);
    setSending(true);
    setStreaming(false);
    setStreamingText("");

    // True once this send no longer owns the view (deal switched / unmounted).
    // projectRef is assigned synchronously during render, so this is correct the
    // instant the operator switches deals — no commit-to-effect window.
    const stale = () => !mountedRef.current || projectRef.current !== sentProject;

    // Shared rollback for a terminal failure: nothing persisted server-side, so
    // drop the optimistic user turn + restore the draft so a retry is clean.
    const rollback = (errMsg: string) => {
      setTurns((prev) => prev.filter((t) => t !== optimistic));
      setStreaming(false);
      setStreamingText("");
      setInput(message);
      setSendError(errMsg);
    };

    try {
      let acc = "";
      await api.projectChatStream(
        sentProject,
        { project: sentProject, message, cross_projects: sentCross },
        {
          onStart: () => { if (!stale()) setStreaming(true); },
          onDelta: (e) => {
            // Deal switched mid-stream → abandon + stop reading. The cleanup
            // effect also aborts, but abort here too in case a delta races it.
            if (stale()) { ac.abort(); return; }
            acc += e.text;
            setStreaming(true);
            setStreamingText(acc);
          },
          onDone: (e) => {
            if (stale()) return;
            // Swap the live bubble for the canonical persisted turn (carries the
            // server timestamp + sources footer) + the meta line. Mark the turn
            // with the scope the SERVER ran it under (fall back to the sent flag).
            setTurns((prev) => [...prev, { ...e.turn, crossProjects: e.cross_projects ?? sentCross }]);
            setLastMeta({ recall_hits: e.recall_hits, duration_ms: e.duration_ms });
            setStreaming(false);
            setStreamingText("");
          },
          onError: (e) => {
            if (stale()) return;
            // Server-side terminal failure — nothing was persisted. corrupt_log
            // mirrors the non-stream 409 wording; everything else is generic.
            rollback(
              e.code === "corrupt_log"
                ? `Chat log unreadable — ${e.message}`
                : `Send failed — ${e.message}`,
            );
          },
        },
        ac.signal,
      );
    } catch (e) {
      if (e instanceof StreamUnavailableError) {
        // Streaming endpoint not on this bridge → one-shot non-stream fallback.
        await sendNonStream(message, optimistic, sentProject, sentCross, stale);
        return;
      }
      // Aborted (deal switch / unmount) → abandoned; leave state to the new
      // view. AbortError is a DOMException (NOT an Error subclass in browsers),
      // so match on the name rather than instanceof.
      if ((e as { name?: unknown })?.name === "AbortError") return;
      if (stale()) return;
      // Network fault on the stream itself (not a server `error` event).
      rollback(`Send failed — ${formatErr(e)}`);
    } finally {
      if (streamAbortRef.current === ac) streamAbortRef.current = null;
      // Always clear the busy flag for the live instance so the composer unlocks
      // (even if the deal was switched mid-send).
      if (mountedRef.current) setSending(false);
    }
  };

  // Non-stream fallback (old bridge): one-shot POST. Mirrors the pre-#42-v2
  // behaviour exactly — optimistic turn already appended; append the response
  // turn on success, roll back + restore draft on error (incl. the 409 corrupt
  // -log wording). Kept as a closure so it shares the live setters + guards.
  const sendNonStream = async (
    message: string,
    optimistic: ChatTurn,
    sentProject: string,
    sentCross: boolean,
    stale: () => boolean,
  ) => {
    try {
      const res = await api.projectChat(sentProject, { project: sentProject, message, cross_projects: sentCross });
      if (stale()) return;
      setTurns((prev) => [...prev, { ...res.turn, crossProjects: res.cross_projects ?? sentCross }]);
      setLastMeta({ recall_hits: res.recall_hits, duration_ms: res.duration_ms });
    } catch (e) {
      if (stale()) return;
      setTurns((prev) => prev.filter((t) => t !== optimistic));
      setStreaming(false);
      setStreamingText("");
      setInput(message);
      if (e instanceof ApiError && e.status === 409) {
        setSendError(`Chat log unreadable — ${e.message}`);
      } else if (e instanceof ApiError) {
        setSendError(`Send failed (${e.status}) — ${e.message}`);
      } else {
        setSendError(`Send failed — ${formatErr(e)}`);
      }
    }
  };

  const onKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    // Enter sends; Shift+Enter inserts a newline.
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      void send();
    }
  };

  // ── Non-project workspace: explicit scope notice, no demo leak ─────────────
  if (!isProject) {
    return (
      <section className="flex flex-col">
        <Header project={project} onRefresh={loadHistory} disabled />
        <div className="py-[14px] text-[12px] leading-[1.55] text-[#D8EAE8]">
          <div className="mb-[4px] font-medium text-[#F4F8F7]">Project chat is deal-scoped.</div>
          Switch to a project workspace to ask questions against its vault folder.
        </div>
      </section>
    );
  }

  return (
    <section className="flex flex-col">
      <Header
        project={project}
        onRefresh={loadHistory}
        refreshing={historyLoading}
        // Block a manual refresh mid-send: a GET that lands around the POST's
        // atomic append can momentarily drop/duplicate the in-flight pair.
        refreshDisabled={sending}
      />

      {/* Thread */}
      <div
        ref={threadRef}
        className="flex max-h-[440px] min-h-[120px] flex-col gap-[12px] overflow-y-auto py-[14px]"
      >
        {historyError && (
          <div className="rounded-lg border border-accent-line bg-accent-soft px-[11px] py-[8px] text-[11px] leading-[1.5] text-accent">
            History unavailable — {historyError}
          </div>
        )}

        {!historyLoading && !historyError && turns.length === 0 && (
          <div className="rounded-lg border border-white/15 bg-white/5 px-[13px] py-[12px] text-[12px] italic leading-[1.55] text-[#D8EAE8]">
            No conversation yet. Ask a question about{" "}
            <span className="font-mono not-italic text-[#F4F8F7]">{project}</span>{" "}
            — answers run project-filtered recall over the deal's vault folder and
            persist to <span className="font-mono not-italic text-[#C8DEDC]">_chat.md</span>.
          </div>
        )}

        {historyLoading && turns.length === 0 && (
          <div className="text-[12px] italic text-[#C8DEDC]">Loading history…</div>
        )}

        {turns.map((t, i) => (
          <TurnBubble key={`${t.timestamp}-${t.role}-${i}`} turn={t} />
        ))}

        {/* Live answer bubble while streaming; the plain "thinking…" placeholder
            covers the gap before the first delta (prompt-eval time). */}
        {streaming ? (
          <StreamingBubble text={streamingText} />
        ) : sending ? (
          <div className="flex items-baseline gap-[8px]">
            <RoleTag role="assistant" />
            <span className="text-[12px] italic text-[#C8DEDC]">thinking…</span>
          </div>
        ) : null}
      </div>

      {/* Composer */}
      <div className="border-t border-white/15 pt-[12px]">
        {sendError && (
          <div className="mb-[10px] rounded-lg border border-red/40 bg-red/10 px-[11px] py-[7px] text-[11px] leading-[1.45] text-red">
            {sendError}
          </div>
        )}

        {/* Cross-projects scope toggle (#42 v2). OFF = strict deal scope; ON
            widens recall to the WHOLE vault with out-of-deal content capped at
            ≤ internal sensitivity (confidential/MNPI from other deals never
            surface; the current deal stays full-tier). Resets to OFF per deal. */}
        <div className="mb-[10px] flex items-center justify-between gap-[8px]">
          <button
            type="button"
            onClick={() => setCrossProjects((v) => !v)}
            aria-pressed={crossProjects}
            title="Widen recall beyond this deal to the whole vault. Out-of-deal notes are capped at ≤ internal sensitivity — confidential / MNPI from other deals never surface. The current deal stays full-tier."
            className={cn(
              "inline-flex cursor-pointer items-center gap-[5px] rounded-lg border px-[10px] py-[5px] text-[11px] transition-colors",
              crossProjects
                ? "border-accent-line bg-accent-soft text-[#F4F8F7]"
                : "border-white/20 bg-transparent text-[#C8DEDC] hover:border-white/40 hover:text-[#F4F8F7]",
            )}
          >
            <Crosshair size={12} className="shrink-0" />
            Cross-projects
            <span className={cn("tabular font-medium", crossProjects ? "text-[#F4F8F7]" : "text-[#9FB8B6]")}>
              {crossProjects ? "ON" : "OFF"}
            </span>
          </button>
          {crossProjects && (
            <span className="text-[10.5px] leading-[1.35] text-[#9FB8B6]">
              whole vault · out-of-deal ≤ internal
            </span>
          )}
        </div>

        <div className="flex items-end gap-[8px]">
          <textarea
            ref={composerRef}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={onKeyDown}
            rows={2}
            placeholder={`Ask about ${project}…`}
            aria-label={`Ask a question about ${project}`}
            disabled={sending}
            className="min-h-[40px] flex-1 resize-none rounded-lg border border-white/15 bg-white/10 px-[11px] py-[8px] text-[12.5px] leading-[1.5] text-[#F4F8F7] outline-none transition-colors placeholder:text-white/40 focus:border-white/40 disabled:opacity-60"
          />
          <button
            type="button"
            onClick={() => void send()}
            disabled={sending || input.trim() === ""}
            aria-label="Send"
            className={cn(
              "flex h-[40px] w-[40px] shrink-0 items-center justify-center rounded-lg transition-colors",
              sending || input.trim() === ""
                ? "cursor-default border border-white/15 bg-white/5 text-white/40"
                : "cursor-pointer bg-accent text-white hover:brightness-110",
            )}
          >
            {sending ? <RotateCw size={15} className="animate-spin" /> : <ArrowUp size={16} />}
          </button>
        </div>

        <div className="mt-[8px] flex items-center justify-between text-[10.5px] text-[#9FB8B6]">
          <span>Enter to send · Shift+Enter for newline</span>
          {lastMeta && (
            <span className="tabular text-[#C8DEDC]" title="Recall hits + synthesis time for the last turn">
              {lastMeta.recall_hits} recall {lastMeta.recall_hits === 1 ? "hit" : "hits"} · {formatMs(lastMeta.duration_ms)}
            </span>
          )}
        </div>
      </div>
    </section>
  );
}

// ── Header ───────────────────────────────────────────────────────────────────

function Header({
  project, onRefresh, refreshing = false, refreshDisabled = false, disabled = false,
}: {
  project: string;
  onRefresh: () => void;
  refreshing?: boolean;
  refreshDisabled?: boolean;
  disabled?: boolean;
}) {
  return (
    <div className="flex items-baseline justify-between border-b border-white/15 pb-[10px]">
      <h3 className="flex items-baseline gap-[10px] text-[10px] font-semibold uppercase tracking-[0.14em] text-[#C8DEDC]">
        Project chat
        <span className="font-mono text-[11.5px] normal-case tracking-[0.02em] text-[#F4F8F7]">
          {project}
        </span>
      </h3>
      <div className="flex items-center gap-[10px] text-[11px]">
        <button
          type="button"
          onClick={onRefresh}
          disabled={disabled || refreshing || refreshDisabled}
          title="Refresh history"
          className="inline-flex cursor-pointer items-center gap-[5px] text-[#C8DEDC] transition-colors hover:text-[#F4F8F7] disabled:cursor-default disabled:opacity-50"
        >
          <RotateCw size={13} className={cn("shrink-0", refreshing && "animate-spin")} />
          Refresh
        </button>
        {!disabled && (
          <button
            type="button"
            onClick={() => openChatInObsidian(project)}
            title={`Open Projects/${project}/_chat.md in Obsidian`}
            className="inline-flex cursor-pointer items-center gap-[5px] text-[#C8DEDC] transition-colors hover:text-[#F4F8F7]"
          >
            <ExternalLink size={13} className="shrink-0" />
            Obsidian
          </button>
        )}
      </div>
    </div>
  );
}

// ── Turn bubble ──────────────────────────────────────────────────────────────

function TurnBubble({ turn }: { turn: DisplayTurn }) {
  const isUser = turn.role === "user";
  return (
    <div className={cn("flex flex-col gap-[5px]", isUser && "items-end")}>
      <div className="flex items-baseline gap-[8px]">
        <RoleTag role={turn.role} />
        <span className="font-mono text-[10px] tabular tracking-[0.02em] text-[#C8DEDC]">
          {formatTime(turn.timestamp)}
        </span>
        {/* #42 v2 — cross-scope marker: this answer drew on the whole vault
            (out-of-deal content capped at ≤ internal). Shown only on turns
            produced this session (the scope isn't persisted in _chat.md). */}
        {turn.role === "assistant" && turn.crossProjects && (
          <span title="This answer drew on cross-project scope — the whole vault, with out-of-deal content capped at ≤ internal sensitivity (no confidential/MNPI from other deals).">
            <Chip
              variant="accent"
              icon={Crosshair}
              label="cross-project"
              className="px-[6px] py-[1px] text-[9px] uppercase tracking-[0.08em]"
            />
          </span>
        )}
      </div>
      <div
        className={cn(
          "max-w-[92%] whitespace-pre-wrap rounded-xl px-[12px] py-[9px] text-[12.5px] leading-[1.55] text-[#F4F8F7]",
          isUser
            ? "bg-white/10"
            : "border border-white/20 bg-white/5",
        )}
      >
        {turn.text || <span className="text-[#C8DEDC]">—</span>}
      </div>
      {turn.role === "assistant" && turn.sources.length > 0 && (
        <SourcesFooter sources={turn.sources} />
      )}
    </div>
  );
}

// ── Live streaming bubble (#42 v2) ────────────────────────────────────────────
// Renders the in-progress assistant answer as deltas arrive. Mirrors the
// assistant TurnBubble's bubble styling, plus a "streaming…" tag + a blinking
// caret. Replaced by the canonical TurnBubble on the terminal `done` event.
function StreamingBubble({ text }: { text: string }) {
  return (
    <div className="flex flex-col gap-[5px]">
      <div className="flex items-baseline gap-[8px]">
        <RoleTag role="assistant" />
        <span className="font-mono text-[10px] tracking-[0.02em] text-[#C8DEDC]">streaming…</span>
      </div>
      <div className="max-w-[92%] whitespace-pre-wrap rounded-xl border border-white/20 bg-white/5 px-[12px] py-[9px] text-[12.5px] leading-[1.55] text-[#F4F8F7]">
        {text}
        <span className="ml-[1px] inline-block animate-pulse text-accent" aria-hidden="true">▋</span>
      </div>
    </div>
  );
}

function RoleTag({ role }: { role: ChatTurn["role"] }) {
  const isUser = role === "user";
  return (
    <span
      className={cn(
        "font-mono text-[10px] font-semibold uppercase tracking-[0.12em]",
        isUser ? "text-[#F4F8F7]" : "text-accent",
      )}
    >
      {isUser ? "Operator" : "ANTON"}
    </span>
  );
}

// ── Expandable citation footer (v1 decision: footer-only, no inline marks) ────

function SourcesFooter({ sources }: { sources: ChatSource[] }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="w-full">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="inline-flex cursor-pointer items-center gap-[4px] text-[10.5px] uppercase tracking-[0.08em] text-[#C8DEDC] transition-colors hover:text-[#F4F8F7]"
      >
        {open ? <ChevronDown size={12} className="shrink-0" /> : <ChevronRight size={12} className="shrink-0" />}
        {sources.length} {sources.length === 1 ? "source" : "sources"}
      </button>
      {open && (
        <div className="mt-[8px] flex flex-col gap-[9px] border-l-2 border-white/20 pl-[12px]">
          {sources.map((s, i) => (
            <div key={`${s.path}-${i}`}>
              <div className="flex items-baseline justify-between gap-[8px]">
                <button
                  type="button"
                  onClick={() => openInObsidian(s.path)}
                  title={`Open ${s.path} in Obsidian`}
                  className="min-w-0 flex-1 cursor-pointer truncate text-left font-mono text-[11px] text-[#C8DEDC] hover:text-[#F4F8F7] hover:underline"
                >
                  [[{s.path}]]
                </button>
                <span className="shrink-0 font-mono text-[10.5px] tabular text-[#C8DEDC]">
                  {s.score.toFixed(2)}
                </span>
              </div>
              {s.excerpt && (
                <div className="mt-[3px] text-[11px] leading-[1.5] text-[#D8EAE8]">
                  {s.excerpt}
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ── Helpers ──────────────────────────────────────────────────────────────────

/** Open a vault-relative file in the operator's Obsidian vault ("OS AI Vault",
 *  per InboxTab). Sources carry vault-relative POSIX paths already. */
function openInObsidian(vaultRelativePath: string): void {
  const url = `obsidian://open?vault=OS%20AI%20Vault&file=${encodeURIComponent(vaultRelativePath)}`;
  window.open(url, "_blank", "noopener,noreferrer");
}

function openChatInObsidian(project: string): void {
  openInObsidian(`Projects/${project}/_chat.md`);
}

function formatErr(e: unknown): string {
  if (e instanceof ApiError) return `${e.status}: ${e.message}`;
  if (e instanceof Error) return e.message;
  return "unknown error";
}

/** ISO timestamp → local HH:MM. Falls back to the raw string if unparseable. */
function formatTime(iso: string): string {
  const d = new Date(iso);
  if (!Number.isFinite(d.getTime())) return iso;
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function formatMs(ms: number): string {
  if (ms < 1000) return `${ms}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}
