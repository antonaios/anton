import { useCallback, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { api, ApiError } from "../lib/api";
import { cn } from "../lib/cn";
import { SegmentedToggle } from "./ui/SegmentedToggle";
import type {
  BudgetPolicyRow, CreateBudgetBody, LLMBurnSummary, PlanRow, PlansResponse, WorkspaceListItem,
} from "../types";

type FormScope = "project" | "bd" | "llm" | "global" | "project_llm";

// The LLMs ANTON routes to (per routines/shared/routing.py). `key` is the
// telemetry/budget provider string; edit to match the subscriptions in play.
const PROVIDERS: { key: string; label: string }[] = [
  { key: "claude",  label: "Claude" },
  { key: "codex",   label: "ChatGPT" },
  { key: "minimax", label: "MiniMax" },
  { key: "ollama",  label: "Ollama (local)" },
];

const fmtInt = (n: number) => (n ?? 0).toLocaleString("en-US");
const fmtUsd = (n: number) => (!n ? "$0" : `$${n.toFixed(n < 1 ? 4 : 2)}`);

/** Compact token-count formatting — e.g. 1,420,000 → "1.42M", 380000 → "380k". */
const fmtTok = (n: number): string => {
  const v = n ?? 0;
  if (v >= 999_500) return `${(v / 1e6).toFixed(2)}M`;   // rounds up to ≥1.00M (avoids "1000k")
  if (v >= 1e3) return `${Math.round(v / 1e3)}k`;
  return String(v);
};

/** Format one number in a subscription plan's native unit (messages / $ / £). */
function fmtPlanAmount(n: number, unit: PlanRow["unit"]): string {
  if (unit === "gbp") return `£${n.toFixed(n < 10 ? 2 : 0)}`;
  if (unit === "usd") return `$${n.toFixed(n < 10 ? 2 : 0)}`;
  return fmtInt(n);
}

/** Used / cap label for a plan row — e.g. "12 / 50 msg" or "£3.20 / £10". */
function fmtPlanUsage(p: PlanRow): string {
  if (p.unit === "messages") return `${fmtInt(p.used)} / ${fmtInt(p.cap)} msg`;
  return `${fmtPlanAmount(p.used, p.unit)} / ${fmtPlanAmount(p.cap, p.unit)}`;
}

/** Rolling-window-remaining seconds → compact "3h12m" / "44m" suffix. */
function fmtReset(p: PlanRow): string {
  if (p.resetKind === "monthly") return "month";
  const s = Math.max(0, Math.round(p.resetInSec));
  if (s <= 0) return "now";
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (h > 0) return `${h}h${String(m).padStart(2, "0")}m`;
  return `${m}m`;
}

/** Start of the current month in UTC — aligns usage with the monthly_utc period. */
function monthStartIso(): string {
  const now = new Date();
  return new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), 1)).toISOString();
}

function scopeLabel(p: BudgetPolicyRow): string {
  if (p.scope.kind === "global") return "Global";
  return `${p.scope.a} · ${p.scope.b}`;
}

/** Short badge for a budget scope kind (mirrors the Paper cost-gate badges). */
function scopeBadge(p: BudgetPolicyRow): string {
  switch (p.scope.kind) {
    case "global":             return "GLOBAL";
    case "provider":           return "LLM";
    case "workspace_provider": return "PROJ × LLM";
    default:                   return (p.scope.kind || "").toUpperCase();
  }
}

/** Themed card shell — matches the Paper panel chrome (rounded-[14px], hairline, soft elevation). */
function Panel({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <div
      className={cn(
        "flex flex-col overflow-clip rounded-[14px] border border-line bg-bg-1",
        "[box-shadow:#23211C0D_0px_1px_2px,#23211C26_0px_10px_26px_-16px]",
        className,
      )}
    >
      {children}
    </div>
  );
}

