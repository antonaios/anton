import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ShieldAlert } from "lucide-react";
import { api, ApiError } from "../lib/api";
import { cn } from "../lib/cn";
import { Card } from "./ui/Card";
import type { SensitivityOverride } from "../types";

interface Props {
  /** Notifies App.tsx after the active-override set changes (open count) so a
   *  sibling surface could react. Optional — the panel is otherwise standalone. */
  onCountChange?: (count: number) => void;
}

const POLL_MS = 30_000;
// Ignore focus-triggered refetches that land within this window of the last
// fetch — stops rapid alt-tabbing from hammering the endpoint.
const FOCUS_DEBOUNCE_MS = 5_000;

// Mirror of the backend UNTIL_CLOSED_HARD_CAP_SECONDS (sensitivity_overrides/
// policy.py) — an until-closed window (expires_at=null) auto-drops from "active"
// 24h after opened_at, even with no auto-expiry, as a defense-in-depth backstop
// (#llm-routing-postjune15 P2). We surface that deadline + drive the one-shot
// refetch off it so the window leaves the list promptly when the cap hits.
const UNTIL_CLOSED_HARD_CAP_MS = 24 * 60 * 60 * 1000;

// Client-side re-confirm nudge cadence for until-closed windows. This is a UX
// reminder ONLY — a UI timer can silently fail (see the background-timer-no-wake
// lesson), which is exactly why the server's 24h hard cap above is the real
// safety net, not this. After this long without an operator re-confirm the
// banner escalates to prompt one (or a close).
const RECONFIRM_INTERVAL_MS = 60 * 60 * 1000;   // 1h
const RECONFIRM_LS_PREFIX = "anton:sov-reconfirm:";

// Pretty-name provider keys (mirrors BurnRatePanel). Unknown keys fall through
// to upper-cased verbatim.
const PROVIDER_LABEL: Record<string, string> = {
  anthropic:           "CLAUDE",
  "claude-subprocess": "CLAUDE",
  "claude-api":        "CLAUDE",
  openai:              "OPENAI",
  codex:               "CODEX",
  "codex-subprocess":  "CODEX",
  ollama:              "OLLAMA",
  "ollama-only":       "OLLAMA",
  m27:                 "M2.7",
  minimax:             "M2.7",
};

/**
 * #llm-routing-override · right-rail "Active sensitivity overrides" panel.
 *
 * Each active window relaxes the confidential sensitivity gate for one
 * (skill, workspace, provider) tuple — the bridge's guard ACTUALLY honors these
 * (post round-3/4 wiring), so the timing is consequential, not decorative.
 * Hidden entirely when no window is open.
 *
 * Two window shapes (#llm-routing-postjune15 P2):
 *   - TIMED (expires_at set) — a live mm:ss countdown; the last minute reddens.
 *   - UNTIL-CLOSED (expires_at=null) — a persistent "active until closed" banner
 *     with NO countdown; it surfaces the server's 24h hard-cap drop time + a
 *     client-side periodic re-confirm nudge (UX only — the 24h cap is the
 *     backstop), and reddens once the re-confirm interval lapses.
 *
 * Data lifecycle (self-contained):
 *   - poll GET /api/sensitivity/overrides every 30s + on window focus
 *     (focus debounced; in-flight poll aborted when superseded)
 *   - display ticks client-side each second, anchored to the server `as_of`
 *     clock (skew-corrected) so a wrong client clock can't drift the timing
 *   - a one-shot timer refetches right after the soonest window leaves "active"
 *     (timed: expires_at; until-closed: opened_at + 24h) so a spent override
 *     drops out promptly instead of lingering
 *   - Close → POST .../{id}/close, race-guarded against double-clicks; a 404
 *     (already closed/expired elsewhere) is treated as "already gone".
 *
 * v5 chrome: a self-contained amber-headed alert Card (an open window relaxes
 * the gate, so the whole block reads as a warning), with one inset row per
 * window. Token-only — flips between the light-teal and dark-navy themes.
 */
