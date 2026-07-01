import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Search, Star } from "lucide-react";
import { api, ApiError } from "../lib/api";
import { cn } from "../lib/cn";
import { wiredStateForSkill, workflowForSkill, type WiredState } from "../lib/wiring";
import { TaxonomyGlossary } from "./TaxonomyGlossary";
import { Chip } from "./ui/Chip";
import { StatusBadge, type StatusKind } from "./ui/StatusBadge";
import type { SkillTaxonomyRow, SkillTaxonomyResponse, SkillsProvidersResponse, CrewManifestEntry } from "../types";

const POLL_MS = 60_000;
const FOCUS_DEBOUNCE_MS = 5_000;

// Favourite pins are operator-local (no backend) — persisted to localStorage so
// they survive a reload. Keyed by verb name.
const PIN_STORAGE_KEY = "taxonomy.pins.v1";

const SENSITIVITY_CLS: Record<string, string> = {
  public:       "text-green",
  internal:     "text-t2",
  confidential: "text-amber",
  MNPI:         "text-red",
};

// Wired-state → status-dot kind + label. The dot colour mirrors the catalog-
// shape legend: wired = sage, stub = running, routine = faint, unmapped = error.
const WIRED_STATUS: Record<WiredState, { status: StatusKind; label: string; title: string }> = {
  wired:   { status: "ok",      label: "wired",   title: "Reaches a live bridge call from the dashboard" },
  stub:    { status: "running", label: "stub",    title: "Has a dashboard tile/command but isn't wired through yet" },
  routine: { status: "paused",  label: "routine", title: "Background cron routine — no operator-facing tile" },
  unknown: { status: "error",   label: "unmapped", title: "Not classified in the dashboard's wired-workflow table — registry↔dashboard drift; add it to SKILL_TO_WORKFLOW" },
};

// Paper STATE cell: the dot carries semantic colour, the word reads muted —
// except stub (accent, the catalog-shape stub colour) and unmapped (the drift
// warning stays loud).
const STATE_WORD_CLS: Record<WiredState, string> = {
  wired:   "text-t2",
  routine: "text-t3",
  stub:    "text-accent",
  unknown: "text-red",
};

type SensFilter = "all" | "public" | "internal" | "confidential" | "MNPI";
type WiredFilter = "all" | WiredState;

// Presentational family grouping for the catalog tables. Purely a render-time
// classifier over the verb name — it does NOT touch the data, the fetch, the
// filters or the pinned-first/alphabetical sort; it only decides which family
// lane a row renders under. Verbs not in any bucket fall into "Other".
const FAMILY_ORDER = [
  "Valuation & modelling",
  "Research & monitoring",
  "Vault & housekeeping",
  "Other",
] as const;
type Family = (typeof FAMILY_ORDER)[number];

const FAMILY_OF: Record<string, Family> = {
  comps:              "Valuation & modelling",
  lbo:                "Valuation & modelling",
  "lbo-intake-agent": "Valuation & modelling",
  "ticker-multiples": "Valuation & modelling",
  "equity-research":  "Research & monitoring",
  "sector-news":      "Research & monitoring",
  "deal-tracker":     "Research & monitoring",
  "morning-brief":    "Research & monitoring",
  "recall-query":     "Vault & housekeeping",
  "actions-decay":    "Vault & housekeeping",
  "bd-decay":         "Vault & housekeeping",
  "vault-health":     "Vault & housekeeping",
  "lessons-suggest":  "Vault & housekeeping",
};

function familyOf(name: string): Family {
  return FAMILY_OF[name] ?? "Other";
}

/**
 * #35 · TAXONOMY tab — the verb catalog.
 *
 * One row per registered skill verb, sourced from SKILL.md frontmatter over
 * GET /api/skills/taxonomy. Columns: Verb · Tile · Description · Sensitivity ·
 * Scope · Cost cap · Output destination · Wired-state. Search box + sensitivity
 * / scope / wired filters (the metadata-filter pattern from #35 §6) + favourite
 * pins + a per-row "recent runs" hint (telemetry roll-up).
 *
 * The taxonomy endpoint is NEW — a bridge that hasn't restarted onto it returns
 * 404. In that case the tab degrades to the always-live providers matrix
 * (sensitivity / scope / cost / telemetry) and shows a banner: the three
 * frontmatter columns (description, tile, cost ceilings) light up after a
 * bridge restart. Refresh: focus + every 60s. Self-fetches (like SkillsProvidersTab).
 */
