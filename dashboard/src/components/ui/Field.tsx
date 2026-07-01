import type { ReactNode } from "react";
import { cn } from "../../lib/cn";

/**
 * Field — a labelled input group. A mono UPPERCASE label sits above a controlled
 * text input wearing the shared inset chrome (bg-bg-2 + line-2 hairline). Used by
 * the Budget cap inputs and the Operator config fields; themes for free via tokens.
 */
export interface FieldProps {
  label: string;
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  type?: string;
  required?: boolean;
  className?: string;
}

/** Shared label + control chrome so Field and FieldBox stay visually identical. */
const LABEL_CLASS = "mono text-t3 text-[10px] tracking-[0.1em] uppercase";
const CONTROL_CLASS =
  "bg-bg-2 border border-line-2 rounded-lg px-[12px] h-[34px] text-t1 placeholder:text-t3";

export function Field({
  label, value, onChange, placeholder, type = "text", required, className,
}: FieldProps) {
  return (
    <label className={cn("flex flex-col gap-[6px]", className)}>
      <span className={LABEL_CLASS}>
        {label}
        {required && <span className="text-accent"> *</span>}
      </span>
      <input
        type={type}
        value={value}
        required={required}
        placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)}
        className={cn(
          CONTROL_CLASS,
          "w-full text-[13px] outline-none transition-colors focus:border-accent-line",
        )}
      />
    </label>
  );
}

export interface FieldBoxProps {
  label: string;
  children: ReactNode;
  required?: boolean;
  className?: string;
}

/** FieldBox — same label + outer chrome as Field, but renders an arbitrary
 *  control (select, segmented toggle, read-only value) in the input slot. */
export function FieldBox({ label, children, required, className }: FieldBoxProps) {
  return (
    <div className={cn("flex flex-col gap-[6px]", className)}>
      <span className={LABEL_CLASS}>
        {label}
        {required && <span className="text-accent"> *</span>}
      </span>
      <div className={cn(CONTROL_CLASS, "flex items-center w-full text-[13px]")}>
        {children}
      </div>
    </div>
  );
}
