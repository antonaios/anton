import { useEffect, useMemo, useRef, useState } from "react";
import { X, Plus, ChevronLeft, Search } from "lucide-react";
import { cn } from "../lib/cn";
import { IconButton } from "./ui/IconButton";
import type { WorkspaceType } from "../types";

/** A switchable workspace row (real data from App's workspace lists). */
export interface PickerWorkspace {
  type: WorkspaceType;
  name: string;
  age: string;       // relative age from last_touched, e.g. "2h ago" / "yesterday"
}

interface Props {
  open: boolean;
  onClose: () => void;
  /** Switch to an existing workspace (no API round-trip — App.setWorkspace). */
  onSwitch: (ws: { type: WorkspaceType; name: string }) => void;
  /** Create a NEW workspace. Throw/reject with an ApiError-shaped object to
   *  surface inline errors; resolve to close. App owns the api.createWorkspace
   *  call so the lists refresh + the new workspace activates. */
  onCreate: (ws: { type: WorkspaceType; name: string }) => Promise<void>;
  /** Real switchable workspaces (project + BD + general), most-recent first. */
  workspaces: PickerWorkspace[];
  /** The currently-active workspace name — highlighted in the list. */
  activeName?: string;
  counts: { project: number; bd: number; general: number };
  /** Type pre-selected when opening straight into create mode (GENERAL's +New). */
  initialType?: WorkspaceType;
}

const PATH_TEMPLATES: Record<WorkspaceType, string> = {
  project: "<workspace-root>\\1. Projects\\<name>",
  bd:      "<workspace-root>\\2. Business development\\<name>",
  general: "<workspace-root>\\3. General\\<name>",
};

const TYPE_LABEL: Record<WorkspaceType, string> = {
  project: "Projects",
  bd:      "BD",
  general: "General",
};

const TYPE_SUB: Record<WorkspaceType, string> = {
  project: "Project",
  bd:      "BD",
  general: "General",
};

// Client-side mirror of the server's regex (workspaces.py _NAME_RE) so we can
// disable the CREATE button before submission. Server-side is the source of
// truth — 422 surfaces if this lags.
const NAME_RE = /^[A-Za-z0-9][A-Za-z0-9 _-]{0,63}$/;

/**
 * Workspace picker — matching the agreed Paper **v2** "Switch workspace"
 * artboard (AJB-0): SWITCH-first, not create-first.
 *
 * Default ("switch") view: a search field + a flat list of the real
 * workspaces (avatar initials · name · type · age), the active one tinted;
 * click a row to switch. A dashed "New workspace" row at the bottom opens the
 * ("create") view — the three type cards + name input + Create. Both the
 * switch and create paths reuse App's existing handlers + the 409→switch
 * semantics, so behaviour is unchanged; only the primary affordance flipped.
 */
