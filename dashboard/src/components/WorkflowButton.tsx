import { cn } from "../lib/cn";
import type { WorkflowKey } from "../types";

interface Props {
  code: string;
  label: string;
  workflowKey: WorkflowKey;
  wired?: boolean;
  suggested?: boolean;
  disabled?: boolean;
  active?: boolean;
  onClick?: (key: WorkflowKey) => void;
}

export function WorkflowButton({
  label, workflowKey,
  wired = false, suggested = false, disabled = false, active = false,
  onClick,
}: Props) {
  return (
    <button
      type="button"
      disabled={disabled}
      onClick={() => onClick?.(workflowKey)}
      className={cn(
        "flex min-h-[30px] w-full items-center gap-[7px] border px-[9px] py-[7px] text-left text-[11.5px] transition-colors",
        "bg-bg-2 border-line text-text-primary",
        !disabled && !suggested && "hover:bg-panel-hover hover:border-line-strong",
        wired && !suggested && "hover:border-green/35",
        suggested && "border-brand-red bg-panel-warm hover:bg-[rgba(200,32,29,0.10)]",
        disabled && "cursor-default opacity-[0.38] pointer-events-none",
        active && !suggested && "border-info bg-panel-hover",
      )}
    >
      <span className="flex-1 truncate">{label}</span>
      {wired && <span className="ml-auto h-[5px] w-[5px] shrink-0 rounded-full bg-green" />}
    </button>
  );
}
