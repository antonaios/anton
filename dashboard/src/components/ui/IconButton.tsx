import type { LucideIcon } from "lucide-react";
import { cn } from "../../lib/cn";

interface IconButtonProps {
  /** lucide-react icon component, rendered with currentColor. */
  icon: LucideIcon;
  /** Required accessible label — there is no visible text. */
  label: string;
  /** Click handler. */
  onClick?: () => void;
  /** Pressed/selected state — paints the box in the accent tint. */
  active?: boolean;
  /**
   * `ghost` (default) — borderless, hover paints a soft inset.
   * `bordered` — adds a `line-2` outline + rounded box (scheduler run/pause/
   * history controls in the Activity screen).
   */
  variant?: "ghost" | "bordered";
  /** Icon glyph size in px (the box stays a fixed ~28px square). */
  size?: number;
  /** Disable interaction + dim the box. */
  disabled?: boolean;
  /** Optional native tooltip / title. */
  title?: string;
}

/**
 * IconButton — square ghost icon button (~28px box).
 *
 * The default `ghost` variant matches the borderless hover-bg pattern used by
 * the ThemeToggle and other header controls; `bordered` matches the boxed
 * scheduler run / pause / history controls on the Activity screen. Colours come
 * entirely from theme tokens, so it works in both the LIGHT teal and DARK navy
 * themes without per-theme branches.
 */
export function IconButton({
  icon: Icon,
  label,
  onClick,
  active = false,
  variant = "ghost",
  size = 15,
  disabled = false,
  title,
}: IconButtonProps) {
  return (
    <button
      type="button"
      aria-label={label}
      aria-pressed={active}
      onClick={onClick}
      disabled={disabled}
      title={title ?? label}
      className={cn(
        "flex h-[28px] w-[28px] shrink-0 items-center justify-center rounded-lg transition-colors",
        "outline-none focus-visible:ring-2 focus-visible:ring-accent-line",
        "disabled:cursor-default disabled:opacity-40",
        variant === "bordered" && "border border-line-2",
        active
          ? "bg-accent-soft text-accent"
          : "text-t2 enabled:hover:text-t1 enabled:hover:bg-bg-2",
      )}
    >
      <Icon size={size} />
    </button>
  );
}
