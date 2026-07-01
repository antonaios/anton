// Left-rail session partitioning — splits the flat bridge session list into
// the two visual sections SessionList renders (selected project/BD on top,
// General below). Kept pure + side-effect free so it can be reasoned about
// (and unit-tested) in isolation of React.

import type { ServerSession, WorkspaceType } from "../types";

export interface WorkspaceSelection {
  type: WorkspaceType;
  name: string;
}

export interface PartitionedSessions {
  /** Sessions belonging to the selected project/BD workspace — exact
   *  (workspaceType, workspaceName) match. Empty unless the selection is a
   *  project or BD: the top section is reserved for those two. */
  top: ServerSession[];
  /** Every session whose workspaceType is "general", regardless of which
   *  general workspace it belongs to ("not tied to a project/BD"). */
  general: ServerSession[];
  /** True when the selection is a project/BD (so `top` is the scoped list);
   *  false when a general workspace is selected and the top section should
   *  show its empty-state placeholder instead. */
  topScoped: boolean;
}

/**
 * Partition `sessions` into the top (selected project/BD) and bottom (General)
 * left-rail sections.
 *
 * Input order is preserved — the bridge returns sessions last_active DESC, so
 * each section stays most-recent-first.
 */
export function partitionSessionsByWorkspace(
  sessions: ServerSession[],
  selection: WorkspaceSelection,
): PartitionedSessions {
  const topScoped = selection.type === "project" || selection.type === "bd";
  const top = topScoped
    ? sessions.filter(
        (s) => s.workspaceType === selection.type && s.workspaceName === selection.name,
      )
    : [];
  const general = sessions.filter((s) => s.workspaceType === "general");
  return { top, general, topScoped };
}

/**
 * Pick the active session for the CURRENT workspace after a (re)load, workspace
 * switch, or delete (#session-workspace-sync). The active session must belong to
 * the SELECTED workspace's scope — the `top` section for a project/BD workspace,
 * the `general` section for a general workspace — so the chat body never shows a
 * session from a different workspace than the header. Keeps `current` if it is
 * still in scope; otherwise the most-recent in-scope session; otherwise "" (the
 * start screen). Deliberately never cross-falls into General from inside a
 * project — an empty project shows the start screen, not someone else's chat.
 */
export function pickActiveSession(
  partitioned: PartitionedSessions,
  selection: WorkspaceSelection,
  current: string,
): string {
  // In a project/BD workspace the scope is its own sessions; in a general
  // workspace it is the general sessions of THAT named workspace — general
  // sessions belonging to a *different* general workspace would re-introduce
  // the header/body mismatch this guards against.
  const inScope = partitioned.topScoped
    ? partitioned.top
    : partitioned.general.filter((s) => s.workspaceName === selection.name);
  if (current && inScope.some((s) => s.id === current)) return current;
  return inScope[0]?.id ?? "";
}
