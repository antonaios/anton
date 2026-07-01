import { useEffect, useState } from "react";
import { api } from "../lib/api";
import { cn } from "../lib/cn";
import type { DraftItem } from "../types";

/**
 * Drafts tab — the Library · Outputs surface.
 *
 * Lists draft outputs from Projects/<X>/12 Outputs/ across all projects (or
 * filtered to one), grouped by project as a grid of cards: a derived type badge,
 * the document title, its vault path, file-meta (age · size · ext) and an Open
 * affordance that deep-links into Obsidian for .md. Token-only, so it flips
 * automatically between the LIGHT teal and DARK navy+gold themes.
 */

/**
 * Derive a short UPPERCASE type badge from a draft's file name — purely
 * presentational (no data-model field), so it scans like a document kind.
 * Falls back to the extension (sans dot) when nothing matches.
 */
function typeLabel(it: DraftItem): string {
  const n = it.name.toLowerCase();
  if (n.includes("ic-memo") || n.includes("ic memo")) return "IC MEMO";
  if (n.includes("lbo")) return "LBO MODEL";
  if (n.includes("comps")) return "COMPS";
  if (n.includes("teaser")) return "TEASER";
  if (n.includes("deal-tracker") || n.includes("deal tracker")) return "DEAL TRACKER";
  if (n.includes("valuation")) return "VALUATION";
  if (n.includes("dcf")) return "DCF";
  if (n.includes("merger")) return "MERGER MODEL";
  if (n.includes("model")) return "MODEL";
  if (n.includes("memo")) return "MEMO";
  return it.ext.replace(/^\./, "").toUpperCase() || "FILE";
}

