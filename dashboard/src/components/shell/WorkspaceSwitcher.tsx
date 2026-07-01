import { useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import { ChevronDown, Plus } from "lucide-react";
import { cn } from "../../lib/cn";
import { Chip } from "../ui/Chip";
import { Field } from "../ui/Field";
import type { WorkspaceListItem, WorkspaceType } from "../../types";

/**
 * WorkspaceSwitcher — the top-left active-workspace control.
 *
 * Renders as a compact pill (PRJ/BD/GEN tag + workspace name + chevron) that sits
 * just right of the ANTON wordmark in the header (Paper `Desk — Helix`). Clicking
 * it opens a popover: a search field, a "Recent" list, and the full Projects /
 * Business Development / General lists grouped under mono section headers, ending
 * in a "+ New workspace" row that hands off to App's picker modal.
 *
 * Purely presentational — no fetching, no tab/nav state. Selection + the picker
 * open are delegated to the parent via `onWorkspaceChange` / `onOpenPicker`.
 * Token-only styling so it flips between the light-teal and dark-navy themes.
 */
export interface WorkspaceSwitcherProps {
  /** The active workspace — drives the trigger pill's tag + name. */
  workspace: { type: WorkspaceType; name: string };
  /** Fires when a workspace row is picked from the popover. */
  onWorkspaceChange: (ws: { type: WorkspaceType; name: string }) => void;
  /** Opens App's full workspace picker / create modal (the "+ New workspace" row). */
  onOpenPicker: () => void;
  /** Live project list feeding the Projects group. */
  projects?: WorkspaceListItem[];
  /** Live BD list feeding the Business Development group. */
  bds?: WorkspaceListItem[];
  /** Live general list feeding the General group. */
  generals?: WorkspaceListItem[];
}

/** Short tag + Chip variant per workspace type. */
const TYPE_META: Record<WorkspaceType, { tag: string; variant: "accent" | "neutral" | "internal" }> = {
  project: { tag: "PRJ", variant: "accent" },
  bd: { tag: "BD", variant: "neutral" },
  general: { tag: "GEN", variant: "internal" },
};

export function WorkspaceSwitcher({
  workspace,
  onWorkspaceChange,
  onOpenPicker,
  projects = [],
  bds = [],
  generals = [],
}: WorkspaceSwitcherProps) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [collapsed, setCollapsed] = useState<Partial<Record<WorkspaceType, boolean>>>({});
  const rootRef = useRef<HTMLDivElement>(null);

  // Outside-click + Escape dismissal.
  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    window.addEventListener("mousedown", onDoc);
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("mousedown", onDoc);
      window.removeEventListener("keydown", onKey);
    };
  }, [open]);

  // Reset the search field whenever the popover re-opens.
  useEffect(() => {
    if (open) setQuery("");
  }, [open]);

  // All workspaces, tagged with their type, for the "Recent" cross-type list.
  const all = useMemo<TypedWorkspace[]>(
    () => [
      ...projects.map((w) => ({ ...w, type: "project" as const })),
      ...bds.map((w) => ({ ...w, type: "bd" as const })),
      ...generals.map((w) => ({ ...w, type: "general" as const })),
    ],
    [projects, bds, generals],
  );

  const q = query.trim().toLowerCase();
  const matches = (w: TypedWorkspace) => q === "" || w.name.toLowerCase().includes(q);

  // Recent = the 4 most-recently-touched across every type (ISO desc).
  const recent = useMemo(
    () =>
      [...all]
        .filter(matches)
        .sort((a, b) => b.lastTouched.localeCompare(a.lastTouched))
        .slice(0, 4),
    [all, q],
  );

  const groups: { label: string; type: WorkspaceType; items: TypedWorkspace[] }[] = [
    { label: "PROJECTS", type: "project", items: projects.map((w) => ({ ...w, type: "project" as const })).filter(matches) },
    { label: "BD & ORIGINATION", type: "bd", items: bds.map((w) => ({ ...w, type: "bd" as const })).filter(matches) },
    { label: "GENERAL", type: "general", items: generals.map((w) => ({ ...w, type: "general" as const })).filter(matches) },
  ];

  const activeTag = TYPE_META[workspace.type].tag;

  const pick = (ws: TypedWorkspace) => {
    onWorkspaceChange({ type: ws.type, name: ws.name });
    setOpen(false);
  };

  const isActive = (ws: TypedWorkspace) => ws.type === workspace.type && ws.name === workspace.name;

  return (
    <div ref={rootRef} className="relative">
      {/* Trigger pill — Paper Desk header: type tag + name + chevron in a soft box. */}
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-haspopup="dialog"
        aria-expanded={open}
        className={cn(
          "flex items-center gap-[10px] rounded-[10px] border border-line-2 bg-bg px-[13px] py-[7px] text-left transition-colors",
          open && "border-accent-line",
        )}
      >
        <div className="flex flex-col gap-[1px]">
          <span className="text-[13px] font-semibold leading-[16px] text-t1">{workspace.name}</span>
          <span className="mono text-[9.5px] tracking-[0.12em] uppercase leading-[12px] text-t3">
            {activeTag} · workspace
          </span>
        </div>
        <ChevronDown
          size={14}
          className={cn("shrink-0 text-t3 transition-transform", open && "rotate-180 text-accent")}
        />
      </button>

      {open && (
        <div
          role="dialog"
          aria-label="Switch workspace"
          className="absolute left-0 top-full z-40 mt-[6px] w-[368px] overflow-hidden rounded-xl border border-line bg-bg-1 shadow-card"
        >
          {/* Search */}
          <div className="border-b border-line p-[12px]">
            <Field
              label="Search workspaces"
              value={query}
              onChange={setQuery}
              placeholder="Filter by name…"
            />
          </div>

          <div className="max-h-[360px] overflow-y-auto py-[6px]">
            {/* Recent — cross-type, hidden when a query empties it. */}
            {recent.length > 0 && (
              <Section label="Recent">
                {recent.map((w) => (
                  <Row
                    key={`recent-${w.type}-${w.sourceRoot}-${w.name}`}
                    ws={w}
                    active={isActive(w)}
                    showTag
                    onPick={() => pick(w)}
                  />
                ))}
              </Section>
            )}

            {/* Grouped lists — collapsible headers with a count badge (Paper 2LK-0). */}
            {groups.map((g) => (
              <Section
                key={g.type}
                label={g.label}
                count={g.items.length}
                collapsed={collapsed[g.type]}
                onToggle={() => setCollapsed((c) => ({ ...c, [g.type]: !c[g.type] }))}
              >
                {g.items.length === 0 ? (
                  <div className="px-[14px] py-[7px] text-[11px] italic text-t3">— none —</div>
                ) : (
                  g.items.map((w) => (
                    <Row
                      key={`${g.type}-${w.sourceRoot}-${w.name}`}
                      ws={w}
                      active={isActive(w)}
                      onPick={() => pick(w)}
                    />
                  ))
                )}
              </Section>
            ))}
          </div>

          {/* + New workspace — hands off to App's picker modal. */}
          <button
            type="button"
            onClick={() => {
              setOpen(false);
              onOpenPicker();
            }}
            className="flex w-full items-center gap-[8px] border-t border-line px-[14px] py-[11px] text-left text-[12.5px] text-accent transition-colors hover:bg-accent-soft"
          >
            <Plus size={14} className="shrink-0" />
            New workspace
          </button>
        </div>
      )}
    </div>
  );
}

