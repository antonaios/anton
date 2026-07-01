import { useCallback, useEffect, useRef, useState } from "react";
import { RotateCcw } from "lucide-react";
import { api, ApiError } from "../lib/api";
import { cn } from "../lib/cn";
import { ProviderOverrideModal } from "./ProviderOverrideModal";
import { RoutingPosturePanel } from "./RoutingPosturePanel";
import type { SkillProviderRow, SkillsProvidersResponse } from "../types";

const POLL_MS = 60_000;
const FOCUS_DEBOUNCE_MS = 5_000;

// effective_source → SOURCE-pill styling. Each layer gets a distinct register so
// the operator sees at a glance which one won the resolution. Token-only soft
// pills: the precedence layers (sidecar / frontmatter / task-class) read as
// filled accent tints, the fall-throughs (default) + the policy floor read as
// quieter outlines. Paper "Settings — Providers": rounded-[5px] py-0.5 px-2.
const SOURCE_BADGE: Record<string, { label: string; cls: string; title: string }> = {
  sidecar:               { label: "sidecar",      cls: "bg-accent-soft text-t2",                               title: "Operator-set via the dashboard (provider_overrides.yaml) — highest precedence" },
  frontmatter:           { label: "frontmatter",  cls: "bg-accent-soft text-t2",                               title: "Set by the skill author in SKILL.md frontmatter" },
  env:                   { label: "env",          cls: "bg-bg-2 text-t3 border border-line",                   title: "From the AGENTIC_CLOUD_PROVIDER environment variable" },
  "task-class":          { label: "task-class",   cls: "bg-accent-soft text-t2",                               title: "Per-task-class provider bias below env (#llm-routing-postjune15 P2) — e.g. cross-check → openai" },
  default:               { label: "default",      cls: "bg-bg-2 text-t3 border border-line",                   title: "Nothing set — falls back to the anthropic default" },
  "confidential-policy": { label: "policy",       cls: "bg-amber/15 text-amber",                               title: "Forced local-only by sensitivity policy — confidential data never clouds; cannot be overridden" },
};

// SENS column — plain coloured mono text (matching the Paper matrix): public →
// sage/green, internal → slate, confidential → amber, MNPI → red.
const SENSITIVITY_TEXT: Record<string, string> = {
  public:       "text-green",
  internal:     "text-t2",
  confidential: "text-amber",
  MNPI:         "text-red",
};

// effective_provider is always one of shared.routing._PREFERRED_PROVIDER_VALUES
// {anthropic, openai, ollama-only, prefer_local} — the two LOCAL SENTINELS are
// routing *directives*, not provider names: both run on the local Ollama lane,
// but raw they hide (a) the real lane is local and (b) WHY. Render them legibly
// + distinguish a token-saving downgrade (prefer_local, cloud-eligible) from a
// fail-closed lock (ollama-only). Wording mirrors the override modal's labels
// ("Ollama (local only)" / "Prefer local (downgrade)").
// Partial<Record<…>> (not Record): a lookup by the runtime `effective_provider`
// string returns `… | undefined`, so the `sentinel ?` branch below is correctly
// typed (a plain Record would mask the miss as always-defined).
const LOCAL_SENTINELS: Partial<Record<string, { lane: string; badge: string; cls: string; title: string }>> = {
  prefer_local: {
    lane: "ollama",
    badge: "↓ prefer-local",
    cls:  "text-accent border-accent-line",
    title: "Prefer local (downgrade) — runs LOCAL on Ollama. Cloud-eligible, but the operator downgraded this public/internal pick to local to save tokens. NOT a hard lock (cf. ollama-only).",
  },
  "ollama-only": {
    lane: "ollama-only",
    badge: "local-only",
    cls:  "text-t3 border-line-2",
    title: "Ollama (local only) — runs LOCAL, fail-closed: refuses any cloud call. Either an operator hard-lock or a confidential/MNPI policy floor (see the Source column).",
  },
};

