import { useEffect, useMemo, useRef, useState } from "react";
import { ArrowLeft, CornerDownLeft, Search, X } from "lucide-react";
import { cn } from "../lib/cn";
import { api, ApiError } from "../lib/api";
import { Chip } from "./ui/Chip";
import { IconButton } from "./ui/IconButton";
import type { RecallResponse, WorkflowKey } from "../types";

/**
 * Cmd-K omnibox. ⌘K / Ctrl-K opens it; Esc closes. Type a slash command
 * (or bare text → defaults to /recall) to fire a workflow. Inline result
 * rendering for /recall; everything else dispatches via the parent's
 * `onFire(key)` and bubbles result into RunResultPanel.
 */

export type CommandIntent =
  | { kind: "fire"; key: WorkflowKey; promptText?: string }
  | { kind: "recall"; query: string; project?: string; max_sensitivity?: SensLevel; limit?: number }
  // #42 v2 Feature A — route to the right-rail Project chat tab for the selected
  // deal (or the named `<project>`). Dispatched to the parent via `onChat`.
  | { kind: "chat"; project?: string };

type SensLevel = "public" | "internal" | "confidential" | "MNPI";

// Section buckets for the grouped results list (v2 palette AOX-0). The flat
// COMMANDS order is preserved (keyboard nav + dispatch index into the flat
// `filtered` array unchanged) — `category` only drives the labelled section
// header that prints when the category changes while walking that flat list.
type CmdCategory = "recall" | "research" | "deal-docs" | "meetings" | "models";

// Display labels for each section header, in render order.
const CATEGORY_LABEL: Record<CmdCategory, string> = {
  recall:     "RECALL & MEMORY",
  research:   "RESEARCH & DATA",
  "deal-docs":"DEAL DOCUMENTS",
  meetings:   "MEETINGS",
  models:     "MODELS & ENGINES",
};

interface CmdDef {
  slash: string;          // "/recall"
  label: string;          // "Recall · query the vault"
  hint: string;           // "/recall <question>"
  category: CmdCategory;  // drives the grouped section header
  workflowKey?: WorkflowKey;
  wired?: boolean;
  disabled?: boolean;
  // Optional: a custom intent constructor when the command needs args
  buildIntent?: (rest: string) => CommandIntent | null;
}

