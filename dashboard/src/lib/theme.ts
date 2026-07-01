// Runtime theme — LIGHT teal (default) ↔ DARK navy+gold. Token VALUES live in
// src/index.css (:root vs [data-theme="navy"]); this module flips the data-theme
// attribute on <html>, persists the choice, and animates the swap via the View
// Transitions API (circular wipe) with a reduced-motion / no-API fallback.

export type Theme = "teal" | "navy";

const STORAGE_KEY = "anton-theme";

/** Persisted theme; defaults to "teal" (light). Storage-safe (private mode). */
export function getStoredTheme(): Theme {
  try {
    return localStorage.getItem(STORAGE_KEY) === "navy" ? "navy" : "teal";
  } catch {
    return "teal";
  }
}

/** The theme currently applied to the document — the DOM attribute is the
 *  single source of truth (the pre-paint script in index.html set it). */
export function getActiveTheme(): Theme {
  return document.documentElement.getAttribute("data-theme") === "navy" ? "navy" : "teal";
}

/** Idempotent: set/remove the single data-theme attribute (teal = absent). */
export function applyTheme(theme: Theme): void {
  const root = document.documentElement;
  if (theme === "navy") root.setAttribute("data-theme", "navy");
  else root.removeAttribute("data-theme");
}

function storeTheme(theme: Theme): void {
  try {
    localStorage.setItem(STORAGE_KEY, theme);
  } catch {
    /* blocked storage — non-fatal, the toggle still works for the session */
  }
}

/**
 * Switch theme with a circular-wipe View Transition originating at the toggle.
 * Falls back to an instant swap when the API is unavailable or the user prefers
 * reduced motion. Call only from a user gesture (never a mount effect) so
 * React.StrictMode's dev double-invoke can't fire two transitions.
 */
export function switchTheme(next: Theme, origin?: { x: number; y: number }): void {
  const apply = () => { applyTheme(next); storeTheme(next); };

  const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (reduce || !document.startViewTransition) { apply(); return; }

  const x = origin?.x ?? window.innerWidth - 40;
  const y = origin?.y ?? 40;
  const radius = Math.hypot(
    Math.max(x, window.innerWidth - x),
    Math.max(y, window.innerHeight - y),
  );

  const transition = document.startViewTransition(apply);
  transition.ready
    .then(() => {
      document.documentElement.animate(
        { clipPath: [`circle(0px at ${x}px ${y}px)`, `circle(${radius}px at ${x}px ${y}px)`] },
        { duration: 300, easing: "ease-in-out", pseudoElement: "::view-transition-new(root)" },
      );
    })
    .catch(() => { /* transition skipped — the swap was already applied */ });
}