export function DraftsTab() {
  const [items, setItems] = useState<DraftItem[]>([]);
  const [project, setProject] = useState<string>("");
  const [projects, setProjects] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const refresh = async () => {
    setBusy(true);
    setError(null);
    try {
      const r = await api.drafts(project || undefined, 100);
      setItems(r.items);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Unknown error");
    } finally {
      setBusy(false);
    }
  };

  useEffect(() => {
    api.listWorkspaces("project")
      .then((r) => setProjects(r.workspaces.map((w) => w.name)))
      .catch(() => { /* ignore */ });
  }, []);

  useEffect(() => { void refresh(); /* eslint-disable-next-line react-hooks/exhaustive-deps */ }, [project]);

  // Group by project for display.
  const grouped = items.reduce<Record<string, DraftItem[]>>((acc, it) => {
    acc[it.project] = acc[it.project] ?? [];
    acc[it.project].push(it);
    return acc;
  }, {});

  // Per-project draft counts (from the current items) for the filter chips.
  const countByProject = items.reduce<Record<string, number>>((acc, it) => {
    acc[it.project] = (acc[it.project] ?? 0) + 1;
    return acc;
  }, {});

  return (
    <div className="flex max-w-[1060px] flex-col gap-5 px-7 py-[30px]">
      {/* ── Page header ──────────────────────────────────────────────────── */}
      <header className="flex flex-col gap-[7px]">
        <div className="flex items-baseline justify-between gap-4">
          <h2 className="text-[22px] font-semibold leading-[120%] tracking-[-0.01em] text-t1">Outputs</h2>
          <span className="mono text-[11px] leading-[14px] text-t3">
            {items.length} drafts · {Object.keys(grouped).length} projects · /api/drafts
          </span>
        </div>
        <p className="max-w-full text-[13px] leading-[150%] text-t2">
          Generated documents — models, memos, comps and teasers. Drafts open in Obsidian for review.
        </p>
      </header>

      {/* ── Project filter chips + sort ──────────────────────────────────── */}
      <div className="flex items-center justify-between gap-[14px]">
        <div className="flex flex-wrap items-center gap-2">
          <button
            type="button"
            onClick={() => setProject("")}
            className={cn(
              "flex h-[30px] items-center gap-[7px] rounded-lg border text-[12.5px] font-medium leading-4 transition-colors",
              project === ""
                ? "border-accent-line bg-accent-soft px-[13px] text-accent"
                : "border-line-2 bg-bg-1 px-3 text-t1 hover:bg-bg-2",
            )}
          >
            All
            <span className="mono text-[11px] leading-[14px] text-t3">{items.length}</span>
          </button>
          {projects.map((p) => {
            const count = countByProject[p];
            return (
              <button
                key={p}
                type="button"
                onClick={() => setProject(p)}
                className={cn(
                  "flex h-[30px] items-center gap-[7px] rounded-lg border px-3 text-[12.5px] font-medium leading-4 transition-colors",
                  project === p
                    ? "border-accent-line bg-accent-soft text-accent"
                    : count
                      ? "border-line-2 bg-bg-1 text-t1 hover:bg-bg-2"
                      : "border-line-2 bg-bg-1 text-t3 hover:bg-bg-2",
                )}
              >
                {p}
                {count ? <span className="mono text-[11px] leading-[14px] text-t3">{count}</span> : null}
              </button>
            );
          })}
        </div>
        <span className="mono flex h-[30px] shrink-0 items-center gap-[7px] rounded-lg border border-line-2 bg-bg-1 px-[11px] text-[12px] leading-4 text-t3">
          Recent first
          <svg width="10" height="10" viewBox="0 0 12 12" xmlns="http://www.w3.org/2000/svg" className="shrink-0">
            <path d="M3 4.5L6 7.5L9 4.5" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
        </span>
      </div>

      {error && (
        <div className="rounded-lg border border-red/40 bg-red/[0.1] px-[14px] py-[10px] text-[12px] text-red">
          Bridge offline — {error}
        </div>
      )}

      {!busy && items.length === 0 && !error && (
        <div className="rounded-xl border border-line bg-bg-1 px-4 py-[15px] text-[12px] text-t3">
          No drafts found in any <span className="mono text-t1">Projects/&lt;X&gt;/12 Outputs/</span>.
        </div>
      )}

      {/* ── Project-grouped draft cards ──────────────────────────────────── */}
      {Object.entries(grouped).map(([proj, rows]) => (
        <section key={proj} className="flex flex-col gap-[13px]">
          <div className="flex items-center gap-[11px]">
            <span className="h-[7px] w-[7px] shrink-0 rounded-[2px] bg-accent" />
            <h3 className="shrink-0 whitespace-nowrap text-[13px] font-semibold leading-4 text-t1">Project {proj}</h3>
            <span className="mono shrink-0 text-[11px] leading-[14px] text-t3">
              {rows.length} draft{rows.length === 1 ? "" : "s"}
            </span>
            <span className="h-px grow bg-line" />
          </div>

          <div className="flex flex-wrap gap-4">
            {rows.map((it) => {
              // Obsidian opens (or reveals) any vault file via obsidian://open,
              // so every output card gets an "Open ↗" affordance — not just .md.
              const obsHref = `obsidian://open?path=${encodeURIComponent(it.path)}`;
              const sizeKb = Math.round(it.size_bytes / 1024);
              return (
                <div
                  key={it.path}
                  className="flex flex-[1_1_410px] min-w-0 max-w-[520px] flex-col gap-[9px] rounded-xl border border-line bg-bg-1 px-4 py-[15px]"
                >
                  <div className="flex items-center gap-2.5">
                    <span className="flex h-[19px] items-center rounded-[5px] border border-line-2 px-[7px] text-[9px] font-bold leading-3 tracking-[0.07em] text-t2">
                      {typeLabel(it)}
                    </span>
                  </div>
                  <div className="flex flex-col gap-1">
                    <h4 className="line-clamp-1 text-[14px] font-semibold leading-[135%] text-t1">{it.name}</h4>
                    <span className="mono line-clamp-1 text-[10.5px] leading-[14px] text-accent">{it.path}</span>
                  </div>
                  <div className="flex items-center justify-between gap-2.5">
                    <span className="mono text-[10.5px] leading-[14px] text-t3">
                      {it.ago} · <span className="tabular">{sizeKb} KB</span> · {it.ext}
                    </span>
                    <a
                      href={obsHref}
                      className="shrink-0 text-[11.5px] leading-[14px] text-accent hover:underline"
                    >
                      Open ↗
                    </a>
                  </div>
                </div>
              );
            })}
          </div>
        </section>
      ))}
    </div>
  );
}