const COMMANDS: CmdDef[] = [
  { slash: "/recall",       label: "Recall · query the vault",      hint: "/recall <question>", category: "recall", workflowKey: "recall-query",     wired: true,
    buildIntent: (rest) => rest.trim() ? { kind: "recall", query: rest.trim() } : null },
  { slash: "/chat",         label: "Project chat · ask the deal",   hint: "/chat [project]",    category: "recall", wired: true,
    // No workflowKey — routes to the right-rail Chat tab via onChat (Feature A).
    buildIntent: (rest) => ({ kind: "chat", project: rest.trim() || undefined }) },
  { slash: "/reindex",      label: "Reindex the vault embeddings",  hint: "/reindex",           category: "recall", workflowKey: "reindex",          wired: true },
  { slash: "/promote",      label: "Promote memory · scan all",      hint: "/promote",           category: "recall", workflowKey: "promote-memory",   wired: true },
  { slash: "/newsletter",   label: "Sector newsletter run",          hint: "/newsletter [sector]",category: "recall", workflowKey: "newsletter-run",   wired: true },

  { slash: "/profile",      label: "Company profile (equity research)", hint: "/profile <ticker>", category: "research", workflowKey: "company-profile", wired: true },
  { slash: "/equity",       label: "Equity research (data only)",     hint: "/equity <ticker>",   category: "research", workflowKey: "company-profile", wired: true },
  { slash: "/market",       label: "Market snapshot",                hint: "/market",            category: "research", workflowKey: "market-snapshot" },
  { slash: "/sector",       label: "Sector read",                    hint: "/sector <name>",     category: "research", workflowKey: "sector-read" },
  { slash: "/comps",        label: "Comps pull (ticker multiples)",  hint: "/comps <ticker>",    category: "research", workflowKey: "comps-pull", wired: true },
  { slash: "/comps-build",  label: "Comps build (Stage 0-3 pipeline)", hint: "/comps-build",     category: "research", workflowKey: "comps-build", wired: true },
  { slash: "/precedents",   label: "Precedent transactions",         hint: "/precedents",        category: "research", workflowKey: "precedents-pull" },
  { slash: "/deal",         label: "Add to deal tracker",             hint: "/deal (paste modal)", category: "research", workflowKey: "deal-tracker-add", wired: true },

  { slash: "/teaser",       label: "Anonymous teaser",                hint: "/teaser <project>",  category: "deal-docs", workflowKey: "teaser" },
  { slash: "/cim",          label: "CIM draft",                       hint: "/cim <project>",     category: "deal-docs", workflowKey: "cim-draft" },
  { slash: "/icmemo",       label: "Investment-committee memo",        hint: "/icmemo <project>",  category: "deal-docs", workflowKey: "ic-memo" },
  { slash: "/buyers",       label: "Buyer list",                      hint: "/buyers <project>",  category: "deal-docs", workflowKey: "buyer-list" },
  { slash: "/ndas",         label: "NDAs pack",                       hint: "/ndas <project>",    category: "deal-docs", workflowKey: "ndas" },
  { slash: "/processletter",label: "Process letter",                  hint: "/processletter <project>", category: "deal-docs", workflowKey: "process-letter" },
  { slash: "/proposal",     label: "Investment proposal",              hint: "/proposal <project>",category: "deal-docs", workflowKey: "proposal" },

  { slash: "/agenda",       label: "Build meeting agenda",             hint: "/agenda <meeting>",  category: "meetings", workflowKey: "build-agenda" },
  { slash: "/preread",      label: "Pre-read pack",                    hint: "/preread <meeting>", category: "meetings", workflowKey: "pre-read-pack" },
  { slash: "/preqa",        label: "Pre-call Q&A",                     hint: "/preqa <meeting>",   category: "meetings", workflowKey: "pre-call-qa" },
  { slash: "/postcall",     label: "Post-call cleanup",                hint: "/postcall <meeting>",category: "meetings", workflowKey: "post-call-cleanup" },

  { slash: "/dcf",          label: "DCF run (engine)",                hint: "/dcf <company>",     category: "models", workflowKey: "dcf-run",     disabled: true },
  { slash: "/lbo",          label: "LBO run (intake → engine)",       hint: "/lbo",               category: "models", workflowKey: "lbo-run",     wired: true },
  { slash: "/sens",         label: "Sensitivity (engine)",            hint: "/sens <model>",      category: "models", workflowKey: "sensitivity", disabled: true },
  { slash: "/threes",       label: "3-statement build",               hint: "/threes <company>",  category: "models", workflowKey: "three-statement" },
  { slash: "/ff",           label: "Football field",                  hint: "/ff <company>",      category: "models", workflowKey: "ff" },
  { slash: "/audit",        label: "Audit a model",                   hint: "/audit <model>",     category: "models", workflowKey: "audit-model" },
  { slash: "/hinotes",      label: "HiNotes status",                  hint: "/hinotes",           category: "models", workflowKey: "meeting-notes-sync" },
];

const SENS_LEVELS: SensLevel[] = ["public", "internal", "confidential", "MNPI"];

// Sensitivity → Chip variant. public = green, internal = t2/neutral,
// confidential = amber, MNPI = red. Keeps the MNPI button (and its
// fail-loud red treatment) visually distinct in the Sens-max selector.
const SENS_VARIANT: Record<SensLevel, "public" | "internal" | "confidential" | "mnpi"> = {
  public: "public",
  internal: "internal",
  confidential: "confidential",
  MNPI: "mnpi",
};

interface Props {
  open: boolean;
  onClose: () => void;
  onFire: (key: WorkflowKey, promptText?: string) => void;
  projects: string[];   // for /recall project filter (loaded from /api/workspaces?type=project)
  // #42 v2 Feature A — `/chat [project]` routes to the right-rail Project chat
  // tab. `project` is the resolved deal code (undefined → current deal).
  onChat: (project?: string) => void;
}

