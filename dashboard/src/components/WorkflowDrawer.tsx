import { useEffect, useMemo, useRef, useState } from "react";
import {
  Search,
  BarChart3,
  PenLine,
  Calendar,
  Archive,
  Users,
  LayoutGrid,
  type LucideIcon,
} from "lucide-react";
import { cn } from "../lib/cn";
import { useMediaQuery } from "../lib/useMediaQuery";

// Per-category leading icon (Paper 37Q-0 draws a 16px line glyph per category,
// not a dot). Keyed on the live WORKFLOW_SECTIONS titles (which differ from
// Paper's sample names but are the REAL taxonomy): Research→search,
// Valuation≈Model→bar-chart, Transaction materials≈Draft→pen, Meetings→calendar,
// Vault & Ops≈Vault→archive, Crews→users. Unknown titles fall back to a grid.
function sectionIcon(title: string): LucideIcon {
  const t = title.toLowerCase();
  if (t.includes("research")) return Search;
  if (t.includes("valuation") || t.includes("model")) return BarChart3;
  if (t.includes("transaction") || t.includes("draft") || t.includes("material")) return PenLine;
  if (t.includes("meeting")) return Calendar;
  if (t.includes("vault")) return Archive;
  if (t.includes("crew")) return Users;
  return LayoutGrid;
}

interface WorkflowTile {
  label: string;
  key: string;                 // "company-profile" etc.
  pinned?: boolean;
  active?: boolean;
  disabled?: boolean;
  kbd?: string;                // "⌘1" / "↵"
  desc?: string;               // one-line palette subline (Paper 37Q-0 it.desc)
  suggested?: boolean;
}

interface WorkflowSection {
  title: string;
  tiles: WorkflowTile[];
  fullWidth?: boolean;
  cols?: number;
}

interface Props {
  /** Controlled visibility — App owns the open state (opened by `/` in the
   *  composer or ⌘E; closed by Esc / backdrop / firing a workflow). */
  open: boolean;
  onClose: () => void;
  sections: WorkflowSection[];
  /** Accepted for backward compatibility but no longer displayed — the header
   *  count is now derived from `sections` so it can never drift (Paper 3A1-0). */
  totalCount?: number;
  onFire: (key: string) => void;
}

// A flat search result — a tile plus the category it came from (so a filtered
// row can render its origin tag + the left rail can sum per-category matches).
interface FlatRow {
  tile: WorkflowTile;
  catIdx: number;
  catTitle: string;
}

/**
 * v2 workflow palette — a `/`-triggered floating OVERLAY (Paper page 2-0,
 * artboard 37Q-0 / 3CE-0). We moved away from the old bottom DRAWER (the
 * collapsible handle that slid up from the chat canvas) — this is now a
 * centered modal palette over the Desk.
 *
 * Raycast-style two-pane: a LIVE search header (· match / total count) + a LEFT
 * Categories rail (each category = a source section: icon + name + count) + a
 * RIGHT Workflows list (section header with the selected category + count, then
 * NAME + dim kbd rows with an "↵ run" affordance and optional SOON badge), + a
 * footer of keyboard hints.
 *
 * Keyboard model (handoff "/ command palette" screen):
 *   - Empty query → category-driven two-pane. `↑↓` walk the FOCUSED pane;
 *     `→`/`Tab` jump category-rail → workflow list; `←` back; `↵` fires the
 *     active tile (skips `disabled`/"Soon"); `Esc` closes.
 *   - Non-empty query → a FLAT filtered list across every category (each row
 *     tagged with its category); the left rail shows per-category match counts;
 *     `↑↓` walk the flat list; `↵` fires the active row.
 * Indices are clamped when `sections` change and reset each time the palette
 * opens. Focus is held in the search input so typing always filters; Arrow/Enter
 * are intercepted + `preventDefault`-ed so they drive navigation, not the caret.
 *
 * Categories are the existing `sections` (the source carries no separate category
 * metadata); each tile now carries a one-line `desc` (Paper 37Q-0) rendered as
 * the dim sub-line, with its `kbd` shortcut shown as a right-aligned pill.
 *
 * Firing a tile dispatches the workflow into the active session and closes the
 * palette (parent decides what dispatch means — typically a skill-input form).
 */
