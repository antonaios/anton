import { useEffect, useRef, useState, type ChangeEvent, type DragEvent as ReactDragEvent, type RefObject } from "react";
import { Archive, Check, ChevronDown, Lock, MoreVertical, Paperclip, Pencil, Pin, Trash2, X } from "lucide-react";
import { cn } from "../lib/cn";
import { api, ApiError } from "../lib/api";
import { isSafeFileUrl, isSafeHttpUrl } from "../lib/url";
import { useMediaQuery } from "../lib/useMediaQuery";
import { Markdown } from "./Markdown";
import type { Sensitivity, WorkspaceType } from "../types";

/** True when the OS "reduce motion" preference is set — gates the status-line
 *  pulse + shimmer (item 1) down to a single static "Working…". */
function usePrefersReducedMotion(): boolean {
  return useMediaQuery("(prefers-reduced-motion: reduce)");
}

// Grow a textarea with its content up to ~8 rows, then scroll — the "stretch as
// you type, cap at ~8 rows" behaviour shared by the composer + the crew reply box.
//
// `maxPx` may be a fixed pixel cap (the crew box) OR a function evaluated at
// resize-time (the composer, which caps at `min(184px, 30vh)` so it can't swallow
// a 13″ viewport — re-read on every resize so a window resize is honoured).
//
// The resize is bound BOTH as a React effect (catches programmatic value changes:
// draft prefill, clear-on-send, hydration) AND as a NATIVE `input` listener on the
// element (catches every keystroke the instant the browser repaints, not waiting
// on React's commit) — per the design brief. The `input` listener also flips
// overflow-y to `auto` only once the cap is hit, so the scrollbar appears only
// when the textarea is actually clipped.
function useAutoGrow(
  ref: RefObject<HTMLTextAreaElement>,
  value: string,
  maxPx: number | (() => number),
) {
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const cap = () => (typeof maxPx === "function" ? maxPx() : maxPx);
    const resize = () => {
      const max = cap();
      el.style.height = "auto";                          // reset so scrollHeight reflects content
      const next = Math.min(el.scrollHeight, max);
      el.style.height = `${next}px`;
      el.style.overflowY = el.scrollHeight > max ? "auto" : "hidden";
    };
    resize();                                            // sync on value / mount
    el.addEventListener("input", resize);                // native — every keystroke
    window.addEventListener("resize", resize);           // re-cap when the 30vh viewport cap moves
    return () => {
      el.removeEventListener("input", resize);
      window.removeEventListener("resize", resize);
    };
    // `maxPx` is stable (a literal or a module/stable callback); `value` re-syncs
    // on programmatic changes (prefill / clear) the native listener can't see.
  }, [ref, value, maxPx]);
}

// Composer cap — min(184px, 30vh): ~8 rows, viewport-bounded so it can't swallow
// a 13″ laptop. Evaluated per-resize so a window resize re-caps. (Brief item 2.)
function composerMaxPx(): number {
  const vh = typeof window !== "undefined" ? window.innerHeight : 800;
  return Math.min(184, vh * 0.3);
}

// ── Message shapes ──────────────────────────────────────────────────────────
export interface KPICell {
  label: string;
  value: string;
  unit?: string;        // suffix unit: %, x, m, yr — always rendered AFTER the value
  unitBefore?: string;  // prefix unit (currency): £, $, € — rendered BEFORE the value
  flag?: boolean;
  delta?: string;
}

/** Click handlers for chips. Discriminated by `type` so the dispatcher
 *  in `Bubble` (handleChipClick) can switch cleanly. Add a new variant
 *  when a chip needs a new client-side behaviour — e.g. download, copy,
 *  inline-expand. Server-side actions land via the dispatcher (#22). */
export type ChipAction =
  | { type: "open-file"; path: string }            // file:// open via window.open
  | { type: "open-url";  url: string }             // external URL (e.g. news links)
  | { type: "show-modal"; modalId: string };       // named modal — real modal lands Session D

export interface MessageChip {
  label: string;
  primary?: boolean;
  /** Optional glyph (Paper 53T-0): "window" renders a leading table/window icon
   *  (the primary "Open model" chip); "arrow" renders a trailing right-arrow
   *  (the "Run sensitivity" chip). Omit for a text-only chip. */
  icon?: "window" | "arrow";
  /** When present, the chip is clickable. Absent → chip is rendered but
   *  dimmed + not interactive. The dispatcher lives in `Bubble`. */
  action?: ChipAction;
}

export interface Message {
  id: string;
  role: "user" | "anton";
  who: string;                    // "Operator" / "ANTON"
  time: string;                   // "14:18"
  durationMs?: number;            // "8.4s"
  route?: string;                 // "ROUTED · LOCAL OLLAMA → ENGINE"
  running?: boolean;
  body?: string;                  // bubble prose
  kpis?: KPICell[];
  commentary?: string;
  chips?: MessageChip[];
  runningText?: string;           // "Iterating engine grid · acq ∈ [9.5, …]"
  etaMs?: number;
  steps?: { text: string; ok?: boolean }[];

  // ── Contract fields, added 2026-05-24 (OUTSTANDING.md ## CONTRACTS · sessions)
  sessionId?: string;
  parentMessageId?: string | null;
  created?: string;               // ISO timestamp from server
  lane?: "chat" | "skill" | "composite" | "crew";
  parentRunId?: string;           // composite child rows
  crewRunId?: string;             // crew role rows
  /** Set while a crew is BLOCKED on a mid-run human-input ask (the
   *  `human_input_required` SSE event). On a lane:"crew" message it makes the
   *  bubble render an inline reply box; App clears it once answered (or on a
   *  404 / when the run finalizes). One ask at a time — the crew blocks on the
   *  reply — keyed by `msgId`. */
  crewAsk?: { msgId: string; prompt: string } | null;
  /** Anton-side UNWIRED placeholder marker. Bridge stub returns this until
   *  cloud/local lanes are wired through; renders dimmed with a "stub" badge. */
  unwired?: boolean;
  /** Anton-side error marker (e.g. sensitivity refused). Renders with accent
   *  border instead of the normal line border so failures aren't visually
   *  identical to real responses. */
  failed?: boolean;
}

// ── Chat document attachments (#chat-attach) ────────────────────────────────
/** One document attached to the composer. `text` is the backend-extracted body
 *  forwarded on send; `uploading` flags an optimistic chip whose upload is still
 *  in flight; `error` flags a chip that failed to upload (or was rejected by the
 *  client validation below) — it carries the reason + is excluded from send. */
export interface Attachment {
  /** Stable client id so optimistic chips can be reconciled / removed. */
  id: string;
  filename: string;
  /** Extracted text (empty while uploading / on error). */
  text: string;
  chars: number;
  truncated: boolean;
  sensitivity?: Sensitivity;
  uploading?: boolean;
  error?: string;
}

// Client-side validation (UX only — the backend re-validates on upload). The
// accept-list mirrors the hidden <input accept=…>; size/count are soft caps that
// surface inline on a reject chip rather than silently dropping the file.
const ATTACH_MAX_BYTES = 25 * 1024 * 1024;   // ~25 MB / file
const ATTACH_MAX_FILES = 6;                   // max attached docs
const ATTACH_ACCEPT = ".pdf,.docx,.pptx,.xlsx,.txt,.md,.csv";
const ATTACH_EXTS = new Set([".pdf", ".docx", ".pptx", ".xlsx", ".txt", ".md", ".csv"]);

