// Persist + restore the last-active session (and its workspace) so opening or
// refreshing ANTON lands you back on the session you were on, instead of the
// hard-coded default workspace. Storage-safe (private mode / blocked storage →
// no-op), mirroring the localStorage idiom in lib/theme.ts.

import type { WorkspaceType } from "../types";

const KEY = "anton-last-session";

export interface LastSession {
  sessionId: string;
  workspace: { type: WorkspaceType; name: string };
}

const VALID_TYPES: readonly WorkspaceType[] = ["project", "bd", "general"];

/** The persisted last session + workspace, or null when absent / malformed /
 *  storage-blocked. Validated so a corrupt entry can't crash the boot. */
export function loadLastSession(): LastSession | null {
  try {
    const raw = localStorage.getItem(KEY);
    if (!raw) return null;
    const v = JSON.parse(raw) as Partial<LastSession> | null;
    const ws = v?.workspace;
    if (
      typeof v?.sessionId === "string" &&
      ws != null &&
      typeof ws.name === "string" &&
      ws.name.length > 0 &&
      VALID_TYPES.includes(ws.type)
    ) {
      return { sessionId: v.sessionId, workspace: { type: ws.type, name: ws.name } };
    }
  } catch {
    /* malformed JSON or blocked storage — fall through to the default */
  }
  return null;
}

/** Persist the current session + workspace. Best-effort; never throws. */
export function saveLastSession(
  sessionId: string,
  workspace: { type: WorkspaceType; name: string },
): void {
  try {
    localStorage.setItem(KEY, JSON.stringify({ sessionId, workspace }));
  } catch {
    /* blocked storage — non-fatal */
  }
}
