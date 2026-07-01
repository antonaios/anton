import type { LucideIcon } from "lucide-react";
import { cn } from "../../lib/cn";

/**
 * Chip — a small pill/tag primitive.
 *
 * Visual reference: Inbox tier chips (e.g. "APPROVAL" with a check) + Recall
 * sensitivity chips (PUBLIC / INTERNAL / CONFIDENTIAL / MNPI). Each variant is a
 * soft tinted background paired with the matching semantic text colour, so it
 * scans at a glance and reads correctly in both the light-teal and dark-navy
 * themes (it uses theme-token classes only — no raw hex).
 *
 * Sensitivity map: public = green, internal = t2, confidential = amber,
 * mnpi = red. The colour aliases (sage / amber / oxblood) mirror those so a
 * caller can pick by either the sensitivity name or the colour name.
 */
export type ChipVariant =
  | "neutral"
  | "accent"
  | "public"
  | "internal"
  | "confidential"
  | "mnpi"
  | "sage"
  | "amber"
  | "oxblood";

export interface ChipProps {
  /** The text shown inside the pill. */
  label: string;
  /** Colour/semantic treatment. Defaults to "neutral". */
  variant?: ChipVariant;
  /** Optional leading lucide icon, rendered at 11px in the chip's text colour. */
  icon?: LucideIcon;
  /** Extra classes merged onto the root span. */
  className?: string;
}

// Per-variant background + text + border treatment. Tints come from the
// theme-token aliases so the chip flips automatically between themes.
const VARIANT_CLASS: Record<ChipVariant, string> = {
  neutral: "bg-bg-2 text-t2 border-line",
  accent: "bg-accent-soft text-accent border-accent-line",
  public: "bg-green/15 text-green border-green/40",
  sage: "bg-green/15 text-green border-green/40",
  internal: "bg-bg-2 text-t2 border-line",
  confidential: "bg-amber/15 text-amber border-amber/40",
  amber: "bg-amber/15 text-amber border-amber/40",
  mnpi: "bg-red/15 text-red border-red/40",
  oxblood: "bg-red/15 text-red border-red/40",
};

export function Chip({ label, variant = "neutral", icon: Icon, className }: ChipProps) {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-[4px] rounded-md border px-[8px] py-[2px] text-[11px] leading-[16px] whitespace-nowrap",
        VARIANT_CLASS[variant],
        className,
      )}
    >
      {Icon && <Icon size={11} className="shrink-0" />}
      {label}
    </span>
  );
}