function fileExt(name: string): string {
  const i = name.lastIndexOf(".");
  return i >= 0 ? name.slice(i).toLowerCase() : "";
}

interface Props {
  /** The deal/project name shown in the big deal-header title (Paper: "Project
   *  Helix"). Distinct from the session title below it. */
  dealName: string;
  sessionTitle: string;           // "LBO @ acq=11.5, hold=5"
  sessionId: string;              // "7c2a"
  startedAt: string;              // "14:18"
  /** Percentage 0-100 of the LLM context window consumed by this session
   *  (session tokens ÷ the active model's window). null when the bridge
   *  hasn't reported the session's context size yet → renders "—". */
  contextPct: number | null;
  messages: Message[];
  /** Send the composed turn. `attachments` carries the extracted-text payloads
   *  for any attached docs (empty array when none). `sensitivityOverride`, when
   *  present, forces a stricter lane for THIS turn — set to "confidential" by the
   *  cloud-routing warn modal so a document-bearing general-workspace turn can be
   *  kept on the local model. (#chat-attach) */
  onSend: (
    text: string,
    attachments: { filename: string; text: string }[],
    sensitivityOverride?: Sensitivity,
  ) => void;
  /** #chat-attach — the active workspace type. "general" routes chat to the
   *  cloud, so a document-bearing send is intercepted by the warn/escalate
   *  modal; project/bd already route locally and never prompt. */
  workspaceType: WorkspaceType;
  /** #chat-attach — composer attachments are LIFTED to App and keyed PER SESSION
   *  there (so a stale in-flight upload that resolves after a session switch
   *  patches its OWN session's bucket, never the now-active one — a §5.2
   *  cross-deal leak). Controlled: `attachments` is THIS session's value;
   *  `onAttachmentsChange` is a FUNCTIONAL updater bound (in App) to this
   *  session's id, so every add/patch/remove/clear reads the freshest bucket and
   *  can never reach across sessions. */
  attachments: Attachment[];
  onAttachmentsChange: (updater: (prev: Attachment[]) => Attachment[]) => void;
  /** Opens the workflow palette overlay — fired when the operator types "/" on
   *  an empty composer (v2: the palette is a `/`-triggered overlay, not a drawer). */
  onSlash?: () => void;
  /** Hydration in flight (GET /messages). Renders a skeleton above the composer. */
  loading?: boolean;
  /** Send in flight (POST /messages). Disables composer to prevent double-submit. */
  sending?: boolean;
  /** #3b — composer draft is LIFTED to App so a drawer tile / Cmd-K can read it
   *  as the skill argument (composer-draft → fire()) and pre-fill a slash stub
   *  into it. Controlled: `draft` is the value, `onDraftChange` the setter. */
  draft: string;
  onDraftChange: (v: string) => void;
  /** #3b — bumped by App when a needs-arg drawer tile drafts a command into the
   *  composer, so the textarea takes focus for the operator to type the arg. */
  focusSignal?: number;
  /** Submit the operator's answer to a crew's mid-run human-input ask. POSTs
   *  to /api/crew/runs/{runId}/human-input. Resolves once accepted (or after a
   *  404, which App treats as expired/already-answered and drops the box);
   *  rejects on other errors so the reply box can surface them inline. */
  onCrewReply: (runId: string, msgId: string, response: string) => Promise<void>;
  /** #session-ops — chat-header ⋮ menu actions. `pinned` drives the Pin/Unpin
   *  label + a pin glyph on the title; the four callbacks are optional so the
   *  header still renders if a caller doesn't wire them. */
  pinned?: boolean;
  onRename?: (title: string) => void;
  onArchive?: () => void;
  onTogglePin?: () => void;
  onDelete?: () => void;
  /** #minimax-chat-model — operator chat-model selection. "default" = the
   *  sensitivity-routed lane; "minimax" forces MiniMax. `minimaxAllowed` gates
   *  the option (cloud model → general workspaces only). */
  model?: "default" | "minimax";
  onModelChange?: (m: "default" | "minimax") => void;
  minimaxAllowed?: boolean;
}


/**
 * Chat-header model picker (#minimax-chat-model). A small popover: Claude
 * (the default sensitivity-routed lane) vs MiniMax (a cloud model, offered only
 * when the session is cloud-eligible).
 */
function ModelPicker({
  model = "default", onModelChange, minimaxAllowed,
}: {
  model?: "default" | "minimax";
  onModelChange?: (m: "default" | "minimax") => void;
  minimaxAllowed?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => { if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false); };
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onKey);
    return () => { document.removeEventListener("mousedown", onDoc); document.removeEventListener("keydown", onKey); };
  }, [open]);

  const label = model === "minimax" ? "MiniMax" : "Claude";
  const opt = "flex items-center justify-between w-full px-[12px] h-[33px] text-[12px] text-left transition-colors";
  const pick = (m: "default" | "minimax") => { onModelChange?.(m); setOpen(false); };

  return (
    <div ref={ref} className="relative">
      <button
        type="button"
        title="Chat model"
        onClick={() => setOpen((v) => !v)}
        className={cn("flex items-center gap-[6px] h-[28px] rounded-lg px-[10px] text-[11.5px] font-medium transition-colors",
          open ? "bg-bg-2 text-t1" : "text-t3 hover:text-t1 hover:bg-bg-2")}
      >
        <span className="text-t4 font-normal uppercase tracking-[0.08em] text-[9.5px]">Model</span>
        <span className="text-t2">{label}</span>
        <ChevronDown size={13} strokeWidth={1.8} />
      </button>
      {open && (
        <div className="absolute right-0 top-[calc(100%+6px)] z-30 w-[182px] py-[5px] rounded-[10px] bg-bg-2 border border-line shadow-modal overflow-hidden">
          <button type="button" onClick={() => pick("default")}
            className={cn(opt, model === "default" ? "text-accent font-medium" : "text-t2 hover:bg-bg-1 hover:text-t1")}>
            Claude {model === "default" && <Check size={13} strokeWidth={2} />}
          </button>
          {minimaxAllowed ? (
            <button type="button" onClick={() => pick("minimax")}
              className={cn(opt, model === "minimax" ? "text-accent font-medium" : "text-t2 hover:bg-bg-1 hover:text-t1")}>
              MiniMax {model === "minimax" && <Check size={13} strokeWidth={2} />}
            </button>
          ) : (
            <div className={cn(opt, "text-t4 cursor-not-allowed")}
              title="MiniMax is a cloud model — available in general workspaces only (deal workspaces route locally)">
              MiniMax <span className="text-[9px] uppercase tracking-[0.06em]">deal‑local</span>
            </div>
          )}
        </div>
      )}
    </div>
  );
}


/**
 * Chat-header session ⋮ menu (#session-ops). A small popover anchored to the
 * kebab: Rename (delegates to the header's inline edit), Pin/Unpin, Archive,
 * and a two-click arm-to-confirm Delete. Dismisses on Escape / outside-click.
 */