export function WorkflowDrawer({ open, onClose, sections, onFire }: Props) {
  // Selection state. `pane` decides which side `↑↓`/`↵` drive while browsing.
  const [catIdx, setCatIdx] = useState(0);
  const [wfIdx, setWfIdx] = useState(0);
  const [pane, setPane] = useState<"cat" | "wf">("cat");
  const [query, setQuery] = useState("");

  const inputRef = useRef<HTMLInputElement | null>(null);

  // Reduced-motion: drop the entrance scale/fade (same detection pattern as
  // ChatCanvas — the shared useMediaQuery hook). `entered` flips on after mount
  // to play a CSS-transition entrance; under reduce-motion it starts true so the
  // palette appears at rest with no scale/opacity tween.
  const reduce = useMediaQuery("(prefers-reduced-motion: reduce)");
  const [entered, setEntered] = useState(false);

  // Reset selection + query each time the palette opens; focus the input.
  useEffect(() => {
    if (!open) return;
    setCatIdx(0);
    setWfIdx(0);
    setPane("cat");
    setQuery("");
    setEntered(reduce);                 // no entrance tween under reduce-motion
    const raf = requestAnimationFrame(() => {
      if (!reduce) setEntered(true);    // next frame → CSS transition runs
      inputRef.current?.focus();
    });
    return () => cancelAnimationFrame(raf);
  }, [open, reduce]);

  // Total workflow count — derived from sections so the header can never drift
  // from the real tile count (Paper 3A1-0; supersedes the hardcoded prop).
  const totalCount = useMemo(
    () => sections.reduce((n, s) => n + s.tiles.length, 0),
    [sections],
  );

  const q = query.trim().toLowerCase();
  const browsing = q.length === 0;

  // Flat filtered list across every category (non-empty query). Match on the
  // tile label, its one-line `desc`, AND its category title.
  const flat = useMemo<FlatRow[]>(() => {
    if (browsing) return [];
    const out: FlatRow[] = [];
    sections.forEach((sect, ci) => {
      sect.tiles.forEach((tile) => {
        const hay = (tile.label + " " + (tile.desc ?? "") + " " + sect.title).toLowerCase();
        if (hay.includes(q)) out.push({ tile, catIdx: ci, catTitle: sect.title });
      });
    });
    return out;
  }, [sections, q, browsing]);

  // Per-category match counts for the left rail while filtering.
  const matchCounts = useMemo<number[]>(() => {
    if (browsing) return sections.map((s) => s.tiles.length);
    const counts = sections.map(() => 0);
    flat.forEach((r) => { counts[r.catIdx] += 1; });
    return counts;
  }, [sections, flat, browsing]);

  // Clamp the category index whenever `sections` shrink.
  const selCat = Math.min(catIdx, Math.max(sections.length - 1, 0));
  const activeSection = sections[selCat];

  // The rows the right pane actually renders: the flat list while filtering,
  // else the selected category's tiles. wfIdx is clamped against this length.
  const rows: WorkflowTile[] = browsing ? (activeSection?.tiles ?? []) : flat.map((r) => r.tile);
  const wfMax = Math.max(rows.length - 1, 0);
  const selWf = Math.min(wfIdx, wfMax);

  // While filtering the workflow list is ALWAYS the focused pane; while browsing
  // it depends on which side the user stepped into.
  const wfFocused = browsing ? pane === "wf" : true;

  const fire = (key: string) => { onFire(key); onClose(); };
  const runRow = (tile: WorkflowTile | undefined) => {
    if (!tile || tile.disabled) return;   // skip disabled / "Soon" tiles
    fire(tile.key);
  };

  // Keyboard nav — intercepted at the window level so Arrow/Enter drive the
  // palette (preventDefault) rather than moving the input caret. Esc closes
  // (preserved from the original wiring).
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      const k = e.key;
      if (k === "Escape") { e.preventDefault(); onClose(); return; }

      const catMax = Math.max(sections.length - 1, 0);
      if (k === "ArrowDown") {
        e.preventDefault();
        if (browsing && pane === "cat") {
          setCatIdx((c) => Math.min(Math.min(c, catMax) + 1, catMax));
          setWfIdx(0);
        } else {
          setWfIdx((w) => Math.min(w + 1, wfMax));
        }
      } else if (k === "ArrowUp") {
        e.preventDefault();
        if (browsing && pane === "cat") {
          setCatIdx((c) => Math.max(Math.min(c, catMax) - 1, 0));
          setWfIdx(0);
        } else {
          setWfIdx((w) => Math.max(w - 1, 0));
        }
      } else if (k === "ArrowRight") {
        // Category rail → workflow list (only meaningful while browsing).
        if (browsing && pane === "cat") { e.preventDefault(); setPane("wf"); setWfIdx(0); }
      } else if (k === "ArrowLeft") {
        if (browsing && pane === "wf") { e.preventDefault(); setPane("cat"); }
      } else if (k === "Tab") {
        // Tab also jumps category-rail → workflow list while browsing.
        if (browsing) {
          e.preventDefault();
          setPane((p) => (p === "cat" ? "wf" : "cat"));
          setWfIdx(0);
        }
      } else if (k === "Enter") {
        e.preventDefault();
        runRow(rows[selWf]);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, onClose, browsing, pane, sections, wfMax, selWf, rows]);

  if (!open) return null;

  const matchLabel = browsing
    ? `${totalCount} workflows`
    : `${flat.length} / ${totalCount}`;

  return (
    <div
      className="fixed inset-0 z-[1000] flex items-start justify-center bg-black/60 p-6 pt-[13vh] backdrop-blur-sm"
      onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}
      role="dialog"
      aria-modal="true"
      aria-label="Workflow palette"
    >
      <div
        className={cn(
          "w-full max-w-[760px] overflow-hidden rounded-[14px] border border-line-2 bg-bg-1 shadow-[#23211C5C_0px_22px_50px_-14px,#23211C38_0px_6px_16px_-6px]",
          // Entrance scale/fade via a CSS transition (no new @keyframes). Dropped
          // entirely under prefers-reduced-motion (`entered` starts true there).
          !reduce && "transition-[opacity,transform] duration-150 ease-out will-change-transform",
          !reduce && (entered ? "scale-100 opacity-100" : "scale-[0.97] opacity-0"),
        )}
      >
        <div className="p-[12px]">
          {/* Search header — "/" badge + LIVE input + match / total count */}
          <div className="flex h-[42px] items-center gap-[10px] rounded-[10px] border border-line-2 bg-bg-2 px-[12px]">
            <span className="flex h-[20px] min-w-[20px] items-center justify-center rounded-[5px] bg-accent-soft px-[6px]">
              <span className="mono text-[11px] font-semibold leading-none text-accent">/</span>
            </span>
            <input
              ref={inputRef}
              value={query}
              onChange={(e) => { setQuery(e.target.value); setWfIdx(0); setPane("wf"); }}
              placeholder="Find a workflow"
              autoFocus
              spellCheck={false}
              autoComplete="off"
              aria-label="Find a workflow"
              className="grow bg-transparent text-[13px] leading-none text-t1 outline-none placeholder:text-t3"
            />
            <span className="tabular text-[11px] leading-none text-t3">{matchLabel}</span>
          </div>

          {/* Two-pane: LEFT categories rail · RIGHT workflows list */}
          <div className="mt-[10px] flex gap-[11px]">
            {/* LEFT — Categories rail (≈206px), one per source section. While
                filtering it shows per-category match counts; the highlighted
                category is the browse cursor (no highlight while filtering). */}
            <div className="flex w-[212px] shrink-0 flex-col gap-[2px] border-r border-line pr-[11px]">
              {sections.map((sect, i) => {
                const isSel = browsing && i === selCat;
                const Icon = sectionIcon(sect.title);
                const count = matchCounts[i] ?? sect.tiles.length;
                const dimmed = !browsing && count === 0;
                return (
                  <button
                    type="button"
                    key={sect.title}
                    onClick={() => { setCatIdx(i); setPane("cat"); setWfIdx(0); setQuery(""); inputRef.current?.focus(); }}
                    className={cn(
                      "flex h-[38px] items-center gap-[10px] rounded-lg px-[11px] text-left transition-colors",
                      isSel ? "bg-accent-soft" : "hover:bg-bg-2",
                    )}
                  >
                    <Icon
                      size={16}
                      strokeWidth={2}
                      className={cn("shrink-0", isSel ? "text-accent" : dimmed ? "text-t3" : "text-t2")}
                    />
                    <span
                      className={cn(
                        "grow truncate text-[12.5px] leading-[16px]",
                        isSel ? "font-semibold text-t1" : dimmed ? "font-medium text-t3" : "font-medium text-t1",
                      )}
                    >
                      {sect.title}
                    </span>
                    <span className={cn("mono shrink-0 text-[10.5px] leading-[14px]", isSel ? "text-accent" : "text-t3")}>
                      {count}
                    </span>
                  </button>
                );
              })}
            </div>

            {/* RIGHT — Workflows list: filtered flat list, or the selected
                category's tiles. */}
            <div className="flex max-h-[340px] grow basis-0 flex-col gap-[1px] overflow-y-auto">
              <div className="flex items-center justify-between px-[10px] pb-[5px] pt-[4px]">
                <span className="text-[9.5px] font-bold uppercase tracking-[0.11em] leading-[12px] text-t3">
                  {browsing ? (activeSection?.title ?? "") : "Results"}
                </span>
                <span className="tabular text-[10px] leading-[12px] text-t3">
                  {rows.length} {rows.length === 1 ? "workflow" : "workflows"}
                </span>
              </div>

              {rows.length === 0 ? (
                <div className="flex flex-col items-center gap-[5px] py-[36px] text-t3">
                  <span className="text-[12.5px] leading-[16px]">No workflow matches “{query.trim()}”</span>
                  <span className="text-[11px] leading-[14px]">Try a shorter term.</span>
                </div>
              ) : (
                rows.map((t, i) => {
                  const isActive = wfFocused && i === selWf;
                  const catTag = browsing ? null : flat[i]?.catTitle;
                  return (
                    <button
                      type="button"
                      key={`${t.key}-${i}`}
                      onClick={() => runRow(t)}
                      onMouseMove={() => { setWfIdx(i); if (browsing) setPane("wf"); }}
                      disabled={t.disabled}
                      aria-current={isActive ? "true" : undefined}
                      className={cn(
                        "group flex min-h-[40px] items-center gap-[10px] rounded-[9px] px-[10px] py-[6px] text-left transition-colors",
                        t.disabled
                          ? "pointer-events-none cursor-default opacity-50"
                          : isActive
                            ? "bg-accent-soft"
                            : "hover:bg-bg-2",
                      )}
                    >
                      {/* Left — a 2-line stack: NAME (+ category tag while
                          filtering) over a dim one-line DESC (Paper 37Q-0). */}
                      <div className="min-w-0 flex-1">
                        <div className="flex items-center gap-[8px]">
                          {t.active && <span className="shrink-0 text-[7px] leading-none text-accent">●</span>}
                          <span className="text-[12.5px] font-semibold leading-[16px] text-t1 whitespace-nowrap">{t.label}</span>
                          {catTag && (
                            <span className="shrink-0 rounded-[4px] bg-paper2 px-[6px] py-[1px] text-[9px] font-medium uppercase tracking-[0.04em] leading-[12px] text-t3">
                              {catTag}
                            </span>
                          )}
                        </div>
                        {t.desc && (
                          <div className={cn("mt-[2px] truncate text-[11px] leading-[14px]", isActive ? "text-t2" : "text-t3")}>
                            {t.desc}
                          </div>
                        )}
                      </div>
                      {/* Right — Soon (disabled), else the kbd shortcut pill + a
                          hover/active ↵ run affordance. */}
                      {t.disabled ? (
                        <span className="flex shrink-0 items-center rounded-[5px] border border-line-2 bg-paper2 px-[7px] py-[2px]">
                          <span className="text-[9px] font-semibold uppercase tracking-[0.06em] leading-[12px] text-t3">Soon</span>
                        </span>
                      ) : (
                        <>
                          {t.kbd && (
                            <span className="mono shrink-0 rounded-[5px] border border-line-2 px-[6px] py-[2px] text-[10px] font-semibold leading-[12px] text-t2">
                              {t.kbd}
                            </span>
                          )}
                          <span
                            className={cn(
                              "shrink-0 text-[10.5px] font-medium leading-[14px] text-accent transition-opacity",
                              isActive ? "opacity-100" : "opacity-0 group-hover:opacity-100",
                            )}
                          >
                            ↵ run
                          </span>
                        </>
                      )}
                    </button>
                  );
                })
              )}
            </div>
          </div>

          {/* Footer — keyboard hints */}
          <div className="mx-[2px] mt-[10px] h-px bg-line" />
          <div className="flex items-center gap-[16px] px-[9px] pb-[1px] pt-[8px]">
            <span className="text-[10.5px] leading-[14px] text-t3">↑↓ Navigate</span>
            <span className="text-[10.5px] leading-[14px] text-t3">← → Category</span>
            <span className="text-[10.5px] leading-[14px] text-t3">↵ Run</span>
            <span className="text-[10.5px] leading-[14px] text-t3">esc Dismiss</span>
            <div className="grow" />
            <span className="text-[10.5px] leading-[14px] text-t3">Type to filter all {totalCount} workflows</span>
          </div>
        </div>
      </div>
    </div>
  );
}
