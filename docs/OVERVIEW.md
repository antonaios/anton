# ANTON — platform overview

> A candid, public‑safe map of the platform. Every claim here is backed by code in the
> mirror. Limits are stated as plainly as capabilities. This is not a sales document and not
> a forward projection — it describes what exists.
>
> **Audience:** anyone reviewing, testing, or extending ANTON. **Sensitivity:** public.
> Single‑operator detail (identity, firm, hardware specifics) is generalised by design.

---

## 1. In one paragraph

ANTON is a local‑first **second brain + valuation engine + automation stack** for
M&A and investment professionals. A Markdown **vault** is the canonical memory; a FastAPI **bridge** is
the HTTP + CLI core that enforces a four‑tier **sensitivity guard**; a deterministic Python
**engine** does every calculation; a React **dashboard** is the cockpit. It is **single‑operator,
single‑tenant, and local by default** — confidential and inside‑information work runs on a local
model and does not leave the machine. It is **an operator‑led LLM OS for M&A and investment professionals, designed to work alongside you — with auditability, control, and professional judgement at the centre.**

---

## 2. Architecture

Five layers that communicate **only over the filesystem, HTTP, or a subprocess — never by
importing each other.** That boundary discipline is the spine of the system: it keeps the maths
auditable, the sensitive data contained, and the heavy engines swappable (re‑authoring a
composite against a different runtime is roughly a week, not a rewrite).

| Layer | Directory | What it is |
|---|---|---|
| **Vault** | `vault/` | Plain‑Markdown canonical memory. Every note, decision, deliverable conclusion, register, and source is a `.md` file with mandatory YAML frontmatter. Git‑managed; no database, no SaaS lock‑in — the file *is* the record. Only a generic **skeleton** ships in this mirror. |
| **Bridge** | `routines/` | A FastAPI app — the only layer that touches everything. ~111 loopback endpoints across 49 router files, the sessions store, the in‑process scheduler, an encrypted credentials vault, per‑call telemetry, and the central hook/guard system. Serves the dashboard in production. |
| **Engine** | `engine/` | The valuation engine. A Python wrapper that drives versioned Excel templates (DCF / LBO / comps / 3‑statement / …) through a cell‑map registry binding code to workbook geography. It computes and self‑checks; **no LLM ever computes a number.** |
| **Dashboard** | `dashboard/` | A React + TypeScript + Vite cockpit: chat shell, workflow tiles, an inbox of pending proposals, plus burn‑rate, routing, and project panels. Talks to the bridge over a typed HTTP client. |
| **Sidecar engines** | external | Two heavier orchestration runtimes kept deliberately out‑of‑process: a **composite** engine for declarative multi‑step DAGs (HTTP) and an **autonomous crew** engine for multi‑role agent work (subprocess, isolated venv). Reached only across a boundary; never vendored in. |

### Data flow

```
operator inputs (meetings · web · documents · notes)
        │
        ▼
ingestion routines (transcript intake · news sweep · PDF intake · extractors)
        │  atomic writes, frontmatter contract
        ▼
THE VAULT (Markdown canonical memory) ── indexed ──▶ local embedding store
        │
        ▼
THE BRIDGE ── every LLM call passes the central before_llm_call hook ──┐
        │                                                    (guard fires here)
        ▼
FOUR DISPATCH LANES:  chat · single skill · composite · crew
        │
        ▼
reading / synthesis surfaces (dashboard · Obsidian · recall · briefs)
        │
        ▼
self‑improvement loop (clusters follow‑ups → proposes rule edits → operator‑gated)
        └──────────────▶ appended to the vault only on approval
```

### The governance core

The sensitivity guard is a **single `before_llm_call` hook registered once at startup** — not a
per‑route check. Before *any* LLM call, regardless of lane, the hook runs the sensitivity‑lane
enforcement, the cost/budget gate, and the audit write. **No lane bypasses it** — a single skill,
a step inside a composite, and every role inside a crew all flow through the same chokepoint.
Even a legacy skill with no declared sensitivity gets a fail‑closed fallback (sensitivity inferred
from the workspace). This one chokepoint is what turns a soft promise into a hard guarantee.

Two further structural facts make the loop trustworthy:

- **Nothing auto‑mutates the knowledge store.** Routines and skills emit an operator‑gated
  *proposal*; only on approval does a dated, sourced fact get **appended** (never overwritten) to
  the relevant page.
- **Every vault write passes one gate** — an `atomic_write` chokepoint with an allowlist of
  permitted locations and a deny‑set protecting the operating‑rule files.

---

## 3. The four‑lane dispatch taxonomy

Every operator request runs through **exactly one** lane, chosen **explicitly by verb** — a tile
click or a slash‑command. No LLM classifier guesses. Crossing a lane boundary changes four things
at once — cost, latency, determinism, and audit shape — so the operator opts in knowingly.