function SessionOptionsMenu({
  pinned, onRename, onArchive, onTogglePin, onDelete,
}: {
  pinned?: boolean;
  onRename?: () => void;
  onArchive?: () => void;
  onTogglePin?: () => void;
  onDelete?: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [armed, setArmed] = useState(false);
  const [armedAt, setArmedAt] = useState(0);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) { setOpen(false); setArmed(false); }
    };
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") { setOpen(false); setArmed(false); } };
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onKey);
    return () => { document.removeEventListener("mousedown", onDoc); document.removeEventListener("keydown", onKey); };
  }, [open]);

  const close = () => { setOpen(false); setArmed(false); };
  const item = "flex items-center gap-[9px] w-full px-[12px] h-[34px] text-[12.5px] text-left transition-colors";

  return (
    <div ref={ref} className="relative">
      <button
        type="button"
        title="Session options"
        onClick={() => setOpen((v) => !v)}
        className={cn("flex items-center justify-center h-[28px] w-[28px] rounded-[7px] transition-colors",
          open ? "text-t1 bg-bg-2" : "text-t3 hover:text-t1 hover:bg-bg-2")}
      >
        <MoreVertical size={15} strokeWidth={1.8} />
      </button>
      {open && (
        <div className="absolute right-0 top-[calc(100%+6px)] z-30 w-[190px] py-[5px] rounded-[10px] bg-bg-2 border border-line shadow-modal overflow-hidden">
          <button type="button" className={cn(item, "text-t2 hover:bg-bg-1 hover:text-t1")} onClick={() => { close(); onRename?.(); }}>
            <Pencil size={14} strokeWidth={1.8} className="shrink-0 text-t3" /> Rename
          </button>
          <button type="button" className={cn(item, "text-t2 hover:bg-bg-1 hover:text-t1")} onClick={() => { close(); onTogglePin?.(); }}>
            <Pin size={14} strokeWidth={1.8} className="shrink-0 text-t3" /> {pinned ? "Unpin" : "Pin"}
          </button>
          <button type="button" className={cn(item, "text-t2 hover:bg-bg-1 hover:text-t1")} onClick={() => { close(); onArchive?.(); }}>
            <Archive size={14} strokeWidth={1.8} className="shrink-0 text-t3" /> Archive
          </button>
          <div className="my-[4px] h-px bg-line" />
          <button
            type="button"
            className={cn(item, "text-red hover:bg-red/10", armed && "bg-red/10 font-medium")}
            onClick={() => {
              // Two deliberate clicks; ignore an accidental double-click (the
              // second event landing <350ms after arming) — delete is irreversible.
              if (!armed) { setArmed(true); setArmedAt(Date.now()); return; }
              if (Date.now() - armedAt < 350) return;
              close(); onDelete?.();
            }}
          >
            <Trash2 size={14} strokeWidth={1.8} className="shrink-0" /> {armed ? "Click again to delete" : "Delete"}
          </button>
        </div>
      )}
    </div>
  );
}

/**
 * v5 chat canvas — replaces PromptPanel + RunResultPanel.
 *
 * Layout (flex column inside the center grid cell):
 *   - Session head (label + h1 + meta + ghost actions row)
 *   - Scrollable messages region (.msgs-wrap)
 *   - Workflow drawer (optional slot)
 *   - Composer (textarea + hints + SEND)
 *
 * Composer is just a controlled input for now — wired to onSend prop.
 */