/**
 * #llm-routing-tier-2 · /skills/providers page.
 *
 * Per-skill provider matrix from GET /api/skills/providers. Columns: skill,
 * sensitivity, effective-source badge, effective lane (provider · model /
 * sentinel + temp / last-fire / cost folded in), and an Override action.
 *
 *   - Confidential/MNPI rows are forced local (effective_provider="ollama-only",
 *     source="confidential-policy") — the Override button is disabled (the
 *     bridge would 422 a cloud override; the UI prevents the attempt).
 *   - When a row carries an operator sidecar entry (override != null) a "reset"
 *     affordance reverts it to frontmatter (PATCH clear:true).
 *   - effective_error (post-restart field) renders as a red badge with the
 *     resolution-error string on hover.
 *
 * Restyle (light-teal / dark-navy): rows are grouped into "overridable to cloud"
 * (public/internal) and "locked · local-only" (confidential/MNPI) so the
 * sensitivity floor reads at a glance, matching the Paper "Settings — Providers"
 * surface. Provider ceilings fold into the ROUTING CONTROL card here; the
 * actionable Crews + MNPI sections come from <RoutingPosturePanel/>. The
 * read-only routing-posture readouts (lane matrix, fallback ladders, task-class
 * tiering) live on the dedicated Routing tab (RoutingTab).
 *
 * Refresh: focus + every 60s. Self-fetches (like TknBudgetTab).
 */
