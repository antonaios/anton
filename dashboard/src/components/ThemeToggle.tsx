import { Moon, Sun } from "lucide-react";
import { useTheme } from "./ThemeProvider";

/**
 * Light/dark toggle — swaps LIGHT teal ↔ DARK navy. The click coordinates seed
 * the circular-wipe View Transition (src/lib/theme.ts).
 */
export function ThemeToggle() {
  const { theme, toggle } = useTheme();
  const isNavy = theme === "navy";
  return (
    <button
      type="button"
      aria-label={isNavy ? "Switch to light theme" : "Switch to dark theme"}
      aria-pressed={isNavy}
      onClick={(e) => toggle({ x: e.clientX, y: e.clientY })}
      className="flex h-[28px] w-[28px] items-center justify-center rounded-lg text-t2 hover:text-t1 hover:bg-bg-2 transition-colors"
    >
      {isNavy ? <Sun size={15} /> : <Moon size={15} />}
    </button>
  );
}