export function CommandModal({ open, onClose, onFire, projects, onChat }: Props) {
  const [text, setText] = useState("");
  const [selected, setSelected] = useState(0);
  const [recallMode, setRecallMode] = useState<null | {
    query: string;
    project: string;
    sens: SensLevel;
    limit: number;
    /** When OFF, the bridge skips the Ollama map-reduce step and returns
     *  raw retrieval hits in ~2s (vs 3-10 min on local Ollama). Defaults
     *  ON for parity with prior behaviour. #16c — surfaced 2026-05-24. */
    synth: boolean;
    busy: boolean;
    /** When busy, ms-since-run-start for the elapsed-time counter (#16d). */
    startedAt?: number;
    result?: RecallResponse;
    error?: string;
  }>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  // Reset state when opening/closing.
  useEffect(() => {
    if (open) {
      setText("");
      setSelected(0);
      setRecallMode(null);
      // Focus on next paint.
      setTimeout(() => inputRef.current?.focus(), 10);
    }
  }, [open]);

  // Filter slash-commands by prefix-or-substring match on slash or label.
  // When the user has typed `/cmd arg1 arg2`, match only against the slash
  // part (`/cmd`) so the command stays visible while they're typing args.
  const filtered = useMemo<CmdDef[]>(() => {
    const q = text.trim().toLowerCase();
    if (!q) return COMMANDS.slice(0, 10);
    if (q.startsWith("/")) {
      // Strip everything after the first whitespace — that's args, not the verb.
      const slashPart = q.split(/\s/)[0];
      return COMMANDS.filter((c) => c.slash.startsWith(slashPart)).slice(0, 12);
    }
    // Bare query → /recall is always first.
    const recall = COMMANDS.find((c) => c.slash === "/recall")!;
    const others = COMMANDS.filter((c) => c.label.toLowerCase().includes(q) || c.slash.includes(q));
    return [recall, ...others.filter((c) => c.slash !== "/recall")].slice(0, 10);
  }, [text]);

  // Clamp selection when filtered shrinks.
  useEffect(() => {
    if (selected >= filtered.length) setSelected(Math.max(0, filtered.length - 1));
  }, [filtered.length, selected]);

  if (!open) return null;

  const execute = (cmd: CmdDef, rest: string) => {
    if (cmd.disabled) {
      // Show inline message? For now silently no-op — buttons in workflow tiles
      // already show "disabled" visually.
      return;
    }
    // Custom intent: /recall expands into the in-modal Recall mode; /chat routes
    // to the right-rail Project chat tab via the parent's onChat.
    if (cmd.buildIntent) {
      const intent = cmd.buildIntent(rest);
      if (intent && intent.kind === "recall") {
        setRecallMode({
          query: intent.query,
          project: intent.project ?? "",
          sens: intent.max_sensitivity ?? "internal",
          limit: intent.limit ?? 10,
          synth: true,
          busy: false,
        });
        return;
      }
      if (intent && intent.kind === "chat") {
        // Resolve a typed project to its canonical code (case-insensitive). An
        // UNKNOWN name resolves to undefined → chat the currently-selected deal
        // rather than switching the rail to a non-existent workspace (codex).
        // Empty arg → undefined → same (chat the current deal).
        const resolved = intent.project
          ? projects.find((p) => p.toLowerCase() === intent.project!.toLowerCase())
          : undefined;
        onChat(resolved);
        onClose();
        return;
      }
    }
    if (cmd.workflowKey) {
      onFire(cmd.workflowKey, rest);
      onClose();
    }
  };

  const onKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Escape") { onClose(); return; }
    if (e.key === "ArrowDown") { e.preventDefault(); setSelected((s) => Math.min(s + 1, filtered.length - 1)); return; }
    if (e.key === "ArrowUp")   { e.preventDefault(); setSelected((s) => Math.max(s - 1, 0)); return; }
    if (e.key === "Enter") {
      e.preventDefault();
      // Split text into slash+rest
      const trimmed = text.trim();
      if (!trimmed) return;
      if (trimmed.startsWith("/")) {
        // Pick the highlighted command from filtered list
        const cmd = filtered[selected];
        if (!cmd) return;
        const rest = trimmed.slice(cmd.slash.length).trim();
        execute(cmd, rest);
      } else {
        // Bare text. If the user actively moved selection away from index 0
        // (the default /recall) they intended a different command — honour
        // it and pass the bare text as the arg. Otherwise default to /recall.
        // #16b — fixes arrow keys "lying" about Enter behaviour.
        if (selected > 0 && filtered[selected]) {
          execute(filtered[selected], trimmed);
        } else {
          const recall = COMMANDS.find((c) => c.slash === "/recall")!;
          execute(recall, trimmed);
        }
      }
    }
  };

  const runRecall = async () => {
    if (!recallMode) return;
    setRecallMode({ ...recallMode, busy: true, startedAt: Date.now(), error: undefined });
    try {
      const res = await api.recall({
        query: recallMode.query,
        limit: recallMode.limit,
        project: recallMode.project || undefined,
        max_sensitivity: recallMode.sens,
        synthesise: recallMode.synth,
      });
      setRecallMode({ ...recallMode, busy: false, startedAt: undefined, result: res });
    } catch (e) {
      const msg = e instanceof ApiError ? `${e.status}: ${e.message}` : e instanceof Error ? e.message : "Unknown error";
      setRecallMode({ ...recallMode, busy: false, startedAt: undefined, error: `Recall failed — ${msg}` });
    }
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center bg-black/60 backdrop-blur-sm"
      onClick={onClose}
    >
      <div
        className="mt-[12vh] w-full max-w-[1040px] overflow-hidden rounded-[16px] border border-line bg-bg-1 shadow-modal"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Top-accent strip — neutral/command (palette) */}
        <div className="h-[3px] shrink-0 bg-accent" />
        {recallMode ? (
          <RecallSubMode
            state={recallMode}
            setState={(s) => setRecallMode(s)}
            projects={projects}
            onRun={runRecall}
            onBack={() => setRecallMode(null)}
            onClose={onClose}
          />
        ) : (
          <>
            {/* Header — themed omnibox input row */}
            <div className="flex items-center gap-[10px] border-b border-line px-[16px] py-[12px]">
              <Search size={15} className="shrink-0 text-accent" />
              <input
                ref={inputRef}
                value={text}
                onChange={(e) => setText(e.target.value)}
                onKeyDown={onKeyDown}
                placeholder="Type a slash command, or ask anything — defaults to /recall"
                className="flex-1 bg-transparent text-[13px] text-t1 placeholder:text-t4 outline-none"
              />
              <span className="mono shrink-0 whitespace-nowrap text-[10.5px] text-t4">type to recall  ·  / to run a skill</span>
            </div>

            {/* Body — live result rows, grouped under labelled section headers
                (v2 palette AOX-0). The flat `filtered` index `i` still drives
                selection + keyboard nav; a header prints only when the category
                changes as we walk that same flat list. */}
            <div className="max-h-[60vh] overflow-y-auto">
              {filtered.length === 0 ? (
                <div className="px-[16px] py-[14px] text-[12px] italic text-t3">No matching commands.</div>
              ) : (
                filtered.map((cmd, i) => {
                  const showHeader = i === 0 || cmd.category !== filtered[i - 1].category;
                  return (
                    <div key={cmd.slash}>
                      {showHeader && (
                        <div className="mono border-b border-line bg-bg-2/40 px-[16px] pb-[5px] pt-[10px] text-[9.5px] uppercase tracking-[0.12em] text-t3">
                          {CATEGORY_LABEL[cmd.category]}
                        </div>
                      )}
                      <button
                        type="button"
                        onClick={() => execute(cmd, text.startsWith(cmd.slash) ? text.slice(cmd.slash.length).trim() : "")}
                        className={cn(
                          "flex w-full cursor-pointer items-center justify-between gap-[12px] px-[16px] py-[10px] text-left transition-colors",
                          i < filtered.length - 1 && "border-b border-line",
                          i === selected ? "bg-bg-2" : "hover:bg-bg-2",
                          cmd.disabled && "opacity-50",
                        )}
                      >
                        <div className="flex min-w-0 items-baseline gap-[12px]">
                          <span className={cn(
                            "mono shrink-0 text-[11px] font-semibold",
                            cmd.wired ? "text-ok-bright" : cmd.disabled ? "text-t4" : "text-t2",
                          )}>
                            {cmd.slash}
                          </span>
                          <span className="truncate text-[12.5px] text-t1">{cmd.label}</span>
                        </div>
                        <span className="mono shrink-0 text-[10px] text-t4">{cmd.hint}</span>
                      </button>
                    </div>
                  );
                })
              )}
            </div>

            {/* Footer — keyboard hint + count */}
            <div className="flex items-center justify-between gap-3 border-t border-line bg-bg-2/40 px-[16px] py-[8px] text-[10px] text-t4">
              <span className="mono flex items-center gap-[6px]">
                ↑↓ navigate
                <span className="inline-flex items-center gap-[3px]"><CornerDownLeft size={11} /> run</span>
                · esc close
              </span>
              <span className="tabular">{filtered.length} of {COMMANDS.length}</span>
            </div>
          </>
        )}
      </div>
    </div>
  );
}

