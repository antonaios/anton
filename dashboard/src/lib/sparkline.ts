/**
 * Derive a sparkline's "direction" (up / down / flat) from its SVG
 * polyline string by comparing the first and last y-coordinates.
 *
 * Our coordinate system is: y=2 is the top of the viewbox (= highest
 * price in the series), y=18 is the bottom (= lowest price). So a
 * smaller y at the end means the line *ended higher* and we should
 * render green.
 *
 * This is separate from the intraday `Quote.direction` field which is
 * computed from last_price vs today's open. Those answer different
 * questions:
 *   - sparkline stroke colour  → did the price END the 12-month window
 *                                higher than it started?
 *   - change-pct text colour   → is today's move green/red?
 *
 * Threshold (0.5px) is roughly the SVG resolution of a noisy weekly
 * series — anything inside that band reads as flat.
 */
export function sparklineDirection(points: string | null | undefined): "up" | "down" | "flat" {
  if (!points) return "flat";
  const parts = points.trim().split(/\s+/);
  if (parts.length < 2) return "flat";

  const firstCoord = parts[0].split(",");
  const lastCoord = parts[parts.length - 1].split(",");
  if (firstCoord.length !== 2 || lastCoord.length !== 2) return "flat";

  const firstY = parseFloat(firstCoord[1]);
  const lastY = parseFloat(lastCoord[1]);
  if (Number.isNaN(firstY) || Number.isNaN(lastY)) return "flat";

  // Lower y = higher price in our viewbox.
  if (firstY > lastY + 0.5) return "up";
  if (firstY < lastY - 0.5) return "down";
  return "flat";
}

/** Convenience: stroke colour token for a sparkline direction. */
export function sparklineStroke(dir: "up" | "down" | "flat"): string {
  if (dir === "up") return "#6bb083";
  if (dir === "down") return "#c0524a";
  return "#7a8898";
}