/** Panel header bar — small-caps title + faint mono meta, hairline underline. */
function PanelHeader({ title, meta }: { title: ReactNode; meta?: ReactNode }) {
  return (
    <div className="flex items-center justify-between border-b border-line py-[13px] px-[18px]">
      <div className="flex items-center gap-[9px]">
        <span className="mono text-[11px] tracking-[0.1em] font-semibold text-t2">{title}</span>
      </div>
      {meta != null && <span className="mono text-[10.5px] text-t4">{meta}</span>}
    </div>
  );
}

/**
 * TKN BUDGET tab — set monthly token budgets (track + warn) and view this
 * month's token usage. Budgets can be set globally, per project/BD, per LLM
 * (provider), or per LLM-within-a-project. Token caps do NOT block calls (v1);
 * the hard cost-safety block is the separate USD budget gate.
 *
 * Backend: the token-budget store + the per-LLM-within-a-project
 * `workspace_provider` scope are LIVE (#llm-routing-postjune15 B6) — POSTing a
 * token cap on any scope, incl. `workspace_provider`, returns 201 and the
 * track+warn usage surfaces here. (This path 422'd before the backend shipped;
 * the monthly $-credit / Agent-SDK credit is a separate USD cap shown in the
 * LLM-usage panel.)
 */