export function ChatCanvas({
  dealName, sessionTitle, sessionId, startedAt, contextPct, messages, onSend, onSlash, loading, sending,
  draft, onDraftChange, focusSignal, onCrewReply,
  workspaceType, attachments, onAttachmentsChange,
  pinned, onRename, onArchive, onTogglePin, onDelete,
  model, onModelChange, minimaxAllowed,
}: Props) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const composerRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  // #session-ops — inline session rename (the ⋮ Rename item + the edit pencil
  // both open this; Enter / blur commits, Escape cancels).
  const [renaming, setRenaming] = useState(false);
  const [renameDraft, setRenameDraft] = useState("");
  const startRename = () => { setRenameDraft(sessionTitle); setRenaming(true); };
  const commitRename = () => {
    const t = renameDraft.trim();
    if (t && t !== sessionTitle) onRename?.(t);
    setRenaming(false);
  };

  // Auto-scroll to bottom whenever messages change (new send, hydration, etc.)
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
  }, [messages.length]);

  // #3b — focus the composer (+ caret to end) when App drafts a command into it.
  // Skip the initial render (focusSignal 0) so we don't steal focus on mount.
  useEffect(() => {
    if (!focusSignal) return;
    const el = composerRef.current;
    if (!el) return;
    el.focus();
    const len = el.value.length;
    el.setSelectionRange(len, len);
  }, [focusSignal]);

  // Auto-grow the composer with its content, up to ~8 rows capped at
  // min(184px, 30vh), then scroll. Bound natively + on draft change so a
  // programmatic clear/prefill also re-fits. (Brief item 2.)
  useAutoGrow(composerRef, draft, composerMaxPx);

  // ── Document attachments (#chat-attach) ───────────────────────────────────
  // `attachments` is App-owned (lifted, like `draft`) so it resets per-session.
  // `dragActive` is local — a transient drag-over highlight, no need to lift.
  const [dragActive, setDragActive] = useState(false);
  // The cloud-routing warn modal (general workspace + ≥1 attachment). When
  // `pendingSend` is non-null the modal is open, holding the turn the operator
  // is about to send; "Keep local" / "Send to cloud" resolve it.
  const [pendingSend, setPendingSend] = useState<{ text: string } | null>(null);

  // All attachment mutations go through the functional updater (no ref snapshot):
  // `onAttachmentsChange` is bound in App to THIS session's bucket, and the
  // functional form reads the freshest bucket at apply-time — so async upload
  // callbacks resolving out of order (or after a session switch) always patch
  // the correct, current list without a stale-closure race.
  const patchAttachment = (id: string, patch: Partial<Attachment>) => {
    onAttachmentsChange((prev) => prev.map((a) => (a.id === id ? { ...a, ...patch } : a)));
  };
  const removeAttachment = (id: string) => {
    onAttachmentsChange((prev) => prev.filter((a) => a.id !== id));
  };

  // Validate + upload a batch of files. Each gets an optimistic "uploading" chip
  // immediately; a reject (bad type / too big / over the count cap) lands as an
  // error chip instead of an upload. Resolves to ready text, or an error chip on
  // a failed upload. Client checks are UX only — the backend re-validates.
  const ingestFiles = (files: FileList | File[]) => {
    if (!sessionId) return;
    const list = Array.from(files);
    if (!list.length) return;
    // Count cap is measured against the NON-error chips already attached: a
    // rejected/failed chip must not consume a slot, else a string of rejects
    // would soft-lock further picks (LOW). The operator can clear rejects, but
    // shouldn't have to in order to pick a valid replacement.
    let slots = ATTACH_MAX_FILES - attachments.filter((a) => !a.error).length;

    const fresh: Attachment[] = [];
    const uploads: { id: string; file: File }[] = [];
    for (const file of list) {
      const id = `att-${crypto.randomUUID()}`;
      const ext = fileExt(file.name);
      if (slots <= 0) {
        fresh.push({ id, filename: file.name, text: "", chars: 0, truncated: false,
          error: `Max ${ATTACH_MAX_FILES} files` });
        continue;
      }
      slots -= 1;
      if (!ATTACH_EXTS.has(ext)) {
        fresh.push({ id, filename: file.name, text: "", chars: 0, truncated: false,
          error: "Unsupported type" });
        continue;
      }
      if (file.size > ATTACH_MAX_BYTES) {
        fresh.push({ id, filename: file.name, text: "", chars: 0, truncated: false,
          error: "Too large (>25 MB)" });
        continue;
      }
      fresh.push({ id, filename: file.name, text: "", chars: 0, truncated: false, uploading: true });
      uploads.push({ id, file });
    }
    // Append all new chips in one functional update (optimistic), then resolve
    // uploads. The functional form reads the freshest bucket so concurrent
    // ingests/patches don't clobber each other.
    onAttachmentsChange((prev) => [...prev, ...fresh]);
    for (const { id, file } of uploads) {
      api.uploadAttachment(sessionId, file)
        .then((r) => patchAttachment(id, {
          filename: r.filename, text: r.text, chars: r.chars,
          truncated: r.truncated, sensitivity: r.sensitivity,
          uploading: false, error: undefined,
        }))
        .catch((e) => patchAttachment(id, {
          uploading: false,
          error: e instanceof ApiError ? `Upload failed (${e.status})`
            : e instanceof Error ? "Upload failed" : "Upload failed",
        }));
    }
  };

  // Hidden file-input pick → ingest, then RESET the input value so re-picking the
  // same filename refires `onChange` (the browser suppresses an identical value).
  const onPickFiles = (e: ChangeEvent<HTMLInputElement>) => {
    if (e.target.files) ingestFiles(e.target.files);
    e.target.value = "";
  };

  // Composer drag-and-drop. Each handler preventDefault + stopPropagation; onDrop
  // reads dataTransfer.files. A window-level guard (below) stops a MISSED drop
  // from navigating the tab to the file.
  const onDragOver = (e: ReactDragEvent) => {
    e.preventDefault(); e.stopPropagation();
    if (!sending) setDragActive(true);
  };
  const onDragLeave = (e: ReactDragEvent) => {
    e.preventDefault(); e.stopPropagation();
    setDragActive(false);
  };
  const onDrop = (e: ReactDragEvent) => {
    e.preventDefault(); e.stopPropagation();
    setDragActive(false);
    if (sending) return;
    if (e.dataTransfer.files?.length) ingestFiles(e.dataTransfer.files);
  };

  // Window-level dragover/drop guard — without this, a file dropped ANYWHERE
  // outside the composer makes the browser navigate the tab to the file:// URL,
  // blowing away the session. preventDefault on both kills that default.
  useEffect(() => {
    const stop = (e: DragEvent) => e.preventDefault();
    window.addEventListener("dragover", stop);
    window.addEventListener("drop", stop);
    return () => {
      window.removeEventListener("dragover", stop);
      window.removeEventListener("drop", stop);
    };
  }, []);

  // The attachments that are READY to ship (uploaded, no error). Files-only sends
  // are allowed, so we gate on text OR at least one ready attachment.
  const readyAttachments = attachments.filter((a) => !a.uploading && !a.error && a.text);
  const anyUploading = attachments.some((a) => a.uploading);
  const canSend = (draft.trim() !== "" || readyAttachments.length > 0) && !sending && !anyUploading;

  // Fire the turn, optionally forcing a stricter lane for this turn (the warn
  // modal's "Keep local" path). We clear the DRAFT optimistically, but NOT the
  // attachments — App clears this session's attachment bucket only on a
  // SUCCESSFUL send (MED), so a refused/failed send (403/network) keeps the
  // staged files for retry rather than silently dropping them.
  const fire = (text: string, sensitivityOverride?: Sensitivity) => {
    onSend(
      text,
      readyAttachments.map((a) => ({ filename: a.filename, text: a.text })),
      sensitivityOverride,
    );
    onDraftChange("");
  };

  const submit = () => {
    if (!canSend) return;
    const text = draft.trim();
    // WARN/ESCALATE: a document-bearing turn in a CLOUD-routed (general)
    // workspace is intercepted by a confirm modal. project/bd already route
    // locally → send straight through.
    if (workspaceType === "general" && readyAttachments.length > 0) {
      setPendingSend({ text });
      return;
    }
    fire(text);
  };

  return (
    <section className="flex flex-col flex-1 min-h-0 overflow-hidden">

      {/* Deal header — Paper 2B5-0: the deal/PROJECT name + lock (e.g. "Project
          Helix"). The session strip below carries the SESSION name + meta. */}
      <div className="flex-none flex flex-col gap-[16px] pt-[22px] pb-[18px] px-[30px] border-b border-line">
        <div className="flex items-center justify-between gap-[20px]">
          <div className="flex items-center gap-[10px] min-w-0">
            <h1 className="m-0 text-[23px] font-semibold text-t1 tracking-[-0.02em] leading-[27px] truncate">{dealName}</h1>
            <Lock size={16} strokeWidth={1.9} className="shrink-0 text-t3" />
          </div>
        </div>
      </div>

      {/* Session strip — Paper 54U-0: session label · meta on the left; CONTEXT
          meter, Handoff button, edit + kebab on the right. */}
      <div className="flex-none flex items-center justify-between flex-wrap gap-[10px] py-[9px] px-[30px] border-b border-line">
        <div className="flex items-center gap-[10px] min-w-0">
          {renaming ? (
            <input
              value={renameDraft}
              autoFocus
              onChange={(e) => setRenameDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") { e.preventDefault(); commitRename(); }
                else if (e.key === "Escape") { e.preventDefault(); setRenaming(false); }
              }}
              onBlur={commitRename}
              className="min-w-0 w-[230px] text-[13px] font-semibold text-t1 leading-none bg-bg-1 border border-accent-line rounded-[6px] px-[7px] py-[3px] outline-none"
            />
          ) : (
            <span className="min-w-0 flex items-center gap-[6px] text-[13px] font-semibold text-t1 leading-none">
              {pinned && <Pin size={12} strokeWidth={2} className="shrink-0 text-accent" />}
              <span className="truncate">{sessionTitle}</span>
            </span>
          )}
          <span className="shrink-0 w-[3px] h-[3px] rounded-full bg-t3" />
          <span className="shrink-0 text-[11px] font-medium text-t3 leading-none">{messages.length} messages</span>
          <span className="shrink-0 w-[3px] h-[3px] rounded-full bg-t3" />
          <span className="shrink-0 text-[11px] font-medium text-t3 leading-none">started {startedAt}</span>
        </div>
        <div className="flex items-center gap-[12px]">
          {/* CONTEXT meter — Paper 55B-0 */}
          <div className="flex items-center gap-[9px]">
            <span className="text-[9.5px] font-semibold tracking-[0.1em] uppercase text-t3 leading-none">Context</span>
            <div className="w-[74px] h-[5px] rounded-[3px] overflow-hidden shrink-0 bg-paper2">
              <div
                className={cn("h-full rounded-[3px] transition-[width]",
                  contextPct == null ? "bg-green" : contextPct >= 85 ? "bg-red" : contextPct >= 70 ? "bg-accent" : "bg-green")}
                style={{ width: contextPct != null ? `${Math.min(100, Math.max(0, contextPct))}%` : "0%" }}
              />
            </div>
            <span className={cn("mono text-[11px] font-medium leading-none tabular tabular-nums",
              contextPct == null ? "text-t3" : contextPct >= 85 ? "text-red" : contextPct >= 70 ? "text-accent" : "text-t2")}>
              {contextPct != null ? `${contextPct}%` : "—"}
            </span>
          </div>
          {/* Model picker (#minimax-chat-model) */}
          <ModelPicker model={model} onModelChange={onModelChange} minimaxAllowed={minimaxAllowed} />
          {/* Handoff — Paper 556-0: accent-soft pill with the arrow-out-of-panel
              glyph (3IC-0: polyline + open-panel path), currentColor = accent. */}
          <button type="button" className="flex items-center h-[28px] rounded-lg px-[11px] gap-[6px] bg-accent-soft border border-accent-line text-accent transition hover:brightness-105">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="shrink-0">
              <polyline points="15 14 20 9 15 4" />
              <path d="M4 20v-7a4 4 0 0 1 4-4h12" />
            </svg>
            <span className="text-[11.5px] font-semibold leading-none">Handoff</span>
          </button>
          {/* Edit + kebab — Paper 54W-0; #session-ops wires both. */}
          <div className="flex items-center gap-[5px]">
            <button type="button" title="Rename session" onClick={startRename} className="flex items-center justify-center h-[28px] w-[28px] rounded-[7px] text-t3 hover:text-t1 hover:bg-bg-2 transition-colors">
              <Pencil size={14} strokeWidth={1.8} />
            </button>
            <SessionOptionsMenu
              pinned={pinned}
              onRename={startRename}
              onArchive={onArchive}
              onTogglePin={onTogglePin}
              onDelete={onDelete}
            />
          </div>
        </div>
      </div>

      {/* Scrollable thread — Paper 53C-0: column, gap-24, py-26 px-30 */}
      <div ref={scrollRef} className="flex-1 overflow-y-auto min-h-0">
        <div className="flex flex-col gap-[24px] min-h-full py-[26px] px-[30px]">
          {messages.map((m) => <Bubble key={m.id} m={m} onCrewReply={onCrewReply} />)}
          {loading && messages.length === 0 && <HydrationSkeleton />}
          {!loading && messages.length === 0 && (
            <div className="m-auto flex flex-col items-center gap-[26px] py-[48px]">
              <div className="w-[62px] h-[62px] flex items-center justify-center rounded-full shrink-0 bg-accent-soft">
                <span className="font-semibold text-accent text-[27px] leading-none">A</span>
              </div>
              <div className="flex flex-col items-center gap-[11px]">
                <div className="font-semibold text-t1 text-[24px] leading-[30px]">Start the conversation</div>
                <div className="max-w-[420px] text-center text-[13px] leading-[1.6] text-t3">
                  Ask anything, or open a workflow below.
                </div>
              </div>
            </div>
          )}
        </div>
      </div>

      {/* Composer dock — Paper 532-0: a single rounded card (lock icon ·
          textarea · ↵ hint · round accent send), pt-14 pb-22 px-30, top border.
          #chat-attach: drag-drop onto the card + a paperclip + an above-textarea
          file-chip row. */}
      <div className="flex-none flex flex-col pt-[14px] pb-[22px] px-[30px] border-t border-line">
        <div
          onDragOver={onDragOver}
          onDragLeave={onDragLeave}
          onDrop={onDrop}
          className={cn(
            "flex flex-col rounded-[13px] bg-bg-1 border shadow-card transition-colors",
            dragActive ? "border-accent ring-2 ring-accent-line" : "border-line-2",
            sending && "opacity-60",
          )}
        >
          {/* File chips — above the textarea (Paper 53T-0 pill styling). */}
          {attachments.length > 0 && (
            <div className="flex flex-wrap gap-[8px] px-[16px] pt-[12px]">
              {attachments.map((a) => (
                <AttachmentChip key={a.id} att={a} onRemove={() => removeAttachment(a.id)} />
              ))}
            </div>
          )}

          {/* Input row — lock · paperclip · textarea · clear · hint · send. */}
          <div className="flex items-end pr-[10px] pl-[18px] gap-[12px]">
            <Lock size={16} strokeWidth={1.8} className="shrink-0 text-t3 mb-[18px]" />
            {/* Paperclip — opens the hidden multi-file picker. */}
            <input
              ref={fileInputRef}
              type="file"
              multiple
              accept={ATTACH_ACCEPT}
              onChange={onPickFiles}
              className="hidden"
            />
            <button
              type="button"
              onClick={() => fileInputRef.current?.click()}
              disabled={sending}
              title="Attach a document"
              className="shrink-0 mb-[14px] flex h-[26px] w-[26px] items-center justify-center rounded-[7px] text-t3 transition-colors hover:text-accent hover:bg-bg-2 disabled:cursor-not-allowed disabled:opacity-50"
            >
              <Paperclip size={16} strokeWidth={1.8} />
            </button>
            <textarea
              ref={composerRef}
              value={draft}
              onChange={(e) => onDraftChange(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submit(); }
                // "/" on an empty composer opens the workflow palette (v2: the
                // palette is a `/`-triggered overlay, not the old bottom drawer).
                else if (e.key === "/" && draft.trim() === "") { e.preventDefault(); onSlash?.(); }
              }}
              disabled={sending}
              rows={1}
              placeholder={sending ? "Sending…" : "Ask Anton, or type / for a tool"}
              // overflow-y is owned by useAutoGrow (hidden until the cap is hit,
              // then auto); start hidden so a one-line composer has no scrollbar.
              className="block w-full py-[16px] bg-transparent border-0 outline-none text-[14px] leading-[1.5] text-t1 placeholder:text-t3 resize-none overflow-y-hidden font-inherit disabled:cursor-not-allowed"
              style={{ minHeight: "24px", maxHeight: "min(184px, 30vh)" }}
            />
            {draft.trim() !== "" && !sending && (
              <button
                type="button"
                onClick={() => onDraftChange("")}
                title="Clear draft"
                className="shrink-0 mb-[20px] mono text-[11px] text-t3 transition-colors hover:text-t1"
              >
                Clear
              </button>
            )}
            <button
              type="button"
              onClick={submit}
              disabled={!canSend}
              title={sending ? "Sending…" : anyUploading ? "Waiting for uploads…" : "Send"}
              className="shrink-0 mb-[9px] flex h-[36px] items-center gap-[7px] rounded-[10px] px-[15px] bg-accent-soft border border-accent-line text-accent text-[13px] font-semibold transition hover:brightness-105 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {sending ? "Working…" : "Send"}
              <span className="mono text-[11px] text-accent opacity-80">↵</span>
            </button>
          </div>
        </div>
      </div>

      {/* Cloud-routing warn/escalate modal (#chat-attach) — general workspace +
          ≥1 attachment. "Keep local" forces sensitivity_override:"confidential"
          for this turn; "Send to cloud" sends normally. */}
      {pendingSend && (
        <CloudAttachWarnModal
          onKeepLocal={() => { const p = pendingSend; setPendingSend(null); fire(p.text, "confidential"); }}
          onSendCloud={() => { const p = pendingSend; setPendingSend(null); fire(p.text); }}
          onCancel={() => setPendingSend(null)}
        />
      )}
    </section>
  );
}

