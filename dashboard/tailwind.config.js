/** @type {import('tailwindcss').Config} */
//
// v2 redesign (2026-06-24) — runtime two-theme system. Token NAMES are preserved
// (every component themes without edits); VALUES are now CSS custom properties
// defined in src/index.css and FLIP at runtime: `:root` = LIGHT teal (default),
// `[data-theme="navy"]` = DARK navy+gold. See src/lib/theme.ts for the toggle.
//
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        // ── 16 design tokens (themed in index.css) ───────────────────────
        bg:             "var(--bg)",        // ground / page
        "bg-1":         "var(--card)",      // card / panel
        "bg-2":         "var(--surface)",   // inset (inputs, meter tracks)
        line:           "var(--line)",      // hairline
        "line-2":       "var(--line2)",     // stronger hairline
        t1:             "var(--ink)",       // primary text
        t2:             "var(--slate)",     // secondary
        t3:             "var(--mist)",      // tertiary
        t4:             "var(--faint)",     // faint
        accent:         "var(--accent)",
        "accent-soft":  "var(--accent-soft)",
        "accent-line":  "var(--accent-line)",
        // ── Structural-blue register (live / static) ─────────────────────
        "accent-2":     "var(--accent2)",
        "accent-2-soft":"var(--accent2-soft)",
        "accent-2-line":"var(--accent2-line)",
        ok:             "var(--mist)",       // dim — completed / static
        "ok-bright":    "var(--accent2)",    // live — LIVE dot, usage bars, meter
        warn:           "var(--amber)",      // reserved
        // ── New v2 tokens (no legacy name) ───────────────────────────────
        paper2:         "var(--paper2)",
        rail:           "var(--rail)",       // sidebars / context rails
        "rail-line":    "var(--rail-line)",
        // ── Back-compat aliases → tokens ─────────────────────────────────
        page:             "var(--bg)",
        panel:            "var(--card)",
        "panel-hover":    "var(--surface)",
        "panel-warm":     "var(--card)",
        "line-strong":    "var(--line2)",
        "line-faint":     "var(--line)",
        "text-primary":   "var(--ink)",
        "text-secondary": "var(--slate)",
        "text-tertiary":  "var(--mist)",
        label:            "var(--slate)",
        green:            "var(--sage)",     // ticker / up
        amber:            "var(--amber)",    // warn tone
        red:              "var(--oxblood)",  // down / negative
        info:             "var(--accent2)",  // structural blue
        "brand-red":      "var(--accent)",   // brand mark follows accent
      },
      fontFamily: {
        // v2 redesign: Geist (UI) + Geist Mono — self-hosted via @fontsource.
        sans:    ["Geist Variable", "Geist", "ui-sans-serif", "system-ui", "sans-serif"],
        mono:    ["Geist Mono Variable", "Geist Mono", "ui-monospace", "SFMono-Regular", "Menlo", "Consolas", "monospace"],
        display: ["Geist Variable", "Geist", "ui-sans-serif", "system-ui", "sans-serif"],
      },
      fontSize: {
        xs:   ["12px", { lineHeight: "16px" }],
        sm:   ["13px", { lineHeight: "18px" }],
        base: ["13px", { lineHeight: "1.45" }],   // body baseline
        lg:   ["15px", { lineHeight: "22px" }],
        xl:   ["20px", { lineHeight: "28px" }],
        "2xl":["28px", { lineHeight: "34px" }],
      },
      borderRadius: {
        DEFAULT: "4px",
        lg: "8px",     // pills / buttons
        xl: "12px",    // cards
        "2xl": "16px",
      },
      boxShadow: {
        // card elevation — Paper's 2-layer warm lift: a tight contact shadow
        // plus a wide, lifted ambient layer (#23211C @ 5% / 15%). Reads as
        // soft elevated paper rather than a hard black drop.
        card: "0 1px 2px rgba(35,33,28,0.05), 0 10px 26px -16px rgba(35,33,28,0.15)",
        // pronounced card lift — Paper's hero / Sources panels carry a wider,
        // more ambient version of the card drop (#23211C @ 5% / 15%).
        "card-lift": "0 2px 5px rgba(35,33,28,0.05), 0 16px 40px -20px rgba(35,33,28,0.15)",
        // single-layer contact shadow — Paper's search bar (no ambient layer).
        contact: "0 1px 2px rgba(35,33,28,0.05)",
        // modal elevation — Paper's deep overlay drop (#081412 @ 30%).
        modal: "0 26px 64px rgba(8,20,18,0.30)",
      },
    },
  },
  plugins: [],
};