export function SkillsProvidersTab() {
  const [data, setData] = useState<SkillsProvidersResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [actionErr, setActionErr] = useState<string | null>(null);
  const [busyKey, setBusyKey] = useState<string | null>(null);
  const [overrideTarget, setOverrideTarget] = useState<SkillProviderRow | null>(null);

  const abortRef = useRef<AbortController | null>(null);
  const lastFetchAtRef = useRef(0);

  const load = useCallback(async () => {
    abortRef.current?.abort();
    const ac = new AbortController();
    abortRef.current = ac;
    lastFetchAtRef.current = Date.now();
    try {
      const r = await api.skillsProviders(ac.signal);
      if (ac.signal.aborted) return;
      setData(r);
      setError(null);
    } catch (e) {
      if (ac.signal.aborted) return;
      setError(e instanceof ApiError ? `${e.status}: ${e.message}` : String(e));
    } finally {
      if (!ac.signal.aborted) setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
    const id = window.setInterval(() => void load(), POLL_MS);
    return () => { window.clearInterval(id); abortRef.current?.abort(); };
  }, [load]);

  useEffect(() => {
    const onFocus = () => {
      if (Date.now() - lastFetchAtRef.current >= FOCUS_DEBOUNCE_MS) void load();
    };
    window.addEventListener("focus", onFocus);
    return () => window.removeEventListener("focus", onFocus);
  }, [load]);

  // Reset a row's operator override back to frontmatter (PATCH clear:true).
  const resetOverride = async (key: string) => {
    if (busyKey) return;
    setBusyKey(key);
    setActionErr(null);
    try {
      await api.patchSkillProvider(key, { clear: true });
      await load();
    } catch (e) {
      setActionErr(e instanceof ApiError ? `Reset failed: ${e.message}` : "Reset failed.");
    } finally {
      setBusyKey(null);
    }
  };

  const rows = data?.skills ?? [];
  // Presentational partition only — same rows, order preserved within each group.
  // The "locked" group is exactly the confidential/MNPI sensitivity floor.
  const openRows = rows.filter((r) => !isLocked(r));
  const lockedRows = rows.filter((r) => isLocked(r));
  // The visually-last data row drops its bottom hairline (Paper matrix).
  const orderedRows = lockedRows.length ? lockedRows : openRows;
  const lastKey = orderedRows.length ? orderedRows[orderedRows.length - 1].key : undefined;

  return (
    <div className="flex flex-col gap-[20px] px-[28px] py-[30px] [font-synthesis:none] antialiased">
      {/* Header — Paper "Settings — Providers": title + endpoint hint, then the
          actionable-side description. */}
      <div className="flex flex-col gap-[7px]">
        <div className="flex items-baseline justify-between gap-[16px]">
          <h2 className="text-[22px] font-semibold leading-[120%] tracking-[-0.01em] text-t1">Providers</h2>
          <span className="mono shrink-0 text-[11px] leading-[14px] text-t3">/api/skills/providers · /crew/providers</span>
        </div>
        <p className="text-[13px] leading-[150%] text-t2">
          The actionable side of routing — per-skill provider overrides, crew cloud-promotion, and MNPI
          attestations. Confidential / MNPI skills are forced <span className="text-t1">local-only</span> by policy
          and cannot be overridden onto cloud. Overrides are persistent — they write the operator sidecar
          {data?.sidecarPath && <span className="text-t3"> ({data.sidecarPath})</span>}, which you commit per
          CLAUDE.md §5.7.
        </p>
      </div>

      {/* ROUTING CONTROL summary — Paper "Settings — Providers" top card: the
          sensitivity floor at a glance, derived from the SAME matrix rows. Two
          tiles: N overridable-to-cloud (public/internal) vs M locked-local
          (confidential/MNPI, isLocked → the gate-forced floor). Counts only
          render once the matrix has loaded. */}
      {data && (
        <div className="rounded-[13px] border border-line bg-bg-1 px-[20px] py-[16px]">
          <div className="mb-[13px] flex items-center justify-between">
            <div className="mono text-[9.5px] font-semibold tracking-[0.1em] leading-[12px] text-t2">
              ROUTING CONTROL
            </div>
            <div className="text-[10.5px] leading-[14px] text-t3">
              what can move to cloud · and what the sensitivity floor locks local
            </div>
          </div>
          <div className="flex gap-[14px]">
            <div className="flex grow basis-0 items-center gap-[12px] rounded-[11px] border border-green/[0.26] bg-green/[0.09] px-[15px] py-[12px]">
              <div className="mono text-[24px] leading-[30px] text-green">{openRows.length}</div>
              <div className="flex flex-col gap-[2px]">
                <div className="text-[13px] font-semibold leading-[16px] text-t1">overridable to cloud</div>
                <div className="text-[11px] leading-[14px] text-t2">public &amp; internal skills</div>
              </div>
            </div>
            <div className="flex grow basis-0 items-center gap-[12px] rounded-[11px] border border-amber/[0.26] bg-amber/[0.09] px-[15px] py-[12px]">
              <div className="mono text-[24px] leading-[30px] text-amber">{lockedRows.length}</div>
              <div className="flex flex-col gap-[2px]">
                <div className="text-[13px] font-semibold leading-[16px] text-t1">locked · local-only</div>
                <div className="text-[11px] leading-[14px] text-t2">confidential &amp; MNPI — gate-forced</div>
              </div>
            </div>
          </div>

          {/* PROVIDER CEILINGS — Paper "Settings — Providers" folds the per-CLOUD-
              provider sensitivity ceiling (providers.<name>.max_sensitivity) into
              the routing-control card: a 1px divider, then a mono label + a small
              bordered pill per provider. null = UNCONFIGURED (no per-provider cap;
              the §4 matrix governs). A deny-wins cap — even over a P5 attestation.
              The two cloud providers ALWAYS render (merged over a default so an
              empty/missing map still shows anthropic + openai as unconfigured),
              with the deny-wins caption always visible — matching the mock. */}
          <div className="mt-[14px] border-t border-line pt-[13px]">
            <div className="mono mb-[9px] text-[10px] font-semibold tracking-[0.1em] leading-[12px] text-t2">
              PROVIDER CEILINGS
            </div>
            <div className="flex flex-wrap items-center gap-[8px]">
              {Object.entries({ anthropic: null, openai: null, ...(data.providerCeilings ?? {}) }).map(([prov, ceil]) => (
                <span
                  key={prov}
                  className="inline-flex items-center gap-[7px] rounded-[7px] border border-line px-[11px] py-1 text-[11px] leading-[14px] whitespace-nowrap"
                  title={
                    ceil == null
                      ? "Unconfigured — no per-provider cap; the §4 sensitivity matrix governs (= internal in bridge tier). Rises per-provider via providers.<name>.max_sensitivity when Enterprise/ZDR lands."
                      : `Operator-capped at ${ceil} via providers.${prov}.max_sensitivity (deny-wins, even over a P5 attestation).`
                  }
                >
                  <span className="font-medium text-t1">{prov}</span>
                  {ceil == null
                    ? <span className="mono text-[10px] text-t4">unconfigured</span>
                    : <span className="mono text-[10px] text-accent">{ceil}</span>}
                </span>
              ))}
              <span className="text-[10px] leading-[14px] text-t4">
                deny-wins — a per-provider cap tightens routing even over an active MNPI attestation
              </span>
            </div>
          </div>
        </div>
      )}

      {actionErr && (
        <div className="rounded-lg border border-red/40 bg-red/10 px-[12px] py-[7px] text-[11.5px] text-red">{actionErr}</div>
      )}

      {loading && !data ? (
        <div className="text-[11.5px] italic text-t3">Loading…</div>
      ) : error && !data ? (
        <div className="text-[11.5px] italic text-t3">Matrix unavailable — {error}</div>
      ) : (
        <div className="overflow-clip rounded-[13px] border border-line text-[12px] leading-[16px]">
          {/* Card title strip */}
          <div className="flex items-baseline justify-between gap-[12px] border-b border-line px-[16px] py-[10px]">
            <span className="mono text-[10px] font-semibold tracking-[0.1em] leading-[12px] text-t1">
              SKILL PROVIDERS · {rows.length}
            </span>
            <span className="text-[10px] leading-[12px] text-t3">
              resolution: sidecar › frontmatter › env › task-class › default · confidential-policy overrides all
              {data && <span> · default {data.defaultProvider}</span>}
              {data?.envProvider ? <span> · env {data.envProvider}</span> : null}
              {data ? <span> · as of {new Date(data.asOf).toLocaleTimeString()}</span> : null}
            </span>
          </div>

          {/* Matrix legend — Paper "Settings — Providers" glosses the three
              resolution columns between the title strip and the column header. */}
          <div className="border-b border-line bg-bg-1 px-[16px] py-[8px] text-[10.5px] leading-[1.55] text-t3">
            <span className="text-t2 font-medium">Sens</span> = data-sensitivity tier (gates cloud) ·{" "}
            <span className="text-t2 font-medium">Set by</span> = which rule resolved the lane ·{" "}
            <span className="text-t2 font-medium">Effective lane</span> = the active provider · model (last run · cost beneath)
          </div>

          {/* Column header row */}
          <div className="mono flex items-center gap-[12px] border-b border-line bg-bg-2 px-[16px] py-[8px] text-[9px] font-semibold tracking-[0.07em] leading-[12px] text-t1">
            <div className="w-[158px] shrink-0">SKILL</div>
            <div className="w-[84px] shrink-0">SENS</div>
            <div className="w-[100px] shrink-0">SET BY</div>
            <div className="grow basis-0">EFFECTIVE LANE</div>
            <div className="w-[96px] shrink-0 text-right">ACTION</div>
          </div>

          {openRows.length > 0 && (
            <GroupRow label={`OVERRIDABLE TO CLOUD · ${openRows.length}`} note="public & internal skills" />
          )}
          {openRows.map((row) => (
            <ProviderRow
              key={row.key}
              row={row}
              busy={busyKey === row.key}
              isLast={row.key === lastKey}
              onOverride={() => setOverrideTarget(row)}
              onReset={() => void resetOverride(row.key)}
            />
          ))}

          {lockedRows.length > 0 && (
            <GroupRow
              label={`LOCKED · LOCAL-ONLY · ${lockedRows.length}`}
              note="sensitivity gate — not operator-changeable here"
              tone="locked"
            />
          )}
          {lockedRows.map((row) => (
            <ProviderRow
              key={row.key}
              row={row}
              busy={busyKey === row.key}
              isLast={row.key === lastKey}
              onOverride={() => setOverrideTarget(row)}
              onReset={() => void resetOverride(row.key)}
            />
          ))}
        </div>
      )}

      <RoutingPosturePanel />

      {error && data && (
        <div className="text-[10.5px] text-t4">last refresh failed ({error}) — showing the previous snapshot.</div>
      )}

      <ProviderOverrideModal
        row={overrideTarget}
        sidecarPath={data?.sidecarPath}
        onClose={() => setOverrideTarget(null)}
        onSaved={() => void load()}
      />
    </div>
  );
}

// Group-header strip spanning the matrix — partitions overridable vs locked
// skills. Purely presentational; tints the locked group amber to echo the
// sensitivity floor. Paper: bg-(--paper2), py-1.5 px-4.
function GroupRow({ label, note, tone = "open" }: { label: string; note?: string; tone?: "open" | "locked" }) {
  return (
    <div className="flex items-center justify-between bg-paper2 px-[16px] py-[6px]">
      <span
        className={cn(
          "mono text-[9px] font-semibold tracking-[0.09em] leading-[12px]",
          tone === "locked" ? "text-amber" : "text-t2",
        )}
      >
        {label}
      </span>
      {note && <span className="text-[9.5px] leading-[12px] text-t3">{note}</span>}
    </div>
  );
}

// SOURCE-pill (per-row). Token-only soft register; Paper rounded-[5px] py-0.5 px-2.
function SourceBadge({ label, cls, title }: { label: string; cls: string; title: string }) {
  return (
    <span
      className={cn(
        "mono inline-flex items-center rounded-[5px] px-[8px] py-[2px] text-[9.5px] leading-[12px] whitespace-nowrap",
        cls,
      )}
      title={title}
    >
      {label}
    </span>
  );
}

function ProviderRow({
  row, busy, isLast, onOverride, onReset,
}: {
  row: SkillProviderRow;
  busy: boolean;
  isLast: boolean;
  onOverride: () => void;
  onReset: () => void;
}) {
  const locked = isLocked(row);
  const badge = SOURCE_BADGE[row.effectiveSource] ?? { label: row.effectiveSource, cls: "bg-bg-2 text-t3 border border-line", title: row.effectiveSource };
  const temp = row.effectiveLlmParams.temperature;
  const hasOverride = row.override != null;
  // A local sentinel (prefer_local / ollama-only) renders as the real lane +
  // a directive badge instead of the opaque raw string.
  const sentinel = LOCAL_SENTINELS[row.effectiveProvider];
  const allowed = (row.allowedProviders.length ? row.allowedProviders : ["anthropic", "openai", "ollama"]).join(", ");

  return (
    <div className={cn("flex items-center gap-[12px] px-[16px] py-[9px]", !isLast && "border-b border-line")}>
      <div className="mono w-[158px] shrink-0 self-start pt-[1px] text-[12px] leading-[16px] text-t1">{row.key}</div>

      <div className={cn("mono w-[84px] shrink-0 self-start pt-[3px] text-[10px] leading-[12px]", SENSITIVITY_TEXT[row.sensitivity] ?? "text-t2")}>
        {row.sensitivity}
      </div>

      <div className="w-[100px] shrink-0 self-start">
        <div className="flex flex-col items-start gap-[4px]">
          <SourceBadge label={badge.label} cls={badge.cls} title={badge.title} />
          {hasOverride && (
            <button
              type="button"
              onClick={onReset}
              disabled={busy}
              title="Operator override set — reset to SKILL.md frontmatter"
              className="mono inline-flex items-center gap-[3px] text-[9px] tracking-[0.06em] uppercase text-t3 transition-colors hover:text-accent disabled:cursor-default disabled:opacity-40"
            >
              <RotateCcw size={9} className="shrink-0" />
              reset
            </button>
          )}
        </div>
      </div>

      <div className="grow basis-0">
        {/* Primary lane line — provider · model / sentinel + error + fallback. */}
        <div className="flex flex-wrap items-center gap-[6px] text-[11.5px] leading-[14px]">
          {sentinel ? (
            <>
              <span className={cn(locked ? "text-t3" : "text-t2")}>{sentinel.lane}</span>
              <span
                className={cn(
                  "mono inline-flex items-center rounded-md border px-[6px] py-[1px] text-[9.5px] tracking-[0.06em] uppercase whitespace-nowrap",
                  sentinel.cls,
                )}
                title={sentinel.title}
              >
                {sentinel.badge}
              </span>
            </>
          ) : (
            <>
              <span className={cn(locked ? "text-t3" : "text-t2")}>{row.effectiveProvider}</span>
              {row.effectiveModel && (
                <span className="text-[11px] text-accent" title="Pinned Claude model (effective)">· {row.effectiveModel}</span>
              )}
            </>
          )}
          {row.effectiveError && (
            <span
              className="mono inline-flex items-center gap-[3px] rounded-md border border-red/40 bg-red/10 px-[6px] py-[1px] text-[9.5px] tracking-[0.06em] uppercase text-red whitespace-nowrap"
              title={row.effectiveError}
            >
              ⚠ {row.effectiveError}
            </span>
          )}
          {row.fallbackProvider && (
            <span className="text-[10.5px] text-t4" title="Per-skill fallback provider">↘ {row.fallbackProvider}</span>
          )}
        </div>
        {/* Secondary meta — allowed providers · temp · last fire · cost.
            Folded here so every datum stays rendered under the simplified columns. */}
        <div className="mt-[3px] flex flex-wrap items-center gap-x-[10px] gap-y-[2px] text-[10.5px] text-t4">
          <span title="Allowed providers">{allowed}</span>
          <span className="text-line-2">·</span>
          <span className="tabular tabular-nums" title="Effective sampling temperature">
            temp {temp != null ? temp.toFixed(2) : "—"}
          </span>
          <span className="text-line-2">·</span>
          {row.lastFire ? (
            <span title={`${row.lastProvider ?? ""} ${row.lastFire}`}>
              {fmtLastFire(row.lastFire)}
              {row.lastProvider && <span> · {row.lastProvider}</span>}
            </span>
          ) : (
            <span>never fired</span>
          )}
          <span className="text-line-2">·</span>
          <span className="tabular tabular-nums" title="Cost since the bridge started">
            {row.calls > 0 ? fmtUsd(row.costUsd) : "—"}
          </span>
        </div>
      </div>

      <div className="flex w-[96px] shrink-0 items-center justify-end self-start">
        {locked ? (
          <span
            className="mono inline-flex items-center gap-[5px] text-[10px] leading-[12px] text-t3 cursor-default"
            title="Confidential/MNPI skills are forced local-only — cannot be overridden onto cloud (CLAUDE.md §5.2)"
          >
            <span className="text-[11px] leading-[14px]" aria-hidden>🔒</span>
            locked
          </span>
        ) : (
          <button
            type="button"
            onClick={onOverride}
            disabled={busy}
            className="inline-flex items-center rounded-[7px] border border-line-2 px-[12px] py-[5px] text-[11px] font-medium leading-[14px] text-t1 transition-colors hover:border-accent-line hover:text-accent disabled:cursor-default disabled:opacity-40"
          >
            {hasOverride ? "Edit" : "Override"}
          </button>
        )}
      </div>
    </div>
  );
}

// ── Helpers ──────────────────────────────────────────────────────────────────

// A row is "locked" when its sensitivity floor forces it local-only — exactly the
// confidential/MNPI rows whose Override action is suppressed. Single source of
// truth for both the partition and the per-row guard.
function isLocked(row: SkillProviderRow): boolean {
  return row.sensitivity === "confidential" || row.sensitivity === "MNPI";
}

function fmtLastFire(iso: string): string {
  const t = Date.parse(iso);
  if (!Number.isFinite(t)) return iso;
  const d = new Date(t);
  const now = Date.now();
  const days = Math.floor((now - t) / 86_400_000);
  if (days <= 0) return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  if (days < 7) return `${days}d ago`;
  return d.toLocaleDateString([], { month: "short", day: "numeric" });
}

function fmtUsd(v: number): string {
  if (v === 0) return "$0";
  if (v < 0.01) return "<$0.01";
  if (v < 1)    return `$${v.toFixed(3)}`;
  if (v < 100)  return `$${v.toFixed(2)}`;
  return `$${Math.round(v).toLocaleString()}`;
}