function HydrationSkeleton() {
  // Three grey bars at varying widths, animating quietly — only shown when
  // GET /messages is in flight on an empty thread.
  return (
    <div className="flex flex-col gap-[14px] py-[8px]">
      {[64, 88, 52].map((w, i) => (
        <div
          key={i}
          className="h-[44px] rounded-xl bg-bg-1 border border-line"
          style={{
            width: `${w}%`,
            opacity: 0.55,
            animation: "pulse 1.8s ease-in-out infinite",
            animationDelay: `${i * 0.15}s`,
            alignSelf: i % 2 ? "flex-end" : "flex-start",
          }}
        />
      ))}
    </div>
  );
}


// ── Bubble + sub-components ────────────────────────────────────────────────

function Bubble({ m, onCrewReply }: { m: Message; onCrewReply: (runId: string, msgId: string, response: string) => Promise<void> }) {
  const isUser = m.role === "user";
  // Paper: the prose surface is only drawn when there's prose to carry. KPI grid,
  // chips, running/steps rows and the crew-reply box live as siblings BELOW it so
  // they sit on the off-white ground (Paper "Msg: Anton 1" = bubble + result card
  // + chips stacked in one message column).
  const hasBubble = !!(m.body || m.commentary);
  return (
    <div className={cn(
      // Paper per-role column caps: Anton 772px (max-w-193), Operator 540px (max-w-135).
      "flex flex-col",
      isUser ? "self-end items-end max-w-[540px]" : "self-start items-start max-w-[772px]",
    )}>
      {/* Head — Paper 54Q-0 / 53M-0 meta line. For users the order flips
          (time then name) so the row reads right-aligned. */}
      <div className={cn("flex gap-[9px] items-center mb-[10px] px-[2px]", isUser && "flex-row-reverse")}>
        <span className={cn(
          "text-[11.5px] font-semibold tracking-[0.04em]",
          m.failed ? "text-red" : isUser ? "text-t2" : "text-accent",
        )}>{m.who}</span>
        <span className="w-[3px] h-[3px] rounded-full bg-t3" />
        <span className="mono text-[10.5px] tracking-[0.02em] text-t3">{m.time}</span>
        {m.durationMs !== undefined && (<><span className="text-t4">·</span><span className="mono text-[10.5px] text-t3">{(m.durationMs/1000).toFixed(1)}s</span></>)}
        {m.route && <span className="mono text-[10.5px] text-t3 tracking-[0.02em]">· {m.route}</span>}
        {m.running && <span className="mono text-[10.5px] text-accent tracking-[0.02em]">· RUNNING…</span>}
        {m.unwired && <span className="mono text-[10.5px] text-t3 tracking-[0.02em]">· STUB</span>}
        {m.failed && <span className="mono text-[10.5px] text-red tracking-[0.02em]">· FAILED</span>}
      </div>

      {/* Prose bubble — ANTON = sage-tinted --bubble-anton (theme-flips: mint in
          light, dark sage in navy); Operator = paper2; failed/unwired tone the surface
          so failures aren't visually identical to real replies. */}
      {hasBubble && (
        <div
          className={cn(
            "border py-[12px] px-[16px] text-[14px] leading-[1.64] max-w-[600px] relative",
            m.failed
              ? "bg-bg-1 text-t1 border-red/40"
              : m.unwired
                ? "bg-bg-1 text-t2 border-line"
                : isUser
                  ? "bg-paper2 text-t1 border-line"
                  : "bg-[var(--bubble-anton)] text-t1 border-line",
          )}
          style={{
            borderTopLeftRadius: "14px",
            borderTopRightRadius: "14px",
            borderBottomRightRadius: isUser ? "4px" : "14px",
            borderBottomLeftRadius:  isUser ? "14px" : "4px",
          }}
        >
          {/* Assistant replies render as markdown (paragraphs, lists, code,
              headings, safe links). User text stays literal — whitespace-pre-wrap
              preserves the user's own newlines; we never reinterpret typed text
              as markdown. */}
          {m.body && (isUser
            ? <span className="whitespace-pre-wrap">{m.body}</span>
            : <Markdown>{m.body}</Markdown>)}

          {m.commentary && (
            <p className={cn("text-t2 text-[13px] leading-[1.6] font-inherit", m.body && "mt-[12px]")}>
              {m.commentary}
            </p>
          )}
        </div>
      )}

      {/* Result card — Paper 549-0: connected white KPI strip, sits below the bubble */}
      {m.kpis && <div className={cn(hasBubble && "mt-[12px]")}><KPICard cells={m.kpis} /></div>}

      {/* Live status one-liner — Paper Desk 1b "{{ statusText }} … {{ elapsed }}s".
          Renders whenever the turn carries a `runningText` (skill + crew runs set
          it; a plain chat turn now gets a client-derived phase sequence from
          App.handleSend). Resolves into the streamed answer below once `running`
          clears. (Brief item 1.) */}
      {m.runningText && (
        <StatusLine
          text={m.runningText}
          etaMs={m.etaMs}
          running={!!m.running}
          className={cn(hasBubble && "mt-[12px]")}
        />
      )}

      {m.steps && (
        <div className={cn("flex flex-wrap items-center gap-x-[4px] gap-y-[3px] text-t2 text-[12px]", (hasBubble || m.runningText) && "mt-[10px]")}>
          {m.steps.map((s, i) => (
            <span key={i} className="inline-flex items-center gap-[6px]">
              <span className={cn("text-[11px]", s.ok ? "text-green" : "text-accent")}>{s.ok ? "✓" : "●"}</span>
              <span>{s.text}</span>
              {i < m.steps!.length - 1 && <span className="text-t4 px-[3px]">·</span>}
            </span>
          ))}
        </div>
      )}

      {/* Chips — Paper 53T-0: pill row below the result. Primary = accent-soft
          icon pill; secondary = bg pill with a line-2 border. */}
      {m.chips && (
        <div className={cn("flex gap-[9px] flex-wrap", (hasBubble || m.kpis || m.runningText || m.steps) && "mt-[15px]")}>
          {m.chips.map((c, i) => (
            <button
              key={i}
              type="button"
              disabled={!c.action}
              onClick={() => handleChipClick(c.action)}
              title={chipTooltip(c.action)}
              className={cn(
                "flex items-center h-[33px] rounded-[9px] px-[13px] gap-[7px] text-[12.5px] transition-colors",
                c.action ? "cursor-pointer" : "cursor-default opacity-55",
                c.primary
                  ? "bg-accent-soft border border-accent-line text-accent font-semibold hover:brightness-105 disabled:hover:brightness-100"
                  : "bg-bg border border-line-2 text-t2 font-medium hover:border-accent-line hover:text-accent disabled:hover:border-line-2 disabled:hover:text-t2",
              )}
            >
              {/* Leading table/window glyph — Paper 2EG-0 (primary "Open model"). */}
              {c.icon === "window" && (
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="shrink-0">
                  <rect x="3" y="4" width="18" height="16" rx="2" />
                  <line x1="3" y1="9" x2="21" y2="9" />
                  <line x1="9" y1="9" x2="9" y2="20" />
                </svg>
              )}
              <span>{c.label}</span>
              {/* Trailing right-arrow glyph — Paper 2EN-0 ("Run sensitivity"). */}
              {c.icon === "arrow" && (
                <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="shrink-0">
                  <line x1="5" y1="12" x2="19" y2="12" />
                  <polyline points="13 6 19 12 13 18" />
                </svg>
              )}
            </button>
          ))}
        </div>
      )}

      {m.lane === "crew" && m.crewAsk && m.crewRunId && (
        // key by msgId so a second sequential ask remounts a FRESH box
        // (clears text/busy/err) even if its SSE event races ahead of the
        // first reply's POST response.
        <div className="w-full">
          <CrewReplyBox key={m.crewAsk.msgId} runId={m.crewRunId} ask={m.crewAsk} onReply={onCrewReply} />
        </div>
      )}
    </div>
  );
}