export function TknBudgetTab() {
  const [policies, setPolicies] = useState<BudgetPolicyRow[]>([]);
  const [burn, setBurn] = useState<LLMBurnSummary | null>(null);
  const [plans, setPlans] = useState<PlanRow[]>([]);
  const [projects, setProjects] = useState<WorkspaceListItem[]>([]);
  const [bds, setBds] = useState<WorkspaceListItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [usageErr, setUsageErr] = useState<string | null>(null);
  const [actionErr, setActionErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // Context picker + draft cap inputs (keyed by a stable row id).
  const [scope, setScope] = useState<FormScope>("llm");
  const [wsName, setWsName] = useState("");
  const [draft, setDraft] = useState<Record<string, string>>({});

  const fmtErr = (e: unknown) => (e instanceof ApiError ? `${e.status}: ${e.message}` : String(e));

  const refresh = useCallback(async () => {
    setLoading(true);
    let pols: BudgetPolicyRow[] = [];
    try { pols = (await api.listBudgets()).policies; setPolicies(pols); }
    catch { /* GET /budgets is safe; ignore transient */ }
    try { setBurn(await api.llmBurn({ group_by: "all", since: monthStartIso() })); setUsageErr(null); }
    catch (e) { setUsageErr(fmtErr(e)); setBurn(null); }
    // Subscription/plan rolling-window rows — same source the right-rail consumes.
    try { const pr: PlansResponse = await api.usagePlans(); setPlans(pr.plans ?? []); }
    catch { /* GET /usage/plans is read-only; degrade quietly */ }
    // Seed draft inputs from existing caps so the tables show current state.
    const seeded: Record<string, string> = {};
    for (const p of pols) {
      if (p.capTokens == null) continue;
      if (p.scope.kind === "provider" && p.scope.b === "*") seeded[`llm:${p.scope.a}`] = String(p.capTokens);
      else if (p.scope.kind === "workspace_provider") seeded[`${p.scope.a}:${p.scope.b}`] = String(p.capTokens);
      else if (p.scope.kind === "global") seeded["global"] = String(p.capTokens);
    }
    setDraft(seeded);
    setLoading(false);
  }, []);

  useEffect(() => { void refresh(); }, [refresh]);
  useEffect(() => {
    api.listWorkspaces("project").then((r) => setProjects(r.workspaces)).catch(() => { /* offline */ });
    api.listWorkspaces("bd").then((r) => setBds(r.workspaces)).catch(() => { /* offline */ });
  }, []);

  // workspace_provider scope key (`type:name`). For Project/BD we prefix the
  // bare workspace name; for the combined Project × LLM segment the picker
  // already yields a fully-qualified `type:name` value, so use it verbatim.
  const wsKey = scope === "project" || scope === "bd"
    ? (wsName ? `${scope}:${wsName}` : null)
    : scope === "project_llm"
      ? (wsName || null)
      : null;

  // ── usage lookups ──────────────────────────────────────────────────────
  const usageGlobal = (prov: string): number => {
    const pb = burn?.byProvider?.[prov];
    return pb ? (pb.tokensIn ?? 0) + (pb.tokensOut ?? 0) : 0;
  };
  const usageInWs = (key: string, prov: string): number => {
    const wb = burn?.byWorkspace?.[key];
    const mb = wb?.providers?.[prov];
    return mb ? (mb.tokensIn ?? 0) + (mb.tokensOut ?? 0) : 0;
  };

  // ── cap actions ────────────────────────────────────────────────────────
  // LLM (global per-provider): provider scope, b="*" = all models of provider.
  // Project×LLM: new workspace_provider scope (a=type:name, b=provider).
  const saveCap = async (body: CreateBudgetBody, rowId: string) => {
    const raw = draft[rowId];
    const n = Number(raw);
    if (!raw || !Number.isFinite(n) || n <= 0 || busy) return;
    setBusy(true);
    try { await api.upsertBudget({ ...body, cap_tokens: n }); setActionErr(null); await refresh(); }
    catch (e) { setActionErr(fmtErr(e)); }
    finally { setBusy(false); }
  };
  const clearCap = async (scopeRef: { kind: string; a?: string | null; b?: string | null }) => {
    if (busy) return;
    setBusy(true);
    try { await api.deleteBudget(scopeRef); setActionErr(null); await refresh(); }
    catch (e) { setActionErr(fmtErr(e)); }
    finally { setBusy(false); }
  };

  // overall project × LLM matrix (bottom section)
  const matrix = useMemo(() => {
    const byWs = burn?.byWorkspace ?? {};
    const set = new Set<string>();
    const rows = Object.entries(byWs).map(([key, wb]) => {
      Object.keys(wb.providers ?? {}).forEach((p) => set.add(p));
      return { key, wb };
    });
    rows.sort((a, b) => ((b.wb.tokensIn ?? 0) + (b.wb.tokensOut ?? 0)) - ((a.wb.tokensIn ?? 0) + (a.wb.tokensOut ?? 0)));
    return { rows, providers: [...set].sort() };
  }, [burn]);

  // column + grand totals for the matrix footer (derived from the same burn data)
  const matrixTotals = useMemo(() => {
    const byProv: Record<string, number> = {};
    const costByProv: Record<string, number> = {};
    let cost = 0;
    for (const { wb } of matrix.rows) {
      for (const p of matrix.providers) {
        const mb = wb.providers?.[p];
        byProv[p] = (byProv[p] ?? 0) + (mb ? (mb.tokensIn ?? 0) + (mb.tokensOut ?? 0) : 0);
        costByProv[p] = (costByProv[p] ?? 0) + (mb?.costUsd ?? 0);
      }
      cost += wb.costUsd ?? 0;
    }
    return { byProv, costByProv, cost };
  }, [matrix]);

  // ── burn summary (BURN panel) ────────────────────────────────────────────
  // Spend so far over the loaded window (month-to-date) + a linear projection
  // to the end of the period, plus the calls/tokens/providers footnote — all
  // derived from the already-loaded `burn.totals`/`byProvider`, no extra fetch.
  const burnSummary = useMemo(() => {
    if (!burn) return null;
    const t = burn.totals;
    const spend = t.costUsd ?? 0;
    const since = new Date(burn.window.since).getTime();
    const until = new Date(burn.window.until).getTime();
    const elapsed = Math.max(1, until - since);
    const periodEnd = Date.UTC(
      new Date(until).getUTCFullYear(), new Date(until).getUTCMonth() + 1, 1,
    );
    const fullPeriod = Math.max(elapsed, periodEnd - since);
    const projected = elapsed > 0 ? spend * (fullPeriod / elapsed) : spend;
    const provs = Object.keys(burn.byProvider ?? {}).length;
    return {
      spend,
      projected,
      calls: t.calls ?? 0,
      tokens: (t.tokensIn ?? 0) + (t.tokensOut ?? 0),
      provs,
    };
  }, [burn]);

  // ── reusable per-provider budget table ──────────────────────────────────
  const providerTable = (opts: {
    getUsage: (prov: string) => number;
    rowId: (prov: string) => string;
    scopeRef: (prov: string) => { kind: string; a?: string | null; b?: string | null };
    capFor: (prov: string) => number | null;
    pctFor: (prov: string) => number | null;
  }) => (
    <div className="flex flex-col">
      {PROVIDERS.map(({ key, label }) => {
        const id = opts.rowId(key);
        const used = opts.getUsage(key);
        const cap = opts.capFor(key);
        const pct = opts.pctFor(key);
        const usedColour = pct == null ? "text-t3" : pct >= 100 ? "text-red" : pct >= 80 ? "text-amber" : "text-t3";
        return (
          <div key={key} className="flex items-center gap-[14px] py-[13px] px-[18px] border-b border-line last:border-b-0">
            <div className="grow min-w-0 flex flex-col gap-[6px]">
              <span className="text-[12.5px] font-medium text-t1">{label}</span>
              {cap != null && pct != null && <CapBar pct={pct} />}
            </div>
            <span className={cn("w-[120px] shrink-0 text-right mono text-[11.5px]", usedColour)}>
              {fmtTok(used)}{cap != null && pct != null ? ` · ${pct.toFixed(0)}%` : ""}
            </span>
            <div className="flex items-center h-[38px] shrink-0 rounded-[9px] gap-[8px] px-[12px] bg-bg-2 border border-line-2 focus-within:border-accent-line transition-colors">
              <input
                value={draft[id] ?? ""}
                onChange={(e) => setDraft((d) => ({ ...d, [id]: e.target.value.replace(/[^0-9]/g, "") }))}
                placeholder={cap != null ? fmtTok(cap) : "no cap"}
                inputMode="numeric"
                className="w-[110px] bg-transparent mono text-[13px] text-t1 outline-none placeholder:text-t4"
              />
            </div>
            <button
              type="button"
              onClick={() => void saveCap({ scope: opts.scopeRef(key) as CreateBudgetBody["scope"], cap_usd: 0 }, id)}
              disabled={busy || !draft[id]}
              className="flex items-center h-[36px] shrink-0 rounded-[9px] px-[16px] bg-accent-soft border border-accent-line text-[12.5px] font-semibold text-t1 hover:opacity-90 transition-opacity disabled:opacity-40 disabled:cursor-default disabled:hover:opacity-40"
            >Set</button>
            <button
              type="button"
              onClick={() => void clearCap(opts.scopeRef(key))}
              disabled={busy || cap == null}
              className="flex items-center h-[36px] shrink-0 rounded-[9px] px-[14px] border border-line-2 text-[12.5px] text-t3 hover:text-t1 transition-colors disabled:opacity-30 disabled:cursor-default disabled:hover:text-t3"
            >Clear</button>
          </div>
        );
      })}
    </div>
  );

  // helpers to find an existing cap + pct for a scope
  const capLookup = (kind: string, a: string | null, b: string | null): BudgetPolicyRow | undefined =>
    policies.find((p) => p.scope.kind === kind && (p.scope.a ?? null) === a && (p.scope.b ?? null) === b && p.capTokens != null);

  const globalPolicy = capLookup("global", null, null);

  // The scope hint shown beside the segmented control (mirrors the active scope).
  const scopeHint =
    scope === "llm"     ? "scope = provider:<llm>:*"
    : scope === "project" ? (wsKey ? `scope = workspace_provider:${wsKey}` : "scope = workspace_provider:project:…")
    : scope === "bd"      ? (wsKey ? `scope = workspace_provider:${wsKey}` : "scope = workspace_provider:bd:…")
    : scope === "project_llm" ? (wsKey ? `scope = workspace_provider:${wsKey}` : "scope = workspace_provider:<project>:<llm>")
    : "scope = global";

  const cappedPolicies = policies.filter((p) => p.capTokens != null);

  return (
    <div className="flex flex-col items-center">
      <div className="w-full max-w-[1060px] flex flex-col py-[30px] px-[28px] gap-[20px]">
        {/* ── HEADER ──────────────────────────────────────────────────────── */}
        <div className="flex flex-col gap-[8px]">
          <div className="flex items-baseline justify-between gap-[16px]">
            <h2 className="text-[22px] leading-[120%] tracking-[-0.01em] font-semibold text-t1">Budget</h2>
            <span className="mono text-[11px] text-t3">
              /api/budgets · tokens · monthly (UTC){burn ? ` · since ${burn.window.since.slice(0, 10)}` : ""}
            </span>
          </div>
          <p className="text-[13px] leading-[150%] text-t2">
            Set a monthly token cap globally, per project/BD, or per LLM. Budgets are
            <span className="text-t1"> track + warn</span> — usage shows against the cap and turns amber
            past the threshold, but token caps don't block calls (the hard cost-safety block is the
            separate USD budget gate).
          </p>
        </div>

        {actionErr && (
          <div className="rounded-[9px] border border-red/40 bg-red/10 px-[12px] py-[7px] text-[11.5px] text-red">{actionErr}</div>
        )}

        {/* ── CAP SETTER ──────────────────────────────────────────────────── */}
        <Panel>
          {/* header bar: cap-scope picker + live scope hint */}
          <div className="flex items-center flex-wrap gap-[12px] py-[15px] px-[18px] border-b border-line">
            <span className="text-[10.5px] tracking-[0.08em] font-semibold text-t2 shrink-0">CAP SCOPE</span>
            <SegmentedToggle
              value={scope}
              onChange={(v) => { setScope(v as FormScope); setWsName(""); }}
              options={[
                { value: "global",      label: "Global" },
                { value: "project",     label: "Project" },
                { value: "bd",          label: "BD" },
                { value: "llm",         label: "Per-LLM" },
                { value: "project_llm", label: "Project × LLM" },
              ]}
            />

            <span className="ml-auto mono text-[10px] text-t4 shrink-0">{scopeHint}</span>
          </div>

          {/* body: the dynamic cap UI for the active scope */}
          {loading ? (
            <div className="py-[16px] px-[18px] text-[11px] italic text-t3">Loading…</div>
          ) : scope === "global" ? (
            // Single total cap
            <div className="flex items-center flex-wrap gap-[12px] py-[16px] px-[18px]">
              <span className="mono text-[10px] tracking-[0.1em] uppercase text-t3">Global total</span>
              <div className="flex items-center h-[38px] rounded-[9px] gap-[8px] px-[12px] bg-bg-2 border border-line-2 focus-within:border-accent-line transition-colors">
                <input
                  value={draft["global"] ?? ""}
                  onChange={(e) => setDraft((d) => ({ ...d, global: e.target.value.replace(/[^0-9]/g, "") }))}
                  placeholder={globalPolicy?.capTokens != null ? fmtTok(globalPolicy.capTokens) : "e.g. 5000000"}
                  inputMode="numeric"
                  className="w-[160px] bg-transparent mono text-[13px] text-t1 outline-none placeholder:text-t4"
                />
              </div>
              <button type="button" onClick={() => void saveCap({ scope: { kind: "global" }, cap_usd: 0 }, "global")}
                disabled={busy || !draft["global"]}
                className="flex items-center h-[36px] rounded-[9px] px-[16px] bg-accent-soft border border-accent-line text-[12.5px] font-semibold text-t1 hover:opacity-90 transition-opacity disabled:opacity-40 disabled:cursor-default disabled:hover:opacity-40">Set total cap</button>
              {globalPolicy && (
                <button type="button" onClick={() => void clearCap({ kind: "global" })} disabled={busy}
                  className="flex items-center h-[36px] rounded-[9px] px-[14px] border border-line-2 text-[12.5px] text-t3 hover:text-t1 transition-colors disabled:opacity-40">Clear</button>
              )}
              {globalPolicy?.capTokens != null && (
                <span className="ml-auto mono text-[11.5px] text-t3">used {fmtTok(globalPolicy.currentTokens)} / {fmtTok(globalPolicy.capTokens)}</span>
              )}
            </div>
          ) : scope === "llm" ? (
            providerTable({
              getUsage: usageGlobal,
              rowId: (prov) => `llm:${prov}`,
              scopeRef: (prov) => ({ kind: "provider", a: prov, b: "*" }),
              capFor: (prov) => capLookup("provider", prov, "*")?.capTokens ?? null,
              pctFor: (prov) => capLookup("provider", prov, "*")?.currentTokenPct ?? null,
            })
          ) : (
            // project / bd / project_llm: the workspace picker lives in the body
            // as its own labelled row; the per-LLM provider table renders below
            // it once a workspace is chosen, else an inline "Pick a …" hint.
            <div className="flex flex-col">
              <div className={cn("flex items-center flex-wrap gap-[10px] py-[16px] px-[18px]", wsKey && "border-b border-line")}>
                <span className="mono text-[10px] tracking-[0.1em] uppercase text-t4 shrink-0">
                  {scope === "project" ? "Project" : scope === "bd" ? "BD" : "Workspace"}
                </span>
                {scope === "project_llm" ? (
                  <select
                    value={wsName}
                    onChange={(e) => setWsName(e.target.value)}
                    className="rounded-[9px] bg-bg-2 border border-line-2 px-[10px] h-[34px] text-[12px] text-t1 min-w-[200px] outline-none focus:border-accent-line"
                  >
                    <option value="">— select —</option>
                    {projects.length > 0 && (
                      <optgroup label="Projects">
                        {projects.map((w) => <option key={`project:${w.name}`} value={`project:${w.name}`}>{w.name}</option>)}
                      </optgroup>
                    )}
                    {bds.length > 0 && (
                      <optgroup label="BD">
                        {bds.map((w) => <option key={`bd:${w.name}`} value={`bd:${w.name}`}>{w.name}</option>)}
                      </optgroup>
                    )}
                  </select>
                ) : (
                  <select
                    value={wsName}
                    onChange={(e) => setWsName(e.target.value)}
                    className="rounded-[9px] bg-bg-2 border border-line-2 px-[10px] h-[34px] text-[12px] text-t1 min-w-[200px] outline-none focus:border-accent-line"
                  >
                    <option value="">— select —</option>
                    {(scope === "project" ? projects : bds).map((w) => <option key={w.name} value={w.name}>{w.name}</option>)}
                  </select>
                )}
                {!wsKey && (
                  <span className="text-[11.5px] text-t4">
                    Pick a {scope === "bd" ? "BD" : scope === "project_llm" ? "workspace" : "project"} to set its per-LLM caps.
                  </span>
                )}
              </div>
              {wsKey && providerTable({
                getUsage: (prov) => usageInWs(wsKey, prov),
                rowId: (prov) => `${wsKey}:${prov}`,
                scopeRef: (prov) => ({ kind: "workspace_provider", a: wsKey, b: prov }),
                capFor: (prov) => capLookup("workspace_provider", wsKey, prov)?.capTokens ?? null,
                pctFor: (prov) => capLookup("workspace_provider", wsKey, prov)?.currentTokenPct ?? null,
              })}
            </div>
          )}
        </Panel>

        {/* ── ACTIVE CAPS ─────────────────────────────────────────────────── */}
        {cappedPolicies.length > 0 && (
          <Panel>
            <PanelHeader
              title="TOKEN BUDGETS"
              meta={`${cappedPolicies.length} active cap${cappedPolicies.length === 1 ? "" : "s"}`}
            />
            {cappedPolicies.map((p) => {
              const pct = p.currentTokenPct;
              const tone = pct == null ? "text-t3" : pct >= p.hardPct ? "text-red" : pct >= p.warnPct ? "text-amber" : "text-t3";
              const dot = pct == null ? "bg-green" : pct >= p.hardPct ? "bg-red" : pct >= p.warnPct ? "bg-amber" : "bg-green";
              return (
                <div key={`${p.scope.kind}:${p.scope.a}:${p.scope.b}`} className="flex items-center gap-[14px] py-[13px] px-[18px] border-b border-line last:border-b-0">
                  <div className="grow min-w-0 flex items-center gap-[10px]">
                    <span className="flex items-center h-[18px] shrink-0 rounded-[5px] px-[7px] border border-line-2 mono text-[9px] tracking-[0.06em] font-bold text-t3">{scopeBadge(p)}</span>
                    <span className="text-[12.5px] font-medium text-t1 truncate">{scopeLabel(p)}</span>
                  </div>
                  <div className="shrink-0 flex items-center gap-[14px]">
                    <span className="w-[120px] text-right mono text-[11.5px] text-t3">{fmtTok(p.currentTokens)} / {fmtTok(p.capTokens ?? 0)}</span>
                    <div className="w-[72px]"><CapBar pct={pct ?? 0} /></div>
                    <span className="w-[88px] flex items-center justify-end gap-[6px]">
                      <span className={cn("size-[6px] shrink-0 rounded-[3px]", dot)} />
                      <span className={cn("text-[11px]", tone)}>{pct != null ? `${pct.toFixed(0)}%` : "—"}</span>
                    </span>
                  </div>
                </div>
              );
            })}
          </Panel>
        )}

        {/* ── USAGE MATRIX ────────────────────────────────────────────────── */}
        {loading ? (
          <div className="text-[11px] italic text-t3">Loading…</div>
        ) : usageErr ? (
          <div className="text-[11px] italic text-t3">Usage unavailable — {usageErr}</div>
        ) : matrix.rows.length === 0 ? (
          <Panel>
            <PanelHeader title="USAGE · PROJECT × LLM" meta="tokens · track + warn (no block) · month to date" />
            <div className="py-[16px] px-[18px] text-[11px] italic text-t3">No LLM usage recorded this month yet.</div>
          </Panel>
        ) : (
          <Panel className="overflow-x-auto">
            <PanelHeader title="USAGE · PROJECT × LLM" meta="tokens · track + warn (no block) · month to date" />
            <table className="w-full">
              <thead>
                <tr className="bg-paper2 border-b border-line">
                  <th className="w-[180px] text-left py-[9px] px-[16px] text-[10px] tracking-[0.06em] font-semibold text-t1">PROJECT</th>
                  {matrix.providers.map((p) => (
                    <th key={p} className="text-left py-[9px] px-[14px] text-[10px] tracking-[0.06em] font-semibold text-t1 uppercase">{p}</th>
                  ))}
                  <th className="w-[96px] text-right py-[9px] px-[16px] text-[10px] tracking-[0.06em] font-semibold text-t1">$ MONTH</th>
                </tr>
              </thead>
              <tbody>
                {matrix.rows.map(({ key, wb }) => (
                  <tr key={key} className="border-b border-line">
                    <td className="w-[180px] py-[11px] px-[16px] text-[12.5px] font-medium text-t1">{wb.workspaceType} · {wb.workspaceName}</td>
                    {matrix.providers.map((p) => {
                      const mb = wb.providers?.[p];
                      const t = mb ? (mb.tokensIn ?? 0) + (mb.tokensOut ?? 0) : 0;
                      return <td key={p} className="py-[11px] px-[14px] mono text-[11px] text-t2">{t ? fmtTok(t) : <span className="text-t4">—</span>}</td>;
                    })}
                    <td className="w-[96px] py-[11px] px-[16px] text-right mono text-[11.5px] text-t1">{fmtUsd(wb.costUsd ?? 0)}</td>
                  </tr>
                ))}
                <tr className="bg-paper2">
                  <td className="w-[180px] py-[11px] px-[16px] text-[12px] font-semibold text-t1">Total</td>
                  {matrix.providers.map((p) => (
                    <td key={p} className="py-[11px] px-[14px] mono text-[11px] font-medium text-t1">
                      <div>{fmtTok(matrixTotals.byProv[p] ?? 0)}</div>
                      <div className="mt-[2px] text-[9.5px] font-normal text-t3">{fmtUsd(matrixTotals.costByProv[p] ?? 0)}</div>
                    </td>
                  ))}
                  <td className="w-[96px] py-[11px] px-[16px] text-right mono text-[11.5px] font-medium text-accent">{fmtUsd(matrixTotals.cost)}</td>
                </tr>
              </tbody>
            </table>
          </Panel>
        )}

        {/* ── SUBSCRIPTION PLANS + BURN ────────────────────────────────────── */}
        {(plans.length > 0 || burnSummary) && (
          <div className="flex gap-[18px]">
            {/* SUBSCRIPTION PLANS — rolling-window plan/credit rows in native units */}
            <Panel className="grow min-w-0">
              <PanelHeader title="SUBSCRIPTION PLANS" meta="rolling windows" />
              <div className="flex flex-col py-[16px] px-[18px] gap-[14px]">
                {plans.length === 0 ? (
                  <div className="text-[11px] italic text-t3">No subscription plans reported.</div>
                ) : plans.map((p) => {
                  const pct = Math.max(0, Math.min(100, (p.usedPct ?? 0) * 100));
                  const fill = pct >= 100 ? "bg-red" : pct >= 80 ? "bg-amber" : "bg-accent";
                  return (
                    <div key={`${p.provider}:${p.planTier}`} className="flex flex-col gap-[7px]">
                      <div className="flex items-baseline justify-between gap-[8px]">
                        <span className="text-[12.5px] leading-[16px] font-medium text-t1">{p.planTier}</span>
                        <span className="mono text-[11px] leading-[16px] text-t3">
                          {fmtPlanUsage(p)} · {fmtReset(p)}
                        </span>
                      </div>
                      <div className="h-[6px] flex overflow-hidden rounded-[3px] shrink-0 bg-paper2">
                        <div className={cn("h-[6px] rounded-[3px] transition-all", fill)} style={{ width: `${pct}%` }} />
                      </div>
                    </div>
                  );
                })}
                {plans.length > 0 && !plans.some((p) => p.resetKind === "monthly") && (
                  <div className="flex items-center pt-[1px] gap-[7px]">
                    <span className="size-[6px] shrink-0 rounded-[3px] bg-amber" />
                    <span className="text-[10.5px] leading-[15px] text-t4">
                      Agent-SDK monthly $-credit — parked (AGENTIC_AGENT_SDK_CREDIT_USD unset).
                    </span>
                  </div>
                )}
              </div>
            </Panel>

            {/* BURN — spend + projected EOD + the per-provider footnote */}
            {burnSummary && (
              <Panel className="w-[300px] shrink-0">
                <PanelHeader title="BURN" meta="month to date" />
                <div className="flex py-[16px] px-[18px] gap-[18px]">
                  <div className="grow basis-0 flex flex-col gap-[4px]">
                    <span className="mono text-[9px] tracking-[0.06em] text-t3">SPENT</span>
                    <span className="text-[21px] leading-[26px] font-semibold text-t1 tabular">{fmtUsd(burnSummary.spend)}</span>
                  </div>
                  <div className="w-px shrink-0 bg-line" />
                  <div className="grow basis-0 flex flex-col gap-[4px]">
                    <span className="mono text-[9px] tracking-[0.06em] text-t3">PROJ. EOM</span>
                    <span className="text-[21px] leading-[26px] font-semibold text-t2 tabular">{fmtUsd(burnSummary.projected)}</span>
                  </div>
                </div>
                <div className="pb-[15px] px-[18px]">
                  <span className="mono text-[10.5px] leading-[15px] text-t3">
                    {fmtInt(burnSummary.calls)} calls · {fmtTok(burnSummary.tokens)} tokens · {burnSummary.provs} provider{burnSummary.provs === 1 ? "" : "s"}
                  </span>
                </div>
              </Panel>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

/** CapBar — thin track-and-fill bar that mirrors a token-cap usage %.
 *  Presentational only; the fill colour steps amber/red as usage climbs. */
function CapBar({ pct }: { pct: number }) {
  const clamped = Math.max(0, Math.min(100, pct));
  const fill = pct >= 100 ? "bg-red" : pct >= 80 ? "bg-amber" : "bg-accent";
  return (
    <div className="h-[6px] w-full max-w-[180px] overflow-hidden rounded-[3px] bg-paper2">
      <div className={cn("h-full rounded-[3px] transition-all", fill)} style={{ width: `${clamped}%` }} />
    </div>
  );
}
