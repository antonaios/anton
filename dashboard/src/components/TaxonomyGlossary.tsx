import { useState } from "react";

/**
 * #taxonomy-glossary — a static "Key" panel atop the Taxonomy tab that defines the
 * shared vocabulary used across this catalog AND the Providers tab, so the operator
 * can reconcile the two in one place. Read-only, no backend; the live "what exists"
 * stays the catalog tables below. Collapse state persists per-operator (localStorage).
 */

const STORE_KEY = "taxonomy.glossary.open.v1";

function loadOpen(): boolean {
  try {
    const v = window.localStorage.getItem(STORE_KEY);
    return v === null ? false : v === "1";   // default collapsed (Paper bar); persists an explicit choice
  } catch {
    return false;
  }
}

type Accent = "red" | "accent" | undefined;
interface Term { term: string; def: string; accent?: Accent }
interface Group { title: string; terms: Term[] }

const GROUPS: Group[] = [
  {
    title: "What you run",
    terms: [
      { term: "Skill", def: "one named tool you invoke (lbo, comps, sector-news) — a single worker doing one job." },
      { term: "Crew", def: "a team of agents run together as a subprocess (triage, explore, debate, digest), each with named roles." },
      { term: "Composite", def: "a multi-step workflow over the HTTP boundary (Synapse) — chains several steps into one verb." },
    ],
  },
  {
    title: "How work is routed",
    terms: [
      { term: "Task type", def: "the KIND of work (synthesis, triage, cross-check…). Paired with sensitivity it decides which model is used — a routing label, not something you launch." },
      { term: "Lane", def: "the concrete model channel a call uses — claude-cli, claude-cli-haiku, codex-cli, ollama, minimax." },
      { term: "Provider", def: "who actually runs the call: Claude (anthropic), Codex (openai), local (ollama), or MiniMax." },
      { term: "Plan tier", def: "bridge = default, local-first (confidential/MNPI stay local). enterprise = unlocks confidential/MNPI → cloud under the rules below." },
    ],
  },
  {
    title: "Sensitivity tiers (most → least restrictive)",
    terms: [
      { term: "MNPI", def: "material non-public info. Local-only — unless enterprise tier AND an active per-provider attestation.", accent: "red" },
      { term: "Confidential", def: "local — unless enterprise tier, and then the Claude lane only (never Codex).", accent: "accent" },
      { term: "Internal", def: "cloud-eligible." },
      { term: "Public", def: "cloud-eligible." },
    ],
  },
  {
    title: "Controls (the Providers tab)",
    terms: [
      { term: "Promotion", def: "move a crew — or just one role — from local to a cloud model. Sensitivity is still enforced server-side." },
      { term: "Attestation", def: "the operator permission (DPA + ZDR + no-training) that lets MNPI reach a specific provider's cloud, under enterprise tier. The most consequential control." },
      { term: "Provider ceiling", def: "an optional per-provider cap on how sensitive a provider may handle (e.g. openai → internal only)." },
    ],
  },
  {
    title: "Catalog columns (this tab)",
    terms: [
      { term: "Wired-state", def: "wired = reaches a live bridge call from a tile; stub = tile but not wired; routine = cron-only, no tile; unmapped = registry↔dashboard drift." },
      { term: "Scope", def: "which workspace types a skill may run in (project / bd / general / any)." },
      { term: "Cost cap", def: "the per-run token cap the bridge enforces, plus a wall-clock timeout at the dispatcher." },
      { term: "Output", def: "where a skill writes — its vault-write globs and any captured-conclusion target." },
    ],
  },
];

function termCls(accent: Accent): string {
  if (accent === "red") return "text-red";
  if (accent === "accent") return "text-accent";
  return "text-t2";
}

export function TaxonomyGlossary() {
  const [open, setOpen] = useState<boolean>(loadOpen);

  const toggle = () => setOpen((v) => {
    const next = !v;
    try { window.localStorage.setItem(STORE_KEY, next ? "1" : "0"); } catch { /* best-effort */ }
    return next;
  });

  return (
    <section className="flex flex-col gap-4">
      {/* Collapsed key bar — single horizontal row matching Paper B8T-0:
          ▸ glyph · title · subtitle · ml-auto show/hide. */}
      <button
        type="button"
        onClick={toggle}
        aria-expanded={open}
        className="flex items-center gap-[11px] rounded-[11px] border border-line bg-bg-2 py-[11px] px-[16px] text-left group"
      >
        <span className="text-[11px] leading-[14px] text-t2 whitespace-nowrap">{open ? "▾" : "▸"}</span>
        <span className="text-[12.5px] w-max shrink-0 font-semibold leading-4 text-t1">Key — what these terms mean</span>
        <span className="text-[11.5px] w-max shrink-0 leading-[14px] text-t3">Reconciles the vocabulary used here and on the Providers tab</span>
        <span className="ml-auto text-[11px] w-max shrink-0 leading-[14px] text-t2 group-hover:text-accent transition-colors whitespace-nowrap">
          {open ? "hide" : "show"}
        </span>
      </button>

      {open && (
        <div className="rounded-[14px] border border-line bg-bg-1 overflow-hidden">
          <div className="grid grid-cols-[repeat(auto-fit,minmax(300px,1fr))] sm:grid-cols-2 lg:grid-cols-3">
            {GROUPS.map((g) => (
              <div key={g.title} className="flex flex-col gap-[10px] p-[15px_18px] border-r border-b border-line">
                <span className="text-[10px] tracking-[0.11em] uppercase text-t3">{g.title}</span>
                <div className="flex flex-col gap-[9px]">
                  {g.terms.map((t) => (
                    <div key={t.term} className="grid grid-cols-[118px_1fr] gap-3 items-start">
                      <span className={`text-[11px] leading-relaxed font-medium ${termCls(t.accent)}`}>{t.term}</span>
                      <span className="text-[11px] leading-relaxed text-t2">{t.def}</span>
                    </div>
                  ))}
                </div>
              </div>
            ))}
          </div>
        </div>
      )}
    </section>
  );
}