| Lane | Trigger | Engine | Determinism | Typical latency | LLM calls | Status |
|---|---|---|---|---|---|---|
| **Chat** | typing | single model call | high | 2–10 s | 1 | live |
| **Single skill** | tile · `/recall` `/comps` `/lbo` … | bridge route → engine | total | 3–30 s | 0–1 | live |
| **Composite** | `/pitch` `/teaser` `/ic-memo` | declarative DAG (HTTP) | total — fixed topology | minutes | per‑step, declared | engine installed; 0 built |
| **Autonomous crew** | `/triage` `/explore` `/debate` `/digest` | multi‑role agents (subprocess) | emergent | minutes | tens–hundreds | **4 live** |

**Safety rules that always apply:** the sensitivity guard fires before any lane engine; a
composite's tier is `max(steps)` (one confidential step routes the whole DAG to local); `/triage`
always runs locally because its CIM inputs default to MNPI; when the lane is unclear, default to
the deterministic one and offer the richer variant as a follow‑up. Each verb carries a hard token
and wall‑clock ceiling; overflow returns a partial result and a cost‑cap error rather than
silently spending more.

---

## 4. Sensitivity model and routing

Every note carries a `sensitivity:` tier. The guard maps tier → allowed lane **before** dispatch
and always defaults to the more restrictive lane when uncertain.

| Tier | Examples | Bridge‑phase routing |
|---|---|---|
| `public` | Listed‑co financials, press releases, sector stats | any cloud lane |
| `internal` | Own analysis on public material; no party names | cloud frontier model (consumer tier today) |
| `confidential` | Deal codenames, target/buyer names, NDA contents, VDR docs | **local model only** |
| `MNPI` | Pre‑announcement results, embargoed news, inside information | **local only, every phase — never leaves the machine** |

MNPI is treated as *categorically* different from merely‑sensitive data: it is **regulated**, so
the local‑only floor is structural and fail‑closed — data‑protection guarantees alone don't
address the regulatory dimension. The whole posture flips on a single `AGENTIC_PLAN_TIER`
environment variable (`bridge` | `enterprise`), decided in **one** routing module that the guard
re‑checks server‑side. There is no second code path that can reach a cloud provider behind the
router's back.

**Routing governance that has shipped:** per‑task‑class tiering (planning → frontier cloud,
heavy analytical → a cross‑check cloud lane, extraction + all confidential/MNPI → local);
two‑way operator override windows (a justification‑required, time‑boxed, audited `confidential →
cloud` window that never covers MNPI; and a non‑sensitive `prefer‑local` window); per‑provider
sensitivity ceilings; budget gating with credit‑exhaustion → local degrade; and a **default‑off
enterprise‑MNPI attestation gate** that only opens when plan‑tier is enterprise *and* a signed,
revocable, expiring per‑provider attestation is active — enforced independently at the router, the
central gate, and a dispatch‑time recheck, fail‑closed and audited per send.

**Local lane (target spec).** Windows 11 + WSL2 with an NVIDIA GPU (≈12 GB VRAM recommended).
A main reasoning model and a smaller triage model for classification, a local embedding model for
recall, and a multimodal model for scanned‑document fallback. CPU‑only inference is too slow to be
usable.

---

## 5. Capabilities — the routine layer

27 routine modules, all scheduled or operator‑triggered, all audit‑logged through the structured
activity pipeline. Highlights:

- **Meeting‑note ingestion** — watches a folder, extracts a structured note (attendees, decisions, actions, mentions), atomic‑writes it to the right project, and stubs People + Companies.
- **Recall** — hybrid retrieval over a local embedding index: vector + lexical (FTS5) channels fused with reciprocal‑rank fusion, then re‑ranked, with importance/freshness/provenance weighting and contradiction detection.
- **Sector news / sector extraction** — sweeps configured sources, dedups, scores relevance, drafts a newsletter; extracts per‑sector claims from five source types into a weighted claim ledger.
- **Deal & earnings trackers** — regex pre‑filter then LLM extraction of deal fields; quarterly results appended to per‑company pages with variance commentary.
- **Morning brief / daily digest** — a 06:30 action list and a 17:00 wrap‑up, generated locally.
- **Vault health** — orphan‑wikilink scan + claim‑freshness sweep.
- **Multimodal PDF intake** — auto‑picks a text vs image path; page‑batched; deterministic merge.
- **Learning loop** — clusters the operator's follow‑up questions and proposes template / operating‑rule edits; a companion step stamps each accepted change with its commit.
- **System self‑reflection** — clusters scheduler misses, budget incidents, latency outliers, retry spirals, and audit anomalies into operator‑gated inbox proposals. It never auto‑routes — the deliberate inversion of systems that mutate their own knowledge store.

