import { useState } from "react";
import { cn } from "../lib/cn";
import type { TabKey } from "./MainTabs";
import type { WorkspaceListItem, WorkspaceType } from "../types";

interface Props {
  workspace: { type: WorkspaceType; name: string };
  onWorkspaceChange: (ws: { type: WorkspaceType; name: string }) => void;
  onOpenPicker: () => void;
  tab: TabKey;
  onTabChange: (t: TabKey) => void;
  reviewCount?: number;
  // ── Live workspace lists (#6b) ──────────────────────────────────────────
  /** All projects from /api/workspaces?type=project. Empty list shows the
   *  "— none —" hint inside the dropdown. */
  projects?: WorkspaceListItem[];
  /** All BD workspaces from /api/workspaces?type=bd. */
  bds?: WorkspaceListItem[];
  /** All general workspaces from /api/workspaces?type=general. Not yet
   *  surfaced as a separate dropdown — kept for future use. */
  generals?: WorkspaceListItem[];
}

/**
 * v5 context bar — workspace dropdowns + NEW WORKSPACE button + tab strip.
 *
 * Layout (2-col grid): [PRJ/BD dropdowns + NEW WORKSPACE] · [AGENT/DAILY/INBOX/RUNS/DRAFTS/VAULT tabs]
 *
 * Project + BD dropdowns now hydrate from `/api/workspaces?type=...` (Phase 2
 * part 4). Generals are listed by the picker modal but not surfaced as a
 * top-bar dropdown today — most general workspaces are short-lived ad-hoc.
 */
export function WorkspaceSelector({
  workspace, onWorkspaceChange, onOpenPicker, tab, onTabChange, reviewCount,
  projects = [], bds = [], generals: _generals = [],
}: Props) {
  const [openDD, setOpenDD] = useState<"project" | "bd" | null>(null);

  return (
    <div className="grid grid-cols-[1fr_auto] items-center gap-6 bg-bg px-[22px] py-[10px]">
      {/* Left: workspace dropdowns + new */}
      <div className="flex items-center gap-[12px]">

        {/* Project dropdown */}
        <Dropdown
          label="Project"
          tag="PRJ"
          value={workspace.type === "project" ? workspace.name : "—"}
          open={openDD === "project"}
          onToggle={() => setOpenDD(openDD === "project" ? null : "project")}
          items={projects}
          onPick={(name) => { onWorkspaceChange({ type: "project", name }); setOpenDD(null); }}
        />

        {/* BD dropdown */}
        <Dropdown
          label="BD"
          tag="BD"
          value={workspace.type === "bd" ? workspace.name : "—"}
          open={openDD === "bd"}
          onToggle={() => setOpenDD(openDD === "bd" ? null : "bd")}
          items={bds}
          onPick={(name) => { onWorkspaceChange({ type: "bd", name }); setOpenDD(null); }}
        />

        {/* + New — amber-outline button, opens the workspace picker modal */}
        <button
          type="button"
          onClick={onOpenPicker}
          className="flex items-center h-[34px] rounded-[9px] px-[14px] border border-accent text-accent text-[13px] hover:bg-accent-soft transition-colors"
        >
          + New
        </button>
      </div>

      {/* Right: tabs */}
      <div className="flex items-center gap-[1px]">
        {TAB_ORDER.map(({ key, label }) => {
          // INBOX absorbs the old REVIEW chip: when there are pending
          // proposals, the tab label + count turn coral (no box).
          const pendingReview = key === "inbox" && reviewCount !== undefined && reviewCount > 0;
          return (
            <button
              type="button"
              key={key}
              onClick={() => onTabChange(key)}
              className={cn(
                "flex items-center h-[32px] rounded-lg px-[11px] text-[12px] transition-colors",
                tab === key ? "bg-accent-soft text-accent" : "text-t2 hover:text-t1",
              )}
            >
              {label}
              {pendingReview && (
                <span className="ml-[6px] inline-flex items-center rounded-[9px] px-[5px] py-[1px] text-[9px] font-semibold leading-none text-bg bg-accent">{reviewCount}</span>
              )}
            </button>
          );
        })}
      </div>
    </div>
  );
}


const TAB_ORDER: { key: TabKey; label: string }[] = [
  { key: "agent",  label: "Agent"  },
  { key: "daily",  label: "Daily"  },
  { key: "inbox",  label: "Inbox"  },
  { key: "runs",   label: "Runs"   },
  { key: "drafts", label: "Drafts" },
  { key: "vault",  label: "Vault"  },
  { key: "budget", label: "Budget" },
  { key: "providers", label: "Providers" },
  { key: "taxonomy", label: "Taxonomy" },
  { key: "operator", label: "Operator" },
];


function Dropdown(props: {
  label: string;
  tag: string;
  value: string;
  open: boolean;
  onToggle: () => void;
  items: WorkspaceListItem[];
  onPick: (name: string) => void;
}) {
  // Selected workspace → amber badge + bright name; the idle slot ("—") stays
  // muted so the active context reads as the one with the accent.
  const selected = props.value !== "—";
  return (
    <div className="relative">
      <button
        type="button"
        onClick={props.onToggle}
        aria-label={props.label}
        className="flex items-center h-[34px] rounded-[9px] px-[13px] gap-[9px] bg-bg-2 hover:bg-line-2 transition-colors cursor-pointer"
      >
        {selected ? (
          <span className="rounded-[5px] px-[6px] py-[2px] bg-accent-soft text-accent text-[9.5px] tracking-[0.08em]">{props.tag}</span>
        ) : (
          <span className="text-[9.5px] tracking-[0.12em] uppercase text-t3">{props.tag}</span>
        )}
        <span className={cn("text-[14px] text-left", selected ? "text-t1" : "text-t3")}>{props.value}</span>
        <span className="text-[9px] text-t3">▾</span>
      </button>

      {props.open && (
        <div className="absolute top-full left-0 z-20 mt-[4px] min-w-[240px] rounded-lg border border-line-2 bg-bg-1 shadow-card overflow-hidden">
          {props.items.length === 0 ? (
            <div className="px-[12px] py-[10px] text-[11px] italic text-t3">— none yet · use + New —</div>
          ) : (
            props.items.map((it) => (
              <button
                type="button"
                key={`${it.sourceRoot}/${it.name}`}
                onClick={() => props.onPick(it.name)}
                title={`${it.sourceRoot}\\${it.name}\nlast touched ${formatLastTouched(it.lastTouched)}`}
                className="block w-full px-[12px] py-[7px] text-left text-[12px] text-t2 hover:bg-bg-2 hover:text-t1 transition-colors"
              >
                <div className="flex items-baseline justify-between gap-[8px]">
                  <span>{it.name}</span>
                  <span className="text-[10px] text-t3 tabular">{formatLastTouched(it.lastTouched)}</span>
                </div>
              </button>
            ))
          )}
        </div>
      )}
    </div>
  );
}

function formatLastTouched(iso: string): string {
  try {
    const d = new Date(iso);
    if (!Number.isFinite(d.getTime())) return iso;
    const now = new Date();
    const sec = Math.max(0, Math.round((now.getTime() - d.getTime()) / 1000));
    if (sec < 60)   return `${sec}s`;
    const min = Math.round(sec / 60);
    if (min < 60)   return `${min}m`;
    const hr = Math.round(min / 60);
    if (hr < 24)    return `${hr}h`;
    const day = Math.round(hr / 24);
    if (day < 7)    return `${day}d`;
    const wk = Math.round(day / 7);
    return `${wk}w`;
  } catch { return iso; }
}
