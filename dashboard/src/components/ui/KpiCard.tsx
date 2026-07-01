import { cn } from "../../lib/cn";

interface KpiCardProps {
  /** Small UPPERCASE label above the value, e.g. "SPONSOR IRR". Rendered in mono. */
  label: string;
  /** The headline figure, e.g. "22.4%" or "2.6×". Strings keep formatting control with the caller. */
  value: string | number;
  /** Optional trailing unit shown next to the value at a smaller size, e.g. "×" or "/yr". */
  unit?: string;
  /** Optional sub-line under the value, e.g. a delta or context note. */
  sub?: string;
  className?: string;
}

/**
 * KpiCard — a single KPI tile (label · big value · optional unit/sub) in a
 * bordered box. Theme-token only, so it flips between LIGHT teal and DARK navy
 * automatically. Ref: the Desk SPONSOR IRR / MOIC / EQUITY CHEQUE / ENTRY tiles.
 */
export function KpiCard({ label, value, unit, sub, className }: KpiCardProps) {
  return (
    <div className={cn("rounded-lg border border-line bg-bg-2 p-[12px]", className)}>
      <div className="mono text-[10px] tracking-wide uppercase text-t3">{label}</div>
      <div className="mt-[6px] flex items-baseline gap-[3px]">
        <span className="tabular text-[20px] font-medium leading-none text-t1">{value}</span>
        {unit && <span className="text-[12px] text-t3">{unit}</span>}
      </div>
      {sub && <div className="mt-[5px] text-[11px] text-t3">{sub}</div>}
    </div>
  );
}