export function TaxonomyTab() {
  const [data, setData] = useState<SkillTaxonomyResponse | null>(null);
  // Set when the taxonomy endpoint 404s and we fell back to the providers matrix.
  const [degraded, setDegraded] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [query, setQuery] = useState("");
  const [sensFilter, setSensFilter] = useState<SensFilter>("all");
  const [wiredFilter, setWiredFilter] = useState<WiredFilter>("all");
  const [pins, setPins] = useState<Set<string>>(() => loadPins());
  // #front-door — crews are a separate (subprocess) lane; surfaced from the live
  // manifest so the catalog can't drift. Best-effort: a pre-crew bridge 404s.
  const [crews, setCrews] = useState<CrewManifestEntry[]>([]);

  const abortRef = useRef<AbortController | null>(null);
  const lastFetchAtRef = useRef(0);

  const load = useCallback(async () => {
    abortRef.current?.abort();
    const ac = new AbortController();
    abortRef.current = ac;
    lastFetchAtRef.current = Date.now();
    try {
      const r = await api.skillsTaxonomy(ac.signal);
      if (ac.signal.aborted) return;
      setData(r);
      setDegraded(false);
      setError(null);
    } catch (e) {
      if (ac.signal.aborted) return;
      // 404 → the running bridge predates the taxonomy endpoint. Fall back to
      // the always-live providers matrix and flag the degraded columns.
      if (e instanceof ApiError && (e.status === 404 || e.status === 405)) {
        try {
          const p = await api.skillsProviders(ac.signal);
          if (ac.signal.aborted) return;
          setData(providersToTaxonomy(p));
          setDegraded(true);
          setError(null);
        } catch (e2) {
          if (ac.signal.aborted) return;
          setError(e2 instanceof ApiError ? `${e2.status}: ${e2.message}` : String(e2));
        }
      } else {
        setError(e instanceof ApiError ? `${e.status}: ${e.message}` : String(e));
      }
    } finally {
      if (!ac.signal.aborted) setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
    const id = window.setInterval(() => void load(), POLL_MS);
    return () => { window.clearInterval(id); abortRef.current?.abort(); };
  }, [load]);

  // #front-door — crews catalog (GET /api/crew/manifest). Best-effort + once on
  // mount; a bridge without the endpoint just leaves the section empty.
  useEffect(() => {
    let alive = true;
    api.crewManifest()
      .then((r) => { if (alive) setCrews(r.crews); })
      .catch(() => { /* pre-crew bridge — leave crews empty */ });
    return () => { alive = false; };
  }, []);

  useEffect(() => {
    const onFocus = () => {
      if (Date.now() - lastFetchAtRef.current >= FOCUS_DEBOUNCE_MS) void load();
    };
    window.addEventListener("focus", onFocus);
    return () => window.removeEventListener("focus", onFocus);
  }, [load]);

  const togglePin = (name: string) => {
    setPins((prev) => {
      const next = new Set(prev);
      if (next.has(name)) next.delete(name); else next.add(name);
      savePins(next);
      return next;
    });
  };

  const allRows = data?.skills ?? [];

  // Filter (search across name/tile/description; sensitivity + wired-state
  // pills), then sort pinned-first, then by name.
  const rows = useMemo(() => {
    const q = query.trim().toLowerCase();
    const filtered = allRows.filter((r) => {
      if (sensFilter !== "all" && r.sensitivity !== sensFilter) return false;
      if (wiredFilter !== "all" && wiredStateForSkill(r.name) !== wiredFilter) return false;
      if (!q) return true;
      return (
        r.name.toLowerCase().includes(q)
        || r.tileLabel.toLowerCase().includes(q)
        || r.description.toLowerCase().includes(q)
        || r.workspaceScope.toLowerCase().includes(q)
      );
    });
    return [...filtered].sort((a, b) => {
      const ap = pins.has(a.name) ? 0 : 1;
      const bp = pins.has(b.name) ? 0 : 1;
      if (ap !== bp) return ap - bp;
      return a.name.localeCompare(b.name);
    });
  }, [allRows, query, sensFilter, wiredFilter, pins]);

  // Presentational: bucket the already-filtered/sorted rows into family lanes,
  // preserving the within-lane order the sort above produced. Pure render-time
  // grouping — no data, fetch, filter or sort semantics change.
  const families = useMemo(() => {
    const map = new Map<Family, SkillTaxonomyRow[]>();
    for (const r of rows) {
      const fam = familyOf(r.name);
      const bucket = map.get(fam);
      if (bucket) bucket.push(r);
      else map.set(fam, [r]);
    }
    return FAMILY_ORDER
      .filter((f) => map.has(f))
      .map((f) => [f, map.get(f)!] as const);
  }, [rows]);

  // Catalog-shape roll-up — derived entirely from the live rows. Counts the four
  // wired states (for the segmented bar + legend) and the sensitivity split.
  const shape = useMemo(() => {
    const wired = { wired: 0, stub: 0, routine: 0, unknown: 0 };
    const sens = { public: 0, internal: 0, confidential: 0, MNPI: 0 };
    for (const r of allRows) {
      wired[wiredStateForSkill(r.name)] += 1;
      if (r.sensitivity in sens) sens[r.sensitivity as keyof typeof sens] += 1;
    }
    return { wired, sens };
  }, [allRows]);

  const counts = data?.counts ?? {};

  return (
    <div className="flex flex-col gap-5 px-[28px] py-[30px] max-w-[1060px]">
      <div className="flex flex-col gap-[7px]">
        <header className="flex items-baseline justify-between gap-4">
          <h2 className="text-[22px] leading-[120%] font-semibold tracking-[-0.01em] text-t1">Taxonomy</h2>
          <span className="font-mono text-[11px] leading-[14px] text-t3">
            {data ? `${allRows.length} verbs` : "…"}
            {typeof counts.composites === "number" ? ` · ${counts.composites} composites` : ""}
            {typeof counts.crews === "number" ? ` · ${counts.crews} crews` : ""}
            {data ? ` · as of ${new Date(data.asOf).toLocaleTimeString()}` : ""}
          </span>
        </header>

        <p className="text-[13px] leading-[150%] text-t2 m-0">
          Every skill verb catalogued from <span className="text-t2">SKILL.md frontmatter</span> — the same
          registry the dispatcher reads, so it can't drift. Wired-state cross-references the
          dashboard's workflow table; <span className="text-t2">routine</span> verbs fire on cron with
          no operator tile. Composites &amp; crews land as their own lanes once on disk.
        </p>
      </div>

      {/* Catalog shape — wired-state distribution bar + sensitivity split, all
          derived from the live rows (no extra fetch). */}
      {data && (
        <div className="rounded-[14px] bg-bg-1 border border-line px-5 py-4">
          <div className="flex items-center justify-between mb-[11px]">
            <span className="font-mono text-[9.5px] tracking-[0.1em] font-semibold leading-3 text-t2">CATALOG SHAPE</span>
            <Chip
              variant={shape.wired.unknown > 0 ? "oxblood" : "sage"}
              label={shape.wired.unknown > 0 ? `${shape.wired.unknown} unmapped` : "✓ no drift · 0 unmapped"}
            />
          </div>

          <ShapeBar wired={shape.wired} total={allRows.length} />

          <div className="mt-[11px] flex flex-wrap items-center gap-x-5 gap-y-[6px]">
            <LegendDot cls="bg-green"  label={`${shape.wired.wired} wired`} />
            <LegendDot cls="bg-accent" label={`${shape.wired.stub} stub`} />
            <LegendDot cls="bg-t4"     label={`${shape.wired.routine} routine`} />
            <LegendDot cls="border border-line-2" label={`${shape.wired.unknown} unmapped`} muted />
          </div>

          <div className="my-[14px] h-px bg-line" />

          <div className="flex flex-wrap items-center justify-between gap-y-[6px] text-[12px] leading-4">
            <div className="flex flex-wrap items-center gap-x-4 gap-y-[4px]">
              <span className="font-mono text-[10px] tracking-[0.07em] leading-3 text-t3">SENSITIVITY</span>
              <div className="flex gap-x-[13px]">
                <span className="text-green">{shape.sens.public} public</span>
                <span className="text-t2">{shape.sens.internal} internal</span>
                <span className="text-amber">{shape.sens.confidential} confidential</span>
                <span className={cn(shape.sens.MNPI > 0 ? "text-red" : "text-t3")}>{shape.sens.MNPI} MNPI</span>
              </div>
            </div>
            <div className="flex items-center gap-x-[13px]">
              <span className="text-t1">{counts.skills ?? allRows.length} skills</span>
              <span className="text-t2">{counts.composites ?? 0} composites</span>
              <span className="text-t2">{counts.crews ?? crews.length} crews</span>
            </div>
          </div>
        </div>
      )}

      <TaxonomyGlossary />

      {degraded && (
        <div className="rounded-lg border border-accent-line bg-accent-soft/40 px-[12px] py-[8px] text-[11px] text-t2">
          Bridge predates <span className="font-mono text-t1">/api/skills/taxonomy</span> — showing the live
          providers matrix. <span className="text-t1">Description, tile, and cost-ceiling columns</span> light
          up after the operator restarts the bridge onto the new endpoint.
        </div>
      )}

      {/* Controls — search + metadata filters (#35 §6 metadata-filter pattern) */}
      <div className="flex flex-wrap items-center gap-[10px]">
        <div className="relative">
          <Search size={13} className="pointer-events-none absolute left-[12px] top-1/2 -translate-y-1/2 text-t3" />
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search verbs, tiles, triggers…"
            className="w-[260px] rounded-[7px] border border-line-2 bg-bg-2 pl-[33px] pr-[12px] py-[8px] text-[12px] text-t1 placeholder:text-t3 outline-none transition-colors focus:border-accent-line"
          />
        </div>
        <FilterPills<SensFilter>
          label="Sensitivity"
          value={sensFilter}
          onChange={setSensFilter}
          options={["all", "public", "internal", "confidential", "MNPI"]}
        />
        <FilterPills<WiredFilter>
          label="State"
          value={wiredFilter}
          onChange={setWiredFilter}
          options={["all", "wired", "stub", "routine", "unknown"]}
        />
        <span className="font-mono text-[10px] leading-3 text-t3">{rows.length} shown</span>
      </div>

      {loading && !data ? (
        <div className="text-[11px] italic text-t3">Loading…</div>
      ) : error && !data ? (
        <div className="text-[11px] italic text-t3">Catalog unavailable — {error}</div>
      ) : (
        <div className="rounded-[13px] overflow-hidden border border-line">
          <div className="flex items-baseline justify-between border-b border-line px-4 py-[10px]">
            <span className="font-mono text-[10px] tracking-[0.1em] font-semibold leading-3 text-t1">SKILLS · {allRows.length}</span>
            <span className="text-[10px] leading-3 text-t3">named tools — one worker, one job · grouped by family</span>
          </div>
          <div className="overflow-x-auto">
            <div className="min-w-[760px]">
              {/* Column header row */}
              <div className="flex items-center gap-3 px-4 py-2 bg-bg-2 border-b border-line">
                <span className="w-[28px] shrink-0" />
                <span className="w-[150px] shrink-0 font-mono text-[9px] tracking-[0.07em] font-semibold leading-3 text-t1">VERB</span>
                <span className="grow basis-0 font-mono text-[9px] tracking-[0.07em] font-semibold leading-3 text-t1">DESCRIPTION</span>
                <span className="w-[104px] shrink-0 font-mono text-[9px] tracking-[0.07em] font-semibold leading-3 text-t1">SENSITIVITY</span>
                <span className="w-[60px] shrink-0 font-mono text-[9px] tracking-[0.07em] font-semibold leading-3 text-t1">SCOPE</span>
                <span className="w-[82px] shrink-0 text-right font-mono text-[9px] tracking-[0.07em] font-semibold leading-3 text-t1">COST</span>
                <span className="w-[108px] shrink-0 font-mono text-[9px] tracking-[0.07em] font-semibold leading-3 text-t1">OUTPUT</span>
                <span className="w-[74px] shrink-0 font-mono text-[9px] tracking-[0.07em] font-semibold leading-3 text-t1">STATE</span>
                <span className="w-[88px] shrink-0 text-right font-mono text-[9px] tracking-[0.07em] font-semibold leading-3 text-t1">RECENT</span>
              </div>

              {families.map(([family, famRows]) => (
                <FamilyGroup key={family}>
                  <div className="flex items-center gap-2 px-4 py-[6px] bg-paper2">
                    <span className="font-mono text-[9px] tracking-[0.09em] font-semibold leading-3 text-t2">{family}</span>
                    <span className="font-mono text-[9px] leading-3 text-t3">· {famRows.length}</span>
                  </div>
                  {famRows.map((row) => (
                    <TaxonomyRow
                      key={row.name}
                      row={row}
                      degraded={degraded}
                      pinned={pins.has(row.name)}
                      onTogglePin={() => togglePin(row.name)}
                    />
                  ))}
                </FamilyGroup>
              ))}
              {rows.length === 0 && (
                <div className="px-4 py-[14px] text-[11px] italic text-t3">No verbs match the current filters.</div>
              )}
            </div>
          </div>
        </div>
      )}

      {error && data && (
        <div className="text-[10.5px] text-t4">last refresh failed ({error}) — showing the previous snapshot.</div>
      )}

      {/* Composites lane — the verb-catalog slot for multi-step composite
          pipelines (routines/composites/*.json). The bridge returns composites=[]
          until the lane lands on disk, so this shows the HONEST empty state (not
          the design mock); the table renders here once typed composite rows exist. */}
      <div className="rounded-[13px] overflow-hidden border border-line">
        <div className="flex items-baseline justify-between border-b border-line px-[14px] py-[9px]">
          <span className="font-mono text-[9.5px] tracking-[0.1em] font-semibold leading-3 text-t1">COMPOSITES · {counts.composites ?? 0}</span>
          <span className="text-[10px] leading-3 text-t3">multi-step workflows over the HTTP boundary (Synapse) — chained into one verb</span>
        </div>
        <div className="px-[14px] py-[13px] text-[11.5px] leading-[150%] text-t3">
          {(counts.composites ?? 0) === 0 ? (
            <>No composites registered yet — the composite lane lands once <span className="font-mono text-t4">routines/composites/*.json</span> are on disk.</>
          ) : (
            <>{counts.composites} composite{counts.composites === 1 ? "" : "s"} registered.</>
          )}
        </div>
      </div>

      {/* #front-door — crews (subprocess lane). Catalogued from the live manifest.
          Always present (parity with COMPOSITES): a pre-crew bridge that returns an
          empty manifest shows the HONEST empty state rather than omitting the card. */}
      <div className="rounded-[13px] overflow-hidden border border-line">
        <div className="flex items-baseline justify-between border-b border-line px-[14px] py-[9px]">
          <span className="font-mono text-[9.5px] tracking-[0.1em] font-semibold leading-3 text-t1">CREWS · {crews.length}</span>
          <span className="text-[10px] leading-3 text-t3">subprocess lane · always-local Ollama · fire from the Workflows drawer</span>
        </div>
        {crews.length === 0 ? (
          <div className="px-[14px] py-[13px] text-[11.5px] leading-[150%] text-t3">
            No crews registered yet — crews land once <span className="font-mono text-t4">routines/crews/*.py</span> are on disk and the bridge serves the manifest.
          </div>
        ) : (
          <div className="overflow-x-auto">
            <div className="min-w-[760px]">
              {/* Column header row */}
              <div className="flex items-center gap-3 px-4 py-[9px] bg-bg-2 border-b border-line">
                <span className="w-[140px] shrink-0 font-mono text-[9px] tracking-[0.07em] font-semibold leading-3 text-t1">CREW</span>
                <span className="grow basis-0 font-mono text-[9px] tracking-[0.07em] font-semibold leading-3 text-t1">DESCRIPTION</span>
                <span className="w-[96px] shrink-0 font-mono text-[9px] tracking-[0.07em] font-semibold leading-3 text-t1">SENSITIVITY</span>
                <span className="w-[210px] shrink-0 font-mono text-[9px] tracking-[0.07em] font-semibold leading-3 text-t1">ROLES</span>
                <span className="w-[104px] shrink-0 text-right font-mono text-[9px] tracking-[0.07em] font-semibold leading-3 text-t1">COST CAP</span>
              </div>

              {crews.map((c) => (
                <div key={c.verb} className="flex items-center gap-3 px-4 py-[11px] border-b border-line last:border-b-0">
                  <span className="w-[140px] shrink-0 font-mono text-[12px] leading-4 text-t1">{c.verb}</span>
                  <span className="grow basis-0 text-[11.5px] leading-[14px] text-t3"><Description text={c.description} /></span>
                  <span className={cn("w-[96px] shrink-0 font-mono text-[10.5px] leading-[14px]", c.sensitivity_override === "MNPI" ? "text-red" : "text-t2")}>
                    {c.sensitivity_override ?? "inherit"}
                  </span>
                  <span className="w-[210px] shrink-0 text-[11px] leading-[14px] text-t2" title={c.roles.join(" · ")}>
                    {c.roles.length} · {c.roles.slice(0, 3).join(", ")}{c.roles.length > 3 ? "…" : ""}
                  </span>
                  <span className="w-[104px] shrink-0 text-right font-mono text-[10.5px] leading-[14px] tabular-nums text-t2">
                    {fmtTokens(c.cost_cap_tokens)} · {c.cost_cap_seconds}s
                  </span>
                </div>
              ))}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

// Wraps a family lane's group of row <div>s. Plain fragment — exists so the map
// can key the lane while still emitting bare row children into the table body.
function FamilyGroup({ children }: { children: ReactNode }) {
  return <>{children}</>;
}

// Segmented distribution bar for the catalog-shape card: one filled segment per
// wired-state, widths proportional to its share of the total.
function ShapeBar({
  wired, total, className,
}: {
  wired: { wired: number; stub: number; routine: number; unknown: number };
  total: number;
  className?: string;
}) {
  const segs = [
    { key: "wired",   n: wired.wired,   cls: "bg-green" },
    { key: "stub",    n: wired.stub,    cls: "bg-accent" },
    { key: "routine", n: wired.routine, cls: "bg-t4" },
    { key: "unknown", n: wired.unknown, cls: "bg-red/60" },
  ];
  return (
    <div className={cn("flex h-[9px] w-full gap-[2px] overflow-hidden rounded-[5px]", className)}>
      {total === 0 ? (
        <div className="h-full w-full rounded-[4px] bg-bg-2" />
      ) : (
        segs
          .filter((s) => s.n > 0)
          .map((s) => (
            <div
              key={s.key}
              className={cn("h-full rounded-[4px]", s.cls)}
              style={{ width: `${(s.n / total) * 100}%` }}
              title={`${s.n} ${s.key}`}
            />
          ))
      )}
    </div>
  );
}

function LegendDot({ cls, label, muted }: { cls: string; label: string; muted?: boolean }) {
  return (
    <span className={cn("inline-flex items-center gap-[6px] text-[12px] leading-4", muted ? "text-t3" : "text-t1")}>
      <span className={cn("h-2 w-2 shrink-0 rounded-[2px]", cls)} />
      {label}
    </span>
  );
}

function TaxonomyRow({
  row, degraded, pinned, onTogglePin,
}: {
  row: SkillTaxonomyRow;
  degraded: boolean;
  pinned: boolean;
  onTogglePin: () => void;
}) {
  const state = wiredStateForSkill(row.name);
  const badge = WIRED_STATUS[state];
  const wf = workflowForSkill(row.name);
  const output = outputDestinations(row);

  return (
    <div className="flex items-center gap-3 px-4 py-[10px] border-b border-line last:border-b-0 hover:bg-bg-2/40 transition-colors">
      <span className="w-[28px] shrink-0">
        <button
          type="button"
          onClick={onTogglePin}
          title={pinned ? "Unpin" : "Pin to top"}
          className={cn("leading-none transition-colors", pinned ? "text-accent" : "text-t4 hover:text-t2")}
        >
          <Star size={13} className={pinned ? "fill-current" : ""} />
        </button>
      </span>

      <span className="w-[150px] shrink-0 font-mono text-[12px] leading-4 text-t1 whitespace-nowrap">
        {row.name}
        {wf && <span className="ml-[6px] text-[10px] text-t4" title="Dashboard workflow">· {wf}</span>}
      </span>

      <span className="grow basis-0 text-[11.5px] leading-[14px] text-t3">
        {degraded ? <span className="text-t4 italic">restart bridge for frontmatter description</span> : <Description text={row.description} />}
      </span>

      <span className={cn("w-[104px] shrink-0 font-mono text-[10.5px] leading-[14px]", SENSITIVITY_CLS[row.sensitivity] ?? "text-t2")}>
        {row.sensitivity}
      </span>

      <span className="w-[60px] shrink-0 text-[11.5px] leading-[14px] text-t2">{row.workspaceScope}</span>

      <span className="w-[82px] shrink-0 text-right font-mono text-[11px] leading-[14px] tabular-nums text-t2">
        {degraded ? (
          <span className="text-t4">—</span>
        ) : (
          <span title={`${row.costCeilingTokens.toLocaleString()} tokens · ${row.costCeilingSeconds}s`}>
            {fmtTokens(row.costCeilingTokens)} · {row.costCeilingSeconds}s
          </span>
        )}
      </span>

      <span className="w-[108px] shrink-0 text-[11px] leading-[14px] text-t3">
        {output.length ? (
          <span title={output.join("\n")}>
            {output[0]}
            {output.length > 1 && <span className="ml-[4px] text-t4">+{output.length - 1}</span>}
          </span>
        ) : (
          <span className="text-t4">—</span>
        )}
      </span>

      <span className="w-[74px] shrink-0 inline-flex items-center gap-[6px]">
        <StatusBadge status={badge.status} className="text-[11px]" />
        <span className={cn("text-[11px] leading-[16px]", STATE_WORD_CLS[state])}>{badge.label}</span>
      </span>

      <span className="w-[88px] shrink-0 text-right text-[11px] leading-[14px] text-t3 whitespace-nowrap">
        {row.calls > 0 ? (
          <span title={`${row.calls} calls · ${fmtUsd(row.costUsd)}${row.lastProvider ? ` · ${row.lastProvider}` : ""}`}>
            {fmtLastFire(row.lastFire)} <span className="text-t4">×{row.calls}</span>
          </span>
        ) : (
          <span className="text-t4">never</span>
        )}
      </span>
    </div>
  );
}

// Truncate the (often multi-paragraph) frontmatter description to the first
// sentence-ish; full text on hover.
function Description({ text }: { text: string }) {
  const oneLine = text.replace(/\s+/g, " ").trim();
  const short = oneLine.length > 150 ? `${oneLine.slice(0, 150)}…` : oneLine;
  return <span title={oneLine}>{short}</span>;
}

const SENS_LABEL: Record<SensFilter, string> = {
  all: "all", public: "pub", internal: "int", confidential: "conf", MNPI: "MNPI",
};
const WIRED_LABEL: Record<WiredFilter, string> = {
  all: "all", wired: "wired", stub: "stub", routine: "rout", unknown: "unmp",
};

function FilterPills<T extends string>({
  label, value, onChange, options,
}: {
  label: string;
  value: T;
  onChange: (v: T) => void;
  options: readonly T[];
}) {
  return (
    <div className="flex items-center gap-[7px]">
      <span className="font-mono text-[9px] tracking-[0.1em] uppercase text-t2">{label}</span>
      <div className="inline-flex items-center rounded-lg overflow-hidden border border-line">
        {options.map((opt) => (
          <button
            key={opt}
            type="button"
            onClick={() => onChange(opt)}
            className={cn(
              "px-[9px] py-[4px] text-[10px] leading-3 uppercase transition-colors",
              value === opt ? "bg-accent-soft text-t1" : "text-t2 hover:text-t1",
            )}
          >
            {(SENS_LABEL as Record<string, string>)[opt] ?? (WIRED_LABEL as Record<string, string>)[opt] ?? opt}
          </button>
        ))}
      </div>
    </div>
  );
}

// ── Helpers ──────────────────────────────────────────────────────────────────

/** "Output destination" = the full declared write surface: the #76 captures
 *  target (the dated semantic fact) PLUS every vault_write glob, deduped, the
 *  captures target first. Empty for read-only / proposal-only skills. The cell
 *  shows the first entry + a "+N" count, with the whole list in the tooltip, so
 *  the audit surface never under-reports where a skill writes. */
function outputDestinations(row: SkillTaxonomyRow): string[] {
  const all = [
    ...(row.capturesTarget ? [row.capturesTarget] : []),
    ...row.vaultWrite,
  ];
  return [...new Set(all)];
}

function fmtTokens(n: number): string {
  if (n >= 1000) return `${(n / 1000).toFixed(n % 1000 === 0 ? 0 : 1)}k`;
  return String(n);
}

function fmtUsd(v: number): string {
  if (v === 0) return "$0";
  if (v < 0.01) return "<$0.01";
  if (v < 1)    return `$${v.toFixed(3)}`;
  if (v < 100)  return `$${v.toFixed(2)}`;
  return `$${Math.round(v).toLocaleString()}`;
}

function fmtLastFire(iso: string | null | undefined): string {
  if (!iso) return "—";
  const t = Date.parse(iso);
  if (!Number.isFinite(t)) return iso;
  const d = new Date(t);
  const days = Math.floor((Date.now() - t) / 86_400_000);
  if (days <= 0) return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  if (days < 7) return `${days}d ago`;
  return d.toLocaleDateString([], { month: "short", day: "numeric" });
}

// Degraded fallback: synthesise taxonomy rows from the always-live providers
// matrix. Carries the columns the providers endpoint DOES have (sensitivity /
// scope / telemetry); the frontmatter-only columns render as "—" + a banner.
function providersToTaxonomy(p: SkillsProvidersResponse): SkillTaxonomyResponse {
  const skills: SkillTaxonomyRow[] = p.skills.map((s) => ({
    name: s.key,
    description: "",
    tileLabel: "",
    sensitivity: s.sensitivity,
    workspaceScope: s.workspaceScope,
    lane: "skill",
    version: "",
    costCeilingTokens: 0,
    costCeilingSeconds: 0,
    allowedTools: [],
    vaultWrite: [],
    capturesTarget: null,
    capturesSection: null,
    lastFire: s.lastFire ?? null,
    lastProvider: s.lastProvider ?? null,
    costUsd: s.costUsd,
    calls: s.calls,
  }));
  return {
    skills,
    composites: [],
    crews: [],
    counts: { skills: skills.length, composites: 0, crews: 0 },
    asOf: p.asOf,
  };
}

// ── Local favourite-pin persistence ──────────────────────────────────────────

function loadPins(): Set<string> {
  try {
    const raw = window.localStorage.getItem(PIN_STORAGE_KEY);
    if (!raw) return new Set();
    const arr = JSON.parse(raw) as unknown;
    return Array.isArray(arr) ? new Set(arr.filter((x): x is string => typeof x === "string")) : new Set();
  } catch {
    return new Set();
  }
}

function savePins(pins: Set<string>): void {
  try {
    window.localStorage.setItem(PIN_STORAGE_KEY, JSON.stringify([...pins]));
  } catch {
    /* localStorage unavailable — pins are best-effort */
  }
}