These run on **11 in‑bridge cron jobs** (morning brief, daily digest, two vault‑health sweeps,
sector news, system self‑reflection, a precedent‑tracker snapshot, an earnings sweep, a weekly
review, a retention job, and a stale‑gate check), with CRUD endpoints for pause / resume /
run‑now / history.

---

## 6. Skills and the `SKILL.md` spec

Each platform skill is a directory containing a `SKILL.md` (YAML frontmatter declaring
sensitivity, workspace scope, cost ceiling, and the dashboard tile label), `scripts/`,
`references/`, and test fixtures. **13 skills** are validated at boot — the registry hard‑fails the
process if any skill is inconsistent, and a capability manifest forces confidential skills to
declare `network: []`. **LBO** is the reference implementation: it runs end‑to‑end through the
engine via an additive `--output-json` flag, and on a successful run captures its result back into
the vault as an operator‑gated `deliverable‑outcome` proposal (Route → a dated, sourced bullet
appended under the target's valuation history).

---

## 7. Knowledge architecture

The vault is the second brain. Its contract:

- **One subject per file** — a Person file is one person; a Company file is one company.
- **Mandatory frontmatter** — `type`, `date`, `sensitivity`, plus type‑specific fields. Templates are the schema.
- **Stable filenames** — the filename is the wikilink target; you change a `firm:` field, you don't rename the file.
- **Paraphrase, don't quote** — verbatim source text stays in its own location; structured notes carry distilled facts with `[[source]]` links back.
- **Certainty markers** — non‑public claims carry a dated `(confirmed | self‑reported | speculation, YYYY‑MM‑DD)` marker so freshness/decay routines can compute weight.
- **Never invent sources** — every non‑public claim traces to a Source Register entry, or it's flagged, not asserted.

Memory is modelled as a tri‑store (semantic / episodic / procedural). The operating rules
themselves are *procedural memory* — a versioned constitution (`vault/CLAUDE.md`) that the system
reads at the start of every session and that the learning loop proposes edits to.

---

## 8. What's built — and what isn't

**Live**
- Chat, single skills, and **4 autonomous crews** (`/triage`, `/explore`, `/debate`, `/digest`).
- ~111 bridge endpoints / 49 router files; **3,871 tests** across 249 files.
- 27 routines on 11 cron jobs; 13 `SKILL.md` skills; LBO end‑to‑end through the engine.
- Sensitivity‑aware routing with task‑class tiering, operator override windows, per‑provider ceilings, budget gating, and the default‑off enterprise‑MNPI attestation gate.
- A dashboard with live project / open‑actions / project‑chat / routing / LLM‑usage / burn‑rate panels, an inbox with a two‑tier approval taxonomy, and a mid‑run crew human‑input reply box.

**In Progress**
- **Composite lane** — the engine is installed and the spike is green, but `/pitch`, `/teaser`, `/ic-memo` aren't built.
- **DCF / comps end‑to‑end** — blocked on per‑template engine authoring (LBO is the proven pattern).
- **Email / calendar ingestion** — pending a Microsoft Graph app registration.
- **Cross‑machine deploy** — local‑first by design; there is no managed service.

**Won't, by design** — the hard rules in `vault/CLAUDE.md`: no LLM does the maths; no raw
pre‑embargo MNPI to any cloud lane; no confidential material to a cheap public model; no invented
sources; no overwriting a note without confirmation; no writes to the archive; safe (reversible)
deletion only.

---

## 9. Requirements (target spec)

| Component | Recommended | Notes |
|---|---|---|
| OS | Windows 11 | macOS/Linux run the bridge + dashboard; the launchers and the Excel‑driving engine are Windows‑specific |
| GPU / VRAM | NVIDIA, 12 GB+ | required for local reasoning at usable quality |
| RAM / Disk | 32 GB / 250 GB+ | vault + models + indexes + sessions grow over time |
| Runtimes | Python 3.13 (bridge) · 3.11 (crew venv) · Node 24 | plus a local model runtime and Excel for the engine |

Secrets live in an encrypted‑at‑rest credentials store (loopback‑only), never in git. `.env`
carries paths and optional provider keys; `.env.example` ships variable *names* only.

---

## 10. Reading order for a new contributor

1. This document, then the `README.md`.
2. `vault/CLAUDE.md` — the operating constitution. It is the most concentrated statement of how
   the system thinks: sensitivity tiers, the never‑list, the atomic‑notes contract.
3. `routines/` — start at the API routers and the central guard/hook registration, then the
   sensitivity routing module.
4. `engine/` — the template registry and the cell‑map pattern.
5. `dashboard/src/` — the chat shell and the workflow/tile wiring.

*Public‑safe overview. The interactive visual tour is at `docs/index.html`.*
