import { useCallback, useEffect, useRef, useState } from "react";
import { Plus, X } from "lucide-react";
import { api, ApiError } from "../lib/api";
import { cn } from "../lib/cn";
import type {
  OperatorConfigResponse,
  OperatorCoverageRow,
  OperatorFileInfo,
  OperatorMacroRow,
  OperatorTickerRow,
  Quote,
} from "../types";

/**
 * #operator-tab · OPERATOR tab — edits the `_claude/` dashboard-config file
 * family IN PLACE via GET/PUT /api/operator/config. The vault file stays the
 * single source of truth (Obsidian round-trip preserved); every save carries
 * the mtime token from the last load and a 409 (mid-flight Obsidian edit)
 * surfaces as a "changed on disk" banner — the operator's draft is KEPT
 * across the reload so it can be reviewed + re-saved against the fresh
 * token. Never a silent clobber, never a silently discarded draft.
 *
 * Six cards: Banners · Earnings watchlist · Expertise sectors · News
 * coverage · Profile · Providers & keys (STATUS-only in v1 — key entry is
 * the agreed v2, via the encrypted credentials store, never the vault).
 */

// ── module-level dirty registry — App's tab-switch guard reads this.
// Cleared on every OperatorTab mount so stale entries from exceptional
// unmount paths / HMR can't wedge the guard (codex fe-review #2).
const dirtyCards = new Set<string>();
export function hasUnsavedOperatorEdits(): boolean {
  return dirtyCards.size > 0;
}

// Stable client-side row ids — editable tables must not use index keys
// (React reuses input instances on remove/reorder and values bleed into
// neighbouring rows; codex fe-review #3). Stripped before PUT.
let nextRowId = 1;
const newId = () => `r${nextRowId++}`;

type WithId<T> = T & { __id: string };
const withIds = <T,>(rows: T[]): WithId<T>[] =>
  rows.map((r) => ({ ...r, __id: newId() }));
const stripIds = <T,>(rows: WithId<T>[]): T[] =>
  rows.map(({ __id: _ignored, ...r }) => r as T);

const MACRO_KINDS = ["equity", "index", "commodity", "rate", "indicator"] as const;

function obsidianHref(vaultRelativePath: string): string {
  return `obsidian://open?vault=OS%20AI%20Vault&file=${encodeURIComponent(vaultRelativePath)}`;
}

function fmtStamp(info: OperatorFileInfo | undefined): string {
  if (!info?.exists) return "not created yet";
  if (!info.mtime_iso) return "";
  return `modified ${new Date(info.mtime_iso).toLocaleString()}`;
}

// ── small shared atoms ─────────────────────────────────────────────────────

// Soft-pill status chip — matches the Paper Operator detail screens (FULL /
// PARTIAL / MISSING, STORE / ENV, live-quote price). Token-only tints so it
// flips between the light-teal and dark-navy themes.
function Chip(props: { tone: "ok" | "live" | "warn" | "off"; children: React.ReactNode; title?: string }) {
  const cls =
    props.tone === "ok" ? "text-green border-green/40 bg-green/15"
    : props.tone === "live" ? "text-ok-bright border-ok-bright/40 bg-ok-bright/10"
    : props.tone === "warn" ? "text-amber border-amber/45 bg-amber/15"
    : "text-t3 border-line bg-bg-2";
  return (
    <span
      title={props.title}
      className={cn(
        "inline-flex items-center rounded-md border px-[8px] py-[2px] text-[9.5px] font-semibold uppercase tracking-[0.06em] leading-[16px] whitespace-nowrap",
        cls,
      )}
    >
      {props.children}
    </span>
  );
}

// ── landing summary cards (Paper 913-0) ────────────────────────────────────
//
// The Operator screen opens on a 2-col grid of summary tiles — each tile shows
// a one-line description + a real, data-derived stat footer and an Edit ›/
// Manage › affordance that drills into the full editor card below. The editor
// cards (BannersCard … CredentialsCard) are unchanged: every hook, the
// dirtyCards registry, useCardSave, putOperatorConfig and the credential
// arm-to-confirm stay byte-identical — only the navigation chrome is new.

type SectionKey =
  | "banners"
  | "watchlist"
  | "sectors"
  | "coverage"
  | "profile"
  | "credentials";

// Lock glyph for the Providers & keys tile — matches the Paper inline SVG.
function LockGlyph() {
  return (
    <svg
      width="13"
      height="13"
      viewBox="0 0 14 14"
      xmlns="http://www.w3.org/2000/svg"
      className="shrink-0"
      aria-hidden="true"
    >
      <rect x="2.5" y="6" width="9" height="6" rx="1.5" fill="none" stroke="currentColor" strokeWidth="1.3" />
      <path d="M4.5 6V4.5a2.5 2.5 0 0 1 5 0V6" fill="none" stroke="currentColor" strokeWidth="1.3" />
    </svg>
  );
}

function SummaryCard(props: {
  title: string;
  action: string;
  description: string;
  onOpen: () => void;
  lock?: boolean;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={props.onOpen}
      className="flex flex-1 basis-[460px] min-w-[420px] cursor-pointer flex-col gap-[11px] rounded-[12px] border border-line bg-bg-1 px-[18px] py-[16px] text-left shadow-card transition-colors hover:border-accent-line"
    >
      <div className="flex items-center justify-between gap-[10px]">
        <div className="flex items-center gap-[8px]">
          {props.lock && <span className="text-t2"><LockGlyph /></span>}
          <span className="text-[14px] font-semibold leading-[18px] text-t1">{props.title}</span>
        </div>
        <span className="text-[11.5px] leading-[14px] text-accent">{props.action} ›</span>
      </div>
      <div className="text-[12.5px] leading-[150%] text-t2">{props.description}</div>
      {props.children}
    </button>
  );
}

// Soft-pill sector chip on the Expertise-sectors tile (Paper paper2 fill).
function SectorPill(props: { children: React.ReactNode }) {
  return (
    <span className="flex h-[22px] items-center rounded-[6px] bg-paper2 px-[9px] text-[11px] leading-[14px] text-t2">
      {props.children}
    </span>
  );
}

// One mono stat line under a tile (e.g. "12 equity peers · 6 macro").
function StatLine(props: { children: React.ReactNode }) {
  return <div className="mono text-[11px] leading-[15px] text-t3">{props.children}</div>;
}

// Title-adjacent count pill (Paper D6P-0 "4 ACTIVE") — a quiet bordered chip,
// distinct from the uppercase status `Chip` atom: mixed-source small-caps via
// `uppercase`, 10px / 600 / tracking 0.04em / --slate on a --line2 outline.
function CountChip(props: { children: React.ReactNode; title?: string }) {
  return (
    <span
      title={props.title}
      className="inline-flex h-[20px] shrink-0 items-center rounded-[6px] border border-line-2 px-[8px] text-[10px] font-semibold uppercase leading-[12px] tracking-[0.04em] text-t2"
    >
      {props.children}
    </span>
  );
}

