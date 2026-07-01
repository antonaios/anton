import { useEffect, useRef, useState, type ReactNode } from "react";
import { FolderOpen, Plus, RefreshCcw } from "lucide-react";
import { api, ApiError } from "../lib/api";
import { cn } from "../lib/cn";
import { useMediaQuery } from "../lib/useMediaQuery";
import { StatusBadge } from "./ui/StatusBadge";

/**
 * News tab — the Library · News surface.
 *
 * Surfaces the wired sector-news workflow: a single live "Run news pull" action
 * (api.sectorNewsRun) plus a faithful, design-ahead illustrative layout of what
 * that run yields — the expertise-sector coverage row, the latest synthesised
 * newsletter, and the "this run produced" routing summary (proposals → Inbox,
 * deals → tracker, newsletter → vault).
 *
 * Honesty: ONLY the run-pull button calls the bridge. The coverage cards,
 * newsletter card and produced-list are static previews of the workflow's shape
 * — the page header carries a small "SOON" framing pill to say so. Token-only,
 * so it flips automatically between the LIGHT teal and DARK navy+gold themes.
 *
 * Motion (presentation only — no data/API change): dispatching the run spins the
 * button glyph, then the "this run produced" rows do a staggered reveal (each row
 * fades + lifts in, delayed by its index). Built with CSS transitions +
 * per-row transition-delay (no @keyframes) plus Tailwind's built-in animate-spin;
 * `prefers-reduced-motion` drops the spin and the stagger (rows just appear).
 */

/** True when the OS "reduce motion" preference is set — drops the dispatch spin
 *  and the staggered reveal down to a plain, instant render. Same pattern as
 *  ChatCanvas's status-line gate. */
function usePrefersReducedMotion(): boolean {
  return useMediaQuery("(prefers-reduced-motion: reduce)");
}

// ── Illustrative preview data (NOT wired — see component doc) ────────────────
interface SectorPreview {
  name: string;
  lastRun: string;
  items: number;
}

const SECTORS: SectorPreview[] = [
  { name: "Logistics & Freight", lastRun: "06-24", items: 14 },
  { name: "Industrials",         lastRun: "06-24", items: 9 },
  { name: "Building Products",   lastRun: "06-24", items: 6 },
];

const NEWSLETTER_BULLETS: string[] = [
  "DSV completes the DB Schenker integration ahead of plan; management guides mid-single-digit synergy upgrade.",
  "Maersk flags Q3 ocean volumes down ~4% y/y; spot rates soften 6% w/w on capacity returns.",
  "Two mid-market 3PL carve-outs entered the market — flagged to the deal-tracker (see below).",
];

interface ProducedRow {
  count: number;
  label: string;
  target: string;
}

const PRODUCED: ProducedRow[] = [
  { count: 3, label: "sector-extraction proposals",              target: "Inbox" },
  { count: 1, label: "sector-synthesis proposal",                target: "Inbox" },
  { count: 2, label: "M&A items auto-fed to the deal-tracker",   target: "Deals" },
  { count: 1, label: "newsletter written to the vault",          target: "Vault" },
];

// Small UPPERCASE section label, matching the other redesigned tabs.
function SectionLabel({ children }: { children: ReactNode }) {
  return (
    <div className="mono text-[10px] tracking-[0.12em] uppercase text-t3">{children}</div>
  );
}

