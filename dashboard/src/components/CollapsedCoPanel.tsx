import { ChevronsLeft, BarChart3 } from "lucide-react";

/**
 * Collapsed Live-Model co-panel — the thin (48px) drawer tab the 55″ co-panel
 * folds into by default. Mirrors the 13″ sessions collapse: a narrow surface
 * strip with an expand chevron, the model icon, a vertical "LIVE MODEL" label,
 * and the amber preview dot. Clicking anywhere expands it to the full
 * LiveModelCoPanel. (Default-collapsed so the engine preview doesn't crowd the
 * Desk until the operator opens it.)
 */
export function CollapsedCoPanel({ onExpand }: { onExpand: () => void }) {
  return (
    <button
      type="button"
      onClick={onExpand}
      title="Expand Live Model"
      aria-label="Expand Live Model panel"
      className="group flex w-[48px] shrink-0 flex-col items-center gap-[16px] rounded-[16px] bg-bg-2 py-[16px] shadow-card transition-colors hover:bg-bg-1 min-h-0"
    >
      <span className="flex size-[30px] items-center justify-center rounded-[8px] text-t3 transition-colors group-hover:text-t1">
        <ChevronsLeft size={15} strokeWidth={1.8} />
      </span>
      <span className="h-px w-[28px] bg-line" />
      <BarChart3 size={16} strokeWidth={1.8} className="text-t2 transition-colors group-hover:text-t1" />
      <span className="mt-[2px] text-[10px] font-bold uppercase tracking-[0.18em] text-t3 [writing-mode:vertical-rl]">
        Live Model
      </span>
      <span className="mt-auto size-[6px] shrink-0 rounded-full bg-amber" title="Preview · roadmap" />
    </button>
  );
}