function CardShell(props: {
  title: string;
  file?: OperatorFileInfo;
  dirty: boolean;
  dirtyLabel?: string;
  justSaved?: boolean;
  busy: boolean;
  error: string | null;
  conflict: boolean;
  onSave: () => void;
  onReload: () => void;
  children: React.ReactNode;
  headerExtra?: React.ReactNode;
}) {
  return (
    <section className="flex flex-col gap-[18px]">
      <div className="flex items-start justify-between gap-[20px]">
        <div className="flex min-w-0 items-center gap-[10px]">
          <h3 className="text-[23px] font-semibold leading-[27px] text-t1">{props.title}</h3>
          {props.headerExtra}
          {props.dirty && (
            <Chip tone="warn" title="Edits not yet written to the vault file">
              {props.dirtyLabel ?? "unsaved"}
            </Chip>
          )}
          {props.justSaved && (
            <Chip tone="ok" title="Written to the vault file">
              ✓ saved
            </Chip>
          )}
        </div>
        <div className="flex shrink-0 items-center gap-[14px] pt-[6px]">
          {props.file && (
            <>
              <span className="mono text-[11px] leading-[14px] text-t3">{fmtStamp(props.file)}</span>
              <a
                href={obsidianHref(props.file.path)}
                className="text-[11px] leading-[14px] text-t3 underline decoration-dotted underline-offset-2 transition-colors hover:text-t1"
                title={`Open ${props.file.path} in Obsidian`}
              >
                open in Obsidian
              </a>
            </>
          )}
          <button
            type="button"
            disabled={!props.dirty || props.busy}
            onClick={props.onSave}
            className={cn(
              "rounded-lg border px-[16px] py-[7px] text-[12px] font-semibold tracking-[0.04em] leading-[16px] transition-colors",
              props.dirty && !props.busy
                ? "cursor-pointer border-accent-line bg-accent-soft text-accent hover:bg-accent/10"
                : "cursor-default border-line text-t4",
            )}
          >
            {props.busy ? "Saving…" : "Save"}
          </button>
        </div>
      </div>
      {props.conflict && (
        <div className="flex items-center gap-[12px] rounded-[10px] border border-accent-line bg-accent-soft px-[16px] py-[10px] text-[12px] text-accent">
          <span className="flex-1">
            File changed on disk (Obsidian edit mid-flight). Reload picks up the disk version —
            your edits stay in the form; review and Save again.
          </span>
          <button
            type="button"
            onClick={props.onReload}
            className="shrink-0 rounded-lg border border-accent-line px-[12px] py-[5px] text-[11px] font-medium uppercase tracking-[0.06em] transition-colors hover:bg-accent/10"
          >
            Reload
          </button>
        </div>
      )}
      {props.error && !props.conflict && (
        <div className="rounded-[10px] border border-red/40 bg-red/10 px-[16px] py-[10px] text-[12px] text-red">{props.error}</div>
      )}
      <div>{props.children}</div>
    </section>
  );
}

// Shared row-input chrome — matches Field/FieldBox (bg-bg-2 inset, line-2
// hairline, accent-line focus, rounded-lg) so the editable tables read as the
// same idiom as the rest of v5.
const inputCls =
  "w-full rounded-lg border border-line-2 bg-bg-1 px-[10px] py-[7px] text-[12.5px] text-t1 outline-none transition-colors placeholder:text-t4 focus:border-accent-line";

// v2 table-CARD chrome (Paper CM5-0 / D6P-0) — the editable tables sit in a
// bordered card with a bg-paper2 column-header row. `tableCardCls` wraps the
// <table>; `theadCls` tints the header row; `thCls` is the 10px/0.08em
// uppercase column label; `rowCls` draws the per-row hairline (last:border-0
// drops it on the final row, matching the Paper artboard).
const tableCardCls = "overflow-hidden rounded-[12px] border border-line bg-bg-1";
const theadCls = "bg-paper2 border-b border-line";
const thCls = "px-[14px] py-[9px] text-left text-[10px] font-semibold uppercase tracking-[0.08em] text-t1";
const rowCls = "border-b border-line last:border-0";

function RemoveBtn(props: { onClick: () => void; label?: string }) {
  return (
    <button
      type="button"
      onClick={props.onClick}
      aria-label={props.label ?? "Remove row"}
      title={props.label ?? "Remove row"}
      className="flex h-[28px] w-[28px] shrink-0 items-center justify-center rounded-lg text-t3 transition-colors hover:bg-red/10 hover:text-red"
    >
      <X size={14} />
    </button>
  );
}

function AddBtn(props: { onClick: () => void; children: React.ReactNode }) {
  return (
    <button
      type="button"
      onClick={props.onClick}
      className="mt-[10px] inline-flex items-center gap-[5px] rounded-lg border border-line-2 px-[12px] py-[7px] text-[11.5px] text-t2 transition-colors hover:border-accent-line hover:text-t1"
    >
      <Plus size={13} className="shrink-0" />
      {props.children}
    </button>
  );
}

// ── card save plumbing (shared hook) ───────────────────────────────────────
//
// Reset protocol (codex fe-review rounds 1+2): every card listens to the
// shared `version`, but a reload initiated ANYWHERE (another card's save,
// a conflict Reload) must not discard a dirty card's draft. The card's
// version-reset effect therefore: (a) force-resets when THIS card just
// saved successfully (forceResetRef — refreshes the draft to the
// just-written, normalised server state); (b) keeps the draft when the
// card is dirty against the fresh baseline (covers both conflict-Reload
// on this card and sibling-triggered reloads); (c) resets clean cards.
// `guardDirty` is what registers in the tab-switch guard — callers pass
// actual operator edits, not save-enablement (a synthesised coverage
// list is saveable but not an edit to protect).

function useCardSave(cardKey: string, guardDirty: boolean, reload: () => Promise<boolean>) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [conflict, setConflict] = useState(false);
  // Transient "✓ saved" confirmation — set true on a successful save, then
  // auto-cleared after ~2.2s (Paper Operator header flash). Also cleared the
  // moment a fresh edit re-dirties the card, so a stale tick never lingers
  // beside a new "unsaved" chip.
  const [justSaved, setJustSaved] = useState(false);
  const savedTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const forceResetRef = useRef(false);

  const clearSavedTimer = () => {
    if (savedTimerRef.current) {
      clearTimeout(savedTimerRef.current);
      savedTimerRef.current = null;
    }
  };

  useEffect(() => {
    if (guardDirty) dirtyCards.add(cardKey);
    else dirtyCards.delete(cardKey);
    return () => { dirtyCards.delete(cardKey); };
  }, [cardKey, guardDirty]);

  // A new edit (card goes dirty) drops the confirmation chip immediately.
  useEffect(() => {
    if (guardDirty && justSaved) {
      clearSavedTimer();
      setJustSaved(false);
    }
  }, [guardDirty, justSaved]);

  // Clear the pending timer on unmount so it can't fire after teardown.
  useEffect(() => clearSavedTimer, []);

  const save = async (section: string, expectedMtime: string | null, data: Record<string, unknown>) => {
    setBusy(true);
    setError(null);
    setConflict(false);
    try {
      await api.putOperatorConfig(section, { expected_mtime: expectedMtime, data });
      // Arm the force-reset ONLY for the immediately-following successful
      // reload (codex fe round 3): load() never throws — it reports
      // success — so a failed refetch must disarm, or a later unrelated
      // version bump would force-reset over the operator's newer edits.
      forceResetRef.current = true;
      const refreshed = await reload();
      if (!refreshed) forceResetRef.current = false;
      // Flash the confirmation chip on a clean save + refetch.
      clearSavedTimer();
      setJustSaved(true);
      savedTimerRef.current = setTimeout(() => {
        setJustSaved(false);
        savedTimerRef.current = null;
      }, 2200);
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) setConflict(true);
      else setError(e instanceof ApiError ? `${e.status}: ${e.message}` : String(e));
    } finally {
      setBusy(false);
    }
  };

  const onReload = async () => {
    setConflict(false);
    setError(null);
    await reload();
  };

  return { busy, error, conflict, justSaved, save, onReload, forceResetRef };
}

/** Shared version-reset rule — see useCardSave docstring. Mutates the ref. */
function shouldResetDraft(
  forceResetRef: React.MutableRefObject<boolean>,
  dirtyVsFreshBaseline: boolean,
): boolean {
  if (forceResetRef.current) {
    forceResetRef.current = false;
    return true;
  }
  return !dirtyVsFreshBaseline;
}

// ── Banners card ───────────────────────────────────────────────────────────

