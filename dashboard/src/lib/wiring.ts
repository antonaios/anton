// Single source of truth for "which workflow verbs are actually wired through
// to a live bridge call." Both App.tsx (the fire() dispatcher) and the TAXONOMY
// tab (#35, wired-vs-stub cross-reference) read from here so the two can't drift.

import type { WorkflowKey } from "../types";

/** WorkflowKeys whose `fire()` reaches a real bridge endpoint today. Everything
 *  else renders as a stub ("not yet wired") bubble. Moved out of App.tsx so the
 *  TAXONOMY catalog can cross-reference the same set (#35). */
export const WIRED: WorkflowKey[] = [
  "recall-query", "reindex", "promote-memory", "newsletter-run",
  "comps-pull", "comps-build", "company-profile", "lbo-run",
  // #front-door — on-demand decay sweeps + lessons-suggest + deal-tracker
  "actions-decay", "bd-decay", "lessons-suggest", "deal-tracker-add",
];

const WIRED_SET = new Set<WorkflowKey>(WIRED);

export function isWired(key: WorkflowKey): boolean {
  return WIRED_SET.has(key);
}

/** Maps a SKILL.md registry verb (the taxonomy `name`) to the dashboard
 *  WorkflowKey it's surfaced through, or `null` when the skill runs as a
 *  background routine/cron with no operator-facing tile. This is the ONLY
 *  hand-maintained bridge between the registry vocabulary and the dashboard
 *  vocabulary — the catalog itself comes from the bridge, not from here. */
export const SKILL_TO_WORKFLOW: Record<string, WorkflowKey | null> = {
  "comps":            "comps-build",      // /comps-build pipeline
  "ticker-multiples": "comps-pull",       // legacy /comps snapshot
  "equity-research":  "company-profile",  // /profile · /equity
  "lbo":              "lbo-run",          // /lbo intake → engine
  "recall-query":     "recall-query",     // /recall
  "sector-news":      "newsletter-run",   // /newsletter
  "deal-tracker":     "deal-tracker-add", // #front-door — paste modal (article text required)
  // #front-door — on-demand operator tiles (Vault & Ops). Each maps to its own
  // workflow + bridge route; any cron schedule is unchanged (now ALSO fireable).
  "actions-decay":    "actions-decay",
  "bd-decay":         "bd-decay",
  "lessons-suggest":  "lessons-suggest",
  // lbo-intake-agent is reachable via the LBORunModal Orchestrate flow
  // (#lbo-agent-leg), not a standalone tile — classify it so the taxonomy shows
  // "wired" rather than "unknown".
  "lbo-intake-agent": "lbo-run",
  // Background routines (cron-fired, no operator tile) — classified "routine".
  "morning-brief":    null,
  "vault-health":     null,
};

export type WiredState = "wired" | "stub" | "routine" | "unknown";

/** Derive a verb's wired-state for the TAXONOMY catalog. `wired` = mapped to a
 *  WIRED workflow; `stub` = mapped to a dashboard workflow that isn't wired yet;
 *  `routine` = EXPLICITLY mapped to null (background cron, no operator tile);
 *  `unknown` = NOT in the map at all — a freshly-authored skill the dashboard
 *  hasn't classified yet. Surfacing `unknown` (rather than silently folding it
 *  into `routine`) is the point of an audit surface: it flags registry↔dashboard
 *  drift instead of hiding it. */
export function wiredStateForSkill(name: string): WiredState {
  if (!(name in SKILL_TO_WORKFLOW)) return "unknown";
  const wf = SKILL_TO_WORKFLOW[name];
  if (wf === null) return "routine";
  return isWired(wf) ? "wired" : "stub";
}

/** The WorkflowKey a verb maps to (or null), for surfacing the dashboard verb
 *  alongside the registry name. `undefined` when the verb has no mapping yet. */
export function workflowForSkill(name: string): WorkflowKey | null | undefined {
  return SKILL_TO_WORKFLOW[name];
}