/**
 * Live status one-liner shown in an in-progress assistant turn (Brief item 1,
 * Paper Desk 1b). A pulsing accent dot + the current phase text (`text`,
 * truncated to one line so it never wraps on a 13″) + a mono elapsed counter
 * that ticks every 100ms from when the line first mounted. While `running`, two
 * faint shimmer bars sit below it (Paper `.ms-shim`), echoing "answer streaming
 * in". Once the turn resolves (`running` clears but a transient `runningText`
 * lingers — e.g. a crew "resuming…"), the shimmer + timer drop and it reads as a
 * static note.
 *
 * `prefers-reduced-motion`: no dot-pulse, no shimmer, and the phase text is
 * replaced by a single static "Working…" (the elapsed timer still updates — it's
 * information, not motion). Honors the brief's "single static Working…" fallback.
 */
function StatusLine({
  text, etaMs, running, className,
}: {
  text: string;
  etaMs?: number;
  running: boolean;
  className?: string;
}) {
  const reduce = usePrefersReducedMotion();
  const [elapsed, setElapsed] = useState(0);
  const startRef = useRef<number>(Date.now());

  // Tick the elapsed timer only while running. Reset the clock if the line
  // remounts for a new run (the placeholder is keyed by message id upstream, so
  // a fresh running turn gets a fresh StatusLine + a fresh start).
  useEffect(() => {
    if (!running) return;
    const id = window.setInterval(() => {
      setElapsed((Date.now() - startRef.current) / 1000);
    }, 100);
    return () => window.clearInterval(id);
  }, [running]);

  // Under reduced-motion, collapse the live phase wording to a single calm label
  // (no per-phase churn reads as motion to motion-sensitive users).
  const phase = reduce && running ? "Working…" : text;

  return (
    <div className={cn("flex flex-col gap-[11px]", className)}>
      <div className="flex items-center gap-[11px] text-t2 text-[13.5px] font-medium">
        <span
          className={cn("h-[9px] w-[9px] shrink-0 rounded-full bg-accent", running && !reduce && "status-pulse")}
        />
        {/* truncate → one line on a 13″ (brief compact guard) */}
        <span className="min-w-0 flex-1 truncate">{phase}</span>
        {running ? (
          <span className="shrink-0 mono text-accent text-[11.5px] tabular tabular-nums">{elapsed.toFixed(1)}s</span>
        ) : etaMs !== undefined ? (
          <span className="shrink-0 mono text-t3 text-[11.5px] tabular">~{Math.round(etaMs / 1000)}s</span>
        ) : null}
      </div>
      {running && !reduce && (
        <>
          <div className="status-shimmer h-[12px] rounded-[4px]" style={{ width: "94%" }} />
          <div className="status-shimmer h-[12px] rounded-[4px]" style={{ width: "74%" }} />
        </>
      )}
    </div>
  );
}

