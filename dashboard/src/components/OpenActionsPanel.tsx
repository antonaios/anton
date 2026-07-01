import { useCallback, useEffect, useState } from "react";
import { api } from "../lib/api";
import { cn } from "../lib/cn";
import type { ActionItem, ActionsResponse, IssuesResponse } from "../types";

interface Props {
  projectName: string;
}

const PILL_CLASS: Record<"overdue" | "open" | "stale", string> = {
  overdue: "border-accent-line bg-accent-soft text-accent",
  open:    "border-line-2 text-t2",
  stale:   "border-line text-t3",
};
const PILL_LABEL: Record<"overdue" | "open" | "stale", string> = {
  overdue: "OVERDUE",
  open:    "OPEN",
  stale:   "STALE",
};

// Subline copy per group — the Paper idiom puts due/status on its own line
// under the title (overdue in accent, the rest muted). Owner is appended as a
// "→ name" tail when present (gating preserved exactly: `a.owner && …`).
const SUBLINE_CLASS: Record<"overdue" | "open" | "stale", string> = {
  overdue: "text-accent",
  open:    "text-t3",
  stale:   "text-t4",
};

/**
 * v5 right-rail Open Actions panel — lifted out of ActiveDealPanel.
 *
 * Fetches /api/projects/<projectName>/actions on mount + after each toggle.
 * Renders four status groups: overdue (coral), open, stale (collapsed to
 * a count + first 3), and done (separate rollup). Click any pill to
 * toggle the underlying file from `- [ ]` ↔ `- [x]`.
 */