function BannersCard(props: {
  loaded: OperatorConfigResponse["sections"]["banners"];
  file: OperatorFileInfo;
  reload: () => Promise<boolean>;
  version: number;
}) {
  const [tickerRows, setTickerRows] = useState<WithId<OperatorTickerRow>[]>(() => withIds(props.loaded.ticker_bar));
  const [macroRows, setMacroRows] = useState<WithId<OperatorMacroRow>[]>(() => withIds(props.loaded.macro_bar));
  const [quotes, setQuotes] = useState<Record<string, Quote | null>>({});
  const [quotesBusy, setQuotesBusy] = useState(false);

  const dirty =
    JSON.stringify({ t: stripIds(tickerRows), m: stripIds(macroRows) })
    !== JSON.stringify({ t: props.loaded.ticker_bar, m: props.loaded.macro_bar });

  const { busy, error, conflict, justSaved, save, onReload, forceResetRef } = useCardSave("banners", dirty, props.reload);

  // Reset drafts when a fresh GET lands — unless this card has live edits
  // (a sibling card's save, or a conflict-Reload, must not discard them).
  useEffect(() => {
    if (!shouldResetDraft(forceResetRef, dirty)) return;
    setTickerRows(withIds(props.loaded.ticker_bar));
    setMacroRows(withIds(props.loaded.macro_bar));
    setQuotes({});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [props.version]);

  // Live-preview chip: fetch quotes for the DRAFT symbols so a typo'd
  // symbol is visible before save (scope §3 card 1).
  const previewQuotes = async () => {
    const syms = [...tickerRows, ...macroRows.filter((m) => m.kind !== "rate" && m.kind !== "indicator")]
      .map((r) => r.symbol.trim().toUpperCase())
      .filter(Boolean);
    if (!syms.length) return;
    setQuotesBusy(true);
    try {
      const r = await api.marketsQuotes([...new Set(syms)].slice(0, 20));
      const map: Record<string, Quote | null> = {};
      for (const s of syms) map[s] = r.quotes.find((q) => q.symbol === s) ?? null;
      setQuotes(map);
    } catch {
      setQuotes({});
    } finally {
      setQuotesBusy(false);
    }
  };

  // Live-quote chip — Paper-matched compact tints (CM5-0): green mono price /
  // amber "no quote". Mixed-case, h-[18px] rounded-[5px], distinct from the
  // uppercase status `Chip` atom.
  const quoteChip = (symbol: string) => {
    const key = symbol.trim().toUpperCase();
    if (!(key in quotes)) return null;
    const q = quotes[key];
    return q ? (
      <span
        title={`${q.name} — live quote resolves`}
        className="mono inline-flex h-[18px] items-center rounded-[5px] border border-green/40 bg-green/15 px-[6px] text-[10px] leading-[12px] text-green"
      >
        {q.price}
      </span>
    ) : (
      <span
        title="No quote returned — check the symbol"
        className="inline-flex h-[18px] items-center rounded-[5px] border border-amber/45 bg-amber/15 px-[6px] text-[9px] font-semibold leading-[12px] text-amber"
      >
        no quote
      </span>
    );
  };

  // Amber-flag a symbol input whose previewed quote came back empty (Paper
  // CM5-0 GX0). Only flags AFTER a preview that returned no quote — never on
  // an un-previewed or unresolved symbol.
  const symbolInputCls = (symbol: string) => {
    const key = symbol.trim().toUpperCase();
    const flagged = key in quotes && quotes[key] === null;
    return cn(inputCls, flagged && "border-amber focus:border-amber");
  };

  const setTicker = (id: string, patch: Partial<OperatorTickerRow>) =>
    setTickerRows((rows) => rows.map((r) => (r.__id === id ? { ...r, ...patch } : r)));
  const setMacro = (id: string, patch: Partial<OperatorMacroRow>) =>
    setMacroRows((rows) => rows.map((r) => (r.__id === id ? { ...r, ...patch } : r)));

  return (
    <CardShell
      title="Banners"
      file={props.file}
      dirty={dirty}
      justSaved={justSaved}
      busy={busy}
      error={error}
      conflict={conflict}
      onReload={() => void onReload()}
      onSave={() => void save("banners", props.file.exists ? props.file.mtime : null, {
        ticker_bar: stripIds(tickerRows).map((r) => ({ symbol: r.symbol.trim().toUpperCase(), name: (r.name ?? "").trim() })),
        macro_bar: stripIds(macroRows).map((r) => ({ symbol: r.symbol.trim().toUpperCase(), name: (r.name ?? "").trim(), kind: r.kind })),
      })}
      headerExtra={
        <button
          type="button"
          onClick={() => void previewQuotes()}
          disabled={quotesBusy}
          className="rounded-lg border border-line-2 px-[11px] py-[5px] text-[11px] text-t2 transition-colors hover:border-accent-line hover:text-t1 disabled:opacity-50"
          title="Fetch a live quote for each drafted symbol — catches typos before save"
        >
          {quotesBusy ? "checking…" : "preview quotes"}
        </button>
      }
    >
      <p className="mb-[16px] max-w-[760px] text-[13px] leading-[150%] text-t2">
        The equity + macro tickers in the top bar. Each drafted symbol is checked against a
        live quote before save, so a typo surfaces here first.
      </p>
      {props.loaded.issues.length > 0 && (
        <ul className="mb-[12px] flex flex-col gap-[3px] text-[11.5px] text-accent">
          {props.loaded.issues.map((i) => <li key={i}>· {i}</li>)}
        </ul>
      )}
      <div className="grid grid-cols-2 gap-[28px]">
        <div>
          <div className="mb-[8px] text-[10.5px] font-semibold uppercase leading-[14px] tracking-[0.08em] text-t2">Spark tickers · top bar · {tickerRows.length} of 10</div>
          <div className={tableCardCls}>
            <table className="w-[460px] border-collapse [&_td]:first:pl-[14px] [&_td]:last:pr-[14px] [&_th]:first:pl-[14px] [&_th]:last:pr-[14px]">
              <thead className={theadCls}><tr><th className={thCls}>Symbol</th><th className={thCls}>Name</th><th className={cn(thCls, "text-right")}>Live</th><th className={thCls} /></tr></thead>
              <tbody>
                {tickerRows.map((r) => (
                  <tr key={r.__id} className={rowCls}>
                    <td className="w-[110px] py-[7px] pr-[8px]"><input className={symbolInputCls(r.symbol)} value={r.symbol} onChange={(e) => setTicker(r.__id, { symbol: e.target.value })} /></td>
                    <td className="py-[7px] pr-[8px]"><input className={inputCls} value={r.name ?? ""} onChange={(e) => setTicker(r.__id, { name: e.target.value })} /></td>
                    <td className="py-[7px] pr-[8px] text-right">{quoteChip(r.symbol)}</td>
                    <td className="py-[7px]"><RemoveBtn onClick={() => setTickerRows((rows) => rows.filter((x) => x.__id !== r.__id))} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {tickerRows.length < 10 && <AddBtn onClick={() => setTickerRows((r) => [...r, { __id: newId(), symbol: "", name: "" }])}>ticker</AddBtn>}
        </div>
        <div>
          <div className="mb-[8px] text-[10.5px] font-semibold uppercase leading-[14px] tracking-[0.08em] text-t2">Macro row · bottom bar · {macroRows.length} of 10</div>
          <div className={tableCardCls}>
            <table className="w-[560px] border-collapse [&_td]:first:pl-[14px] [&_td]:last:pr-[14px] [&_th]:first:pl-[14px] [&_th]:last:pr-[14px]">
              <thead className={theadCls}><tr><th className={thCls}>Symbol</th><th className={thCls}>Name</th><th className={thCls}>Kind</th><th className={cn(thCls, "text-right")}>Live</th><th className={thCls} /></tr></thead>
              <tbody>
                {macroRows.map((r) => (
                  <tr key={r.__id} className={rowCls}>
                    <td className="w-[110px] py-[7px] pr-[8px]"><input className={symbolInputCls(r.symbol)} value={r.symbol} onChange={(e) => setMacro(r.__id, { symbol: e.target.value })} /></td>
                    <td className="py-[7px] pr-[8px]"><input className={inputCls} value={r.name ?? ""} onChange={(e) => setMacro(r.__id, { name: e.target.value })} /></td>
                    <td className="w-[120px] py-[7px] pr-[8px]">
                      <select className={inputCls} value={r.kind} onChange={(e) => setMacro(r.__id, { kind: e.target.value as OperatorMacroRow["kind"] })}>
                        {MACRO_KINDS.map((k) => <option key={k} value={k}>{k}</option>)}
                      </select>
                    </td>
                    <td className="py-[7px] pr-[8px] text-right">{quoteChip(r.symbol)}</td>
                    <td className="py-[7px]"><RemoveBtn onClick={() => setMacroRows((rows) => rows.filter((x) => x.__id !== r.__id))} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {macroRows.length < 10 && <AddBtn onClick={() => setMacroRows((r) => [...r, { __id: newId(), symbol: "", name: "", kind: "index" }])}>macro entry</AddBtn>}
          <p className="mt-[10px] text-[10.5px] text-t4">rate / indicator kinds accept only the synthetic ids (UK_3M · UK_10Y · UK_SONIA · UK_CPI).</p>
        </div>
      </div>
    </CardShell>
  );
}

// ── Watchlist card ─────────────────────────────────────────────────────────

function WatchlistCard(props: {
  loaded: OperatorConfigResponse["sections"]["watchlist"];
  file: OperatorFileInfo;
  reload: () => Promise<boolean>;
  version: number;
}) {
  const [rows, setRows] = useState<WithId<OperatorTickerRow>[]>(() => withIds(props.loaded.earnings_watchlist));

  const dirty = JSON.stringify(stripIds(rows)) !== JSON.stringify(props.loaded.earnings_watchlist);
  const { busy, error, conflict, justSaved, save, onReload, forceResetRef } = useCardSave("watchlist", dirty, props.reload);

  useEffect(() => {
    if (!shouldResetDraft(forceResetRef, dirty)) return;
    setRows(withIds(props.loaded.earnings_watchlist));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [props.version]);

  const set = (id: string, patch: Partial<OperatorTickerRow>) =>
    setRows((rs) => rs.map((r) => (r.__id === id ? { ...r, ...patch } : r)));

  return (
    <CardShell
      title="Earnings watchlist"
      file={props.file}
      dirty={dirty}
      justSaved={justSaved}
      busy={busy}
      error={error}
      conflict={conflict}
      onReload={() => void onReload()}
      onSave={() => void save("watchlist", props.file.exists ? props.file.mtime : null, {
        earnings_watchlist: stripIds(rows).map((r) => ({
          symbol: r.symbol.trim().toUpperCase(),
          ...(r.name?.trim() ? { name: r.name.trim() } : {}),
        })),
      })}
    >
      <p className="mb-[16px] max-w-[640px] text-[13px] leading-[150%] text-t2">
        Public companies the earnings tracker auto-pulls (daily 07:30 sweep). Max 30; symbol must be a
        public ticker yfinance recognises.
      </p>
      {props.loaded.issues.length > 0 && (
        <ul className="mb-[12px] flex flex-col gap-[3px] text-[11.5px] text-accent">{props.loaded.issues.map((i) => <li key={i}>· {i}</li>)}</ul>
      )}
      <div className={cn(tableCardCls, "w-[460px]")}>
        <table className="w-full border-collapse [&_td]:first:pl-[14px] [&_td]:last:pr-[14px] [&_th]:first:pl-[14px] [&_th]:last:pr-[14px]">
          <thead className={theadCls}><tr><th className={thCls}>Symbol</th><th className={thCls}>Name · optional</th><th className={thCls} /></tr></thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.__id} className={rowCls}>
                <td className="w-[110px] py-[7px] pr-[8px]"><input className={inputCls} value={r.symbol} onChange={(e) => set(r.__id, { symbol: e.target.value })} /></td>
                <td className="py-[7px] pr-[8px]"><input className={inputCls} value={r.name ?? ""} onChange={(e) => set(r.__id, { name: e.target.value })} /></td>
                <td className="py-[7px]"><RemoveBtn onClick={() => setRows((rs) => rs.filter((x) => x.__id !== r.__id))} /></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {rows.length < 30 && <AddBtn onClick={() => setRows((r) => [...r, { __id: newId(), symbol: "", name: "" }])}>company</AddBtn>}
    </CardShell>
  );
}

// ── Expertise sectors card ─────────────────────────────────────────────────

function SectorsCard(props: {
  loaded: OperatorConfigResponse["sections"]["sectors"];
  file: OperatorFileInfo;
  reload: () => Promise<boolean>;
  version: number;
}) {
  const [rows, setRows] = useState<WithId<{ value: string }>[]>(
    () => withIds(props.loaded.active_sectors.map((s) => ({ value: s }))),
  );

  const values = rows.map((r) => r.value);
  const dirty = JSON.stringify(values) !== JSON.stringify(props.loaded.active_sectors);
  const { busy, error, conflict, justSaved, save, onReload, forceResetRef } = useCardSave("sectors", dirty, props.reload);

  useEffect(() => {
    if (!shouldResetDraft(forceResetRef, dirty)) return;
    setRows(withIds(props.loaded.active_sectors.map((s) => ({ value: s }))));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [props.version]);

  const treeBySector = new Map(props.loaded.trees.map((t) => [t.sector, t]));

  return (
    <CardShell
      title="Expertise sectors"
      file={props.file}
      dirty={dirty}
      justSaved={justSaved}
      busy={busy}
      error={error}
      conflict={conflict}
      onReload={() => void onReload()}
      onSave={() => void save("sectors", props.file.exists ? props.file.mtime : null, {
        active_sectors: values.map((s) => s.trim()).filter(Boolean),
      })}
      headerExtra={
        <CountChip title="Active sectors in this config">
          {values.filter((v) => v.trim()).length} active
        </CountChip>
      }
    >
      <p className="mb-[16px] max-w-[680px] text-[13px] leading-[150%] text-t2">
        Where knowledge waterfalls in — projects, meetings, research and newsletters route into{" "}
        <span className="text-t1">Sectors/&lt;slug&gt;/</span> trees. News coverage is configured separately,
        so adding a sector here has no newsletter side-effect.
      </p>
      <div className={cn(tableCardCls, "w-[600px]")}>
        <table className="w-full border-collapse [&_td]:first:pl-[16px] [&_td]:last:pr-[16px] [&_th]:first:pl-[16px] [&_th]:last:pr-[16px]">
          <thead className={theadCls}><tr><th className={thCls}>Sector</th><th className={thCls}>Expertise tree</th><th className={thCls} /></tr></thead>
          <tbody>
            {rows.map((r) => {
              const t = treeBySector.get(r.value);
              return (
                <tr key={r.__id} className={rowCls}>
                  <td className="w-[240px] py-[7px] pr-[12px]"><input className={inputCls} value={r.value} onChange={(e) => setRows((rs) => rs.map((x) => (x.__id === r.__id ? { ...x, value: e.target.value } : x)))} /></td>
                  <td className="py-[7px] pr-[8px]">
                    {t ? (
                      <Chip
                        tone={t.tree === "full" ? "ok" : t.tree === "partial" ? "warn" : "off"}
                        title={t.tree === "missing"
                          ? `No Sectors/${t.slug}/ tree yet — scaffold via CLI (see hint below)`
                          : `Sectors/${t.slug}/ — ${t.tree}`}
                      >
                        {t.tree}
                      </Chip>
                    ) : (
                      <Chip tone="off" title="Save to evaluate the tree status">unsaved</Chip>
                    )}
                  </td>
                  <td className="py-[7px]"><RemoveBtn onClick={() => setRows((rs) => rs.filter((x) => x.__id !== r.__id))} /></td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <AddBtn onClick={() => setRows((r) => [...r, { __id: newId(), value: "" }])}>sector</AddBtn>
      {props.loaded.orphan_trees.length > 0 && (
        <p className="mt-[12px] text-[11.5px] text-accent">
          Orphan trees (no matching active sector): {props.loaded.orphan_trees.join(", ")}
        </p>
      )}
      <p className="mt-[10px] text-[10.5px] text-t4">
        Scaffolding a tree stays a CLI action: <span className="mono text-t3">{props.loaded.scaffold_hint}</span>
      </p>
    </CardShell>
  );
}

// ── News coverage card ─────────────────────────────────────────────────────

function CoverageCard(props: {
  loaded: OperatorConfigResponse["sections"]["coverage"];
  file: OperatorFileInfo;
  activeSectors: string[];
  reload: () => Promise<boolean>;
  version: number;
}) {
  const [rows, setRows] = useState<WithId<OperatorCoverageRow>[]>(() => withIds(props.loaded.coverage));

  const changed = JSON.stringify(stripIds(rows)) !== JSON.stringify(props.loaded.coverage);
  // A synthesised list isn't on disk yet — SAVEABLE as-is to materialise
  // the file, but only actual edits (changed) arm the tab-switch guard
  // (codex fe-review round 2).
  const saveable = props.loaded.synthesised || changed;
  const { busy, error, conflict, justSaved, save, onReload, forceResetRef } = useCardSave("coverage", changed, props.reload);

  useEffect(() => {
    if (!shouldResetDraft(forceResetRef, changed)) return;
    setRows(withIds(props.loaded.coverage));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [props.version]);

  const set = (id: string, patch: Partial<OperatorCoverageRow>) =>
    setRows((rs) => rs.map((r) => (r.__id === id ? { ...r, ...patch } : r)));

  const enabledCount = rows.filter((r) => r.enabled !== false).length;

  return (
    <CardShell
      title="News coverage"
      file={props.file}
      dirty={saveable}
      dirtyLabel={props.loaded.synthesised && !changed ? "save to create" : "unsaved"}
      justSaved={justSaved}
      busy={busy}
      error={error}
      conflict={conflict}
      onReload={() => void onReload()}
      onSave={() => void save("coverage", props.file.exists ? props.file.mtime : null, {
        coverage: stripIds(rows).map((r) => ({
          name: r.name.trim(),
          sector: r.sector?.trim() || null,
          sources: r.sources.map((s) => s.trim()).filter(Boolean),
          query: r.query?.trim() || null,
          enabled: r.enabled !== false,
        })),
      })}
      headerExtra={
        <Chip tone={enabledCount > 10 ? "warn" : "off"} title="Each enabled row is one Firecrawl + Ollama leg every weekday 07:00 — keep the list deliberate">
          {enabledCount} morning run{enabledCount === 1 ? "" : "s"}
        </Chip>
      }
    >
      <p className="mb-[16px] max-w-[760px] text-[13px] leading-[150%] text-t2">
        What the daily newsletter covers (<span className="mono text-t1">_claude/news-coverage.md</span>).
        Rows linked to a sector feed that sector's expertise waterfall; standalone topics (UK macro, rates…)
        just produce a newsletter. The list <span className="text-t1">is</span> the 07:00 run list — a row is
        in tomorrow's run the moment it's saved, unless paused.
      </p>
      {props.loaded.synthesised && (
        <div className="mb-[14px] rounded-lg border border-line bg-bg-2 px-[14px] py-[10px] text-[11.5px] leading-relaxed text-t2">
          No <span className="mono">news-coverage.md</span> yet — these defaults are derived from
          <span className="mono"> active_sectors</span> (today's behaviour). Save to materialise the file
          and start editing coverage independently of expertise sectors.
        </div>
      )}
      <div className={tableCardCls}>
        <table className="w-full border-collapse [&_td]:first:pl-[14px] [&_td]:last:pr-[14px] [&_th]:first:pl-[14px] [&_th]:last:pr-[14px]">
          <thead className={theadCls}>
            <tr>
              <th className={thCls}>Topic</th>
              <th className={thCls}>Sector link</th>
              <th className={thCls}>Sources · one URL per line</th>
              <th className={thCls}>Custom query</th>
              <th className={thCls}>Run</th>
              <th className={thCls} />
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.__id} className={cn(rowCls, "align-top")}>
                <td className="w-[160px] py-[7px] pr-[8px]"><input className={inputCls} value={r.name} onChange={(e) => set(r.__id, { name: e.target.value })} /></td>
                <td className="w-[140px] py-[7px] pr-[8px]">
                  <input
                    className={inputCls}
                    list="operator-active-sectors"
                    value={r.sector ?? ""}
                    placeholder="(standalone)"
                    onChange={(e) => set(r.__id, { sector: e.target.value || null })}
                  />
                </td>
                <td className="py-[7px] pr-[8px]">
                  <textarea
                    className={cn(inputCls, "min-h-[34px] resize-y")}
                    rows={Math.max(1, r.sources.length)}
                    value={r.sources.join("\n")}
                    placeholder="empty → search fallback"
                    onChange={(e) => set(r.__id, { sources: e.target.value.split("\n") })}
                  />
                </td>
                <td className="w-[220px] py-[7px] pr-[8px]">
                  <input
                    className={inputCls}
                    value={r.query ?? ""}
                    placeholder="(sector-derived M&A query)"
                    onChange={(e) => set(r.__id, { query: e.target.value || null })}
                  />
                </td>
                <td className="w-[72px] py-[7px] pr-[8px]">
                  <div className="flex h-[34px] items-center">
                  <button
                    type="button"
                    onClick={() => set(r.__id, { enabled: r.enabled === false })}
                    title={r.enabled === false ? "Paused — click to enrol in the daily run" : "Enabled — click to pause without deleting"}
                    className={cn(
                      "rounded-md border px-[10px] py-[4px] text-[10px] font-semibold uppercase tracking-[0.08em] transition-colors",
                      r.enabled === false ? "border-line bg-bg-2 text-t4 hover:text-t2" : "border-green/40 bg-green/15 text-green",
                    )}
                  >
                    {r.enabled === false ? "paused" : "on"}
                  </button>
                  </div>
                </td>
                <td className="py-[7px]"><div className="flex h-[34px] items-center justify-center"><RemoveBtn onClick={() => setRows((rs) => rs.filter((x) => x.__id !== r.__id))} /></div></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <datalist id="operator-active-sectors">
        {props.activeSectors.map((s) => <option key={s} value={s} />)}
      </datalist>
      <AddBtn onClick={() => setRows((r) => [...r, { __id: newId(), name: "", sector: null, sources: [], query: null, enabled: true }])}>topic</AddBtn>
    </CardShell>
  );
}

// ── Profile card ───────────────────────────────────────────────────────────

function ProfileCard(props: {
  loaded: OperatorConfigResponse["sections"]["profile"];
  file: OperatorFileInfo;
  reload: () => Promise<boolean>;
  version: number;
}) {
  const fromLoaded = (l: typeof props.loaded) => ({
    operator: l.operator ?? "",
    operator_slug: l.operator_slug ?? "",
    qualifications: (l.qualifications ?? []).join(", "),
    role_title: l.role_title ?? "",
    role_firm: l.role_firm ?? "",
  });
  const initial = fromLoaded(props.loaded);
  const [draft, setDraft] = useState(initial);

  const dirty = JSON.stringify(draft) !== JSON.stringify(initial);
  const { busy, error, conflict, justSaved, save, onReload, forceResetRef } = useCardSave("profile", dirty, props.reload);

  useEffect(() => {
    if (!shouldResetDraft(forceResetRef, dirty)) return;
    setDraft(fromLoaded(props.loaded));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [props.version]);

  const slugChanged = draft.operator_slug !== initial.operator_slug;

  const field = (label: string, key: keyof typeof draft, hint?: string) => (
    <label className="flex flex-col gap-[6px]">
      <span className="text-[10px] uppercase tracking-[0.12em] text-t3">{label}</span>
      <input
        className={inputCls}
        value={draft[key]}
        onChange={(e) => setDraft((d) => ({ ...d, [key]: e.target.value }))}
      />
      {hint && <span className="text-[10px] text-t4">{hint}</span>}
    </label>
  );

  return (
    <CardShell
      title="Profile"
      file={props.file}
      dirty={dirty}
      justSaved={justSaved}
      busy={busy}
      error={error}
      conflict={conflict}
      onReload={() => void onReload()}
      onSave={() => void save("profile", props.file.exists ? props.file.mtime : null, {
        operator: draft.operator.trim(),
        operator_slug: draft.operator_slug.trim(),
        qualifications: draft.qualifications.split(",").map((q) => q.trim()).filter(Boolean),
        role_title: draft.role_title.trim(),
        role_firm: draft.role_firm.trim(),
      })}
    >
      <p className="mb-[16px] max-w-[760px] text-[13px] leading-[150%] text-t2">
        Operator identity used across audit tags, action-item ownership and session defaults. The slug
        threads through everything downstream, so change it deliberately.
      </p>
      <div className="grid max-w-[760px] grid-cols-2 gap-x-[18px] gap-y-[16px]">
        {field("Operator name", "operator")}
        {field("Slug", "operator_slug", "lowercase, digits, hyphens")}
        {field("Qualifications", "qualifications", "comma-separated, e.g. ACA, CFA")}
        {field("Role title", "role_title")}
        {field("Firm", "role_firm", "wikilink form is fine, e.g. [[Companies/X]]")}
      </div>
      {slugChanged && (
        <div className="mt-[14px] max-w-[760px] rounded-lg border border-accent-line bg-accent-soft px-[14px] py-[10px] text-[11.5px] leading-relaxed text-accent">
          Slug change: <span className="mono">{initial.operator_slug || "(unset)"} → {draft.operator_slug}</span>.
          The slug threads through audit tags and action-item ownership (<span className="mono">[owner:{initial.operator_slug}]</span> lines
          keep the OLD slug and stop defaulting to you). Save only if that's intended.
        </div>
      )}
      <p className="mt-[14px] text-[10.5px] text-t4">
        Prose sections (career arc, sector lens, voice) stay Obsidian-edited — use the deep link above.
      </p>
    </CardShell>
  );
}

// ── Providers & keys (status-only) card ────────────────────────────────────

/** Effective-source chip labels for the known-keys rows. */
const KEY_SOURCE_CHIP: Record<string, { label: string; tone: "ok" | "live" | "warn" | "off"; title: string }> = {
  "store": { label: "store", tone: "ok", title: "Encrypted store key in use (entered via this tab)" },
  "store-over-env": { label: "store · env copy exists", tone: "warn", title: "The stored key is in use, but an OS-level env (setx) copy also exists underneath — remove the setx copy when convenient; it would resurface if the stored key were deleted" },
  "env": { label: "env", tone: "live", title: "Key supplied via an environment variable (setx) — enter it here to manage it from the tab instead" },
  "none": { label: "not set", tone: "off", title: "No key anywhere — the consumer runs degraded or fails" },
};

/** #operator-tab v2 — inline masked key entry.
 *
 *  Secret-handling contract (codex fe round 1): the key lives ONLY in the
 *  DOM input (uncontrolled, ref-read at confirm time) — never in React
 *  state, so it can't surface in DevTools/profiler snapshots. The DOM
 *  value is wiped on success AND cancel. Save errors render a sanitized
 *  status only (FastAPI 422 bodies echo the submitted input — never
 *  display them here). autoComplete=off + manager-ignore attributes keep
 *  password managers from offering to save an API key. */
function KeyEntry(props: {
  provider: string;
  isRotation: boolean;
  onDone: () => void;        // saved successfully → parent reloads
  onCancel: () => void;
}) {
  const inputRef = useRef<HTMLInputElement | null>(null);
  const [hasValue, setHasValue] = useState(false);
  const [arming, setArming] = useState(false);   // first Save click arms the confirm
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const wipe = () => {
    if (inputRef.current) inputRef.current.value = "";
    setHasValue(false);
  };

  const submit = async () => {
    if (!arming) { setArming(true); return; }
    const value = inputRef.current?.value.trim() ?? "";
    if (!value) { setArming(false); return; }
    setBusy(true);
    setErr(null);
    try {
      await api.putCredential(props.provider, value);
      wipe();                                    // never retained anywhere client-side
      setBusy(false);                            // before onDone — parent may unmount us
      props.onDone();
    } catch (e) {
      setArming(false);
      setBusy(false);
      // Sanitized on purpose — server error bodies can echo the input.
      setErr(`Save failed${e instanceof ApiError ? ` (HTTP ${e.status})` : ""} — see the bridge log.`);
    }
  };

  return (
    <span className="inline-flex flex-wrap items-center gap-[8px]">
      <input
        ref={inputRef}
        type="password"
        autoComplete="off"
        data-lpignore="true"
        data-1p-ignore="true"
        data-bwignore="true"
        spellCheck={false}
        placeholder={props.isRotation ? "new key (rotates the stored one)" : "paste key"}
        className={cn(inputCls, "w-[280px]")}
        onChange={(e) => { setHasValue(e.target.value.trim().length > 0); setArming(false); }}
        // eslint-disable-next-line jsx-a11y/no-autofocus
        autoFocus
      />
      <button
        type="button"
        disabled={!hasValue || busy}
        onClick={() => void submit()}
        className={cn(
          "rounded-lg px-[12px] py-[6px] text-[11px] font-medium uppercase tracking-[0.06em] transition-colors",
          arming ? "border border-accent-line bg-accent-soft text-accent"
          : hasValue && !busy ? "bg-accent text-bg hover:brightness-110"
          : "border border-line bg-bg-2 text-t4",
        )}
        title={arming
          ? `Confirm: encrypt + ${props.isRotation ? "ROTATE" : "store"} the key for ${props.provider}. It cannot be viewed afterwards.`
          : "Saves to the encrypted store (Fernet + DPAPI) — never the vault, never displayed again"}
      >
        {busy ? "saving…" : arming ? `confirm ${props.isRotation ? "rotation" : "save"}` : "save"}
      </button>
      <button
        type="button"
        disabled={busy}
        onClick={() => { wipe(); props.onCancel(); }}
        className="rounded-lg border border-line-2 px-[12px] py-[6px] text-[11px] uppercase tracking-[0.06em] text-t3 transition-colors hover:text-t1 disabled:text-t4"
      >
        cancel
      </button>
      {err && <span className="text-[11px] text-red">{err}</span>}
    </span>
  );
}

function CredentialsCard(props: {
  loaded: OperatorConfigResponse["sections"]["credentials"];
  reload: () => Promise<boolean>;
}) {
  const c = props.loaded;
  const [entryFor, setEntryFor] = useState<string | null>(null);   // provider with an open key form
  const [newProvider, setNewProvider] = useState("");
  const [busyDelete, setBusyDelete] = useState<string | null>(null);
  const [actionErr, setActionErr] = useState<string | null>(null);

  const doneEntry = () => { setEntryFor(null); setNewProvider(""); void props.reload(); };

  const removeCredential = async (provider: string) => {
    if (!window.confirm(
      `Delete the stored ${provider} credential? If an env (setx) copy exists it becomes effective again; otherwise the consumer loses its key.`,
    )) return;
    setBusyDelete(provider);
    setActionErr(null);
    try {
      await api.deleteCredential(provider);
      await props.reload();
    } catch (e) {
      setActionErr(e instanceof ApiError ? `Delete failed — ${e.status}: ${e.message}` : "Delete failed.");
    } finally {
      setBusyDelete(null);
    }
  };

  const knownKeys = Object.entries(c.keys ?? {});

  return (
    <section className="flex flex-col gap-[18px]">
      <header className="flex items-center gap-[10px]">
        <h3 className="text-[23px] font-semibold leading-[27px] text-t1">Providers &amp; keys</h3>
        <Chip tone="off" title="Keys are encrypted at rest (Fernet + Windows DPAPI), travel once over loopback, and are never displayed after save. Secrets never touch the vault.">encrypted store</Chip>
      </header>
      {actionErr && (
        <div className="rounded-[10px] border border-red/40 bg-red/10 px-[16px] py-[10px] text-[12px] text-red">{actionErr}</div>
      )}
      <div className="flex flex-col gap-[16px]">
        {/* Known API keys — entry/rotation per provider */}
        <div className="flex flex-col gap-[10px]">
          <span className="text-[10px] uppercase tracking-[0.12em] text-t3">API keys</span>
          {knownKeys.map(([provider, k]) => {
            const chip = KEY_SOURCE_CHIP[k.effective] ?? KEY_SOURCE_CHIP.none;
            const hasStored = k.store;
            return (
              <div key={provider} className="flex flex-wrap items-center gap-[10px]">
                <span className="w-[96px] text-[12.5px] text-t1">{provider}</span>
                <Chip tone={chip.tone} title={chip.title}>{chip.label}</Chip>
                <span className="mono text-[10px] text-t4">{k.env_var}</span>
                {entryFor === provider ? (
                  <KeyEntry
                    provider={provider}
                    isRotation={hasStored}
                    onDone={doneEntry}
                    onCancel={() => setEntryFor(null)}
                  />
                ) : (
                  <>
                    <button
                      type="button"
                      onClick={() => setEntryFor(provider)}
                      className="rounded-lg border border-line-2 px-[11px] py-[5px] text-[11px] uppercase tracking-[0.06em] text-t2 transition-colors hover:border-accent-line hover:text-t1"
                      title={hasStored ? "Replace the stored key (atomic rotation — no window without a key)" : "Enter a key into the encrypted store"}
                    >
                      {hasStored ? "rotate" : "set key"}
                    </button>
                    {hasStored && (
                      <button
                        type="button"
                        disabled={busyDelete === provider}
                        onClick={() => void removeCredential(provider)}
                        className="rounded-lg border border-line-2 px-[11px] py-[5px] text-[11px] uppercase tracking-[0.06em] text-t3 transition-colors hover:border-red/50 hover:text-red disabled:opacity-50"
                      >
                        {busyDelete === provider ? "…" : "delete"}
                      </button>
                    )}
                  </>
                )}
              </div>
            );
          })}
          {/* Other providers — free-form entry (stored, not env-bridged).
              Slug must be a clean kebab token and must not shadow a known
              provider (codex fe round 1 #4 — known rows own their keys). */}
          <div className="flex flex-wrap items-center gap-[10px]">
            <input
              className={cn(inputCls, "w-[96px]")}
              placeholder="other…"
              value={newProvider}
              spellCheck={false}
              onChange={(e) => setNewProvider(e.target.value.toLowerCase().replace(/[^a-z0-9-]/g, ""))}
              title="Any other provider slug (lowercase kebab) — stored encrypted; consumers read it from the store"
            />
            {(() => {
              const slugOk = /^[a-z0-9]+(?:-[a-z0-9]+)*$/.test(newProvider);
              const isKnown = Object.prototype.hasOwnProperty.call(c.keys ?? {}, newProvider);
              if (!newProvider) return null;
              if (isKnown) {
                return <span className="text-[11px] text-accent">use the {newProvider} row above</span>;
              }
              if (!slugOk) {
                return <span className="text-[11px] text-t4">slug: lowercase kebab, e.g. data-vendor</span>;
              }
              return entryFor === `__new__${newProvider}` ? (
                <KeyEntry
                  provider={newProvider}
                  isRotation={false}
                  onDone={doneEntry}
                  onCancel={() => setEntryFor(null)}
                />
              ) : (
                <button
                  type="button"
                  onClick={() => setEntryFor(`__new__${newProvider}`)}
                  className="rounded-lg border border-line-2 px-[11px] py-[5px] text-[11px] uppercase tracking-[0.06em] text-t2 transition-colors hover:border-accent-line hover:text-t1"
                >
                  set key
                </button>
              );
            })()}
          </div>
        </div>
        <div className="border-t border-line" />
        <div className="flex flex-wrap items-center gap-[10px]">
          <span className="w-[120px] text-[10px] uppercase tracking-[0.12em] text-t3">Credential store</span>
          {c.credentials_error ? (
            <Chip tone="warn" title="The encrypted store could not be read — see the bridge log">{c.credentials_error}</Chip>
          ) : c.credentials.length === 0 ? (
            <span className="text-[11.5px] text-t3">no stored credentials</span>
          ) : (
            c.credentials.map((s) => (
              <span key={s.provider} className="inline-flex items-center gap-[4px]">
                <Chip tone="ok" title={`kind=${s.kind} · created ${s.created}${s.last_used ? ` · last used ${s.last_used}` : ""}`}>
                  {s.provider}
                </Chip>
                {s.kind === "api_key" && !(s.provider in (c.keys ?? {})) && (
                  <RemoveBtn label={`Delete stored ${s.provider} credential`} onClick={() => void removeCredential(s.provider)} />
                )}
              </span>
            ))
          )}
        </div>
        <div className="flex flex-wrap items-center gap-[10px]">
          <span className="w-[120px] text-[10px] uppercase tracking-[0.12em] text-t3">Ollama</span>
          {c.ollama.reachable ? (
            <Chip tone="ok" title={`v${c.ollama.version} — ${(c.ollama.models ?? []).join(", ")}`}>
              reachable · {(c.ollama.models ?? []).length} models
            </Chip>
          ) : (
            <Chip tone="warn" title="Local Ollama did not answer the health probe">unreachable</Chip>
          )}
        </div>
        <div className="flex flex-wrap items-center gap-[10px]">
          <span className="w-[120px] text-[10px] uppercase tracking-[0.12em] text-t3">CLI auth</span>
          <span className="text-[11.5px] text-t2">claude / codex — {c.cli_auth}</span>
          <span className="text-[10.5px] text-t4">(their own login flows; not managed here)</span>
        </div>
        <div className="flex flex-wrap items-center gap-[10px]">
          <span className="w-[120px] text-[10px] uppercase tracking-[0.12em] text-t3">Overrides</span>
          <Chip tone={c.provider_overrides.exists ? "ok" : "off"}>{c.provider_overrides.exists ? "sidecar present" : "no sidecar"}</Chip>
          <a href={obsidianHref(c.provider_overrides.path)} className="mono text-[10px] text-t3 underline decoration-dotted transition-colors hover:text-t1">
            {c.provider_overrides.path}
          </a>
          <span className="text-[10.5px] text-t4">per-skill provider matrix lives on the PROVIDERS tab</span>
        </div>
      </div>
    </section>
  );
}

// ── Tab root ───────────────────────────────────────────────────────────────

export function OperatorTab() {
  const [data, setData] = useState<OperatorConfigResponse | null>(null);
  const [version, setVersion] = useState(0);   // bumps per successful GET → cards reset drafts
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState<SectionKey | null>(null);   // drilled-into editor, null = landing
  const abortRef = useRef<AbortController | null>(null);

  // Returns true only when a fresh payload landed (and version bumped) —
  // useCardSave's force-reset arming depends on an honest signal here.
  const load = useCallback(async (): Promise<boolean> => {
    abortRef.current?.abort();
    const ac = new AbortController();
    abortRef.current = ac;
    try {
      const r = await api.operatorConfig(ac.signal);
      if (ac.signal.aborted) return false;
      setData(r);
      setVersion((v) => v + 1);
      setError(null);
      return true;
    } catch (e) {
      if (ac.signal.aborted) return false;
      setError(e instanceof ApiError ? `${e.status}: ${e.message}` : String(e));
      return false;
    } finally {
      if (!ac.signal.aborted) setLoading(false);
    }
  }, []);

  useEffect(() => {
    dirtyCards.clear();   // stale entries from HMR / exceptional unmounts
    void load();
    return () => abortRef.current?.abort();
  }, [load]);

  // Browser-level guard: closing/refreshing with unsaved card edits.
  useEffect(() => {
    const onBeforeUnload = (e: BeforeUnloadEvent) => {
      if (hasUnsavedOperatorEdits()) {
        e.preventDefault();
        e.returnValue = "";   // required by several browsers to show the prompt
      }
    };
    window.addEventListener("beforeunload", onBeforeUnload);
    return () => window.removeEventListener("beforeunload", onBeforeUnload);
  }, []);

  if (loading && !data) {
    return (
      <div className="mx-auto w-full max-w-[1060px] px-[28px] py-[30px] text-[12px] italic text-t3">
        Loading operator config…
      </div>
    );
  }
  if (error && !data) {
    return (
      <div className="mx-auto w-full max-w-[1060px] px-[28px] py-[30px] text-[12px] italic text-t3">
        Operator config unavailable — {error}
      </div>
    );
  }
  if (!data) return null;

  const s = data.sections;

  // Derived summary stat footers — Paper text is placeholder; these render the
  // real loaded config.
  const macroCount = s.banners.macro_bar.length;
  const watchNext = s.watchlist.earnings_watchlist[0];
  const coverageEnabled = s.coverage.coverage.filter((r) => r.enabled !== false).length;
  const coverageSectors = new Set(
    s.coverage.coverage.map((r) => r.sector?.trim()).filter(Boolean),
  ).size;
  const coveragePaused = s.coverage.coverage.length - coverageEnabled;
  const profileBits = [
    s.profile.operator?.trim(),
    s.profile.role_title?.trim(),
    s.profile.role_firm?.trim(),
  ].filter(Boolean);
  const knownKeys = Object.entries(s.credentials.keys ?? {});

  // ── drilldown: render the unchanged editor card for the open section ──────
  if (open) {
    const back = (
      <button
        type="button"
        onClick={() => {
          // Guard the drill-out: the open editor is the ONLY mounted card here,
          // so hasUnsavedOperatorEdits() reflects ITS dirty state. Unmounting it
          // (the effect cleanup deletes its dirtyCards entry) would silently drop
          // the unsaved edits — so confirm first, matching the beforeunload guard.
          if (hasUnsavedOperatorEdits() && !window.confirm("Discard unsaved changes to this section?")) return;
          setOpen(null);
        }}
        className="inline-flex w-fit items-center gap-[6px] text-[12px] text-t3 transition-colors hover:text-t1"
      >
        <svg width="13" height="13" viewBox="0 0 16 16" xmlns="http://www.w3.org/2000/svg" className="shrink-0" aria-hidden="true">
          <path d="M10 3.5L5.5 8l4.5 4.5" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
        Operator
      </button>
    );
    return (
      <div className="mx-auto flex w-full max-w-[1060px] flex-col gap-[18px] px-[28px] py-[30px] text-t1">
        {back}
        {error && (
          <div className="rounded-lg border border-red/40 bg-red/10 px-[12px] py-[8px] text-[12px] text-red">{error}</div>
        )}
        {open === "banners" && (
          <BannersCard loaded={s.banners} file={data.files.tickers} reload={load} version={version} />
        )}
        {open === "watchlist" && (
          <WatchlistCard loaded={s.watchlist} file={data.files.earnings_watchlist} reload={load} version={version} />
        )}
        {open === "sectors" && (
          <SectorsCard loaded={s.sectors} file={data.files.profile} reload={load} version={version} />
        )}
        {open === "coverage" && (
          <CoverageCard
            loaded={s.coverage}
            file={data.files.news_coverage}
            activeSectors={s.sectors.active_sectors}
            reload={load}
            version={version}
          />
        )}
        {open === "profile" && (
          <ProfileCard loaded={s.profile} file={data.files.profile} reload={load} version={version} />
        )}
        {open === "credentials" && <CredentialsCard loaded={s.credentials} reload={load} />}
      </div>
    );
  }

  // ── landing: 2-col grid of summary tiles (Paper 913-0) ────────────────────
  return (
    <div className="mx-auto flex w-full max-w-[1060px] flex-col gap-[20px] px-[28px] py-[30px] text-t1">
      <div className="flex flex-col gap-[7px]">
        <div className="flex items-baseline justify-between gap-[16px]">
          <h2 className="text-[22px] font-semibold leading-[120%] tracking-[-0.01em] text-t1">Operator</h2>
          <span className="mono shrink-0 text-[11px] leading-[14px] text-t3">
            /api/operator/config · saves per card
          </span>
        </div>
        <p className="text-[13px] leading-[150%] text-t2">
          Your identity, coverage and credentials. Each card is edited and saved independently.
        </p>
      </div>

      {error && (
        <div className="rounded-lg border border-red/40 bg-red/10 px-[12px] py-[8px] text-[12px] text-red">{error}</div>
      )}

      <div className="flex flex-wrap gap-[14px]">
        <SummaryCard
          title="Banners"
          action="Edit"
          description="Equity + macro tickers shown in the top bar."
          onOpen={() => setOpen("banners")}
        >
          <StatLine>
            {s.banners.ticker_bar.length} equity peer{s.banners.ticker_bar.length === 1 ? "" : "s"} · {macroCount} macro
          </StatLine>
        </SummaryCard>

        <SummaryCard
          title="Earnings watchlist"
          action="Edit"
          description="Companies tracked for upcoming earnings dates."
          onOpen={() => setOpen("watchlist")}
        >
          <StatLine>
            {s.watchlist.earnings_watchlist.length} ticker{s.watchlist.earnings_watchlist.length === 1 ? "" : "s"}
            {watchNext ? ` · first ${watchNext.symbol.trim().toUpperCase()}` : ""}
          </StatLine>
        </SummaryCard>

        <SummaryCard
          title="Expertise sectors"
          action="Edit"
          description="Sectors Anton specialises in."
          onOpen={() => setOpen("sectors")}
        >
          {s.sectors.active_sectors.length > 0 ? (
            <div className="flex flex-wrap gap-[6px]">
              {s.sectors.active_sectors.map((sec) => <SectorPill key={sec}>{sec}</SectorPill>)}
            </div>
          ) : (
            <StatLine>no active sectors</StatLine>
          )}
        </SummaryCard>

        <SummaryCard
          title="News coverage"
          action="Edit"
          description="Daily news pull sources, per sector."
          onOpen={() => setOpen("coverage")}
        >
          <StatLine>
            {coverageEnabled} source{coverageEnabled === 1 ? "" : "s"} · {coverageSectors} sector{coverageSectors === 1 ? "" : "s"}
            {coveragePaused > 0 ? ` · ${coveragePaused} paused` : ""}
          </StatLine>
        </SummaryCard>

        <SummaryCard
          title="Profile"
          action="Edit"
          description="Operator identity and session defaults."
          onOpen={() => setOpen("profile")}
        >
          <StatLine>{profileBits.length > 0 ? profileBits.join(" · ") : "not configured"}</StatLine>
        </SummaryCard>

        <SummaryCard
          title="Providers & keys"
          action="Manage"
          description="Credential store — masked, two-click, never echoed back."
          onOpen={() => setOpen("credentials")}
          lock
        >
          <div className="flex flex-col gap-[8px]">
            {knownKeys.length === 0 ? (
              <StatLine>no providers configured</StatLine>
            ) : (
              knownKeys.map(([provider, k]) => {
                const set = k.effective !== "none";
                return (
                  <div key={provider} className="flex items-center justify-between gap-[8px]">
                    <span className="mono text-[11px] leading-[14px] text-t2">{provider}</span>
                    <div className="flex items-center gap-[8px]">
                      <span className={cn("mono text-[11px] leading-[14px]", set ? "text-t3" : "text-t4")}>
                        {set ? k.effective : "not set"}
                      </span>
                      <span className={cn("size-[6px] shrink-0 rounded-[3px]", set ? "bg-green" : "bg-t4")} />
                    </div>
                  </div>
                );
              })
            )}
          </div>
        </SummaryCard>
      </div>
    </div>
  );
}
