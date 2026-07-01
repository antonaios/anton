// Skill-result → ChatCanvas Message mappers.
//
// Workflow drawer tile clicks (and Cmd-K dispatches) flow through `fire()`
// in App.tsx, which optimistically inserts a `running: true` Anton bubble,
// awaits the workflow result, then swaps the placeholder for one of the
// Messages built below. ONE mapper per wired skill — additions go here.
//
// Percentages: the bridge returns ratio fractions (e.g. `ebitda_margin: 0.347`
// for 34.7%) — verified against /api/workflows/comps?symbol=AAPL on 2026-05-24.
// All `* 100` conversions below are correct against the current provider.

import { api } from "./api";
import type { Message, MessageChip, KPICell } from "../components/ChatCanvas";
import type {
  WorkflowKey, CompsResult, EquityResearchResult, EquityResearchSnapshot,
  RecallResponse,
} from "../types";

const nowHHMM = (): string => new Date().toISOString().slice(11, 16);

/**
 * Dispatch a wired workflow + return a Message ready to swap into a chat
 * placeholder. Returns a `failed: true` Message for "missing arg" cases
 * (e.g. /comps with no ticker) so the caller can keep the same code path
 * for happy / sad results. Bridge errors throw and are caught by fire().
 */
export async function runSkillToMessage(
  key: WorkflowKey,
  promptText: string | undefined,
  fallbackPrompt: string,
): Promise<Message> {
  const arg = (promptText ?? fallbackPrompt ?? "").trim();

  switch (key) {
    case "recall-query":    return recallToMessage(arg);
    case "comps-pull":      return compsToMessage(arg);
    case "company-profile": return equityToMessage(arg);
    case "reindex":         return statusToMessage("Reindex",        () => api.recallIndex(false));
    case "promote-memory":  return statusToMessage("Promote memory", () => api.memoryPromoteRunAll());
    case "newsletter-run":  return statusToMessage("Newsletter run", () => api.sectorNewsRun());
    case "actions-decay":   return actionsDecayToMessage();
    case "bd-decay":        return bdDecayToMessage();
    case "lessons-suggest": return lessonsSuggestToMessage(arg);
    default:
      return failed(`No mapper for "${key}".`);
  }
}

// ── Recall ─────────────────────────────────────────────────────────────────
async function recallToMessage(query: string): Promise<Message> {
  if (!query) return failed("Type a question first.");
  const res: RecallResponse = await api.recall({ query, limit: 10, synthesise: true });
  const body = res.synthesis?.trim()
    || (res.hits.length === 0 ? "0 hits." : `${res.hits.length} hits — no synthesis returned.`);
  // Top-6 hits as filename chips; click opens the source file (file:// scheme).
  const chips: MessageChip[] = res.hits.slice(0, 6).map((h) => ({
    label: filenameOf(h.path),
    action: { type: "open-file", path: h.path },
  }));
  return {
    id: "placeholder",
    role: "anton",
    who: "ANTON",
    time: nowHHMM(),
    body,
    chips: chips.length ? chips : undefined,
    lane: "skill",
    route: "ROUTED · LOCAL OLLAMA → RECALL",
  };
}