/** A workspace list item carrying its resolved type (for cross-type lists). */
interface TypedWorkspace extends WorkspaceListItem {
  type: WorkspaceType;
}

/**
 * A section header above a list of workspace rows (Paper 2LK-0).
 *
 * RECENT is a plain header (no count, no chevron). The PROJECTS / BD &
 * ORIGINATION / GENERAL groups pass `count` + `onToggle`, which renders a
 * leading down-chevron and a trailing count badge, and collapses the list.
 */
function Section({
  label,
  children,
  count,
  collapsed,
  onToggle,
}: {
  label: string;
  children: ReactNode;
  count?: number;
  collapsed?: boolean;
  onToggle?: () => void;
}) {
  const labelCls =
    "text-[9.5px] font-bold tracking-[0.11em] uppercase leading-none text-t3";
  if (onToggle) {
    return (
      <div className="mb-[2px]">
        <button
          type="button"
          onClick={onToggle}
          aria-expanded={!collapsed}
          className="flex w-full items-center gap-[7px] px-[14px] pb-[2px] pt-[4px] text-left transition-colors hover:bg-bg-2"
        >
          <ChevronDown
            size={11}
            className={cn("shrink-0 text-t3 transition-transform", collapsed && "-rotate-90")}
          />
          <span className={cn("flex-1", labelCls)}>{label}</span>
          {count != null && (
            <span className="mono shrink-0 text-[10px] leading-[12px] text-t3">{count}</span>
          )}
        </button>
        {!collapsed && children}
      </div>
    );
  }
  return (
    <div className="mb-[2px]">
      <div className={cn("px-[14px] pb-[2px] pt-[4px]", labelCls)}>{label}</div>
      {children}
    </div>
  );
}

/** One selectable workspace row inside the popover. */
function Row({
  ws,
  active,
  showTag,
  onPick,
}: {
  ws: TypedWorkspace;
  active: boolean;
  showTag?: boolean;
  onPick: () => void;
}) {
  const meta = TYPE_META[ws.type];
  return (
    <button
      type="button"
      onClick={onPick}
      title={`${ws.sourceRoot}\\${ws.name}\nlast touched ${formatLastTouched(ws.lastTouched)}`}
      className={cn(
        "flex w-full items-center gap-[10px] px-[14px] py-[7px] text-left transition-colors",
        active ? "bg-accent-soft" : "hover:bg-bg-2",
      )}
    >
      {showTag && <Chip label={meta.tag} variant={meta.variant} className="shrink-0" />}
      <span className={cn("min-w-0 flex-1 truncate text-[12.5px]", active ? "text-accent" : "text-t1")}>
        {ws.name}
      </span>
      <span className="shrink-0 text-[10px] tabular text-t3">{formatLastTouched(ws.lastTouched)}</span>
    </button>
  );
}

/** ISO-8601 → compact "38m" / "3d" / "2w" relative label. */
function formatLastTouched(iso: string): string {
  try {
    const d = new Date(iso);
    if (!Number.isFinite(d.getTime())) return iso;
    const sec = Math.max(0, Math.round((Date.now() - d.getTime()) / 1000));
    if (sec < 60) return `${sec}s`;
    const min = Math.round(sec / 60);
    if (min < 60) return `${min}m`;
    const hr = Math.round(min / 60);
    if (hr < 24) return `${hr}h`;
    const day = Math.round(hr / 24);
    if (day < 7) return `${day}d`;
    return `${Math.round(day / 7)}w`;
  } catch {
    return iso;
  }
}
