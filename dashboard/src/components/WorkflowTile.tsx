import type { ReactNode } from "react";
import { cn } from "../lib/cn";

type Cols = 2 | 3 | 4 | 7;
export type TileCategory = "research" | "meetings" | "valuation" | "vault" | "tx";

interface Props {
  title: string;
  count: number;
  cols?: Cols;
  fullWidth?: boolean;
  category?: TileCategory;
  children: ReactNode;
}

const COL_CLASS: Record<Cols, string> = {
  2: "grid-cols-2",
  3: "grid-cols-3",
  4: "grid-cols-4",
  7: "grid-cols-7",
};

// Left-border colour per category — overrides the base border-line via inline style.
const CATEGORY_BORDER_COLOR: Record<TileCategory, string> = {
  research:  "rgba(106,140,175,0.45)",
  meetings:  "rgba(199,137,77,0.38)",
  valuation: "rgba(152,160,164,0.42)",
  vault:     "rgba(107,176,131,0.42)",
  tx:        "rgba(200,32,29,0.38)",
};

// Header background tint per category.
const CATEGORY_HDR_BG: Record<TileCategory, string> = {
  research:  "bg-info/[0.05]",
  meetings:  "bg-amber/[0.04]",
  valuation: "bg-[rgba(152,160,164,0.05)]",
  vault:     "bg-green/[0.05]",
  tx:        "bg-brand-red/[0.04]",
};

export function WorkflowTile({ title, cols = 3, fullWidth = false, category, children }: Props) {
  const borderStyle = category
    ? { borderLeftColor: CATEGORY_BORDER_COLOR[category] }
    : undefined;

  return (
    <div
      className={cn("flex flex-col border border-line bg-bg-2", fullWidth && "col-span-2")}
      style={borderStyle}
    >
      <div className={cn(
        "flex items-center border-b border-line px-[10px] py-[6px]",
        category && CATEGORY_HDR_BG[category],
      )}>
        <span className="text-[9.5px] font-semibold uppercase tracking-[0.18em] text-label">{title}</span>
      </div>
      <div className={cn("grid gap-1 p-2", COL_CLASS[cols])}>
        {children}
      </div>
    </div>
  );
}
