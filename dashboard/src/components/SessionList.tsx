import { useState } from "react";
import { Plus, ChevronsLeft, Pin } from "lucide-react";
import { cn } from "../lib/cn";

export interface Session {
  id: string;
  workspaceTag: string;     // "PRJ · DemoDeal" / "BD · ACME-TELCO" / "GEN · ASOS-COMPS"
  title: string;
  ago: string;              // "2m" / "18m" / "1h" / "yesterday"
  kind: string;             // session verb/mode — "chat" / "comps-build" / "draft" …
  messageCount: number;     // bridge-reported message count
  sub?: string;             // legacy free-form subline (seed data); rarely set live
  running?: string;         // present + visible-pulsed when active
  archived?: boolean;
  pinned?: boolean;         // #session-ops — shows a pin glyph; sorts first (server-ordered)
}

interface Props {
  /** Sessions for the selected project/BD workspace (the "Project" scope). */
  topSessions: Session[];
  /** Workspace tag for the project scope ("PRJ" / "BD") — unused label-side now. */
  topTag: string;
  /** Workspace display name shown on the Project toggle segment. */
  topName: string;
  /** False when a general workspace is selected (no project/BD in scope) — the
   *  Project scope then renders its empty-state placeholder and its + is disabled. */
  topScoped: boolean;
  /** Sessions not tied to a project/BD (the "General" scope). */
  generalSessions: Session[];
  activeId: string;
  onSelect: (id: string) => void;
  /** + while the Project scope is active — creates a session in the project/BD. */
  onNew: () => void;
  /** + while the General scope is active — creates a general session. */
  onNewGeneral: () => void;
  loading?: boolean;
  /** When set (13″ compact peek), shows a « button to fold the rail back to the
   *  56px CollapsedSessions strip. */
  onCollapse?: () => void;
}

/**
 * Sessions rail — the Desk left rail, matching the agreed Paper **v2** artboard
 * (node 2I8-0): a **scope segmented toggle** ("{project} | General"), then ONE
 * "New session" button, then a FLAT list of session cards (the active card
 * tinted accent-soft). We moved away from the earlier two-stacked-sections
 * layout — switching scope swaps which workspace's sessions the list shows.
 *
 * (The 13″ compact artboard 4VJ-0 folds this rail to a 56px icon strip; that
 * collapse is handled in the responsive phase.)
 */
export function SessionList({
  topSessions, topName, topScoped, generalSessions, activeId, onSelect, onNew, onNewGeneral, loading, onCollapse,
}: Props) {
  // Default to the project scope when one is in scope, else General (project is empty).
  const [scope, setScope] = useState<"project" | "general">(() => (topScoped ? "project" : "general"));

  const isProject = scope === "project";
  const sessions = isProject ? topSessions : generalSessions;
  const onNewForScope = isProject ? onNew : onNewGeneral;
  const newDisabled = isProject && !topScoped;
  const projectLabel = topScoped ? topName : "Project";

  const renderRow = (s: Session) => {
    const active = s.id === activeId;
    const sub = s.running
      ? s.running
      : active
        ? `active · ${s.messageCount} message${s.messageCount === 1 ? "" : "s"}`
        : (s.sub ?? s.ago);
    return (
      <button
        type="button"
        key={s.id}
        onClick={() => onSelect(s.id)}
        className={cn(
          "flex w-full flex-col gap-[4px] rounded-[9px] px-[10px] py-[9px] text-left transition-colors",
          active ? "bg-accent-soft" : "hover:bg-paper2",
          s.archived && "opacity-55",
        )}
      >
        <div className="flex items-center gap-[7px]">
          <span
            className={cn(
              "h-[6px] w-[6px] shrink-0 rounded-full",
              s.running ? "bg-green" : active ? "bg-accent" : "bg-line-2",
            )}
            style={s.running ? { animation: "pulse 1.4s infinite ease-out" } : undefined}
          />
          <span className={cn("min-w-0 grow truncate text-[12.5px] leading-[120%] text-t1", active ? "font-semibold" : "font-medium")}>
            {s.title}
          </span>
          {s.pinned && <Pin size={11} strokeWidth={2} className="shrink-0 text-accent" />}
        </div>
        <span className={cn("truncate pl-[13px] text-[10.5px] leading-[120%]", active ? "text-t2" : "text-t3")}>
          {sub}
        </span>
      </button>
    );
  };

  return (
    <div className="flex flex-1 min-h-0 flex-col gap-[10px] overflow-hidden p-[12px]">
      {/* Scope toggle — Project | General (Paper 2I8-0); « collapses on 13″ */}
      <div className="flex shrink-0 items-center gap-[6px]">
        {onCollapse && (
          <button
            type="button"
            onClick={onCollapse}
            title="Collapse sessions"
            aria-label="Collapse sessions"
            className="flex size-[28px] shrink-0 items-center justify-center rounded-[7px] text-t3 transition-colors hover:bg-bg-1 hover:text-t1"
          >
            <ChevronsLeft size={15} strokeWidth={1.8} />
          </button>
        )}
        <div className="flex grow gap-[3px] rounded-[9px] bg-paper2 p-[3px]">
          <ScopeSeg label={projectLabel} active={isProject} onClick={() => setScope("project")} />
          <ScopeSeg label="General" active={!isProject} onClick={() => setScope("general")} />
        </div>
      </div>

      {/* Single "New session" — for the active scope */}
      <button
        type="button"
        onClick={onNewForScope}
        disabled={newDisabled}
        title={newDisabled ? "Select a project or BD first" : "New session in this workspace"}
        aria-label="New session"
        className={cn(
          "flex h-[34px] shrink-0 items-center justify-center gap-[8px] rounded-[9px] border border-dashed border-line-2 px-[11px] text-[12.5px] font-medium text-t2 transition-colors",
          "bg-transparent hover:border-line hover:text-t1",
          "disabled:cursor-default disabled:opacity-40 disabled:hover:border-line-2 disabled:hover:text-t2",
        )}
      >
        <Plus size={14} className="shrink-0" />
        New session
      </button>

      {/* Flat session list for the active scope */}
      <div className="flex min-h-0 flex-col gap-[2px] overflow-y-auto">
        {loading ? (
          <div className="px-[10px] py-[6px] text-[11px] italic text-t3">Loading…</div>
        ) : newDisabled ? (
          <div className="px-[10px] py-[8px] text-[11px] italic leading-relaxed text-t3">
            No project or BD selected. Pick one from the{" "}
            <span className="not-italic text-t2">Project</span> or{" "}
            <span className="not-italic text-t2">BD</span> dropdown above, or use{" "}
            <span className="not-italic text-t2">General</span>.
          </div>
        ) : sessions.length === 0 ? (
          <div className="px-[10px] py-[6px] text-[11px] italic text-t3">
            {isProject ? "No sessions in this workspace yet." : "No general sessions."}
          </div>
        ) : (
          sessions.map(renderRow)
        )}
      </div>
    </div>
  );
}

/** One segment of the Project|General scope toggle. */
function ScopeSeg({ label, active, onClick }: { label: string; active: boolean; onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "flex h-[28px] grow basis-0 items-center justify-center rounded-[7px] px-[8px] transition-colors",
        active ? "bg-bg-2 shadow-card" : "hover:bg-bg-2/40",
      )}
    >
      <span className={cn("truncate text-[11px] leading-none", active ? "font-semibold text-t1" : "font-medium text-t2")}>
        {label}
      </span>
    </button>
  );
}
