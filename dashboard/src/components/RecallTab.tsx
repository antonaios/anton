import { useEffect, useMemo, useRef, useState } from "react";
import { Search, ArrowRight, Quote, ChevronDown } from "lucide-react";
import { cn } from "../lib/cn";
import { api, ApiError } from "../lib/api";
import type { RecallResponse, RecallSource, Sensitivity, WorkspaceListItem } from "../types";
import { Card } from "./ui/Card";
import { StatusBadge } from "./ui/StatusBadge";

/**
 * Recall — the dedicated vault-search surface (Library › Recall).
 *
 * Self-contained: owns its own query / sensitivity-ceiling / synthesise / limit
 * state plus the loading-result-error lifecycle, mirroring the other tabs. On
 * submit it calls `api.recall(...)` with the SAME request shape Cmd-K uses
 * (CommandModal.runRecall) so the two entry points stay behaviourally identical.
 *
 * Result = a two-tile layout: LEFT a synthesis/answer hero (prose + key-figure
 * KpiCard tiles + a GAPS/contradiction line + [[wikilink]] cites), RIGHT a
 * compact Sources companion list where cited rows read in the accent tint and
 * carry an RRF-score chip. Token-only; flips between the LIGHT teal and DARK
 * navy themes automatically.
 */

type SensLevel = Sensitivity; // "public" | "internal" | "confidential" | "MNPI"

const SENS_OPTIONS = [
  { value: "public", label: "Public" },
  { value: "internal", label: "Internal" },
  { value: "confidential", label: "Confidential" },
  { value: "MNPI", label: "MNPI" },
];

const LIMITS = [6, 12, 20];

// ── Presentational derivations off the real RecallSource shape ───────────────
// The wire hit carries { rank, path, score }. Title + sensitivity + the "cited"
// marker are derived for display only; the API request/response are untouched.

/** Basename without extension → a human-ish source title. */
function titleFor(path: string): string {
  const base = path.split("/").pop() ?? path;
  return base.replace(/\.[^.]+$/, "");
}

/** Sources whose score clears the synthesis-grounding bar are marked "cited":
 *  the top-scoring third (min 1) of the returned hits. Purely presentational —
 *  it highlights which rows the answer leans on. */
function citedRanks(hits: RecallSource[]): Set<number> {
  if (!hits.length) return new Set();
  const sorted = [...hits].sort((a, b) => b.score - a.score);
  const n = Math.max(1, Math.round(sorted.length / 3));
  return new Set(sorted.slice(0, n).map((h) => h.rank));
}