interface RecallState {
  query: string;
  project: string;
  sens: SensLevel;
  limit: number;
  synth: boolean;
  busy: boolean;
  startedAt?: number;
  result?: RecallResponse;
  error?: string;
}

interface SubProps {
  state: RecallState;
  setState: (s: RecallState) => void;
  projects: string[];
  onRun: () => void;
  onBack: () => void;
  onClose: () => void;
}

function RecallSubMode({ state: s, setState, projects, onRun, onBack, onClose }: SubProps) {
  return (
    <>
      {/* Header — back · /recall kind chip · query · close */}
      <div className="flex items-center gap-[10px] border-b border-line px-[16px] py-[12px]">
        <IconButton icon={ArrowLeft} label="Back to command palette" onClick={onBack} size={15} />
        <Chip label="/recall" variant="accent" className="mono shrink-0 font-semibold" />
        <span className="min-w-0 flex-1 truncate text-[12.5px] text-t1">{s.query}</span>
        <span className="mono rounded border border-line-2 bg-bg-2 px-[6px] py-[1px] text-[10px] tracking-[0.08em] text-t3">Esc</span>
        <IconButton icon={X} label="Close" onClick={onClose} size={15} />
      </div>

      {/* Filter pills */}
      <div className="flex flex-wrap items-center gap-2 border-b border-line bg-bg-2/40 px-[16px] py-[10px]">
        <span className="mono text-[10px] uppercase tracking-[0.14em] text-t3">Project</span>
        <select
          value={s.project}
          onChange={(e) => setState({ ...s, project: e.target.value })}
          className="mono rounded-lg border border-line-2 bg-bg-2 px-[8px] py-[3px] text-[11px] text-t1 outline-none transition-colors focus:border-accent-line"
        >
          <option value="">— any —</option>
          {projects.map((p) => <option key={p} value={p}>{p}</option>)}
        </select>

        <span className="mono ml-3 text-[10px] uppercase tracking-[0.14em] text-t3">Sens max</span>
        <div className="flex overflow-hidden rounded-lg border border-line-2 bg-bg-2">
          {SENS_LEVELS.map((lvl) => {
            const active = s.sens === lvl;
            const v = SENS_VARIANT[lvl];
            // Active sensitivity paints in its semantic tint so the MNPI ceiling
            // reads as a deliberate, fail-loud selection (red), confidential as
            // amber, etc. Inactive levels stay muted.
            const activeClass =
              v === "public"       ? "bg-green/15 text-green"
              : v === "confidential" ? "bg-amber/15 text-amber"
              : v === "mnpi"         ? "bg-red/15 text-red"
              : "bg-accent-soft text-accent";
            return (
              <button
                key={lvl}
                type="button"
                onClick={() => setState({ ...s, sens: lvl })}
                className={cn(
                  "mono px-[8px] py-[3px] text-[10px] uppercase tracking-[0.08em] transition-colors",
                  active ? activeClass : "text-t3 hover:text-t1",
                )}
              >
                {lvl === "MNPI" ? "MNPI" : lvl.slice(0, 3)}
              </button>
            );
          })}
        </div>

        <span className="mono ml-3 text-[10px] uppercase tracking-[0.14em] text-t3">Limit</span>
        <input
          type="number"
          value={s.limit}
          min={1}
          max={50}
          onChange={(e) => setState({ ...s, limit: Math.max(1, Math.min(50, Number(e.target.value) || 10)) })}
          className="mono w-[52px] rounded-lg border border-line-2 bg-bg-2 px-[8px] py-[3px] text-[11px] text-t1 outline-none transition-colors focus:border-accent-line"
        />

        <span
          className="mono ml-3 text-[10px] uppercase tracking-[0.14em] text-t3"
          title="When OFF, returns raw retrieval hits in ~2s (no Ollama map-reduce). When ON, synth takes 3-10 min on local Ollama."
        >Synth</span>
        <button
          type="button"
          onClick={() => setState({ ...s, synth: !s.synth })}
          className={cn(
            "mono rounded-lg border px-[8px] py-[3px] text-[10px] uppercase tracking-[0.08em] transition-colors",
            s.synth ? "border-accent-line bg-accent-soft text-accent" : "border-line-2 bg-bg-2 text-t3 hover:text-t1 hover:border-line-2",
          )}
        >
          {s.synth ? "ON" : "OFF"}
        </button>

        <div className="flex-1" />
        <button
          type="button"
          onClick={onRun}
          disabled={s.busy}
          className={cn(
            "rounded-lg px-[14px] py-[6px] text-[11.5px] font-medium transition-colors disabled:cursor-default",
            s.busy ? "border border-line-2 text-t3" : "bg-accent text-bg hover:brightness-110",
          )}
        >
          {s.busy ? "Running…" : "Run"}
        </button>
      </div>

      {/* Result area */}
      <div className="max-h-[55vh] min-h-[160px] overflow-y-auto px-[16px] py-[14px]">
        {s.error && (
          <div className="rounded-lg border border-red/40 bg-red/10 px-[12px] py-[8px] text-[12px] text-red">{s.error}</div>
        )}
        {!s.error && !s.result && !s.busy && (
          <div className="text-[12px] text-t3">Press <span className="mono text-t2">Run</span> to query the vault.</div>
        )}
        {s.busy && <BusyText synth={s.synth} startedAt={s.startedAt} />}
        {s.result && (
          <div className="flex flex-col gap-3">
            {s.result.synthesis && (
              <div className="rounded-lg border-l-2 border-accent-line bg-bg-2/50 px-[14px] py-[10px] text-[12px] leading-[1.55] text-t1 whitespace-pre-wrap">
                {s.result.synthesis}
              </div>
            )}
            <div>
              <div className="mono mb-[6px] text-[10px] uppercase tracking-[0.14em] text-t3">
                Sources · {s.result.hits.length} hits
              </div>
              {s.result.hits.map((h) => (
                <div key={h.path} className="grid grid-cols-[28px_1fr_auto] items-baseline gap-x-2 border-b border-line py-[4px] last:border-b-0">
                  <span className="mono text-[10px] text-t4">#{h.rank}</span>
                  <span className="mono truncate text-[10.5px] text-t1">{h.path}</span>
                  <span className="mono tabular text-[10px] text-t3">{h.score.toFixed(3)}</span>
                </div>
              ))}
            </div>
          </div>
        )}
      </div>
    </>
  );
}

/** Busy-state text with elapsed-time counter. #16d — replaces the old
 *  "10-30s" wildly-optimistic message with realistic figures + a live
 *  elapsed counter so the operator can distinguish working-as-designed
 *  from hung. The synth flag toggles which expectation we set. */
function BusyText({ synth, startedAt }: { synth: boolean; startedAt: number | undefined }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, []);
  const elapsedSec = startedAt ? Math.floor((now - startedAt) / 1000) : 0;
  const mm = Math.floor(elapsedSec / 60);
  const ss = elapsedSec % 60;
  const elapsed = mm > 0 ? `${mm}m ${ss.toString().padStart(2, "0")}s` : `${ss}s`;
  return (
    <div className="text-[12px] text-t3">
      Querying… {synth
        ? <>local synthesis is slow (~3-10 min for 10 hits on qwen3:14b; ~10-30s when routed to cloud).</>
        : <>raw hits, no synthesis (~2s).</>}
      {" "}<span className="mono tabular text-t2">elapsed: {elapsed}</span>
    </div>
  );
}
