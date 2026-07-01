import { cn } from "../../lib/cn";

/**
 * StatusBadge — a status dot with an optional label.
 *
 * Visual reference: the Activity status dots + scheduler rows (Activity ·
 * light@2x). A 6px solid dot in the semantic status colour, optionally paired
 * with a small label. Theme-token classes only, so the dot + label flip
 * automatically between the light-teal and dark-navy themes.
 *
 * Status → colour:
 *   ok      → green (sage — completed / healthy)
 *   live    → ok-bright (structural blue — actively live)
 *   running → accent (deep-teal / gold — in progress)
 *   error   → red (oxblood — failed)
 *   paused  → t4 (faint — idle / paused)
 */
export type StatusKind = "ok" | "running" | "error" | "paused" | "live";

export interface StatusBadgeProps {
  /** Which status to render. Drives the dot (and label) colour. */
  status: StatusKind;
  /** Optional label shown next to the dot, at 11px in the status colour. */
  label?: string;
  /** Extra classes merged onto the root span. */
  className?: string;
}

// Per-status dot fill + label text colour, paired so the two always agree.
const STATUS_CLASS: Record<StatusKind, { dot: string; text: string }> = {
  ok:      { dot: "bg-green",     text: "text-green" },
  live:    { dot: "bg-ok-bright", text: "text-ok-bright" },
  running: { dot: "bg-accent",    text: "text-accent" },
  error:   { dot: "bg-red",       text: "text-red" },
  paused:  { dot: "bg-t4",        text: "text-t4" },
};

export function StatusBadge({ status, label, className }: StatusBadgeProps) {
  const { dot, text } = STATUS_CLASS[status];
  return (
    <span className={cn("inline-flex items-center gap-[6px] whitespace-nowrap", className)}>
      <span className={cn("h-[6px] w-[6px] shrink-0 rounded-full", dot)} />
      {label && <span className={cn("text-[11px] leading-[16px]", text)}>{label}</span>}
    </span>
  );
}