/** Inline reply surface for a crew BLOCKED on a mid-run human-input ask.
 *  Renders the crew's question + a textarea; Enter submits (Shift+Enter =
 *  newline, mirroring the composer). On submit it calls `onReply`, which POSTs
 *  the answer; App clears `m.crewAsk` on success/404 (this unmounts), so we
 *  only surface inline errors for genuine failures (network / 5xx) and let the
 *  operator retry. Mirrors the composer's surface (rounded card + amber Send
 *  pill) with an accent border to flag "action needed". */
function CrewReplyBox({
  runId, ask, onReply,
}: {
  runId: string;
  ask: { msgId: string; prompt: string };
  onReply: (runId: string, msgId: string, response: string) => Promise<void>;
}) {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const boxRef = useRef<HTMLTextAreaElement>(null);
  useAutoGrow(boxRef, text, 180);   // grow up to ~8 rows (~180px) then scroll (matches the composer)

  const send = async () => {
    const answer = text.trim();
    if (!answer || busy) return;
    setBusy(true);
    setErr(null);
    try {
      await onReply(runId, ask.msgId, answer);
      // success (or a 404 App swallows) → App clears m.crewAsk and this box
      // unmounts; nothing more to do here.
    } catch (e) {
      // Reached only on a genuine failure (App resolves on 404). Keep the box
      // mounted so the operator can retry.
      setErr(e instanceof Error ? e.message : "Reply failed — try again.");
      setBusy(false);
    }
  };

  return (
    <div className="mt-[14px] rounded-xl border border-accent-line bg-bg-2 overflow-hidden">
      <div className="flex items-center gap-[7px] px-[16px] pt-[12px] text-[10px] font-semibold tracking-[0.14em] uppercase text-accent">
        <span className="h-[6px] w-[6px] rounded-full bg-accent" />
        Crew needs your input
      </div>
      <div className="px-[16px] pt-[7px] pb-[11px] text-[13px] leading-[1.6] text-t1 whitespace-pre-wrap">
        {ask.prompt}
      </div>
      <textarea
        ref={boxRef}
        value={text}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); void send(); } }}
        disabled={busy}
        autoFocus
        placeholder="Type your reply…"
        className="block w-full px-[16px] py-[12px] bg-bg-1 border-t border-line outline-none text-[13px] leading-[1.5] text-t1 placeholder:text-t3 resize-none overflow-y-auto font-inherit disabled:cursor-not-allowed"
        style={{ minHeight: "52px" }}
      />
      <div className="flex justify-between items-center px-[16px] py-[10px] border-t border-line gap-[12px]">
        <span className={cn("text-[11.5px] min-w-0 truncate", err ? "text-red" : "text-t3")}>
          {err ? err : busy ? "Sending…" : "↵ send · ⇧↵ newline · crew is paused"}
        </span>
        <button
          type="button"
          onClick={() => void send()}
          disabled={busy || !text.trim()}
          className="shrink-0 rounded-lg px-[18px] py-[8px] bg-accent text-white text-[12.5px] font-medium hover:brightness-110 disabled:cursor-not-allowed disabled:opacity-50"
        >{busy ? "Sending…" : "Send reply →"}</button>
      </div>
    </div>
  );
}

// ── Attachment chip (#chat-attach) ─────────────────────────────────────────
/** One file chip above the composer textarea: filename + char count (or an
 *  uploading/error state) + a remove ×. Reuses the Paper 53T-0 secondary-pill
 *  styling (bg + line-2 border); an error chip flips to the red tone. */
