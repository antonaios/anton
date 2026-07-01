import { ChevronsRight, Plus } from "lucide-react";
import { cn } from "../lib/cn";
import type { Session } from "./SessionList";

/**
 * Collapsed sessions rail — the 56px icon strip the full SessionList folds into
 * on a 13″ laptop (Paper Desk-compact 4VJ-0, node 61R-0). Top-down: an expand
 * chevron (» → restore the full list), a hairline, the project-scope initial,
 * a dashed "+" new-session, then round session avatars (initials) with the
 * active one accent-ringed. Same select/new handlers as SessionList.
 */
export function CollapsedSessions({
  workspace, sessions, activeId, onSelect, onNew, onExpand,
}: {
  workspace: { name: string; type: string };
  sessions: Session[];
  activeId: string;
  onSelect: (id: string) => void;
  onNew: () => void;
  onExpand: () => void;
}) {
  return (
    <div className="flex w-[56px] shrink-0 flex-col items-center gap-[16px] overflow-y-auto rounded-[16px] bg-bg-2 py-[15px] shadow-card min-h-0">
      <button
        type="button"
        onClick={onExpand}
        title="Expand sessions"
        aria-label="Expand sessions"
        className="flex size-[30px] shrink-0 items-center justify-center rounded-[8px] text-t2 transition-colors hover:bg-bg-1 hover:text-t1"
      >
        <ChevronsRight size={15} strokeWidth={1.8} />
      </button>

      <div className="h-px w-[28px] shrink-0 bg-line" />

      {/* Project-scope initial */}
      <div
        className="flex size-[32px] shrink-0 items-center justify-center rounded-[8px] bg-accent-soft text-[13px] font-semibold text-t1"
        title={workspace.name}
      >
        {firstInitial(workspace.name)}
      </div>

      {/* New session */}
      <button
        type="button"
        onClick={onNew}
        title="New session"
        aria-label="New session"
        className="flex size-[32px] shrink-0 items-center justify-center rounded-full border-[1.5px] border-dashed border-accent-line text-accent transition-colors hover:bg-accent-soft"
      >
        <Plus size={15} strokeWidth={2} />
      </button>

      {/* Session avatars */}
      {sessions.map((s) => {
        const active = s.id === activeId;
        return (
          <button
            key={s.id}
            type="button"
            onClick={() => onSelect(s.id)}
            title={s.title}
            aria-label={s.title}
            aria-current={active ? "page" : undefined}
            className={cn(
              "flex shrink-0 items-center justify-center rounded-full font-mono transition-colors",
              active
                ? "size-[36px] border-2 border-accent bg-accent-soft text-[12px] font-semibold text-t1"
                : "size-[34px] border border-line-2 text-[11.5px] font-medium text-t2 hover:border-accent hover:text-t1",
            )}
          >
            {avatarInitials(s.title)}
          </button>
        );
      })}
    </div>
  );
}

/** First alphanumeric letter of a name, uppercased ("Project-Apex" → "P"). */
function firstInitial(name: string): string {
  const m = name.match(/[A-Za-z0-9]/);
  return m ? m[0].toUpperCase() : "·";
}

/** Up to two initials from a session title ("Base-case LBO" → "BL"). */
function avatarInitials(title: string): string {
  const parts = title.split(/[^A-Za-z0-9]+/).filter(Boolean);
  if (parts.length >= 2) return (parts[0][0] + parts[1][0]).toUpperCase();
  return title.replace(/[^A-Za-z0-9]/g, "").slice(0, 2).toUpperCase() || "··";
}