export function RecallTab({
  projects = [],
  activeProject,
}: {
  /** Project workspaces from /api/workspaces?type=project (App.tsx supplies its
   *  already-loaded list). Empty list still renders the "All projects" option. */
  projects?: WorkspaceListItem[];
  /** Name of the active project workspace — used as the default recall scope.
   *  Undefined (e.g. a BD context is active) defaults to "All projects". */
  activeProject?: string;
}) {
  const [query, setQuery] = useState("");
  const [sens, setSens] = useState<SensLevel>("internal");
  const [synth, setSynth] = useState(true);
  const [limit, setLimit] = useState(12);
  // Project-scope of the recall request (RecallRequest.project). undefined =
  // "All projects" (vault-wide). Defaults to the active project workspace.
  const [project, setProject] = useState<string | undefined>(activeProject);

  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<RecallResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  const cited = useMemo(() => citedRanks(result?.hits ?? []), [result]);

  const runRecall = async () => {
    const q = query.trim();
    if (!q || busy) return;
    setBusy(true);
    setError(null);
    try {
      // SAME shape CommandModal.runRecall posts (plus the project scope).
      const res = await api.recall({
        query: q,
        limit,
        max_sensitivity: sens,
        synthesise: synth,
        project,
      });
      setResult(res);
    } catch (e) {
      const msg =
        e instanceof ApiError ? `${e.status}: ${e.message}`
        : e instanceof Error ? e.message
        : "Unknown error";
      setError(`Recall failed — ${msg}`);
      setResult(null);
    } finally {
      setBusy(false);
    }
  };

  const onSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    void runRecall();
  };

  const hits = result?.hits ?? [];
  const citedCount = hits.filter((h) => cited.has(h.rank)).length;

  return (
    <div className="flex w-full max-w-[1060px] flex-col gap-5 px-7 py-[30px]">
      {/* Header */}
      <header className="flex flex-col gap-[7px]">
        <div className="flex items-baseline justify-between gap-4">
          <h2 className="text-[22px] font-semibold leading-[120%] tracking-[-0.01em] text-t1">Recall</h2>
          <div className="mono text-[11px] leading-[14px] text-t3">
            {hits.length} {hits.length === 1 ? "source" : "sources"} · lexical · semantic · graph
          </div>
        </div>
        <p className="text-[13px] leading-[150%] text-t2">
          Search the vault — ranked sources with on-demand synthesis. The same recall runs from Cmd-K.
        </p>
      </header>

      {/* Search bar */}
      <form
        onSubmit={onSubmit}
        className="flex h-[54px] items-center gap-[14px] rounded-[12px] border border-accent-line bg-bg-1 pl-[18px] pr-[8px] shadow-contact focus-within:border-accent"
      >
        <Search size={18} className="shrink-0 text-t3" />
        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Ask the vault anything — e.g. What did we conclude on Helix's debt capacity?"
          className="min-w-0 grow bg-transparent text-[15px] leading-5 text-t1 placeholder:text-t3 outline-none"
        />
        <button
          type="submit"
          disabled={busy || !query.trim()}
          className={cn(
            "flex h-[38px] shrink-0 items-center gap-[8px] rounded-[9px] border px-[16px] transition-colors",
            busy || !query.trim()
              ? "border-line-2 text-t3"
              : "border-accent-line bg-accent-soft hover:brightness-105",
          )}
        >
          <span className={cn("text-[13px] font-semibold leading-4", busy || !query.trim() ? "text-t3" : "text-t1")}>
            {busy ? "Running…" : "Recall"}
          </span>
          {!busy && <span className="mono text-[12px] leading-[14px] opacity-75">↵</span>}
        </button>
      </form>

      {/* Filter / control row */}
      <div className="flex items-center justify-between gap-[14px]">
        <div className="flex items-center gap-[12px]">
          <ProjectScopePill
            projects={projects}
            value={project}
            onChange={setProject}
          />
          <div className="flex items-center gap-[8px]">
            <span className="text-[11.5px] leading-4 text-t3">Sens ≤</span>
            <div className="flex items-center gap-[6px]">
              {SENS_OPTIONS.map((opt) => (
                <SensCeilingPill
                  key={opt.value}
                  value={opt.value as SensLevel}
                  label={opt.label}
                  selected={sens === opt.value}
                  onSelect={() => setSens(opt.value as SensLevel)}
                />
              ))}
            </div>
          </div>
        </div>

        <div className="flex items-center gap-[14px]">
          <label className="flex items-center gap-[9px]">
            <span className="text-[12.5px] leading-4 text-t2">Synthesize</span>
            <button
              type="button"
              role="switch"
              aria-checked={synth}
              aria-label="Toggle synthesis"
              onClick={() => setSynth((s) => !s)}
              className={cn(
                "relative h-[20px] w-[34px] shrink-0 rounded-[10px] transition-colors",
                synth ? "bg-accent" : "bg-bg-2 border border-line-2",
              )}
            >
              <span
                className={cn(
                  "absolute top-[2px] h-[16px] w-[16px] rounded-[8px] bg-bg-1 shadow-card transition-all",
                  synth ? "left-[16px]" : "left-[2px]",
                )}
              />
            </button>
          </label>

          <div className="relative flex h-[32px] items-center rounded-[8px] border border-line-2 bg-bg-1 pl-[11px] pr-[26px] transition-colors hover:border-accent-line">
            <select
              value={String(limit)}
              onChange={(e) => setLimit(Number(e.target.value))}
              aria-label="Result count"
              className="mono cursor-pointer appearance-none bg-transparent text-[11.5px] font-medium leading-4 text-t1 outline-none"
            >
              {LIMITS.map((n) => (
                <option key={n} value={String(n)}>{`Top ${n}`}</option>
              ))}
            </select>
            <ChevronDown size={11} className="pointer-events-none absolute right-[9px] text-t3" />
          </div>
        </div>
      </div>

      {/* Error */}
      {error && (
        <div className="rounded-lg border border-red/40 bg-red/[0.1] px-[14px] py-[10px] text-[12px] text-red">
          {error}
        </div>
      )}

      {/* Busy */}
      {busy && !error && (
        <Card>
          <div className="flex items-center gap-[10px] text-[12px] text-t2">
            <StatusBadge status="running" />
            Querying the vault
            {synth
              ? <span className="text-t3"> · local synthesis can take a few minutes on Ollama (faster when routed to cloud).</span>
              : <span className="text-t3"> · raw ranked hits, no synthesis (~2s).</span>}
          </div>
        </Card>
      )}

      {/* Empty (pre-first-query) */}
      {!busy && !error && !result && (
        <Card>
          <div className="flex flex-col items-center gap-[10px] py-[40px] text-center">
            <Quote size={26} className="text-t4" />
            <div className="text-[13px] text-t2">Ask a question to search the vault.</div>
            <div className="max-w-[420px] text-[12px] text-t3">
              Recall blends vector + full-text + graph retrieval, fuses the
              ranking (RRF), and — with synthesis on — drafts a grounded answer
              with inline citations.
            </div>
          </div>
        </Card>
      )}

      {/* Result — two-tile layout */}
      {!busy && !error && result && (
        <div className="flex flex-col gap-[18px] lg:flex-row">
          {/* LEFT — synthesis / answer hero */}
          <SynthesisHero result={result} hits={hits} cited={cited} citedCount={citedCount} synth={synth} project={project} />

          {/* RIGHT — sources companion list */}
          <div className="flex w-full shrink-0 flex-col overflow-hidden rounded-[16px] border border-line bg-bg-1 shadow-card-lift lg:w-[382px]">
            <div className="flex items-center justify-between border-b border-line px-[16px] py-[14px]">
              <div className="flex items-center gap-[8px]">
                <h3 className="text-[11px] font-bold uppercase tracking-[0.1em] text-t2">Sources</h3>
                <span className="mono text-[11px] text-t4">{hits.length}</span>
              </div>
              <div className="flex items-center gap-[6px]">
                <span className="h-[6px] w-[6px] shrink-0 rounded-[3px] bg-accent" />
                <span className="text-[11px] leading-[14px] text-accent">{citedCount} cited · in the answer</span>
              </div>
            </div>
            {hits.length === 0 ? (
              <div className="px-[16px] py-[14px] text-[12px] text-t3">No sources matched.</div>
            ) : (
              <ul className="flex flex-col">
                {hits.map((h) => (
                  <SourceRow key={`${h.rank}-${h.path}`} hit={h} cited={cited.has(h.rank)} />
                ))}
              </ul>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

// ── LEFT hero ────────────────────────────────────────────────────────────────

function SynthesisHero({
  result, hits, cited, citedCount, synth, project,
}: {
  result: RecallResponse;
  hits: RecallSource[];
  cited: Set<number>;
  citedCount: number;
  synth: boolean;
  project?: string;
}) {
  // Key-figure tiles + the two callout blocks are extracted from the synthesis
  // prose: monetary / multiple figures become stat tiles; a covenant-breach /
  // contradiction sentence (if any) drives the amber-dot watch line, and a
  // separate "missing / unconfirmed" sentence drives the GAPS block. Each is
  // null when the answer flags nothing — never fabricated. All presentational.
  const figures = useMemo(() => extractFigures(result.synthesis), [result.synthesis]);
  const contradiction = useMemo(() => extractContradiction(result.synthesis), [result.synthesis]);
  const gap = useMemo(
    () => extractGap(result.synthesis, contradiction),
    [result.synthesis, contradiction],
  );
  const citeTitles = useMemo(
    () => hits.filter((h) => cited.has(h.rank)).map((h) => titleFor(h.path)),
    [hits, cited],
  );

  return (
    <div className="flex min-w-0 grow basis-0 flex-col gap-[15px] rounded-[16px] border border-accent-line bg-bg-1 px-[24px] py-[22px] shadow-card-lift">
      <div className="flex items-center gap-[10px]">
        <h3 className="text-[10px] font-bold uppercase tracking-[0.12em] text-accent">Synthesis</h3>
        <span className="h-[3px] w-[3px] shrink-0 rounded-[2px] bg-t4" />
        <span className="text-[11.5px] leading-[14px] text-t2">
          grounded in {citedCount} of {hits.length} sources
          {project && <> · Project {project}</>}
        </span>
      </div>

      {result.synthesis ? (
        <p className="text-[14.5px] leading-[158%] text-t1 whitespace-pre-wrap">{result.synthesis}</p>
      ) : (
        <p className="text-[14.5px] leading-[158%] text-t3">
          {synth
            ? "No synthesis was returned for this query — see the ranked sources."
            : "Synthesis is off. Toggle it on to draft a grounded answer from these sources."}
        </p>
      )}

      {/* Key-figure stat tiles — Paper 1b treatment: bordered tiles on the darker
          bg-2 fill, mono uppercase micro-label, 17px semibold value. */}
      {figures.length > 0 && (
        <div className="flex gap-[10px]">
          {figures.map((f, i) => (
            <div key={`${i}-${f.label}`} className="min-w-0 grow basis-0 rounded-[10px] border border-line bg-paper2 px-[13px] py-[11px]">
              <div className="mono text-[9px] uppercase tracking-[0.06em] text-t3">{f.label}</div>
              <div className="mt-[6px] text-[17px] font-semibold leading-[110%] text-t1">
                {f.value}
                {f.unit && <span className="ml-[3px] text-[11px] font-medium text-t3">{f.unit}</span>}
              </div>
            </div>
          ))}
        </div>
      )}

      {/* Watch — amber-dot contradiction / covenant-breach line. Its own paper2
          callout (no label), rendered only when the answer surfaces one. */}
      {contradiction && (
        <div className="flex items-center gap-[10px] rounded-[9px] bg-paper2 px-[13px] py-[11px]">
          <span className="h-[6px] w-[6px] shrink-0 rounded-[3px] bg-amber" />
          <div className="text-[12px] leading-[150%] text-t2">{contradiction}</div>
        </div>
      )}

      {/* GAPS — a separate paper2 block with a mono GAPS label (no dot),
          rendered only when a missing/unconfirmed sentence exists. */}
      {gap && (
        <div className="flex items-start gap-[9px] rounded-[9px] bg-paper2 px-[13px] py-[11px]">
          <span className="shrink-0 pt-px text-[9.5px] font-bold uppercase tracking-[0.08em] leading-[18px] text-t3">
            Gaps
          </span>
          <div className="text-[12px] leading-[150%] text-t2">{gap}</div>
        </div>
      )}

      {/* Cites — [[wikilink]] chips */}
      {citeTitles.length > 0 && (
        <>
          <div className="grow" />
          <div className="flex items-center justify-between gap-3 border-t border-line pt-[13px]">
            <div className="flex flex-wrap items-center gap-[6px]">
              <span className="text-[11px] leading-[14px] text-t3">Grounded in</span>
              {citeTitles.map((t, i) => (
                <span key={`${i}-${t}`} className="flex items-center gap-[6px]">
                  {i > 0 && <span className="h-[3px] w-[3px] shrink-0 rounded-[2px] bg-t4" />}
                  <span className="mono text-[10.5px] leading-[14px] text-accent">{`[[${t}]]`}</span>
                </span>
              ))}
            </div>
            <div className="flex shrink-0 items-center gap-[6px]">
              <span className="text-[11.5px] leading-[14px] text-t4">Hover to trace sources</span>
              <ArrowRight size={14} className="text-t4" />
            </div>
          </div>
        </>
      )}
    </div>
  );
}

// ── RIGHT source row ─────────────────────────────────────────────────────────

function SourceRow({ hit, cited }: { hit: RecallSource; cited: boolean }) {
  // #recall-detail (task_97c4527d) — the wire hit now carries the note's declared
  // sensitivity tier + mtime, so the row shows a sensitivity chip + a short date +
  // a STALE badge (>180d untouched) alongside the RRF score.
  const dateLabel = shortDate(hit.mtime);
  const stale = isStale(hit.mtime);
  return (
    <li
      className={cn(
        "flex flex-col gap-[6px] border-b border-line px-[16px] py-[12px] last:border-b-0",
        cited && "border-l-2 border-l-accent bg-accent-soft pl-[14px]",
      )}
    >
      <div className="flex items-center justify-between gap-[8px]">
        <div className="flex min-w-0 items-center gap-[7px]">
          <span
            className="mono tabular flex h-[20px] shrink-0 items-center rounded-[5px] border border-accent-line bg-accent-soft px-[7px] text-[11px] font-medium leading-[14px] text-accent"
            title="Reciprocal-rank-fusion score"
          >
            {hit.score.toFixed(3)}
          </span>
          {hit.sensitivity && <SensChip value={hit.sensitivity} />}
          {cited && (
            <span className="flex shrink-0 items-center gap-[3px] rounded-[4px] bg-accent-soft px-[5px] py-[1px] text-accent">
              <svg width="10" height="10" viewBox="0 0 16 16" xmlns="http://www.w3.org/2000/svg" className="shrink-0">
                <path d="M6.5 9.5L10 6M7 5.5h3.5V9" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
              </svg>
              <span className="text-[9px] font-semibold uppercase tracking-[0.06em] leading-[14px]">cited</span>
            </span>
          )}
        </div>
        <div className="flex shrink-0 items-center gap-[6px]">
          {stale && (
            <span className="rounded-[4px] border border-red/40 bg-red/15 px-[5px] text-[8.5px] font-bold uppercase tracking-[0.06em] leading-[14px] text-red">
              stale
            </span>
          )}
          {dateLabel && <span className="mono text-[10.5px] leading-[14px] text-t3">{dateLabel}</span>}
        </div>
      </div>

      <div className="min-w-0">
        <div className="line-clamp-1 text-[12.5px] font-semibold leading-[135%] text-t1">{titleFor(hit.path)}</div>
        <div className="mono line-clamp-1 text-[10px] leading-[13px] text-accent">{hit.path}</div>
      </div>
    </li>
  );
}

/** Per-source sensitivity chip (dot + tier label) for the recall sources rail.
 *  The DOT carries the tier tint — confidential amber, MNPI red, internal/public
 *  slate — while the LABEL stays neutral --slate (text-t2) for every tier, the
 *  standard sensitivity convention. Metadata only (the route gates content). */
function SensChip({ value }: { value: string }) {
  const v = value.toLowerCase();
  const dot =
    v === "mnpi" ? "bg-red" :
    v === "confidential" ? "bg-amber" :
    "bg-t2";
  const label = v === "mnpi" ? "MNPI" : value.charAt(0).toUpperCase() + value.slice(1).toLowerCase();
  return (
    <span className="flex shrink-0 items-center gap-[4px] text-[10.5px] font-medium leading-[14px] text-t2" title={`Sensitivity: ${label}`}>
      <span className={cn("size-1.5 shrink-0 rounded-[3px]", dot)} />
      {label}
    </span>
  );
}

/** Sensitivity-ceiling selector pill (one per tier) for the recall filter row.
 *  Paper treatment: resting Public/Internal/Confidential are quiet bg-paper2 chips
 *  in their tier text colour (only MNPI rests bare); the selected tier keeps the
 *  paper2 fill but gains a full tier border + 600 weight. Purely a control-shape
 *  restyle: selection state + which tiers are selectable are unchanged (the route
 *  gates the actual content). */
function SensCeilingPill({
  value, label, selected, onSelect,
}: {
  value: SensLevel;
  label: string;
  selected: boolean;
  onSelect: () => void;
}) {
  const text =
    value === "MNPI" ? "text-t4" :
    value === "confidential" ? "text-amber" :
    value === "internal" ? "text-t2" : "text-green";
  const restingBg = value === "MNPI" ? "border-transparent" : "bg-paper2 border-transparent";
  const selectedBorder =
    value === "MNPI" ? "border-line-2 text-t2" :
    value === "confidential" ? "border-amber" :
    value === "internal" ? "border-line-2 text-t1" :
    "border-green";
  return (
    <button
      type="button"
      aria-pressed={selected}
      onClick={onSelect}
      className={cn(
        "flex h-[26px] items-center rounded-[7px] border px-[10px] text-[11.5px] leading-4 transition-colors",
        text,
        selected
          ? cn("bg-paper2 font-semibold", selectedBorder)
          : cn("font-medium hover:border-line-2", restingBg),
      )}
    >
      {label}
    </button>
  );
}

/** ISO-8601 → compact zero-padded "MM-DD" label; empty when absent/unparseable. */
function shortDate(iso?: string): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (!Number.isFinite(d.getTime())) return "";
  const mm = String(d.getMonth() + 1).padStart(2, "0");
  const dd = String(d.getDate()).padStart(2, "0");
  return `${mm}-${dd}`;
}

/** A source is "stale" when its note hasn't been touched in >180 days. */
function isStale(iso?: string): boolean {
  if (!iso) return false;
  const t = new Date(iso).getTime();
  if (!Number.isFinite(t)) return false;
  return Date.now() - t > 180 * 24 * 3600 * 1000;
}

// ── Project-scope pill ───────────────────────────────────────────────────────
// First control in the filter row. Sets RecallRequest.project; the dropdown
// lists every project workspace plus an "All projects" (undefined) escape.

function ProjectScopePill({
  projects, value, onChange,
}: {
  projects: WorkspaceListItem[];
  value: string | undefined;
  onChange: (project: string | undefined) => void;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  // Close on outside click / Escape — same affordance as the other pickers.
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const label = value ?? "All projects";

  const pick = (next: string | undefined) => {
    onChange(next);
    setOpen(false);
  };

  return (
    <div ref={ref} className="relative">
      <button
        type="button"
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-label="Project scope"
        onClick={() => setOpen((o) => !o)}
        className="flex h-[32px] items-center gap-[8px] rounded-[8px] border border-line-2 bg-bg-1 px-[12px] transition-colors hover:border-accent-line"
      >
        <span className="h-[7px] w-[7px] shrink-0 rounded-[2px] bg-accent" />
        <span className="text-[12.5px] font-medium leading-4 text-t1">{label}</span>
        <ChevronDown size={10} className={cn("shrink-0 text-t3 transition-transform", open && "rotate-180")} />
      </button>

      {open && (
        <div
          role="listbox"
          className="absolute left-0 top-[calc(100%+6px)] z-20 flex min-w-[200px] flex-col overflow-hidden rounded-[10px] border border-line bg-bg-1 py-[5px] shadow-card"
        >
          <ProjectScopeOption label="All projects" active={value === undefined} onClick={() => pick(undefined)} />
          {projects.length > 0 && <div className="my-[4px] h-px bg-line" />}
          {projects.map((p) => (
            <ProjectScopeOption
              key={`${p.type}-${p.name}`}
              label={p.name}
              active={value === p.name}
              onClick={() => pick(p.name)}
            />
          ))}
        </div>
      )}
    </div>
  );
}

function ProjectScopeOption({
  label, active, onClick,
}: {
  label: string;
  active: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      role="option"
      aria-selected={active}
      onClick={onClick}
      className={cn(
        "flex items-center gap-[8px] px-[12px] py-[7px] text-left text-[12.5px] leading-4 transition-colors hover:bg-accent-soft",
        active ? "text-t1" : "text-t2",
      )}
    >
      <span className={cn("h-[7px] w-[7px] shrink-0 rounded-[2px]", active ? "bg-accent" : "bg-line-2")} />
      <span className="truncate">{label}</span>
    </button>
  );
}

// ── Presentational text extraction (synthesis prose → tiles / gap) ───────────

interface Figure { label: string; value: string; unit?: string }

/** Pull up to three headline figures out of the synthesis prose for the stat
 *  tiles. Recognises monetary amounts (£420m / $1.2bn) and multiples (4.2x).
 *  Purely cosmetic — the answer text remains the source of truth. */
function extractFigures(synthesis: string | undefined): Figure[] {
  if (!synthesis) return [];
  const out: Figure[] = [];
  const seen = new Set<string>();

  const money = synthesis.match(/[£$€]\s?\d[\d,.]*\s?(?:bn|m|k)?/gi) ?? [];
  for (const m of money) {
    const v = m.replace(/\s+/g, "");
    if (seen.has(v)) continue;
    seen.add(v);
    out.push({ label: out.length === 0 ? "Headline" : "Figure", value: v });
    if (out.length >= 2) break;
  }

  const mult = synthesis.match(/\b\d+(?:\.\d+)?\s?x\+?/gi) ?? [];
  for (const m of mult) {
    const v = m.replace(/\s+/g, "");
    if (seen.has(v)) continue;
    seen.add(v);
    out.push({ label: "Multiple", value: v });
    break;
  }

  return out.slice(0, 3);
}

/** Surface the contradiction / covenant-watch sentence from the synthesis — the
 *  first sentence flagging a breach, downside, contradiction, or caveat — to
 *  drive the amber-dot watch line. Returns null when the answer flags nothing
 *  (the line is never fabricated). */
function extractContradiction(synthesis: string | undefined): string | null {
  if (!synthesis) return null;
  const sentences = synthesis.split(/(?<=[.!?])\s+/);
  const flagged = sentences.find((s) =>
    /\b(breach|covenant|downside|contradict|conflict|caveat|however|unless|risk|mitigat)/i.test(s),
  );
  return flagged ? flagged.trim() : null;
}

/** Surface a "gap" sentence from the synthesis — the first sentence flagging
 *  something missing, unconfirmed, or absent from the vault — to drive the GAPS
 *  block. Excludes the sentence already used by the watch line so the two
 *  callouts never duplicate. Returns null when the answer flags nothing. */
function extractGap(synthesis: string | undefined, exclude: string | null): string | null {
  if (!synthesis) return null;
  const sentences = synthesis.split(/(?<=[.!?])\s+/);
  const flagged = sentences.find(
    (s) =>
      s.trim() !== exclude &&
      /\b(gap|missing|unconfirmed|no signed|not (?:in|found)|absent|incomplete|outstanding|pending|unanswered)\b/i.test(s),
  );
  return flagged ? flagged.trim() : null;
}
