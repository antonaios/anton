import { createContext, useContext, useState, type ReactNode } from "react";
import { type Theme, getActiveTheme, switchTheme } from "../lib/theme";

interface ThemeContextValue {
  theme: Theme;
  /** Toggle teal ↔ navy. Pass the click origin to seed the circular wipe. */
  toggle: (origin?: { x: number; y: number }) => void;
}

const ThemeContext = createContext<ThemeContextValue | null>(null);

/**
 * Provides the active theme + a toggle. Seeds state from the DOM attribute the
 * pre-paint script (index.html) already applied — the attribute is the single
 * source of truth, so there is no flash and no client/markup mismatch.
 */
export function ThemeProvider({ children }: { children: ReactNode }) {
  const [theme, setTheme] = useState<Theme>(() => getActiveTheme());

  const toggle = (origin?: { x: number; y: number }) => {
    const next: Theme = theme === "navy" ? "teal" : "navy";
    switchTheme(next, origin);
    setTheme(next);
  };

  return <ThemeContext.Provider value={{ theme, toggle }}>{children}</ThemeContext.Provider>;
}

export function useTheme(): ThemeContextValue {
  const ctx = useContext(ThemeContext);
  if (!ctx) throw new Error("useTheme must be used within ThemeProvider");
  return ctx;
}
