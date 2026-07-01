import type { ReactNode } from "react";
import { cn } from "../../lib/cn";

interface CardProps {
  children: ReactNode;
  /** Extra classes merged after the base card styles (can override padding, etc.). */
  className?: string;
  /** When true (default) the card carries inner padding (16px). Set false for
   *  edge-to-edge content like tables or list rows that manage their own insets. */
  padded?: boolean;
}

/**
 * Card — the base surface primitive.
 *
 * A themed card panel: rounded-xl, level-1 surface, hairline border, soft
 * elevation. Token-driven, so it flips automatically between the LIGHT teal
 * and DARK navy+gold themes. `overflow-hidden` keeps child corners (table
 * heads, media, inset rows) clipped to the rounded shell.
 *
 * Presentational + composable — it renders only its children. Compose richer
 * panels (metric tiles, right-rail blocks, tables) on top of it.
 */
export function Card({ children, className, padded = true }: CardProps) {
  return (
    <div
      className={cn(
        "overflow-hidden rounded-xl border border-line bg-bg-1 shadow-card",
        padded && "p-[16px]",
        className,
      )}
    >
      {children}
    </div>
  );
}
