import type { LucideIcon } from "lucide-react";
import { cn } from "../../lib/cn";

interface NavItemProps {
  /** lucide-react icon component, rendered at 16px with currentColor. */
  icon: LucideIcon;
  /** Visible row label. */
  label: string;
  /** Current-route state — paints the subtle light overlay + full-strength text. */
  active?: boolean;
  /**
   * Optional numeric badge (e.g. Inbox unresolved count). Rendered as a small
   * oxblood pill with white numerals, right-aligned. Hidden when 0/undefined.
   */
  badge?: number;
  /** Indented, slightly smaller sub-item (an expanded child row). */
  sub?: boolean;
  /** Click handler. */
  onClick?: () => void;
}

/**
 * NavItem — a left-sidebar nav row.
 *
 * Sits on the `bg-rail` surface (mid-tone teal in LIGHT, deep navy in DARK), so
 * text is light: muted idle, full-strength on hover, and full-strength under a
 * soft `white/16` overlay when `active`. All colours come from theme tokens +
 * fixed-alpha white overlays, so it reads correctly in both themes without a
 * per-theme branch. Ref: the left nav in the Desk — Helix and Activity screens.
 */
export function NavItem({
  icon: Icon,
  label,
  active = false,
  badge,
  sub = false,
  onClick,
}: NavItemProps) {
  const showBadge = typeof badge === "number" && badge > 0;
  return (
    <button
      type="button"
      onClick={onClick}
      aria-current={active ? "page" : undefined}
      className={cn(
        "group flex w-full items-center rounded-lg transition-colors",
        sub
          ? "gap-[10px] py-[6px] pl-[34px] pr-[12px] text-[12.5px]"
          : "gap-[12px] px-[12px] py-[8px] text-[13.5px]",
        active
          ? "bg-white/[0.16] text-white"
          : "text-[#F4F8F7]/85 hover:bg-white/[0.08] hover:text-white",
      )}
    >
      <Icon size={16} className="shrink-0" />
      <span className="flex-1 truncate text-left font-medium">{label}</span>
      {showBadge && (
        <span className="ml-auto inline-flex h-[18px] min-w-[18px] shrink-0 items-center justify-center rounded-full bg-red px-[5px] text-[10px] font-semibold tabular text-white">
          {badge}
        </span>
      )}
    </button>
  );
}
