import { cn } from "../../lib/cn";

export interface SegmentedOption {
  value: string;
  label: string;
}

interface Props {
  /** The segments, in display order. `value` is the key passed to `onChange`. */
  options: SegmentedOption[];
  /** Currently-selected segment value. Must match one option's `value`. */
  value: string;
  /** Fired with the picked segment's value. No-op if the active segment is re-clicked. */
  onChange: (value: string) => void;
  /** Extra classes on the outer track (e.g. width / self-alignment). */
  className?: string;
}

/**
 * SegmentedToggle — a sliding segmented control: a row of pills sharing one
 * inset track, where the active segment reads as `bg-accent-soft` + `text-accent`
 * and idle segments are quiet (`text-t2`) until hovered.
 *
 * Presentational + theme-token only, so it flips between the LIGHT teal and
 * DARK navy+gold themes automatically. Used for the Activity filter
 * (All / Routine runs / Vault changes) and the Desk right-rail tab switch
 * (Open actions / Chat).
 */
export function SegmentedToggle({ options, value, onChange, className }: Props) {
  return (
    <div
      role="tablist"
      className={cn(
        "inline-flex items-center gap-[2px] rounded-lg bg-bg-2 p-[3px]",
        className,
      )}
    >
      {options.map((opt) => {
        const active = opt.value === value;
        return (
          <button
            key={opt.value}
            type="button"
            role="tab"
            aria-selected={active}
            onClick={() => { if (!active) onChange(opt.value); }}
            className={cn(
              "rounded-[6px] px-[12px] py-[5px] text-[12px] leading-[16px] transition-colors",
              "outline-none focus-visible:ring-1 focus-visible:ring-accent-line",
              active
                ? "bg-accent-soft text-accent font-medium"
                : "text-t2 hover:text-t1",
            )}
          >
            {opt.label}
          </button>
        );
      })}
    </div>
  );
}