export function NewsTab() {
  // The ONLY wired state: the run-pull action + its in-flight / result status.
  const [running, setRunning] = useState(false);
  const [pulled, setPulled] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const reduceMotion = usePrefersReducedMotion();

  // Presentation-only reveal latch for the "this run produced" rows. The rows are
  // a design-ahead preview (always rendered); `revealed` drives the per-row
  // fade-in transition. It starts true (so the preview is visible on first paint
  // and under reduced-motion), and a successful dispatch briefly flips it off →
  // on so the staggered reveal replays. Never touches data — purely cosmetic.
  const [revealed, setRevealed] = useState(true);
  const replayTimer = useRef<number | null>(null);
  useEffect(() => () => {
    if (replayTimer.current != null) window.clearTimeout(replayTimer.current);
  }, []);

  const replayReveal = () => {
    if (reduceMotion) return; // reduced-motion: rows just stay put, no re-animate
    if (replayTimer.current != null) window.clearTimeout(replayTimer.current);
    setRevealed(false); // collapse to the hidden start state…
    // …then flip on next frame so the CSS transition (with per-row delay) runs.
    replayTimer.current = window.setTimeout(() => setRevealed(true), 30);
  };

  const runPull = async () => {
    if (running) return;
    setRunning(true);
    setError(null);
    try {
      await api.sectorNewsRun();
      setPulled(true);
      replayReveal();
    } catch (e) {
      setError(e instanceof ApiError ? `${e.status}: ${e.message}` : String(e));
    } finally {
      setRunning(false);
    }
  };

  return (
    <div className="flex flex-col gap-[18px] px-[26px] py-[24px]">
      {/* ── Page header ──────────────────────────────────────────────────── */}
      <header className="flex flex-col gap-[7px]">
        <div className="flex items-center gap-[11px]">
          <h2 className="text-[22px] font-semibold leading-[120%] tracking-[-0.01em] text-t1">News</h2>
          <span className="mono inline-flex h-[19px] items-center rounded-[5px] border border-line-2 px-[8px] text-[9px] font-bold leading-3 tracking-[0.1em] text-t3">
            SOON
          </span>
        </div>
        <p className="max-w-full text-[13px] leading-[150%] text-t2">
          Surfaces the wired sector-news workflow — runs your expertise sectors into a
          vault newsletter, Inbox proposals and deal-tracker hits.
        </p>
      </header>

      {/* ── Run-pull action row (the ONLY wired action) ──────────────────── */}
      <div className="flex flex-wrap items-center justify-between gap-x-[14px] gap-y-2">
        <div className="flex flex-wrap items-center gap-x-[13px] gap-y-2">
          <button
            type="button"
            onClick={() => void runPull()}
            disabled={running}
            className={cn(
              "inline-flex h-[36px] items-center gap-[8px] rounded-[9px] border border-accent-line bg-accent-soft px-[16px]",
              "text-[12.5px] font-semibold leading-4 text-t1 transition-colors",
              "enabled:hover:brightness-95 disabled:cursor-default disabled:opacity-60",
            )}
          >
            <RefreshCcw size={14} className={cn("text-t1", running && !reduceMotion && "animate-spin")} />
            {running ? "Running news pull…" : "Run news pull"}
          </button>

          {running ? (
            <StatusBadge status="running" label="pulling sectors" />
          ) : error ? (
            <StatusBadge status="error" label={error} />
          ) : pulled ? (
            <StatusBadge status="ok" label="pull dispatched" />
          ) : (
            <span className="text-[12px] leading-4 text-t3">Last pull: never</span>
          )}
        </div>

        <span className="mono text-[11px] leading-[14px] text-t4">
          4 sectors · 11 sources
        </span>
      </div>

      {/* ── COVERAGE · EXPERTISE SECTORS ─────────────────────────────────── */}
      <section className="flex flex-col gap-[9px]">
        <SectionLabel>COVERAGE · EXPERTISE SECTORS</SectionLabel>
        <div className="flex flex-col gap-[12px] sm:flex-row">
          {SECTORS.map((s) => (
            <div
              key={s.name}
              className="flex grow basis-0 flex-col gap-[6px] rounded-[12px] border border-line bg-bg-1 px-[15px] py-[13px]"
            >
              <div className="flex items-center gap-[7px]">
                <span className="h-[7px] w-[7px] shrink-0 rounded-full bg-green" />
                <span className="text-[13.5px] font-semibold leading-[18px] text-t1">{s.name}</span>
              </div>
              <span className="text-[11px] leading-[14px] text-t3">
                last run {s.lastRun} · <span className="tabular">{s.items}</span> items
              </span>
            </div>
          ))}

          {/* + add sector affordance (illustrative — routes to Operator → Expertise) */}
          <div className="flex grow basis-0 flex-col items-center justify-center gap-[6px] rounded-[12px] border border-dashed border-line-2 bg-bg-1 px-[15px] py-[13px]">
            <span className="inline-flex items-center gap-[6px] text-[12.5px] leading-4 text-t2">
              <Plus size={14} className="text-t3" /> add sector
            </span>
            <span className="text-[10px] leading-3 text-t3">Operator → Expertise</span>
          </div>
        </div>
      </section>

      {/* ── LATEST NEWSLETTER ────────────────────────────────────────────── */}
      <section className="flex flex-col gap-[9px]">
        <div className="flex items-center justify-between">
          <SectionLabel>LATEST NEWSLETTER</SectionLabel>
          <span className="text-[11px] leading-[14px] text-t3">written to the vault · re-runnable</span>
        </div>

        <div className="flex overflow-hidden rounded-[14px] border border-line bg-bg-1">
          <div className="w-[4px] shrink-0 bg-green" />
          <div className="grow basis-0 px-[20px] py-[18px]">
            <div className="flex items-start justify-between gap-[12px]">
              <div className="flex flex-col gap-[3px]">
                <h3 className="text-[15px] font-semibold leading-[18px] text-t1">Logistics &amp; Freight — weekly brief</h3>
                <span className="text-[11.5px] leading-[14px] text-t2">24 Jun 2026 · 09:00 · synthesised from 14 sources</span>
              </div>
              <span className="mono inline-flex shrink-0 items-center rounded-[5px] bg-green/15 px-2 py-0.5 text-[9.5px] leading-3 text-green">
                PUBLIC
              </span>
            </div>

            <ul className="mt-[14px] flex flex-col gap-[7px]">
              {NEWSLETTER_BULLETS.map((b) => (
                <li key={b} className="flex gap-[9px]">
                  <span className="text-[12px] leading-4 text-t3">•</span>
                  <span className="text-[12.5px] leading-[145%] text-t2">{b}</span>
                </li>
              ))}
            </ul>

            <div className="mt-[15px] flex flex-wrap items-center gap-[12px] gap-y-[10px] border-t border-line pt-[13px]">
              <span className="mono text-[10.5px] leading-[14px] text-accent">
                Resources/Newsletters/2026-06-24-logistics.md
              </span>
              <div className="ml-auto flex items-center gap-[10px]">
                {/* metadata caption (not a control — the vault path is shown at left) */}
                <span className="inline-flex items-center gap-[5px] text-[11.5px] leading-4 text-t3">
                  <FolderOpen size={13} className="text-t4" /> in Obsidian vault
                </span>
                <button
                  type="button"
                  onClick={() => void runPull()}
                  disabled={running}
                  className={cn(
                    "inline-flex items-center rounded-lg border border-accent-line bg-accent-soft px-[13px] py-[7px]",
                    "text-[12px] font-semibold leading-4 text-t1 transition-colors",
                    "enabled:hover:brightness-95 disabled:cursor-default disabled:opacity-60",
                  )}
                >
                  {running ? "Running…" : "Re-run pull"}
                </button>
              </div>
            </div>
          </div>
        </div>
      </section>

      {/* ── THIS RUN PRODUCED ────────────────────────────────────────────── */}
      <section className="flex flex-col gap-[9px]">
        <SectionLabel>THIS RUN PRODUCED</SectionLabel>
        <div className="flex flex-col gap-[8px] py-[6px]">
          {PRODUCED.map((row, i) => {
            // Per-row colour register, derived from the routing target (no data
            // change): Inbox → accent, Deals → amber, Vault → green.
            const tone =
              row.target === "Deals"
                ? { count: "text-amber", chip: "bg-amber/15 text-amber" }
                : row.target === "Vault"
                  ? { count: "text-green", chip: "bg-green/15 text-green" }
                  : { count: "text-accent", chip: "bg-accent-soft text-accent" };
            return (
              <div
                key={row.label}
                // Staggered reveal: opacity + a small lift, driven by `revealed`
                // with a per-row transition-delay (CSS transition, no @keyframes).
                // Under reduced-motion the transition is disabled so rows render
                // instantly with no offset.
                style={
                  reduceMotion
                    ? undefined
                    : { transitionDelay: revealed ? `${i * 80}ms` : "0ms" }
                }
                className={cn(
                  "flex items-center gap-[12px] rounded-[11px] border border-line bg-bg-1 px-[15px] py-[11px]",
                  !reduceMotion && "transition-[opacity,transform] duration-[450ms] ease-out",
                  !reduceMotion && (revealed ? "opacity-100 translate-y-0" : "opacity-0 translate-y-[5px]"),
                )}
              >
                <span className={cn("mono w-[26px] shrink-0 text-[13px] leading-4 tabular", tone.count)}>
                  {row.count}×
                </span>
                <span className="flex-1 text-[13px] leading-4 text-t1">{row.label}</span>
                <span
                  className={cn(
                    "mono inline-flex shrink-0 items-center gap-[6px] rounded-[5px] px-[9px] py-[3px] text-[10px] leading-3",
                    tone.chip,
                  )}
                >
                  → {row.target}
                </span>
              </div>
            );
          })}
        </div>
      </section>
    </div>
  );
}
