import { useEffect, useState } from "react";

/**
 * Subscribe to a CSS media query and re-render when it flips. SSR-safe-ish
 * (defaults to false before mount). Used by the Desk to drive the responsive
 * layout: collapse the sessions rail on a 13″ laptop, add the Live-Model
 * co-panel on a 55″ display (Paper Desk variants 4VJ-0 / 5CB-0).
 */
export function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = useState<boolean>(() =>
    typeof window !== "undefined" && "matchMedia" in window
      ? window.matchMedia(query).matches
      : false,
  );

  useEffect(() => {
    if (typeof window === "undefined" || !("matchMedia" in window)) return;
    const mql = window.matchMedia(query);
    const onChange = () => setMatches(mql.matches);
    onChange();                       // sync on (re)subscribe
    mql.addEventListener("change", onChange);
    return () => mql.removeEventListener("change", onChange);
  }, [query]);

  return matches;
}

/**
 * The Desk's three width bands, matching the agreed Paper Desk artboards:
 *   compact  (≤ 1535px)  — 13″ laptop: sessions rail folds to a 56px icon strip (4VJ-0).
 *                          Threshold is 1535 (not 1439) so real 13″ laptops, which
 *                          report 1440–1512 logical px, actually trigger the collapse.
 *   wide     (1536–1919) — the default desk (2A0-0)
 *   ultra    (≥ 1920px)  — 55″ display: + the Live-Model co-panel (5CB-0)
 */
export function useDeskLayout(): "compact" | "wide" | "ultra" {
  const compact = useMediaQuery("(max-width: 1535px)");
  const ultra = useMediaQuery("(min-width: 1920px)");
  return ultra ? "ultra" : compact ? "compact" : "wide";
}
