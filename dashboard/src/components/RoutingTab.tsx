import { Fragment, useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "../lib/api";
import { cn } from "../lib/cn";
import { useMediaQuery } from "../lib/useMediaQuery";
import type {
  CloudLane,
  LaneInfo,
  LaneMatrixResponse,
  LaneStatusResponse,
  PlanTierState,
} from "../types";

/** True when the OS "reduce motion" preference is set — gates the hover-trace
 *  rung pulse down to a static highlight. Mirrors ChatCanvas.usePrefersReducedMotion. */
function usePrefersReducedMotion(): boolean {
  return useMediaQuery("(prefers-reduced-motion: reduce)");
}

/**
 * Hover-trace — a cloud lane key (claude | codex) the operator is currently
 * tracing, derived purely off the EXISTING laneMatrix reads (nothing fetched).
 * A lane-matrix cell maps to a cloud ladder by its provider; local (Ollama) and
 * minimax cells trace nothing (no cloud ladder), so hovering them clears the
 * trace. This is the one new piece of client state on the page.
 */
type TracedLane = "claude" | "codex" | null;

/** The cloud ladder a matrix cell routes to — or null when it has none (local
 *  floor / minimax-unwired). Drives both the cell tint and the ladder spotlight. */
function cellLadder(info: LaneInfo | undefined): TracedLane {
  if (!info || !isCloudCell(info)) return null;
  if (info.provider === "claude") return "claude";
  if (info.provider === "codex") return "codex";
  return null;
}

/**
 * #llm-routing-postjune15 — the DEDICATED "Routing" page (left-nav SETTINGS · Routing
 * leaf → its own tab body, split out of the Providers tab). Pixel-matches Paper
 * artboard 8FD-0 "Settings — Routing": a read-only surface of the routing the
 * dispatcher already does — consuming it changes nothing.
 *
 *   · Lane matrix      — (task type × sensitivity) → lane grid (GET /api/routing/lane-matrix, G4)
 *   · Cloud ladders    — per-lane cloud-dispatch fallback rungs   (GET /api/routing/lane-status, G1)
 *   · Provider ceilings — per-CLOUD-provider sensitivity ceiling   (GET /api/skills/providers, G5)
 *
 * Self-fetches the three reads (focus + 60s, allSettled so one missing endpoint
 * doesn't blank the others), mirroring RoutingPosturePanel's fetch/loading/error
 * handling. Renders INSIDE the off-white rounded shell card (bg-bg-2 shadow-card) —
 * no outer page card. The actionable side (overrides / crew promotion / MNPI
 * attestations) lives on the Providers tab.
 */

const POLL_MS = 60_000;
const FOCUS_DEBOUNCE_MS = 10_000;

// ── Lane-matrix cell ────────────────────────────────────────────────────────
// Cloud lanes read in the foreground (accent-soft tint + accent text); the local
// floor reads muted; minimax is wired into the grid but UNWIRED by any skill →
// dim + asterisk. Mirrors RoutingPosturePanel.laneCellCls so the two surfaces
// read as one design system AND the confidential/MNPI local-only floor stays
// legible (a local cell never gets the cloud tint).
function isCloudCell(info: LaneInfo): boolean {
  return !info.local && info.provider !== "minimax";
}

function MatrixCell({
  info,
  traced,
  onTrace,
}: {
  info: LaneInfo | undefined;
  traced: TracedLane;
  onTrace: (lane: TracedLane) => void;
}) {
  if (!info) {
    // Empty cell — no lane to trace, so hovering it clears any active trace.
    return (
      <div
        className="grow basis-0 py-[9px] px-[12px] border-l border-line"
        onMouseEnter={() => onTrace(null)}
      >
        <span className="mono block w-max text-[10.5px] leading-[14px] text-t4">—</span>
      </div>
    );
  }
  const cloud = isCloudCell(info);
  const minimax = info.provider === "minimax";
  const ladder = cellLadder(info);            // the cloud lane this cell routes to (or null)
  const lit = !!traced && ladder === traced;  // this cell shares the traced lane → tint it
  return (
    <div
      className={cn(
        "grow basis-0 py-[9px] px-[12px] border-l border-line transition-colors duration-150",
        // Lit (traced) cloud cells deepen accent-soft → accent-line + an inset
        // ring; otherwise the base cloud/minimax tint. One bg class wins → no
        // class-order ambiguity.
        lit
          ? "bg-accent-line ring-1 ring-inset ring-accent-line"
          : cloud || minimax
            ? "bg-accent-soft"
            : "",
      )}
      // Hovering any cell sets the trace to its cloud ladder (null for local /
      // minimax — they have no cloud fallback to trace).
      onMouseEnter={() => onTrace(ladder)}
      title={`${info.provider} · ${info.model}${info.local ? " · local" : " · cloud"}`}
    >
      <span
        className={cn(
          "mono block w-max text-[10.5px] leading-[14px]",
          cloud ? "text-accent" : "text-t3",
        )}
      >
        {info.lane}{minimax ? " *" : ""}
      </span>
    </div>
  );
}

// Sensitivity header dot colour — public/internal/confidential/MNPI register.
const SENS_DOT: Record<string, string> = {
  public:       "bg-green",
  internal:     "bg-t2",
  confidential: "bg-amber",
  MNPI:         "bg-red",
};

// Display label per sensitivity key — abbreviated to fit the narrow column
// (Paper 9C4-0 reads "CONFID." not the full "CONFIDENTIAL"). The key `s` still
// drives the data lookup + dot colour; only the rendered text is abbreviated.
const SENS_LABEL: Record<string, string> = {
  confidential: "CONFID.",
};

function LaneMatrix({
  m,
  traced,
  onTrace,
}: {
  m: LaneMatrixResponse;
  traced: TracedLane;
  onTrace: (lane: TracedLane) => void;
}) {
  return (
    <div className="flex flex-col rounded-[14px] overflow-clip shadow-card bg-bg-1 border border-line">
      {/* Header row — TASK TYPE + one column per sensitivity */}
      <div className="flex bg-paper2 border-b border-line">
        <div className="w-[188px] shrink-0 py-[10px] px-[14px]">
          <span className="block text-[10px] tracking-[0.08em] leading-[13px] font-semibold text-t1">
            TASK TYPE
          </span>
        </div>
        {m.sensitivities.map((s) => (
          <div key={s} className="grow basis-0 flex items-center py-[10px] px-[12px] gap-[6px] border-l border-line">
            <div className={cn("rounded-[2px] shrink-0 size-[6px]", SENS_DOT[s] ?? "bg-t3")} />
            <span className="block text-[10px] tracking-[0.06em] leading-[13px] font-semibold uppercase text-t1">
              {SENS_LABEL[s] ?? s}
            </span>
          </div>
        ))}
      </div>

      {/* Body — one row per task type */}
      {m.matrix.map((row, i) => (
        <div
          key={row.taskType}
          className={cn("flex", i < m.matrix.length - 1 ? "border-b border-line" : "")}
        >
          <div className="w-[188px] shrink-0 flex items-center py-[9px] px-[14px]">
            <span
              className={cn(
                "mono block w-max shrink-0 text-[11px] leading-[14px]",
                row.taskType === "synthesis" ? "text-accent font-medium" : "text-t1",
              )}
            >
              {row.taskType}
            </span>
          </div>
          {m.sensitivities.map((s) => (
            <MatrixCell key={s} info={row.cells[s]} traced={traced} onTrace={onTrace} />
          ))}
        </div>
      ))}
    </div>
  );
}

// ── Lane-matrix legend ──────────────────────────────────────────────────────

function MatrixLegend({ tier }: { tier: string }) {
  return (
    <div className="flex items-center justify-between flex-wrap pt-[2px] gap-[16px]">
      <div className="flex items-center gap-[16px]">
        <div className="flex items-center gap-[6px]">
          <div className="w-[9px] h-[9px] rounded-[3px] shrink-0 bg-accent-soft border border-accent-line" />
          <span className="text-[11px] leading-[14px] text-t2">cloud lane</span>
        </div>
        <div className="flex items-center gap-[6px]">
          <div className="w-[9px] h-[9px] rounded-[3px] shrink-0 bg-paper2 border border-line-2" />
          <span className="text-[11px] leading-[14px] text-t2">local (Ollama)</span>
        </div>
        <span className="text-[11px] leading-[14px] text-t3">* minimax — routed but unwired</span>
      </div>
      <span className="text-[10.5px] leading-[15px] max-w-[440px] text-right text-t4">
        {tier === "enterprise"
          ? "Enterprise lifts Confidential → Claude (never Codex); MNPI lifts only with an explicit flag + active attestation."
          : "Under Bridge, Public = Internal and Confidential = MNPI. Enterprise lifts Confidential → Claude; MNPI lifts only with an explicit flag + active attestation."}
      </span>
    </div>
  );
}

// ── Tier summary card ───────────────────────────────────────────────────────
// "Bridge tier · live" — the lane-availability glance, derived from the live
// lane-status rungs (top rung = the lane's headline state).

function laneHeadline(lane: CloudLane | undefined): { dot: string; cls: string; label: string } {
  const top = lane?.rungs[0];
  if (top && top.state === "available") return { dot: "bg-green", cls: "text-t2", label: "available" };
  if (top && (top.state === "armed" || top.state === "floor")) return { dot: "bg-amber", cls: "text-t2", label: top.state };
  return { dot: "bg-t3", cls: "text-t3", label: top?.state ?? "—" };
}

function TierSummary({ status, tier }: { status: LaneStatusResponse | null; tier?: string }) {
  const claude = laneHeadline(status?.claude);
  const codex = laneHeadline(status?.codex);
  const label = tier === "enterprise" ? "Enterprise tier · live" : "Bridge tier · live";
  return (
    <div className="flex flex-col rounded-[13px] py-[15px] px-[18px] gap-[9px] bg-bg-1 border border-line">
      <div className="flex items-center gap-[10px]">
        <div className="shrink-0 rounded-full bg-green size-[8px] animate-pulse" />
        <span className="text-[13.5px] font-semibold leading-[18px] text-t1">{label}</span>
        <div className="ml-auto flex items-center gap-[12px]">
          {status && (
            <>
              <span className={cn("mono w-max shrink-0 text-[10.5px] leading-[14px]", claude.cls)}>
                <span className={cn("inline-block rounded-full size-[6px] mr-[5px] align-middle", claude.dot)} />
                claude · {claude.label}
              </span>
              <span className={cn("mono w-max shrink-0 text-[10.5px] leading-[14px]", codex.cls)}>
                <span className={cn("inline-block rounded-full size-[6px] mr-[5px] align-middle", codex.dot)} />
                codex · {codex.label}
              </span>
            </>
          )}
          <span className="mono w-max shrink-0 text-[10.5px] leading-[14px] text-t3">
            <span className="inline-block rounded-full size-[6px] mr-[5px] align-middle bg-t4" />
            minimax · unwired
          </span>
        </div>
      </div>
      <p className="text-[12px] leading-[150%] text-t2">
        Under Bridge, public &amp; internal work routes to cloud; confidential &amp; MNPI stay local.
        Enterprise lifts confidential to the Claude lane only (never Codex); MNPI lifts only with an
        explicit flag + an active attestation. The grid below is read-only — it mirrors the
        dispatcher&apos;s pick_lane.
      </p>
    </div>
  );
}

// ── Cloud fallback ladders ──────────────────────────────────────────────────
// Each rung shows its CONFIGURED state (resolved at bridge startup — not a live
// probe). Reuses the RoutingPosturePanel rung vocabulary.

const RUNG_STATE_DOT: Record<string, { dot: string; cls: string; label: string }> = {
  available:    { dot: "bg-green", cls: "text-t2", label: "available" },
  armed:        { dot: "bg-green", cls: "text-t2", label: "armed" },
  floor:        { dot: "bg-t3",    cls: "text-t3", label: "floor" },
  absent:       { dot: "bg-t4",    cls: "text-t3", label: "absent" },
  unavailable:  { dot: "bg-t4",    cls: "text-t3", label: "unavailable" },
  "not-wired":  { dot: "bg-t4",    cls: "text-t4", label: "not wired" },
};

const RUNG_TITLE: Record<string, string> = {
  "oauth-subprocess": "OAuth subprocess",
  "anthropic-api":    "API key",
  "ollama-degrade":   "Ollama floor",
  "openai-api":       "OpenAI API",
};

/** The "live" rung the lane would dispatch to right now — the topmost rung in a
 *  reachable state (available | armed). That rung pulses under an active trace
 *  (matches the mockup's `live` flag, derived here from lane-status not hardcoded).
 *  Returns the rung index, or -1 when no rung is reachable. */
function liveRungIndex(lane: CloudLane): number {
  return lane.rungs.findIndex((r) => r.state === "available" || r.state === "armed");
}

function LadderCard({
  lane,
  isTraced,
  dimmed,
  reduceMotion,
}: {
  lane: CloudLane;
  isTraced: boolean;
  dimmed: boolean;
  reduceMotion: boolean;
}) {
  const liveIdx = liveRungIndex(lane);
  return (
    <div
      className={cn(
        "grow basis-0 flex flex-col rounded-[12px] overflow-clip shadow-card bg-bg-1 border transition-all duration-200",
        // Trace spotlight: the matching ladder lifts (accent ring + border);
        // the other ladder dims so the eye lands on the traced one.
        isTraced ? "border-accent-line ring-1 ring-accent-line" : "border-line",
        dimmed ? "opacity-40" : "opacity-100",
      )}
    >
      <div className="flex items-center justify-between py-[12px] px-[16px] gap-[10px] border-b border-line">
        <span className="text-[13.5px] leading-[18px] font-semibold text-t1 capitalize">{lane.lane}</span>
        <span className="mono text-[10px] leading-[14px] text-t3">{lane.purpose}</span>
      </div>
      <div className="flex flex-col pt-[2px] pb-[8px] px-[16px]">
        {lane.rungs.map((r, i) => {
          const s = RUNG_STATE_DOT[r.state] ?? { dot: "bg-t4", cls: "text-t3", label: r.state };
          // Pulse only the live rung of the traced ladder, and only when motion
          // is allowed — reduced-motion keeps the static highlight, drops the pulse.
          const pulse = isTraced && i === liveIdx && !reduceMotion;
          return (
            <div
              key={r.rung}
              className={cn("flex items-center py-[11px] gap-[11px]", i < lane.rungs.length - 1 ? "border-b border-line" : "")}
            >
              <span className="mono w-[12px] shrink-0 text-[11px] leading-[14px] text-t4">{i + 1}</span>
              <div className="grow min-w-0 flex flex-col gap-[2px]">
                <span className={cn("text-[12px] leading-[15px]", s.label === "not wired" || s.label === "absent" ? "text-t3" : "text-t1")}>
                  {RUNG_TITLE[r.rung] ?? r.rung}
                </span>
                <span className="mono text-[9.5px] leading-[13px] text-t3">{r.detail}</span>
              </div>
              <div className="flex items-center shrink-0 gap-[5px]" title={r.transport}>
                <div className={cn("rounded-[3px] shrink-0 size-[6px]", s.dot, pulse ? "animate-pulse" : "")} />
                <span className={cn("text-[10.5px] leading-[14px]", s.cls)}>{s.label}</span>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function Ladders({
  status,
  traced,
  reduceMotion,
}: {
  status: LaneStatusResponse;
  traced: TracedLane;
  reduceMotion: boolean;
}) {
  return (
    <div className="flex flex-col gap-[11px]">
      <div className="flex items-center pt-[4px] gap-[12px]">
        <span className="w-max shrink-0 text-[11px] tracking-[0.1em] leading-[14px] font-semibold text-t2">
          CLOUD FALLBACK LADDERS
        </span>
        <div className="grow h-px bg-line" />
        <span className="mono w-max shrink-0 text-[10px] leading-[14px] text-t4">
          {traced ? `tracing → ${traced} lane` : "tried top → bottom"}
        </span>
      </div>
      <div className="flex gap-[14px]">
        <LadderCard
          lane={status.claude}
          isTraced={traced === "claude"}
          dimmed={!!traced && traced !== "claude"}
          reduceMotion={reduceMotion}
        />
        <LadderCard
          lane={status.codex}
          isTraced={traced === "codex"}
          dimmed={!!traced && traced !== "codex"}
          reduceMotion={reduceMotion}
        />
      </div>
    </div>
  );
}

// ── Agent-SDK credit + config caveat strip ──────────────────────────────────

function CreditCaveat({ status }: { status: LaneStatusResponse }) {
  const sdk = status.sdkCredit;
  return (
    <div className="flex items-center rounded-[10px] py-[11px] px-[15px] gap-[14px] bg-paper2">
      {sdk && (
        <div className="flex items-center shrink-0 gap-[7px]" title={`Agent-SDK credit (${sdk.env}) — ${sdk.detail}`}>
          <div className="rounded-[3px] shrink-0 bg-amber size-[6px]" />
          <span className="w-max shrink-0 text-[11.5px] leading-[14px] text-t2">Agent-SDK credit</span>
          <span className="flex items-center h-[18px] rounded-[5px] px-[7px] border border-line-2">
            <span className="mono w-max shrink-0 text-[9.5px] leading-[12px] text-t3">{sdk.state}</span>
          </span>
        </div>
      )}
      <div className="w-px h-[16px] shrink-0 bg-line-2" />
      <p className="text-[11.5px] leading-[150%] text-t3">{status.note}</p>
    </div>
  );
}

// ── Provider ceilings ───────────────────────────────────────────────────────
// Per-CLOUD-provider sensitivity ceiling (providers.<name>.max_sensitivity).
// null = UNCONFIGURED (no per-provider cap; the §4 matrix governs). Mirrors
// RoutingPosturePanel.CeilingsRow semantics — a deny-wins cap (even over a P5
// attestation).

function ProviderCeilings({ ceilings }: { ceilings: Record<string, string | null> }) {
  const entries = Object.entries(ceilings);
  if (!entries.length) return null;
  return (
    <div className="flex flex-col gap-[11px]">
      <div className="flex items-center pt-[4px] gap-[12px]">
        <span className="w-max shrink-0 text-[11px] tracking-[0.1em] leading-[14px] font-semibold text-t2">
          PROVIDER CEILINGS
        </span>
        <div className="grow h-px bg-line" />
        <span className="mono w-max shrink-0 text-[10px] leading-[14px] text-t4">profile.md · max_sensitivity</span>
      </div>
      <div className="flex items-center flex-wrap gap-[10px]">
        {entries.map(([prov, ceil]) => (
          <div
            key={prov}
            className="flex items-center h-[34px] rounded-[8px] px-[14px] gap-[9px] bg-bg-1 border border-line"
            title={
              ceil == null
                ? "Unconfigured — no per-provider cap; the §4 sensitivity matrix governs (= internal in bridge tier). Rises per-provider via providers.<name>.max_sensitivity when Enterprise/ZDR lands."
                : `Operator-capped at ${ceil} via providers.${prov}.max_sensitivity (deny-wins, even over a P5 attestation).`
            }
          >
            <span className="text-[12.5px] leading-[16px] font-medium text-t1">{prov}</span>
            {ceil == null ? (
              <span className="mono text-[10.5px] leading-[14px] text-t4">unconfigured</span>
            ) : (
              <span className="mono text-[10.5px] leading-[14px] text-accent">{ceil}</span>
            )}
          </div>
        ))}
        <span className="text-[10.5px] leading-[150%] grow basis-0 min-w-[200px] text-t4">
          A per-provider cap tightens routing independently — deny-wins even over an active MNPI attestation.
        </span>
      </div>
    </div>
  );
}

// ── Tier-flip confirm modal (#plan-tier-toggle) ─────────────────────────────
// A confidentiality-boundary action, so it's gated by an explicit dialog: the
// lift to Enterprise requires an acknowledgement that confidential material
// will route to cloud; both directions require an operator identity (audited).

function TierFlipModal({
  target,
  current,
  onConfirm,
  onCancel,
  busy,
  error,
}: {
  target: "bridge" | "enterprise";
  current?: string;
  onConfirm: (setBy: string, ack: boolean) => void;
  onCancel: () => void;
  busy: boolean;
  error: string | null;
}) {
  const toEnterprise = target === "enterprise";
  const [setBy, setSetBy] = useState("");
  const [ack, setAck] = useState(false);
  const canConfirm = setBy.trim().length > 0 && (!toEnterprise || ack) && !busy;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 px-[20px]"
      onClick={busy ? undefined : onCancel}
    >
      <div
        className="w-full max-w-[520px] flex flex-col gap-[16px] rounded-[16px] border border-line bg-bg-2 p-[24px] shadow-modal"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex flex-col gap-[6px]">
          <span className="mono text-[10.5px] uppercase tracking-[0.13em] text-t3">
            Plan tier · {current ?? "—"} → {target}
          </span>
          <h3 className="text-[16px] font-semibold leading-[120%] text-t1">
            {toEnterprise ? "Switch to Enterprise tier?" : "Switch to Bridge tier?"}
          </h3>
        </div>

        <p className="text-[13px] leading-[1.6] text-t2">
          {toEnterprise
            ? "Enterprise routes CONFIDENTIAL material — deal chats, confidential skills and crews — to cloud Claude. MNPI stays local (it needs a separate attestation), and the local-only task types and cron are unaffected. Only switch if you hold the Anthropic Enterprise + ZDR basis."
            : "Bridge returns confidential material to the local model — confidential chats and skills route to Ollama again. This is the more restrictive posture and takes effect immediately."}
        </p>

        {toEnterprise && (
          <label className="flex cursor-pointer items-start gap-[9px]">
            <input
              type="checkbox"
              checked={ack}
              onChange={(e) => setAck(e.target.checked)}
              className="mt-[3px] accent-accent"
            />
            <span className="text-[12.5px] leading-[1.5] text-t2">
              I acknowledge that confidential material will route to cloud Claude.
            </span>
          </label>
        )}

        <label className="flex flex-col gap-[5px]">
          <span className="text-[10.5px] uppercase tracking-[0.1em] text-t3">Switched by</span>
          <input
            value={setBy}
            onChange={(e) => setSetBy(e.target.value)}
            placeholder="your name / initials"
            autoFocus
            className="h-[34px] rounded-[8px] border border-line bg-bg-1 px-[11px] text-[13px] text-t1 outline-none focus:border-accent-line"
          />
        </label>

        {error && <div className="text-[12px] text-red">{error}</div>}

        <div className="flex items-center justify-end gap-[10px] pt-[2px]">
          <button
            type="button"
            onClick={onCancel}
            disabled={busy}
            className="h-[34px] rounded-[8px] px-[16px] text-[12.5px] text-t2 transition-colors hover:text-t1 disabled:opacity-50"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={() => onConfirm(setBy.trim(), ack)}
            disabled={!canConfirm}
            className={cn(
              "h-[34px] rounded-[8px] px-[16px] text-[12.5px] font-medium transition-colors",
              canConfirm ? "bg-accent text-white hover:opacity-90" : "cursor-not-allowed bg-bg-1 text-t4",
            )}
          >
            {busy ? "Switching…" : toEnterprise ? "Switch to Enterprise" : "Switch to Bridge"}
          </button>
        </div>
      </div>
    </div>
  );
}

// ── Page ────────────────────────────────────────────────────────────────────

export function RoutingTab() {
  const [matrix, setMatrix] = useState<LaneMatrixResponse | null>(null);
  const [status, setStatus] = useState<LaneStatusResponse | null>(null);
  const [ceilings, setCeilings] = useState<Record<string, string | null> | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const lastFetchAtRef = useRef(0);
  // #plan-tier-toggle — guarded live flip of AGENTIC_PLAN_TIER from the UI.
  const [tierModal, setTierModal] = useState<"bridge" | "enterprise" | null>(null);
  const [flipping, setFlipping] = useState(false);
  const [flipError, setFlipError] = useState<string | null>(null);
  const [planInfo, setPlanInfo] = useState<PlanTierState | null>(null);
  // Hover-trace — the cloud ladder the operator is tracing from the matrix.
  // Pure client state derived off the existing reads; nothing fetched.
  const [traced, setTraced] = useState<TracedLane>(null);
  const reduceMotion = usePrefersReducedMotion();

  const load = useCallback(async () => {
    abortRef.current?.abort();          // dedupe overlapping polls + unmount
    const ac = new AbortController();
    abortRef.current = ac;
    lastFetchAtRef.current = Date.now();
    try {
      // allSettled (not all): the reads were added independently, so a bridge
      // could have one and not the others — keep whichever resolves rather than
      // blanking the whole page; only error when ALL fail.
      const [mr, sr, pr, tr] = await Promise.allSettled([
        api.laneMatrix(ac.signal),
        api.laneStatus(ac.signal),
        api.skillsProviders(ac.signal),   // carries provider_ceilings (G5)
        api.planTier(ac.signal),          // tier provenance (who flipped it / when)
      ]);
      if (ac.signal.aborted) return;
      if (mr.status === "fulfilled") setMatrix(mr.value);
      if (sr.status === "fulfilled") setStatus(sr.value);
      if (pr.status === "fulfilled") setCeilings(pr.value.providerCeilings ?? null);
      if (tr.status === "fulfilled") setPlanInfo(tr.value);
      if (mr.status === "rejected" && sr.status === "rejected" && pr.status === "rejected") {
        const e = mr.reason;
        setError(e instanceof ApiError ? `${e.status}: ${e.message}` : String(e));
      } else {
        setError(null);
      }
    } finally {
      if (!ac.signal.aborted) setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
    const id = window.setInterval(() => void load(), POLL_MS);
    const onFocus = () => {
      if (Date.now() - lastFetchAtRef.current >= FOCUS_DEBOUNCE_MS) void load();
    };
    window.addEventListener("focus", onFocus);
    return () => {
      abortRef.current?.abort();
      window.clearInterval(id);
      window.removeEventListener("focus", onFocus);
    };
  }, [load]);

  const tier = matrix?.tier;
  const hasAny = !!(matrix || status || ceilings);

  const onConfirmFlip = useCallback(
    async (setBy: string, ack: boolean) => {
      const target = tierModal;
      if (!target) return;
      setFlipping(true);
      setFlipError(null);
      try {
        await api.setPlanTier({ tier: target, setBy, acknowledgeCloudRouting: ack });
        setTierModal(null);
        await load();   // re-fetch so the matrix + summary reflect the new tier
      } catch (e) {
        setFlipError(e instanceof ApiError ? `${e.status}: ${e.message}` : String(e));
      } finally {
        setFlipping(false);
      }
    },
    [tierModal, load],
  );

  const openFlip = useCallback(
    (target: "bridge" | "enterprise") => {
      if (!tier || tier === target) return;   // no-op on the active tier / while loading
      setFlipError(null);
      setTierModal(target);
    },
    [tier],
  );

  return (
    <div className="w-full max-w-[1060px] flex flex-col py-[30px] px-[28px] gap-[20px] [font-synthesis:none] antialiased">
      {/* Header */}
      <div className="flex flex-col gap-[8px]">
        <div className="flex items-baseline justify-between gap-[16px]">
          <h2 className="text-[22px] font-semibold leading-[120%] tracking-[-0.01em] text-t1">Routing</h2>
          <span className="mono shrink-0 text-[11px] leading-[14px] text-t3">mirrors pick_lane · read-only · lane-matrix + lane-status</span>
        </div>
        <p className="text-[13px] leading-[150%] text-t2">
          Every run is routed by task type × sensitivity → lane. This mirrors the dispatcher (read-only) —
          the same pick_lane the bridge runs.
        </p>
        <div className="flex items-center pt-[4px] gap-[12px]">
          <span className="text-[11.5px] leading-[16px] text-t3">Plan tier</span>
          <div className="flex items-center rounded-[9px] p-[3px] gap-[2px] bg-bg-1 border border-line-2">
            <button
              type="button"
              onClick={() => openFlip("bridge")}
              disabled={!tier || tier !== "enterprise"}
              title={tier === "enterprise" ? "Switch to Bridge — confidential routing returns to local Ollama" : "Current tier"}
              className={cn(
                "h-[28px] flex items-center rounded-[7px] px-[14px] transition-colors",
                tier !== "enterprise" ? "bg-accent-soft" : "hover:bg-bg-2 cursor-pointer",
              )}
            >
              <span className={cn("text-[12.5px] leading-[16px]", tier !== "enterprise" ? "font-medium text-accent" : "text-t3")}>Bridge</span>
            </button>
            <button
              type="button"
              onClick={() => openFlip("enterprise")}
              disabled={!tier || tier === "enterprise"}
              title={tier === "enterprise" ? "Current tier" : "Switch to Enterprise — lifts confidential routing to cloud Claude"}
              className={cn(
                "h-[28px] flex items-center rounded-[7px] px-[14px] transition-colors",
                tier === "enterprise" ? "bg-accent-soft" : "hover:bg-bg-2 cursor-pointer",
              )}
            >
              <span className={cn("text-[12.5px] leading-[16px]", tier === "enterprise" ? "font-medium text-accent" : "text-t3")}>Enterprise</span>
            </button>
          </div>
          <span className="mono text-[10.5px] leading-[14px] text-t4">
            AGENTIC_PLAN_TIER · click to switch
            {planInfo?.source === "operator" && planInfo.setBy ? ` · set by ${planInfo.setBy}` : ""}
          </span>
        </div>
      </div>

      {loading && !hasAny ? (
        <div className="text-[12px] italic text-t3">Loading routing posture…</div>
      ) : error && !hasAny ? (
        <div className="text-[12px] italic text-t3">Routing posture unavailable — {error}</div>
      ) : (
        <Fragment>
          {/* Tier summary glance */}
          <TierSummary status={status} tier={tier} />

          {/* Lane matrix + legend — hover a cell to trace its cloud ladder.
              Leaving the matrix region clears the trace. */}
          {matrix && (
            <Fragment>
              <div className="flex flex-col gap-[11px]" onMouseLeave={() => setTraced(null)}>
                <div className="flex items-center pt-[4px] gap-[12px]">
                  <span className="w-max shrink-0 text-[11px] tracking-[0.1em] leading-[14px] font-semibold text-t2">
                    LANE MATRIX
                  </span>
                  <div className="grow h-px bg-line" />
                  <span className="mono w-max shrink-0 text-[10px] leading-[14px] text-t4">task type × sensitivity → lane</span>
                </div>
                <LaneMatrix m={matrix} traced={traced} onTrace={setTraced} />
              </div>
              <MatrixLegend tier={matrix.tier} />
            </Fragment>
          )}

          {/* Cloud fallback ladders + the SDK-credit / config caveat strip */}
          {status && (
            <Fragment>
              <Ladders status={status} traced={traced} reduceMotion={reduceMotion} />
              <CreditCaveat status={status} />
            </Fragment>
          )}

          {/* Provider ceilings */}
          {ceilings && <ProviderCeilings ceilings={ceilings} />}

          {error && hasAny && (
            <div className="text-[10px] text-t4">last refresh failed ({error}) — showing the previous snapshot.</div>
          )}
        </Fragment>
      )}

      {tierModal && (
        <TierFlipModal
          target={tierModal}
          current={tier}
          busy={flipping}
          error={flipError}
          onCancel={() => { setTierModal(null); setFlipError(null); }}
          onConfirm={onConfirmFlip}
        />
      )}
    </div>
  );
}