function AttachmentChip({ att, onRemove }: { att: Attachment; onRemove: () => void }) {
  const isError = !!att.error;
  return (
    <span
      className={cn(
        "inline-flex items-center h-[30px] rounded-[9px] pl-[11px] pr-[7px] gap-[8px] text-[12px] max-w-[260px] border",
        isError
          ? "bg-red/10 border-red/40 text-red"
          : "bg-bg border-line-2 text-t2",
      )}
      title={att.error ? `${att.filename} — ${att.error}` : att.filename}
    >
      <Paperclip size={13} strokeWidth={1.8} className={cn("shrink-0", isError ? "text-red" : "text-t3")} />
      <span className="min-w-0 truncate font-medium">{att.filename}</span>
      <span className={cn("shrink-0 mono text-[10.5px]", isError ? "text-red" : "text-t3")}>
        {att.uploading
          ? "uploading…"
          : att.error
            ? att.error
            : `${att.chars.toLocaleString()} chars${att.truncated ? " · truncated" : ""}`}
      </span>
      <button
        type="button"
        onClick={onRemove}
        title="Remove"
        className={cn(
          "shrink-0 flex h-[18px] w-[18px] items-center justify-center rounded-[5px] transition-colors",
          isError ? "text-red hover:bg-red/15" : "text-t3 hover:text-t1 hover:bg-bg-2",
        )}
      >
        <X size={13} strokeWidth={2} />
      </button>
    </span>
  );
}

// ── Cloud-routing warn/escalate modal (#chat-attach) ───────────────────────
/** Shown when a document-bearing turn is about to send in a CLOUD-routed
 *  (general) workspace. Two actions: "Keep local" forces the local lane for the
 *  turn (sensitivity_override:"confidential"); "Send to cloud" sends normally.
 *  Mirrors the existing modal chrome (BudgetAckModal): backdrop + ESC close,
 *  rounded card, accent top-strip. */
function CloudAttachWarnModal({
  onKeepLocal, onSendCloud, onCancel,
}: {
  onKeepLocal: () => void;
  onSendCloud: () => void;
  onCancel: () => void;
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onCancel(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onCancel]);

  return (
    <div
      className="fixed inset-0 z-[1000] flex items-center justify-center p-6 bg-black/60 backdrop-blur-sm"
      onClick={(e) => { if (e.target === e.currentTarget) onCancel(); }}
      role="dialog"
      aria-modal="true"
      aria-labelledby="cloud-attach-warn-title"
    >
      <div className="flex w-full max-w-[460px] flex-col overflow-hidden rounded-[16px] border border-line bg-bg-1 shadow-modal">
        <div className="h-[3px] shrink-0 bg-accent" />
        <div className="flex flex-none items-center gap-[10px] border-b border-line px-[20px] py-[14px]">
          <Lock size={16} strokeWidth={1.9} className="shrink-0 text-accent" />
          <h2 id="cloud-attach-warn-title" className="text-[15px] font-semibold text-t1">
            Document on a cloud-routed chat
          </h2>
        </div>
        <div className="px-[20px] py-[18px]">
          <p className="text-[13px] leading-[1.6] text-t2">
            This document may contain deal-sensitive material, and this chat routes to the
            cloud. Keep this turn on the local model?
          </p>
        </div>
        <div className="flex flex-none items-center justify-end gap-[8px] border-t border-line bg-bg-2/40 px-[20px] py-[12px]">
          <button
            type="button"
            onClick={onSendCloud}
            className="rounded-lg border border-line-2 px-[14px] py-[7px] text-[12.5px] text-t2 transition-colors hover:border-accent-line hover:text-t1"
          >Send to cloud</button>
          <button
            type="button"
            onClick={onKeepLocal}
            className="rounded-lg bg-accent px-[16px] py-[7px] text-[12.5px] font-medium text-white transition hover:brightness-110"
          >Keep local</button>
        </div>
      </div>
    </div>
  );
}

function KPICard({ cells }: { cells: KPICell[] }) {
  // Paper 549-0 — a connected white strip: each metric is a column (label over a
  // big mono value) divided by 1px hairlines, no grid wrap. Flagged / first cell
  // takes the accent value tone. Wraps on overflow so a long metric set still fits.
  return (
    <div className="flex flex-wrap self-start rounded-[14px] overflow-hidden bg-bg-1 border border-line shadow-card">
      {cells.map((c, i) => (
        <div key={i} className="flex items-center">
          {i > 0 && <div className="self-stretch w-px shrink-0 bg-line" />}
          {/* A genuine covenant/breach flag tints RED (not accent) so it reads as
              a risk, not just an emphasised metric (mock 1b: rgba(196,104,90,…)). */}
          <div className={cn("flex flex-col py-[13px] px-[20px] gap-[7px]", c.flag && "bg-red/10")}>
            <div className={cn(
              "text-[10px] font-semibold tracking-[0.08em] uppercase leading-none",
              c.flag ? "text-red" : "text-t3",
            )}>{c.label}</div>
            <div className={cn(
              "mono text-[23px] font-medium leading-none",
              c.flag ? "text-red" : "text-t1",
            )}>
              {c.unitBefore && <span className="text-t2 text-[15px] font-normal mr-[1px]">{c.unitBefore}</span>}
              {c.value}
              {c.unit && <span className="text-t2 text-[15px] font-normal ml-[1px]">{c.unit}</span>}
            </div>
            {c.delta && (
              <div className={cn(
                "text-[10.5px] tracking-[0.01em] leading-none",
                c.flag ? "text-red" : "text-t3",
              )}>{c.delta}</div>
            )}
          </div>
        </div>
      ))}
    </div>
  );
}

// ── Chip click dispatcher ──────────────────────────────────────────────────
// Lives at module scope so it doesn't recreate per-render. Future actions
// (download, copy-to-clipboard, inline-expand) plug in here. Server-side
// fan-outs land via the #22 dispatcher pattern, not here.
function handleChipClick(action: ChipAction | undefined): void {
  if (!action) return;
  switch (action.type) {
    case "open-file": {
      if (!action.path) return;
      // Normalise Windows backslashes; ensure file:/// triple-slash on bare paths
      const norm = action.path.replace(/\\/g, "/");
      const url = norm.startsWith("file:") ? norm : `file:///${norm.replace(/^\/+/, "")}`;
      // scheme-allowlist: only open a genuine file: URL — a server-supplied path
      // that starts with "file:" but smuggles another scheme is rejected
      // (#chip-open-file-scheme; the file-side analogue of the open-url guard).
      if (!isSafeFileUrl(url)) return;
      window.open(url, "_blank", "noopener,noreferrer");
      return;
    }
    case "open-url":
      // scheme-allowlist: only navigate to http(s); reject javascript:/data:/
      // etc. (latent XSS sink — #sec-shannon-residuals XSS-VULN-02).
      if (!isSafeHttpUrl(action.url)) return;
      window.open(action.url, "_blank", "noopener,noreferrer");
      return;
    case "show-modal":
      // Session D will swap this for real modal dispatch (news drawer, comps
      // expand, warnings list). For now we log + leave a hook for the
      // operator to grep — keeps the chip visibly clickable.
      // eslint-disable-next-line no-console
      console.info(`[modal] ${action.modalId} — full modal lands in Session D`);
      return;
  }
}

function chipTooltip(action: ChipAction | undefined): string | undefined {
  if (!action) return undefined;
  switch (action.type) {
    case "open-file":  return action.path;
    case "open-url":   return action.url;
    case "show-modal": return `Opens: ${action.modalId}`;
  }
}