// ── Ticker multiples ───────────────────────────────────────────────────────
// Tile relabeled "Ticker multiples" 2026-06-01 to disambiguate from the new
// `comps` deliverable SKILL (research-pipeline; see COMPS-REDESIGN-2026-06-01).
// Route key `comps-pull` and backend `/api/workflows/comps` are unchanged.
async function compsToMessage(ticker: string): Promise<Message> {
  if (!ticker) return failed("Type a ticker first.");
  const res: CompsResult = await api.compsPull({
    symbol: ticker.toUpperCase(),
    peers_limit: 8,
    years: 5,
    write_note: true,
  });
  const peerCount = res.rows.length;
  const kpis = kpisFromComps(res);
  const sym = res.target_symbol;
  const chips: MessageChip[] = [];
  if (res.note_path) chips.push({
    label: "Open in Excel", primary: true,
    action: { type: "open-file", path: res.note_path },
  });
  if (res.note_path) chips.push({
    label: "Vault note saved",
    action: { type: "open-file", path: res.note_path },
  });
  if (res.warnings.length) chips.push({
    label: `${res.warnings.length} warning${res.warnings.length === 1 ? "" : "s"}`,
    action: { type: "show-modal", modalId: `comps-warnings-${sym}` },
  });

  return {
    id: "placeholder",
    role: "anton",
    who: "ANTON",
    time: nowHHMM(),
    body: `Ticker multiples · ${res.target_symbol}${res.target_name ? " · " + res.target_name : ""} · ${peerCount} row${peerCount === 1 ? "" : "s"}`,
    kpis: kpis.length ? kpis : undefined,
    chips: chips.length ? chips : undefined,
    lane: "skill",
    route: `ROUTED · MARKETS → ${res.target_symbol}`,
  };
}

/** Compute medians client-side from the peer rows. KPICard is 3-col; cap at 6. */
function kpisFromComps(r: CompsResult): KPICell[] {
  const cells: KPICell[] = [];
  const evEbitda = median(r.rows.map((row) => row.ev_ebitda));
  if (evEbitda !== null) cells.push({ label: "EV / EBITDA · median", value: evEbitda.toFixed(1), unit: "x" });
  const pe = median(r.rows.map((row) => row.pe));
  if (pe !== null) cells.push({ label: "P / E · median", value: pe.toFixed(1), unit: "x" });
  const margin = median(r.rows.map((row) => row.ebitda_margin));
  if (margin !== null) cells.push({ label: "EBITDA margin · median", value: (margin * 100).toFixed(1), unit: "%" });
  const ndEbitda = median(r.rows.map((row) => row.net_debt_ebitda));
  if (ndEbitda !== null) cells.push({ label: "Net debt / EBITDA · median", value: ndEbitda.toFixed(1), unit: "x" });
  const divYld = median(r.rows.map((row) => row.dividend_yield));
  if (divYld !== null) cells.push({ label: "Dividend yield · median", value: (divYld * 100).toFixed(2), unit: "%" });
  return cells.slice(0, 6);
}

// ── Equity research (company profile) ──────────────────────────────────────
async function equityToMessage(ticker: string): Promise<Message> {
  if (!ticker) return failed("Type a ticker first.");
  const res: EquityResearchResult = await api.equityResearch({
    symbol: ticker.toUpperCase(),
    years: 5,
    peers_limit: 6,
    news_days: 14,
    news_limit: 12,
    write_note: true,
  });
  const snap = res.snapshot;
  const priceFrag = snap.last_price
    ? `${snap.last_price}${snap.price_change ? " (" + snap.price_change + ")" : ""}`
    : "—";
  const sym = res.target_symbol;
  const chips: MessageChip[] = [];
  if (res.note_path) chips.push({
    label: "Open in Excel", primary: true,
    action: { type: "open-file", path: res.note_path },
  });
  if (res.news.items.length) chips.push({
    label: `News · ${res.news.items.length}`,
    action: { type: "show-modal", modalId: `news-${sym}` },
  });
  if (res.comps.rows.length) chips.push({
    label: `Comps · ${res.comps.rows.length}`,
    action: { type: "show-modal", modalId: `comps-${sym}` },
  });
  if (res.warnings.length) chips.push({
    label: `${res.warnings.length} warning${res.warnings.length === 1 ? "" : "s"}`,
    action: { type: "show-modal", modalId: `equity-warnings-${sym}` },
  });

  return {
    id: "placeholder",
    role: "anton",
    who: "ANTON",
    time: nowHHMM(),
    body: `${res.target_symbol}${snap.name ? " · " + snap.name : ""} · ${priceFrag}`,
    kpis: kpisFromSnapshot(snap),
    chips: chips.length ? chips : undefined,
    lane: "skill",
    route: `ROUTED · MARKETS → ${res.target_symbol}`,
  };
}