export function SensitivityOverridesPanel({ onCountChange }: Props) {
  const [overrides, setOverrides] = useState<SensitivityOverride[] | null>(null);
  const [closingIds, setClosingIds] = useState<Set<string>>(new Set());
  const [rowError, setRowError] = useState<string | null>(null);

  // Server-vs-client clock skew (server_ms − client_ms at the last fetch).
  const skewRef = useRef(0);
  // Bumped every second to re-render the countdowns + re-confirm state; the
  // value itself is unused.
  const [, setTick] = useState(0);
  // Bumped when the operator re-confirms an until-closed window, so the memo
  // below re-reads the persisted mark and the nudge resets.
  const [reconfirmVersion, setReconfirmVersion] = useState(0);

  const abortRef       = useRef<AbortController | null>(null);
  const lastFetchAtRef = useRef(0);
  const closingRef     = useRef<Set<string>>(new Set());

  const load = useCallback(async () => {
    // Supersede any in-flight poll so a focus event mid-poll doesn't double up.
    abortRef.current?.abort();
    const ac = new AbortController();
    abortRef.current = ac;
    lastFetchAtRef.current = Date.now();
    try {
      // Bracket the request so the skew anchors to the round-trip MIDPOINT, not
      // to whenever the awaited callback happens to run — otherwise response /
      // callback latency makes the server `as_of` look like "now" and the
      // countdown lags (an expired window lingers at 00:00). On loopback this is
      // sub-ms, but the midpoint estimate stays correct under load too.
      const t0 = Date.now();
      const r = await api.sensitivityOverrides(ac.signal);
      const t1 = Date.now();
      const serverMs = Date.parse(r.asOf);
      if (Number.isFinite(serverMs)) skewRef.current = serverMs - (t0 + (t1 - t0) / 2);
      setOverrides(r.overrides);
      onCountChange?.(r.overrides.length);
    } catch (e) {
      // Aborted (superseded by a newer poll) — not an error.
      if (e instanceof DOMException && e.name === "AbortError") return;
      // Keep the last-known list on a transient failure so the panel doesn't
      // flicker off mid-window (mirrors the budget-incidents discipline).
      // eslint-disable-next-line no-console
      console.warn("sensitivity overrides fetch failed", e);
    }
  }, [onCountChange]);

  // Poll: initial + every 30s. Abort the in-flight poll on unmount.
  useEffect(() => {
    void load();
    const id = window.setInterval(() => void load(), POLL_MS);
    return () => { window.clearInterval(id); abortRef.current?.abort(); };
  }, [load]);

  // Refresh on window focus (debounced against the last fetch).
  useEffect(() => {
    const onFocus = () => {
      if (Date.now() - lastFetchAtRef.current >= FOCUS_DEBOUNCE_MS) void load();
    };
    window.addEventListener("focus", onFocus);
    return () => window.removeEventListener("focus", onFocus);
  }, [load]);

  const hasRows = !!overrides && overrides.length > 0;

  // 1s display tick — runs while any window is open (timed countdowns AND the
  // until-closed re-confirm escalation both need it).
  useEffect(() => {
    if (!hasRows) return;
    const id = window.setInterval(() => setTick((t) => (t + 1) % 86_400), 1000);
    return () => window.clearInterval(id);
  }, [hasRows]);

  // One-shot refetch just after the soonest window leaves "active" (server drops
  // it), so it disappears promptly. Timed → expires_at; until-closed → its 24h
  // hard cap (opened_at + 24h), so a missed re-confirm still clears on schedule.
  useEffect(() => {
    if (!overrides || overrides.length === 0) return;
    const effNow = Date.now() + skewRef.current;
    const soonest = Math.min(
      ...overrides.map((o) => effectiveExpiryMs(o) - effNow),
    );
    if (!Number.isFinite(soonest)) return;
    const id = window.setTimeout(() => void load(), Math.max(0, soonest) + 750);
    return () => window.clearTimeout(id);
  }, [overrides, load]);

  // Prune re-confirm marks for windows no longer active (closed/expired) so the
  // localStorage keys can't accumulate. Active windows are always in the list;
  // a transient fetch failure keeps the last-known list, so this never drops a
  // still-live window's mark.
  useEffect(() => {
    if (!overrides) return;
    try {
      const activeIds = new Set(overrides.map((o) => o.id));
      for (let i = window.localStorage.length - 1; i >= 0; i--) {
        const k = window.localStorage.key(i);
        if (k && k.startsWith(RECONFIRM_LS_PREFIX)
            && !activeIds.has(k.slice(RECONFIRM_LS_PREFIX.length))) {
          window.localStorage.removeItem(k);
        }
      }
    } catch { /* localStorage unavailable — ignore */ }
  }, [overrides]);

  // Per-(until-closed)-window last-confirmed timestamp (ms). Seeded from the
  // persisted mark, else opened_at (opening IS the first confirmation).
  // Recomputed when the list changes or the operator re-confirms.
  const reconfirmedAt = useMemo(() => {
    const m: Record<string, number> = {};
    const nowApprox = Date.now() + skewRef.current;   // server-anchored, like effNow
    for (const o of overrides ?? []) {
      if (o.expiresAt == null) {
        const openedMs = Date.parse(o.openedAt);
        const base = readReconfirmedAt(o.id) ?? openedMs;
        // Clamp to [openedAt, now]: a hand-edited / clock-skewed persisted value
        // can't push the nudge infinitely out or escalate it before the window
        // even opened (Codex SEV-2).
        m[o.id] = Math.min(Math.max(base, openedMs), nowApprox);
      }
    }
    return m;
    // reconfirmVersion intentionally a dep so a re-confirm re-reads the mark.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [overrides, reconfirmVersion]);

  const reconfirm = useCallback((id: string) => {
    // Anchor to server time (skew-corrected), like every other timestamp in this
    // panel, so a wrong client clock can't make a fresh re-confirm read as
    // already-stale or suppress the nudge for hours (Codex SEV-2).
    writeReconfirmedAt(id, Date.now() + skewRef.current);
    setReconfirmVersion((v) => v + 1);
  }, []);

  const close = async (id: string) => {
    // Race guard: ignore a second click while the first close is in flight.
    if (closingRef.current.has(id)) return;
    closingRef.current.add(id);
    setClosingIds(new Set(closingRef.current));
    setRowError(null);
    try {
      await api.closeSensitivityOverride(id);
      await load();                       // closed window drops out of the list
    } catch (e) {
      if (e instanceof ApiError && e.status === 404) {
        await load();                     // already gone elsewhere — just refresh
      } else {
        setRowError(e instanceof ApiError ? `Close failed: ${e.message}` : "Close failed.");
      }
    } finally {
      closingRef.current.delete(id);
      setClosingIds(new Set(closingRef.current));
    }
  };

  // Empty / first-load / persistent-error-with-no-data → render nothing.
  if (!hasRows) return null;

  const effNow = Date.now() + skewRef.current;

  return (
    <Card className="border-amber/40">
      {/* Section head — amber, since an open window relaxes the gate. */}
      <div className="mb-[12px] flex items-center justify-between">
        <span className="flex items-center gap-[7px] text-[10px] font-semibold uppercase tracking-[0.14em] text-amber">
          <ShieldAlert size={13} className="shrink-0" />
          Active sensitivity overrides
        </span>
        <span className="tabular tabular-nums text-[10px] font-semibold uppercase tracking-[0.12em] text-amber">
          {overrides!.length} open
        </span>
      </div>

      <div className="flex flex-col gap-[10px]">
        {overrides!.map((o) => (
          <OverrideRow
            key={o.id}
            ov={o}
            nowMs={effNow}
            reconfirmedAt={reconfirmedAt[o.id]}
            closing={closingIds.has(o.id)}
            onClose={() => void close(o.id)}
            onReconfirm={() => reconfirm(o.id)}
          />
        ))}
      </div>

      {rowError && (
        <div className="mt-[10px] rounded-lg border border-red/40 bg-red/10 px-[10px] py-[6px] text-[11px] text-red">
          {rowError}
        </div>
      )}
    </Card>
  );
}

function OverrideRow({
  ov, nowMs, reconfirmedAt, closing, onClose, onReconfirm,
}: {
  ov: SensitivityOverride;
  nowMs: number;
  /** Last-confirmed ms for an until-closed window (undefined for timed ones). */
  reconfirmedAt?: number;
  closing: boolean;
  onClose: () => void;
  onReconfirm: () => void;
}) {
  const provider = PROVIDER_LABEL[ov.provider] ?? ov.provider.toUpperCase();
  const { wsType, wsName } = splitWorkspace(ov.workspace);
  const untilClosed = ov.expiresAt == null;

  // Timed window: mm:ss countdown to expires_at (last minute emphasised).
  const remainingMs = untilClosed ? 0 : Date.parse(ov.expiresAt as string) - nowMs;
  const expiring = !untilClosed && remainingMs <= 60_000;

  // Until-closed window: no countdown. Surface the server 24h hard-cap drop time
  // + a client-side re-confirm nudge (UX only; the cap is the real backstop).
  const openedMs = Date.parse(ov.openedAt);
  const capRemainingMs = openedMs + UNTIL_CLOSED_HARD_CAP_MS - nowMs;
  const sinceConfirmMs = nowMs - (reconfirmedAt ?? openedMs);
  const needsReconfirm = untilClosed && sinceConfirmMs >= RECONFIRM_INTERVAL_MS;

  const closeButton = (
    <button
      type="button"
      onClick={onClose}
      disabled={closing}
      className="rounded-md border border-amber/50 px-[10px] py-[3px] text-[10px] font-medium uppercase tracking-[0.1em] text-amber transition-colors hover:bg-amber hover:text-bg disabled:cursor-default disabled:opacity-40 disabled:hover:bg-transparent disabled:hover:text-amber"
    >
      {closing ? "Closing…" : "Close"}
    </button>
  );

  return (
    <div className={cn(
      "rounded-lg border px-[12px] py-[10px]",
      needsReconfirm ? "border-red/50 bg-red/10" : "border-amber/30 bg-amber/[0.07]",
    )}>
      {/* skill · provider · ceiling */}
      <div className="mb-[5px] flex items-baseline justify-between gap-[8px]">
        <span className="truncate text-[11.5px] text-t1">
          <strong className="font-medium">{ov.skill}</strong>
          <span className="text-t3"> · </span>
          <span className="text-t2">{provider}</span>
        </span>
        <span
          className="shrink-0 whitespace-nowrap rounded-md border border-amber/40 bg-amber/15 px-[6px] py-[1px] text-[9.5px] uppercase tracking-[0.1em] text-amber"
          title={`Override ceiling: ${ov.ceiling}`}
        >
          {ov.ceiling}
        </span>
      </div>

      {/* workspace */}
      <div className="mb-[6px] truncate text-[10.5px] text-t3" title={ov.workspace}>
        {wsType && <span className="text-t2">{wsType}</span>}
        {wsType ? " · " : ""}{wsName}
      </div>

      {untilClosed ? (
        <>
          {/* status (no countdown) + close */}
          <div className="mb-[6px] flex items-center justify-between">
            <span
              className={cn(
                "inline-flex items-center gap-[6px] text-[10.5px] uppercase tracking-[0.06em]",
                needsReconfirm ? "text-red" : "text-amber",
              )}
              title={`Opened ${new Date(ov.openedAt).toLocaleString()} · auto-closes (24h safety cap) ${new Date(openedMs + UNTIL_CLOSED_HARD_CAP_MS).toLocaleString()}`}
            >
              <span className={cn("h-[5px] w-[5px] shrink-0 rounded-full", needsReconfirm ? "bg-red" : "bg-amber")} />
              <span className="font-medium">Active until closed</span>
            </span>
            {closeButton}
          </div>

          {/* 24h hard-cap surfacing + the periodic re-confirm affordance */}
          <div className="flex items-center justify-between">
            <span
              className="tabular text-[9.5px] tracking-[0.04em] text-t4"
              title="Defense-in-depth: an until-closed window auto-drops 24h after opening, even if this re-confirm is missed."
            >
              safety cap in {fmtCoarse(capRemainingMs)}
            </span>
            <button
              type="button"
              onClick={onReconfirm}
              title="Re-confirm this window is still needed (resets the periodic reminder). The 24h server cap still applies."
              className={cn(
                "rounded-md border px-[10px] py-[3px] text-[10px] font-medium uppercase tracking-[0.1em] transition-colors",
                needsReconfirm
                  ? "border-red/55 text-red hover:bg-red hover:text-bg"
                  : "border-line text-t3 hover:border-accent-line hover:text-accent",
              )}
            >
              {needsReconfirm ? "Re-confirm" : "Keep open"}
            </button>
          </div>

          {needsReconfirm && (
            <div className="mt-[6px] text-[10px] leading-relaxed text-red">
              Open {fmtCoarse(sinceConfirmMs)} without re-confirmation — re-confirm if still needed, or close it.
            </div>
          )}
        </>
      ) : (
        /* timed window: live mm:ss countdown + close */
        <div className="flex items-center justify-between">
          <span
            className={cn(
              "tabular tabular-nums text-[12px] tracking-[0.04em]",
              expiring ? "font-medium text-red" : "text-amber",
            )}
            title={`Expires ${new Date(ov.expiresAt as string).toLocaleTimeString()}`}
          >
            {fmtCountdown(remainingMs)} <span className="text-[9.5px] tracking-[0.1em] text-t3">LEFT</span>
          </span>
          {closeButton}
        </div>
      )}

      {/* justification — truncated with full text on hover */}
      <div
        className="mt-[6px] truncate text-[10.5px] italic text-t3"
        title={ov.justification}
      >
        “{truncate(ov.justification, 60)}”
      </div>
    </div>
  );
}

// ── Helpers ──────────────────────────────────────────────────────────────────

/** When a window drops from "active": expires_at for a timed window, or
 *  opened_at + 24h (the server hard cap) for an until-closed one. */
function effectiveExpiryMs(o: SensitivityOverride): number {
  if (o.expiresAt != null) return Date.parse(o.expiresAt);
  return Date.parse(o.openedAt) + UNTIL_CLOSED_HARD_CAP_MS;
}

/** mm:ss for a millisecond remainder. Timed windows max out at 1h so two digits
 *  of minutes always suffice; never goes negative. */
function fmtCountdown(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000));
  const m = Math.floor(total / 60);
  const s = total % 60;
  return `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}

/** Coarse "Xh Ym" / "Ym" for a ms remainder — used for the until-closed safety-
 *  cap hint + the since-confirmed nudge. Deliberately NOT the mm:ss live
 *  countdown timed windows use: an until-closed window is open-ended, so this
 *  conveys only the coarse 24h backstop, not a ticking expiry. */
function fmtCoarse(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m`;
  return "<1m";
}

function truncate(s: string, n: number): string {
  return s.length > n ? `${s.slice(0, n - 1)}…` : s;
}

/** Split the bridge's "type:name" workspace identity into a tag + name. Falls
 *  back to the whole string when there's no colon. */
function splitWorkspace(ws: string): { wsType: string; wsName: string } {
  const i = ws.indexOf(":");
  if (i < 0) return { wsType: "", wsName: ws };
  return { wsType: ws.slice(0, i), wsName: ws.slice(i + 1) };
}

/** Read the persisted last-confirmed ms for an until-closed window. null when
 *  unset / unparseable / localStorage unavailable. */
function readReconfirmedAt(id: string): number | null {
  try {
    const v = window.localStorage.getItem(RECONFIRM_LS_PREFIX + id);
    if (!v) return null;
    const n = Number(v);
    return Number.isFinite(n) ? n : null;
  } catch {
    return null;
  }
}

function writeReconfirmedAt(id: string, ms: number): void {
  try {
    window.localStorage.setItem(RECONFIRM_LS_PREFIX + id, String(ms));
  } catch {
    /* localStorage unavailable — the 24h server cap is the backstop anyway */
  }
}