export function OpenActionsPanel({ projectName }: Props) {
  const [actions, setActions] = useState<ActionsResponse | null>(null);
  const [issues, setIssues] = useState<IssuesResponse | null>(null);
  const [busy, setBusy] = useState(false);

  const refetch = useCallback(async () => {
    if (!projectName) return;
    try {
      const data = await api.projectActions(projectName);
      setActions(data);
      // #issues-register v2 — issue metadata for grouping. Tolerated failure:
      // a pre-v2 bridge (no endpoint) or a non-vault project degrades to the
      // ungrouped panel, never blocks actions.
      const iss = await api.projectIssues(projectName).catch(() => null);
      setIssues(iss);
    } catch {
      /* bridge offline / project missing — keep previous state */
    }
  }, [projectName]);

  useEffect(() => { void refetch(); }, [refetch]);

  const onToggle = async (a: ActionItem) => {
    if (!projectName || busy) return;
    setBusy(true);
    try {
      await api.toggleAction(projectName, {
        source_file: a.source_file,
        task_hash:   a.task_hash,
        line_hint:   a.source_line,
        to:          a.status === "done" ? "open" : "done",
      });
      await refetch();
    } catch {
      /* toast in a later pass */
    } finally {
      setBusy(false);
    }
  };

  const renderAction = (a: ActionItem, group: "overdue" | "open" | "stale") => (
    <div
      key={`${a.source_file}#${a.task_hash}`}
      className="group/row flex items-start gap-[10px] py-[8px] cursor-pointer hover:text-t1"
    >
      <button
        type="button"
        onClick={() => void onToggle(a)}
        disabled={busy}
        className={cn(
          "mt-[1px] flex h-[16px] w-[16px] flex-none items-center justify-center rounded-[4px] border transition-colors disabled:opacity-50",
          group === "overdue"
            ? "border-accent-line group-hover/row:border-accent"
            : "border-line-2 group-hover/row:border-accent-line",
        )}
        title="Click to toggle done"
        aria-label={`Toggle action: ${a.title}`}
      >
        {a.urgent && group === "overdue" && (
          <span className="h-[6px] w-[6px] rounded-[1px] bg-accent" />
        )}
      </button>

      <div className="min-w-0 flex-1">
        <div className={cn(
          "text-[12.5px] leading-[145%] text-t2 group-hover/row:text-t1 transition-colors",
          a.urgent && "font-medium text-t1",
        )}>
          {a.title}
        </div>
        <div className={cn(
          "mt-[2px] flex items-baseline gap-[6px] text-[10.5px] leading-[140%]",
          SUBLINE_CLASS[group],
        )}>
          <span className="tabular">{a.due ?? "—"}</span>
          {a.owner && (
            <span className="text-t4 tracking-[0.01em]">→ {a.owner}</span>
          )}
        </div>
      </div>
    </div>
  );

  return (
    <div>
      <div className="mb-[14px] flex items-center justify-between">
        <span className="text-[10px] uppercase tracking-[0.12em] text-t3 mono">
          {actions ? actions.counts.total_open : 0} open
        </span>
        <span className="cursor-pointer text-[11px] text-t3 transition-colors hover:text-t1">All →</span>
      </div>

      {actions === null ? (
        <div className="text-[11px] italic text-t3">Loading…</div>
      ) : actions.counts.total_open === 0 ? (
        <div className="text-[12px] leading-[155%] text-t3">
          No actions surfaced in <span className="text-t2">{projectName}</span>.
        </div>
      ) : (() => {
        // #issues-register v2 — actions tagged [issue:ISS-NN] render grouped
        // under their issue header (with the register's priority badge);
        // untagged actions keep the original flat lists. Grouping is gated on
        // a SUCCESSFUL issues fetch — a pre-v2 bridge or non-vault project
        // renders the original ungrouped panel, tagged actions included.
        if (issues === null) {
          return (
            <div className="flex flex-col divide-y divide-line/60">
              {actions.overdue.map((a) => renderAction(a, "overdue"))}
              {actions.open.map((a) => renderAction(a, "open"))}
              {actions.stale.slice(0, 3).map((a) => renderAction(a, "stale"))}
              {actions.stale.length > 3 && (
                <div className="pt-[8px] text-[10.5px] text-t4">
                  + {actions.stale.length - 3} more stale
                </div>
              )}
            </div>
          );
        }

        const tagged = new Map<string, Array<{ a: ActionItem; g: "overdue" | "open" | "stale" }>>();
        const collect = (list: ActionItem[], g: "overdue" | "open" | "stale") =>
          list.filter((a) => a.issue).forEach((a) => {
            const key = a.issue as string;
            if (!tagged.has(key)) tagged.set(key, []);
            tagged.get(key)!.push({ a, g });
          });
        collect(actions.overdue, "overdue");
        collect(actions.open, "open");
        collect(actions.stale, "stale");

        const flatOverdue = actions.overdue.filter((a) => !a.issue);
        const flatOpen = actions.open.filter((a) => !a.issue);
        const flatStale = actions.stale.filter((a) => !a.issue);

        // Register order first, then any ids the register doesn't know.
        const registerOrder = (issues?.issues ?? []).map((i) => i.id).filter((id) => tagged.has(id));
        const strayIds = [...tagged.keys()].filter((id) => !registerOrder.includes(id)).sort();
        const issueMeta = new Map((issues?.issues ?? []).map((i) => [i.id, i]));

        return (
          <div className="flex flex-col">
            <div className="flex flex-col divide-y divide-line/60">
              {flatOverdue.map((a) => renderAction(a, "overdue"))}
              {flatOpen.map((a) => renderAction(a, "open"))}
              {flatStale.slice(0, 3).map((a) => renderAction(a, "stale"))}
            </div>
            {flatStale.length > 3 && (
              <div className="pt-[8px] text-[10.5px] text-t4">
                + {flatStale.length - 3} more stale
              </div>
            )}

            {[...registerOrder, ...strayIds].map((id) => {
              const meta = issueMeta.get(id);
              return (
                <div key={id} className="mt-[12px] border-t border-line pt-[10px]">
                  <div className="mb-[2px] flex items-baseline gap-[6px] text-[10px] uppercase tracking-[0.1em] text-t3">
                    <span className="mono text-t2">{id}</span>
                    {meta?.priority && (
                      <span className={cn(
                        "rounded-[4px] border px-[5px] py-[0.5px] tracking-[0.08em]",
                        meta.priority === "P1" ? "border-accent-line bg-accent-soft text-accent" : "border-line-2 text-t3",
                      )}>{meta.priority}</span>
                    )}
                    {meta && meta.status !== "open" && (
                      <span className="rounded-[4px] border border-line px-[5px] py-[0.5px] text-t3">{meta.status.toUpperCase()}</span>
                    )}
                    {meta && (
                      <span className="truncate text-[11px] normal-case tracking-normal text-t3">{meta.title}</span>
                    )}
                  </div>
                  <div className="flex flex-col divide-y divide-line/60">
                    {tagged.get(id)!.map(({ a, g }) => renderAction(a, g))}
                  </div>
                </div>
              );
            })}
          </div>
        );
      })()}

      {/* Visible-status legend, only when there are items shown */}
      {actions && actions.counts.total_open > 0 && (
        <div className="mt-[14px] flex gap-[6px] border-t border-line pt-[12px] text-[9px] tracking-[0.08em]">
          {(["overdue", "open", "stale"] as const).map((g) => (
            <span key={g} className={cn("rounded-[4px] border px-[6px] py-[1.5px] font-medium", PILL_CLASS[g])}>{PILL_LABEL[g]}</span>
          ))}
        </div>
      )}
    </div>
  );
}