export function WorkspacePickerModal({
  open, onClose, onSwitch, onCreate, workspaces, activeName, counts, initialType = "project",
}: Props) {
  const [mode, setMode] = useState<"switch" | "create">("switch");
  const [query, setQuery] = useState("");
  const [type, setType] = useState<WorkspaceType>(initialType);
  const [name, setName] = useState("");
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);

  // Reset on open. If App opened us preset to a non-default type (GENERAL's
  // +New), jump straight into create mode on that type.
  useEffect(() => {
    if (!open) return;
    const createFirst = initialType !== "project";
    setMode(createFirst ? "create" : "switch");
    setType(initialType);
    setQuery("");
    setName("");
    setError(null);
    setCreating(false);
    const t = setTimeout(() => inputRef.current?.focus(), 50);
    return () => clearTimeout(t);
  }, [open, initialType]);

  // Focus the name input when entering create mode.
  useEffect(() => {
    if (open && mode === "create") {
      const t = setTimeout(() => inputRef.current?.focus(), 50);
      return () => clearTimeout(t);
    }
  }, [open, mode]);

  const validName = NAME_RE.test(name.trim());

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return q ? workspaces.filter((w) => w.name.toLowerCase().includes(q)) : workspaces;
  }, [workspaces, query]);

  const submit = async (ws: { type: WorkspaceType; name: string }) => {
    if (creating) return;
    setCreating(true);
    setError(null);
    try {
      await onCreate(ws);
      // App closes the modal on success
    } catch (e) {
      setError(formatError(e));
      setCreating(false);
    }
  };

  // ESC closes; Enter creates (create mode, valid name).
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !creating) onClose();
      if (e.key === "Enter" && mode === "create" && validName && !creating) {
        void submit({ type, name: name.trim() });
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, mode, creating, validName, type, name]);

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-[1000] flex items-center justify-center bg-black/60 p-6 backdrop-blur-sm"
      onClick={(e) => { if (e.target === e.currentTarget && !creating) onClose(); }}
      role="dialog"
      aria-modal="true"
      aria-labelledby="picker-title"
    >
      <div className="flex max-h-[88vh] w-full max-w-[560px] flex-col overflow-hidden rounded-[16px] border border-line bg-bg-1 shadow-modal">
        {/* Top-accent strip — neutral/command */}
        <div className="h-[3px] shrink-0 bg-accent" />

        {/* Header */}
        <div className="flex items-start justify-between gap-[12px] px-[26px] pt-[22px] pb-[14px]">
          <div className="flex items-start gap-[10px]">
            {mode === "create" && (
              <button
                type="button"
                onClick={() => !creating && setMode("switch")}
                disabled={creating}
                aria-label="Back to switch"
                className="mt-[2px] flex size-[22px] shrink-0 items-center justify-center rounded-[7px] text-t3 transition-colors hover:bg-bg-2 hover:text-t1 disabled:opacity-40"
              >
                <ChevronLeft size={16} />
              </button>
            )}
            <div className="flex flex-col gap-[3px]">
              <h2 id="picker-title" className="text-[17px] font-semibold leading-[22px] tracking-[-0.01em] text-t1">
                {mode === "switch" ? "Switch workspace" : "New workspace"}
              </h2>
              <span className="text-[12.5px] leading-[16px] text-t2">
                {mode === "switch"
                  ? "Project · BD · General — each scopes its own budget"
                  : "Pick a type, then name it."}
              </span>
            </div>
          </div>
          <IconButton icon={X} label="Close" onClick={onClose} disabled={creating} />
        </div>

        {mode === "switch" ? (
          /* ── SWITCH view ─────────────────────────────────────────── */
          <div className="flex min-h-0 flex-col px-[26px] pb-[22px]">
            {/* Search */}
            <div className="flex items-center gap-[10px] rounded-[9px] border border-line-2 bg-bg-2 px-[14px] py-[10px]">
              <Search size={14} className="shrink-0 text-t3" />
              <input
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="Search deals…"
                autoComplete="off"
                autoFocus
                className="min-w-0 flex-1 bg-transparent text-[13px] text-t1 outline-none placeholder:text-t3"
              />
              <span className="mono shrink-0 text-[10px] text-t4">{workspaces.length} active</span>
            </div>

            {/* Workspace list */}
            <div className="mt-[14px] flex min-h-0 flex-col gap-[7px] overflow-y-auto">
              {filtered.length === 0 ? (
                <div className="px-[4px] py-[10px] text-[12px] italic text-t3">
                  {workspaces.length === 0 ? "No workspaces yet." : "No match."}
                </div>
              ) : (
                filtered.map((w) => {
                  const active = !!activeName && w.name === activeName;
                  return (
                    <button
                      key={`${w.type}/${w.name}`}
                      type="button"
                      onClick={() => { onSwitch({ type: w.type, name: w.name }); onClose(); }}
                      className={cn(
                        "flex items-center gap-[12px] rounded-[10px] border px-[13px] py-[11px] text-left transition-colors",
                        active
                          ? "border-accent-line bg-accent-soft"
                          : "border-line hover:border-line-2 hover:bg-bg-2",
                      )}
                    >
                      <span className={cn(
                        "mono flex size-[30px] shrink-0 items-center justify-center rounded-[8px] text-[11px]",
                        active ? "bg-accent text-white" : "bg-accent-soft text-t2",
                      )}>
                        {initials(w.name)}
                      </span>
                      <span className="flex min-w-0 grow flex-col gap-[2px]">
                        <span className="truncate text-[13.5px] font-semibold leading-[18px] text-t1">{w.name}</span>
                        <span className="truncate text-[11px] leading-[14px] text-t2">{TYPE_SUB[w.type]}</span>
                      </span>
                      <span className="shrink-0 text-[10.5px] text-t3">{active ? "active now" : w.age}</span>
                    </button>
                  );
                })
              )}
            </div>

            {/* New workspace row */}
            <button
              type="button"
              onClick={() => { setType("project"); setMode("create"); }}
              className="mt-[12px] flex items-center gap-[10px] rounded-[10px] border border-dashed border-line-2 px-[13px] py-[11px] text-left transition-colors hover:border-accent-line hover:bg-accent-soft"
            >
              <span className="flex size-[30px] shrink-0 items-center justify-center rounded-[8px] border border-line-2 text-t2">
                <Plus size={16} />
              </span>
              <span className="grow text-[13px] font-medium text-t2">New workspace · Project / BD / General</span>
            </button>
          </div>
        ) : (
          /* ── CREATE view ─────────────────────────────────────────── */
          <div className="flex min-h-0 flex-col overflow-y-auto border-t border-line px-[26px] py-[20px]">
            {/* Three type cards */}
            <div className="grid grid-cols-3 gap-[8px]" role="radiogroup" aria-label="Workspace type">
              {(["project", "bd", "general"] as WorkspaceType[]).map((t) => (
                <button
                  key={t}
                  type="button"
                  role="radio"
                  aria-checked={type === t}
                  disabled={creating}
                  onClick={() => setType(t)}
                  className={cn(
                    "rounded-lg border p-[22px_16px] text-center transition-colors",
                    type === t
                      ? "border-accent-line bg-accent-soft"
                      : "border-line bg-bg hover:border-accent-line hover:bg-accent-soft",
                    creating && "opacity-60 cursor-default",
                  )}
                >
                  <div className={cn(
                    "text-[13px] font-semibold uppercase tracking-[0.16em]",
                    type === t ? "text-accent" : "text-t1",
                  )}>
                    {TYPE_LABEL[t]}
                  </div>
                  <div className={cn(
                    "mt-[14px] text-[10.5px] tracking-[0.1em]",
                    type === t ? "text-accent" : "text-t3",
                  )}>
                    {t === "project" ? `${counts.project} active`
                    : t === "bd"      ? `${counts.bd} active`
                    : `${counts.general} entities`}
                  </div>
                </button>
              ))}
            </div>

            {/* Name input */}
            <div className="mt-[18px] flex items-center gap-[8px]">
              <input
                ref={inputRef}
                value={name}
                onChange={(e) => { setName(e.target.value); setError(null); }}
                disabled={creating}
                placeholder="Type a name — e.g. Heartwood-Bid, Acme-Telco, ABC Ltd"
                autoComplete="off"
                className="flex-1 rounded-lg border border-line-2 bg-bg-2 px-[11px] py-[8px] text-[12.5px] text-t1 outline-none transition-colors placeholder:text-t4 focus:border-accent-line disabled:opacity-60"
              />
            </div>
            <div className="mt-[8px] text-[10px] uppercase tracking-[0.14em] text-t3">
              Will create: <span className="font-mono text-[11px] normal-case tracking-normal text-t2">
                {PATH_TEMPLATES[type].replace("<name>", name.trim() || "<name>")}
              </span>
            </div>

            {name && !validName && !error && (
              <div className="mt-[6px] text-[10.5px] text-amber">
                Must start with a letter or digit; only letters, digits, space, underscore, or hyphen; 1–64 chars.
              </div>
            )}

            {error && (
              <div className="mt-[10px] rounded-lg border border-red/40 bg-red/10 px-[11px] py-[7px] text-[11.5px] text-red">
                {error}
              </div>
            )}

            {/* Footer actions */}
            <div className="mt-[18px] flex items-center justify-between gap-[12px]">
              <span className="text-[10.5px] tracking-[0.04em] text-t3">
                <span className="mr-[4px] rounded border border-line-2 px-[5px] py-[1px] text-[10px] text-t2">↵</span>create
              </span>
              <div className="flex items-center gap-[8px]">
                <button
                  type="button"
                  onClick={() => !creating && setMode("switch")}
                  disabled={creating}
                  className="rounded-lg px-[14px] py-[8px] text-[12.5px] text-t3 transition-colors hover:text-t1 disabled:cursor-default disabled:opacity-40"
                >Cancel</button>
                <button
                  type="button"
                  onClick={() => void submit({ type, name: name.trim() })}
                  disabled={!validName || creating}
                  className="rounded-lg border border-accent-line bg-accent-soft px-[16px] py-[8px] text-[12.5px] font-semibold text-t1 transition-colors hover:brightness-95 disabled:cursor-default disabled:opacity-40"
                >{creating ? "Creating…" : "Create workspace"}</button>
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

/** Two-letter avatar initials from a workspace name ("Project-Apex" → "PA"). */
function initials(name: string): string {
  const parts = name.split(/[^A-Za-z0-9]+/).filter(Boolean);
  if (parts.length >= 2) return (parts[0][0] + parts[1][0]).toUpperCase();
  return name.replace(/[^A-Za-z0-9]/g, "").slice(0, 2).toUpperCase() || "··";
}

// Format a server error into a user-friendly inline message. Recognises the
// HTTP-status shapes our `ApiError` carries.
function formatError(e: unknown): string {
  if (e && typeof e === "object" && "status" in e && "message" in e) {
    const err = e as { status: number; message: string };
    if (err.status === 409) return "That name already exists. Pick another.";
    if (err.status === 422) return err.message || "Invalid name.";
    if (err.status === 500) return `Server error: ${err.message}`;
    return `Failed (${err.status}): ${err.message}`;
  }
  if (e instanceof Error) return `Failed: ${e.message}`;
  return "Failed — see console.";
}