function kpisFromSnapshot(s: EquityResearchSnapshot): KPICell[] | undefined {
  const cells: KPICell[] = [];
  if (typeof s.ev_ebitda === "number")              cells.push({ label: "EV / EBITDA",          value: s.ev_ebitda.toFixed(1),                       unit: "x" });
  if (typeof s.pe === "number")                     cells.push({ label: "P / E",                value: s.pe.toFixed(1),                              unit: "x" });
  if (typeof s.ebitda_margin === "number")          cells.push({ label: "EBITDA margin",        value: (s.ebitda_margin * 100).toFixed(1),           unit: "%" });
  if (typeof s.dividend_yield === "number")         cells.push({ label: "Dividend yield",       value: (s.dividend_yield * 100).toFixed(2),          unit: "%" });
  if (typeof s.revenue_growth_5y_cagr === "number") cells.push({ label: "Rev growth · 5y CAGR", value: (s.revenue_growth_5y_cagr * 100).toFixed(1),  unit: "%" });
  return cells.length ? cells.slice(0, 6) : undefined;
}

// ── Status-only skills (reindex / promote / newsletter) ────────────────────
async function statusToMessage(
  label: string,
  call: () => Promise<{ status: string; pid?: number }>,
): Promise<Message> {
  const res = await call();
  return {
    id: "placeholder",
    role: "anton",
    who: "ANTON",
    time: nowHHMM(),
    body: `${label} started: ${res.status}${res.pid ? ` (pid ${res.pid})` : ""}`,
    lane: "skill",
  };
}

// ── Decay sweeps + lessons-suggest (#front-door — on-demand tiles) ──────────
async function actionsDecayToMessage(): Promise<Message> {
  const res = await api.actionsDecay();
  const c = res.counts;
  return {
    id: "placeholder", role: "anton", who: "ANTON", time: nowHHMM(),
    body: `Actions decay · ${c.overdue} overdue · ${c.stale} stale across ${c.projects_scanned} project${c.projects_scanned === 1 ? "" : "s"}`,
    lane: "skill",
    route: "ROUTED · LOCAL · ACTIONS DECAY",
  };
}

async function bdDecayToMessage(): Promise<Message> {
  const res = await api.bdDecay();
  const c = res.counts;
  return {
    id: "placeholder", role: "anton", who: "ANTON", time: nowHHMM(),
    body: `BD decay · ${c.stale} stale / ${c.scanned} scanned · ${c.fresh} fresh`,
    lane: "skill",
    route: "ROUTED · LOCAL · BD DECAY",
  };
}

async function lessonsSuggestToMessage(project: string): Promise<Message> {
  if (!project) return failed("Name a project or sector first — e.g. /lessons FALCON.");
  const res = await api.lessonsSuggest(project);
  const c = res.counts;
  return {
    id: "placeholder", role: "anton", who: "ANTON", time: nowHHMM(),
    body: c.returned > 0
      ? `Lessons for "${project}" — ${c.returned} of ${c.total_entries} register entries:\n${res.bullets}`
      : `Lessons · no matching register entries for "${project}".`,
    lane: "skill",
    route: "ROUTED · LOCAL · LESSONS",
  };
}

// ── Helpers ────────────────────────────────────────────────────────────────
function failed(body: string): Message {
  return {
    id: "placeholder",
    role: "anton",
    who: "ANTON",
    time: nowHHMM(),
    body,
    failed: true,
  };
}

function filenameOf(p: string): string {
  return p.split(/[/\\]/).pop() || p;
}

function median(xs: (number | null | undefined)[]): number | null {
  const ns = xs
    .filter((n): n is number => typeof n === "number" && Number.isFinite(n))
    .sort((a, b) => a - b);
  if (ns.length === 0) return null;
  const mid = Math.floor(ns.length / 2);
  return ns.length % 2 ? ns[mid] : (ns[mid - 1] + ns[mid]) / 2;
}
