---
name: equity-research
description: |
  Use when building a full equity-research company profile for a public
  target — fetch a price snapshot, 5y fundamentals + ratios, an N-peer
  comps strip (reuses the comps.build_comps leaf), and the last 14 days
  of news; render a structured multi-section markdown profile and append
  a dated "## Equity research" block to Companies/<target>.md (operator
  edits to PRIOR dated sections are preserved by the append-only writer).
  Triggers: equity research, company profile, build profile, /equity-
  research Cmd-K, leaf for /pre-call-qa and /pre-read-pack composites.
  Inputs: target ticker symbol, optional years (default 5), peers_limit
  (default 6), news_days (default 14), news_limit (default 12), write_note
  (default TRUE — the note is the deliverable). Output: an
  EquityResearchResult JSON (snapshot + fundamentals + comps + news +
  warnings) AND a structured Companies/<target>.md note with one
  ## Equity research · YYYY-MM-DD section containing {Snapshot, 5y
  financials, Comps, News, Analyst commentary [empty bullets]}.
version: 0.1.0
license: proprietary
allowed_tools:
  - vault_read
  - vault_write
capabilities:                        # #61-capabilities
  vault_read:  ["Companies/**"]              # read existing note to append; never reads outside Companies/
  vault_write: ["Companies/**"]              # writes the structured profile section; bounded to Companies/
  fs_roots:    []                            # vault-only
  network:     ["query1.finance.yahoo.com", "query2.finance.yahoo.com"]   # OpenBB → YFinance backend; narrow, no wildcard
metadata:
  sensitivity: internal        # public-equity profile data; not MNPI. Operator-layered commentary lives in operator-edited bullet slots
  workspace_scope: any         # workspace-independent; profile can be pulled from any context
  tile_label: "Equity Research"
  cost_ceiling_tokens: 3000    # provider fetch (0 tokens) + Anton's 5-section narration surface
  cost_ceiling_seconds: 90     # 5 provider calls (snapshot + fundamentals + comps sub-leaf + news + render) on cold cache; 90s = ~1.5× cold
  guardrails:
    - every_block_has_provider              # Iron Law clause 1 — each top-level result block (snapshot/fundamentals/comps/news) carries a provider tag
    - analyst_commentary_preserved          # Iron Law clause 2 — re-fire APPENDS a new dated section; prior sections (incl. operator-filled bullets) are preserved verbatim
    - no_fabricated_commentary              # Iron Law clause 3 — analyst commentary bullets STAY EMPTY in the routine output; no LLM-fabrication
  guardrail_max_retries: 1
# NO captures_to_vault: block — the note IS the deliverable, not a captured-fact summary.
# captures_to_vault is for "capture the deliverable's CONCLUSION back to the vault";
# here the deliverable IS the vault write. The #76 capture loop is structurally redundant
# for full-note-write skills. (Documented in the §14 flex notes header.)
---

<!--
§14 FLEX NOTES (equity-research vs prior 6 migrated skills)

1. Iron Law is THREE-CLAUSE — the first skill with a THREE-clause Iron
   Law. Distinct from all prior shapes:
       LBO           -> numeric gate (S&U tie)
       sector-news   -> sourcing (every claim cited)
       vault-health  -> enumeration honesty (sweep that errored is not clean)
       deal-tracker  -> extraction fidelity (target non-empty, no inferred multiples)
       bd-decay      -> taxonomy fidelity (stale != untracked)
       recall-query  -> explainability + skill boundary (every hit scored AND retrieval != narration)
       equity-research -> provenance + preservation + no-fabrication
   Clauses 2 + 3 are WRITE-BEHAVIOUR clauses (about how re-fires interact
   with existing vault state), not data-content clauses.

2. FIRST skill with FULL-NOTE MULTI-SECTION WRITE — distinct from comps-
   pull (a future sibling) which appends ONE table per fire. equity-
   research writes a five-subsection block (Snapshot / 5y financials /
   Comps / News / Analyst commentary). Five distinct write shapes now
   across the seven migrated skills:
       LBO          -> populated XLSX (full template, COM-driven)
       sector-news  -> markdown newsletter file (synthesis)
       vault-health -> markdown report file (enumeration sweep)
       deal-tracker -> single Excel row append + JSON status
       bd-decay     -> pure-return JSON (no file)
       recall-query -> pure-return JSON (no file)
       equity-research -> multi-section markdown APPEND to Companies/<X>.md

3. FIRST skill DELIBERATELY OMITTING captures_to_vault — the full-note
   write IS the deliverable; a captures_to_vault block would be
   structurally redundant (capturing a write that's already the
   deliverable). Documents the contract: capture is for SUMMARISING a
   deliverable that lives elsewhere (e.g. LBO captures IRR/MOIC from a
   Projects/<deal>.xlsx into Companies/<target>.md), not for the
   deliverable itself.

4. FIRST skill calling ANOTHER ROUTINE as a SUB-LEAF — Stage 3 invokes
   comps.build_comps via direct in-process function call (not a route
   fire, not a subprocess). Sub-leaf provenance + dropped-peer logic is
   inherited transitively. Composite skills (/pitch, /pre-call-qa,
   /pre-read-pack) will rely on the same pattern for fan-out.

5. write_note=True is the DEFAULT (would-be distinct from comps-pull
   where write_note=False is the default and operator opts in). For
   equity-research the deliverable IS the note, so default-on is
   correct; the False opt-out exists for testing / dry-run only.

6. ROUTINE-REALITY FLEX POINTS (documented overnight; for operator review):

   a) The brief's Iron Law described per-row `provider` + `as_of`
      provenance. The actual types (markets/types.py) carry `provider`
      at the BLOCK level (Fundamentals.provider, NewsResult.provider,
      CompsResult.provider, Quote.provider) — not per-row inside each
      block. There is NO `as_of` field anywhere in the markets types
      today. The skill's Iron Law clause 1 was authored to match the
      ACTUAL contract (block-level provider; as_of is implicit via the
      dated section header `## Equity research · YYYY-MM-DD` the writer
      stamps at fire time). If the operator wants per-row as_of, the
      routine + types need that field added — out of scope for this
      descriptor-only migration.

   b) The brief's Iron Law clause 2 described a "read existing note,
      extract operator's `## Analyst Commentary` section verbatim,
      re-render structured sections, re-append commentary" preservation
      pattern. The ACTUAL routine (`_write_equity_research_note`) is
      APPEND-ONLY — it rstrips existing content and appends a NEW
      dated `## Equity research · YYYY-MM-DD` section every fire.
      The operator's prior commentary (filled in PRIOR auto-generated
      sections) is preserved because the writer APPENDS instead of
      overwriting — not because it surgically reads-and-re-appends a
      commentary block. The SKILL.md clause 2 was authored to match
      the ACTUAL append-only semantics. The brief's "surgical preserve"
      pattern would be a routine refactor (out of scope here); the
      append-only semantics give the same operator-protection outcome
      (no edits lost) by a simpler mechanism.

   c) The brief recommended `/workflows/equity-research-pull` as the
      route path (symmetry with `/workflows/comps-pull` once that
      ships). This SKILL.md adopts that recommendation; the canonical
      `/workflows/equity-research` (in markets.py) stays live for
      direct callers and is the legacy surface, while the new
      `/workflows/equity-research-pull` is the SKILL-governed route
      that flows through the `before_tool_call` central guard.

   d) Provider hosts: declared as
      ["query1.finance.yahoo.com", "query2.finance.yahoo.com"] — the
      actual YFinance backend OpenBB uses by default. Narrow, no
      wildcard. If a future provider (FMP, Alpha Vantage) is wired in,
      this list grows; the brief's example
      ["api.fmp.com", "api.openbb.co"] referenced placeholder hosts
      that don't exist in the current adapter.

7. Central guard (#61): for an any-scope, internal-tier skill the guard
   is a structural NO-OP for the common case. The only firing path is
   the cross-skill MNPI gate: if a caller flags
   `workspace_sensitivity=MNPI` on the request, the guard refuses with
   `SkillScopeRefused` -> HTTP 403. Tested.

8. The ON-DEMAND skill governs the route fire only (POST
   /api/workflows/equity-research-pull). The existing POST
   /api/workflows/equity-research route in markets.py stays live as the
   canonical endpoint for direct callers (Cmd-K, dashboard tile);
   downstream consumers that call `equity_research.build_equity_research()`
   directly (composites) are UNTOUCHED by this skill.
-->

# Equity Research

## Overview

Drives the existing `equity_research` routine
(`routines/markets/equity_research.py`) — a five-stage deterministic
pipeline that pulls a price snapshot, 5y fundamentals, a comps strip
(via `comps.build_comps` as a sub-leaf), and last-14-day news for a
public ticker, then renders a structured multi-section markdown profile
and appends a dated `## Equity research · YYYY-MM-DD` block to
`Companies/<target>.md`.

**No LLM in the routine.** The routine is deterministic data assembly +
markdown rendering. Analyst-commentary slots (`Thesis / Risks /
Catalysts`) are LEFT EMPTY by design — the operator fills them.

**Anton's job** is to invoke the workflow route, verify each result
block carries its `provider` tag (Iron Law clause 1), surface the
section summaries + `note_path` chip + provider tag in chat, and
confirm the `Analyst commentary` subsection in the rendered output is
EMPTY (Iron Law clause 3 — surface it as "[empty; operator-filled]").
Anton MUST NOT draft Thesis / Risks / Catalysts content.

The routine writes via APPEND (new dated section each fire) — operator
edits to prior sections (including filled-in Analyst commentary
bullets) are preserved verbatim because they live in PRIOR sections
that are never modified (Iron Law clause 2).

## When to Use

**Mandatory triggers** — fire the skill, do not just discuss:
- Operator types `/equity-research <ticker>` (Cmd-K or composer)
- Operator clicks the Equity Research drawer tile
- Operator asks "build me a profile on X" / "pull equity research on
  <ticker>" / "what's the snapshot + comps + news for X?"

**Optional triggers** — propose firing the skill, ask first:
- Pre-call prep: "I'm meeting with X at 4pm, can you build a profile?"
- Composite-skill leaf: `/pre-call-qa <ticker>` and `/pre-read-pack
  <ticker>` would call `equity_research.build_equity_research()`
  directly as a sub-step; the SKILL.md governs ONLY the on-demand
  operator-pulled fires.

**Don't use** — refuse and explain why:
- Operator wants commentary / view / takeaway DRAFTED for them — Iron
  Law clause 3 breach. The slot is empty; operator fills it.
- Operator wants a PRIVATE-COMPANY profile — equity-research is
  public-ticker only (the route validator rejects non-public symbols).
  Use deal-tracker / a CIM extract skill instead.
- Operator wants ONLY the snapshot or ONLY the comps — those have
  their own narrow routes (`/markets/quotes`, `/workflows/comps`).
  equity-research is the FULL profile.
- Operator passed an MNPI workspace flag — the central guard refuses
  any skill on MNPI inputs (CLAUDE.md §5.2 cross-skill gate).

## The Iron Law

> **CLAUSE 1 — EVERY RESULT BLOCK (snapshot, fundamentals, comps,
> news) CARRIES ITS `provider` TAG. NO BLOCK IS SURFACED WITHOUT
> PROVENANCE; the dated section header `## Equity research · YYYY-MM-DD`
> stamps the as-of timestamp implicitly.**
>
> **CLAUSE 2 — RE-FIRE NEVER OVERWRITES PRIOR SECTIONS. The writer is
> APPEND-ONLY — each fire stamps a NEW `## Equity research ·
> YYYY-MM-DD` block. Operator edits to prior sections (including
> filled-in `Analyst commentary` bullets) are preserved verbatim
> because the writer never modifies prior content.**
>
> **CLAUSE 3 — NO ANALYST COMMENTARY IS LLM-FABRICATED. The routine
> leaves `Thesis / Risks / Catalysts` bullets EMPTY by design. Anton
> MUST NOT fill them in narration; the operator writes the view, Anton
> surfaces the data. The empty slot is the contract.**

These three clauses sit on top of the universals at
[universal-iron-laws](<vault>/CLAUDE.md#universal-iron-laws).

Clause 1 is the sourcing-shape rule applied at BLOCK granularity (the
markets types carry `provider` on each top-level result block —
Fundamentals, NewsResult, CompsResult, Quote — not per-row). Maps to
[no-invented-sources](<vault>/CLAUDE.md#no-invented-sources).

Clauses 2 + 3 are WRITE-BEHAVIOUR clauses unique to a full-note-write
skill: they govern how re-fires interact with vault state (clause 2)
and what the routine deliberately doesn't produce (clause 3). The
operator's commentary lives in the bullets the routine left empty;
preserving the operator's pen is what makes the note operator-owned,
not Anton-owned.

> **Routine-reality note (2026-05-29 baseline).** The routine docstring
> states "No LLM in the loop — deterministic data assembly + markdown
> rendering. Analyst commentary slots are left empty for the operator
> to fill." This is the explicit design contract — honour it. Clause 3
> is the operationalisation of that contract on Anton's side. The
> writer's append-only behaviour (clause 2) is structural — operator
> edits to prior `## Equity research · YYYY-MM-DD` sections are
> preserved because the writer rstrips + appends; it never reads
> previously-stamped sections for surgery. Flag if you'd phrase the
> Iron Law differently.

## Red Flags

> Quoted internal monologue that signals a violation. If you catch
> yourself thinking any of these, stop and re-read the relevant clause.

- *"The Analyst commentary section is empty — I should draft a bullet
  or two to make it useful"* — Iron Law clause 3 breach. Empty IS the
  contract. The operator fills it; Anton surfaces "[Analyst commentary
  bullets are empty; operator will fill]" and moves on.
- *"This is the third re-fire on DemoDeal today; the prior dated
  section's commentary still has the morning's bullets — I'll trim or
  consolidate to keep the note clean"* — Iron Law clause 2 breach.
  Preserve verbatim. Prior sections are operator-sacred; the writer
  appends new ones, never modifies prior ones.
- *"News section pulled 12 items, 3 are duplicates from different
  outlets — I'll dedupe in my narration"* — pre-dedupe is a routine
  concern (the `news_limit` parameter caps; OpenBB returns what it
  returns). Anton surfaces what the routine returned; if there's a
  dedupe gap, file a `#equity-research-news-dedupe` follow-on, don't
  paper over.
- *"Provider returned a 503 on the news call — I'll skip the news
  section and proceed"* — refuse the partial. The routine surfaces
  the failure as a `warnings:` entry; Anton surfaces it to the
  operator alongside a re-run chip. Partial profiles undermine the
  operator's confidence in the note.
- *"The 5y financials show a one-year revenue spike that's clearly a
  special item — I'll annotate"* — annotation IS operator commentary,
  not Anton's. Surface the spike + a chip ("Y3 revenue +47% YoY —
  confirm with operator"); do NOT annotate the note.

## Common Rationalizations

> **Append-only** by the operator after each production run that surfaces
> a new shortcut (CLAUDE.md §14.3). The 5 rows below are the v1 seed for
> a full-note-write skill.

| Rationalization | Reality |
|---|---|
| "Re-fire on an existing note — I'll overwrite the prior section; the operator can re-add commentary if needed" | Iron Law clause 2 breach. The writer is APPEND-only and preserves prior dated sections verbatim. Overwriting would lose operator-filled commentary; never do it. |
| "The note has SEVEN sections from prior different routines (memo drafts, meeting notes) — I'll restructure to match my output" | Refuse. The writer appends ONE new `## Equity research · YYYY-MM-DD` section and leaves all prior content untouched. Surface "existing note has non-standard sections; new equity-research section appended below them". |
| "Comps strip returned 5 peers; equity-research asked for 6 — I'll fan out one more directly" | Anton does not call sub-leaves directly. The routine fans out via `comps.build_comps`. If 5 peers is what the comps leaf returned, surface 5 with the dropped-peer reason from `warnings`. |
| "Provider returned a 503 mid-pipeline on the news call — I'll proceed with the snapshot + comps and ship a partial profile" | Refuse the partial. The routine accumulates `warnings`; Anton surfaces them to the operator with a re-run chip. Partial profiles undermine the operator's confidence. |
| "News items have outlet logos in the provider feed — I'll embed them inline in chat" | The routine returns title + url + published + source per item; Anton's narration surfaces source + published date + url in the chip, NOT images. Text + links only. |

## Core Pattern — 5 stages (snapshot → fundamentals → comps_chain → news → render+write)

The routine pipeline (per `equity_research.py` docstring lines 1-13) is
the verification checkpoints Anton applies to the returned
`EquityResearchResult` before surfacing.

### Stage 1 — Snapshot

`provider.get_quotes([symbol])` returns a `Quote` (price, change,
direction, currency, name) populated into an `EquityResearchSnapshot`.
60s cached. Anton verifies the `provider` is set on the underlying
Quote object (Iron Law clause 1). If the quote fetch raises, the
routine catches + adds to `warnings`; Anton surfaces the warning, not
a partial snapshot.

### Stage 2 — Fundamentals

`provider.get_fundamentals(symbol, years=years)` returns a
`Fundamentals` object with the 5y financials strip + ratios
(EBITDA margin, 5y revenue CAGR, PE, EV/EBITDA, dividend yield,
ND/EBITDA). 24h cached. The `provider` field on `Fundamentals` is
the Iron Law clause 1 provenance for this block. The `error` field
is populated to a string if the provider returned nothing; Anton
surfaces it.

### Stage 3 — Comps chain (sub-leaf)

Calls `comps.build_comps(symbol, peers_limit=peers_limit,
years=years, write_note=False)` as a SUB-LEAF — in-process function
call, NOT a recursive route fire. Returns a `CompsResult` whose
`rows` include the target as row 0 + peers as rows 1..N, each
carrying revenue / EBITDA / margins / PE / EV/EBITDA / ND/EBITDA /
dividend yield + fiscal year. The `CompsResult.provider` field is
the Iron Law clause 1 provenance for this block. Dropped-peer
warnings are inherited from the sub-leaf via `warnings`.

### Stage 4 — News

`provider.get_news(symbol, days=news_days, limit=news_limit)`
returns a `NewsResult` with `items` (title / url / published /
source / summary). 30m cached. `NewsResult.provider` is the Iron
Law clause 1 provenance.

### Stage 5 — Render + write (append-only)

If `write_note=True` (default), the routine renders the 5 markdown
subsections (Snapshot, 5y financials, Comps, News, Analyst
commentary) wrapped in a single `## Equity research · YYYY-MM-DD ·
auto-generated` section. The writer:
  * Stubs the file with frontmatter + a `# <Name>` header if missing.
  * Otherwise reads existing content, rstrips trailing whitespace,
    and APPENDS the new dated section.
  * Atomic-write via the shared `vault_writer.atomic_write` helper.
The `EquityResearchResult.note_path` is returned as an ABSOLUTE path
(routine resolves via `(VAULT / rel).resolve()`) so the dashboard's
`file://` chip handler can open it directly.

### Verification Anton applies before surfacing

1. The route returned an `EquityResearchResult` (no exception
   propagated; partial pipelines surface as `warnings`, not as
   missing fields).
2. Every result block carries a `provider` tag: `snapshot` ← derived
   from `Quote.provider`; `fundamentals.provider`;
   `comps.provider`; `news.provider`. Iron Law clause 1.
3. `analyst_commentary` slot — the routine doesn't expose this as a
   discrete API field; the empty-by-design bullets live INSIDE the
   rendered note body. Anton confirms via the rendered output (or
   the section template) that the slot is present and empty. Iron
   Law clause 3.
4. If `note_path` is populated, the writer succeeded — surface as a
   `file://` chip for the operator to click through.
5. `warnings` is surfaced verbatim (per-stage provider failures,
   sub-leaf dropped peers, write failures).
6. Anton does NOT draft Thesis / Risks / Catalysts content.

## Quick Reference

```
operator types /equity-research <ticker>   (or clicks Equity Research tile / asks "build a profile on X")
  |
route fires equity_research.build_equity_research via tool_call_hooks (before_tool_call stack)
  |  (enforce_skill_sensitivity present but a NO-OP: workspace_scope=any, sensitivity=internal)
Stage 1  snapshot via provider.get_quotes([symbol])           (60s cached)
  |
Stage 2  fundamentals via provider.get_fundamentals(...)      (24h cached)
  |
Stage 3  comps chain — comps.build_comps(...) IN-PROCESS      (sub-leaf; fans out to peer fundamentals)
  |
Stage 4  news via provider.get_news(..., days, limit)         (30m cached)
  |
Stage 5  render + APPEND new dated `## Equity research · YYYY-MM-DD` section to Companies/<X>.md
           (atomic; prior content rstripped + new section appended; analyst-commentary bullets EMPTY)
  |
JSON response: EquityResearchResult{snapshot, fundamentals, comps, news, note_path, provider, warnings}
  |
audit row written to runs/tool.equity-research.jsonl          [hook side effect]
```

## Anti-Patterns

- Drafting Thesis / Risks / Catalysts content in the narrative —
  Iron Law clause 3. The slot is empty; the operator fills it.
- Overwriting prior `## Equity research · YYYY-MM-DD` sections — Iron
  Law clause 2. The writer is append-only; respect prior content.
- Calling `comps.build_comps` or `provider.get_*` directly from the
  skill route handler — those are sub-leaf calls owned by the
  routine. The route's job is to fire the routine, surface the
  result, and exit.
- Bypassing the SKILL-governed `/workflows/equity-research-pull` route
  and hitting the canonical `/workflows/equity-research` route from
  the skill code — the skill route exists so the central guard
  (`enforce_skill_sensitivity`) is on the path.
- Filtering out a news item or a comps row because it "looks
  redundant" — surface verbatim; the routine's `news_limit` /
  `peers_limit` are the curation knobs.
- Falsely claiming "the note now has updated commentary" when only
  the auto-generated structured sections changed and the operator's
  prior commentary bullets are intact — surface honestly: "appended
  new dated section; prior commentary preserved".

## Example — "build me an equity-research profile on Whitbread (WTB.L)" (worked end-to-end)

Single realistic example. One good example beats five half-examples.

**Operator input:** "Build me an equity-research profile on
Whitbread (WTB.L). Pull the snapshot, 5y financials, an 8-peer comps
strip, and the last two weeks of news. Write it to the Companies/
note."

**Stage 1 — Anton invokes the workflow route:** POST
`/api/workflows/equity-research-pull` with `{"symbol": "WTB.L",
"years": 5, "peers_limit": 8, "news_days": 14, "news_limit": 12,
"write_note": true}`.

**Stage 2-5 — Anton's chat line (post-response):** "Built equity-
research profile for WTB.L (Whitbread PLC). Provider: openbb-
yfinance. Sections:

  * **Snapshot:** last price £24.18 (-0.3%), PE 14.6x, EV/EBITDA
    8.9x, div yield 2.4%, EBITDA margin 23.1%, 5y revenue CAGR
    4.2%.
  * **5y financials:** FY24 revenue £2,964m → FY20 £589m (COVID
    trough); EBITDA margin recovered 23.1% FY24 vs 11.0% FY22.
  * **Comps (target + 8 peers):** target row 0 + 8 peer rows
    (IHG.L, ACS.PA, MAR, HLT, H, ...). Median peer EV/EBITDA 11.2x;
    WTB.L trades 2.3x below median.
  * **News · last 14 days:** 12 items returned; top 3 are
    [Reuters · 2026-05-28 — Whitbread Q1 trading update], [FT ·
    2026-05-25 — Premier Inn German occupancy], [Bloomberg ·
    2026-05-22 — UK leisure sector preview].
  * **Analyst commentary:** Thesis / Risks / Catalysts bullets are
    EMPTY — operator-filled. (Iron Law clause 3.)

Note appended to `[[Companies/Whitbread PLC.md]]` (open chip).
Prior content preserved — the new `## Equity research · 2026-05-29`
section sits at the bottom. Warnings: 0. Want me to surface the
full comps table inline?"

**Final output bubble** — short lead ("equity-research profile
appended for WTB.L; 5 sections, 0 warnings"), the per-section
spot-check, chips (Open note · Open comps section · Open news
section · Re-fire with different peers_limit).

## When Stuck

| Symptom | Diagnostic |
|---|---|
| Snapshot block has `last_price: None` but no warning | Provider's quote call returned a row without `last_price` — usually an index / futures ticker. The routine falls back to the last historical close internally; if that ALSO failed, surface the `warnings` entry. |
| Fundamentals block has `error: "<message>"` | OpenBB returned nothing usable for this ticker (likely a non-US/UK micro-cap). Surface the error; do not paper over. The 5y financials subsection will render "_No 5y financials returned by provider._" |
| Comps strip has fewer peers than `peers_limit` | Sub-leaf `build_comps` filters peers without usable fundamentals. Surface the count delta + the warnings inherited from the sub-leaf. |
| News block has 0 items | Either no news in the look-back window (legitimate, surface "0 items") or provider failure (surface the `error` field). |
| `note_path` is None despite `write_note=True` | Write failed — check `warnings` for the `Note write failed: ...` entry. The route still returns 200 with the result payload + warning; surface the write failure to the operator with a chip ("Re-fire; or save manually"). |
| Re-fire on DemoDeal — the note is now 4 sections deep with prior dated equity-research blocks | This is correct append-only behaviour. Operator can pin / archive prior sections manually; routine never does so. Surface "appended new section; N prior sections preserved". |
| Operator asks "what's your view?" after the profile lands | Iron Law clause 3 — refuse politely. "Profile is data-only; the Thesis / Risks / Catalysts bullets are empty for you to fill. I can pull additional supporting data if useful." |

## Output Contract

The route returns JSON (`EquityResearchResult` shape):

```json
{
  "target_symbol": "WTB.L",
  "snapshot": {
    "symbol": "WTB.L",
    "name": "Whitbread PLC",
    "currency": "GBp",
    "last_price": "2,418p",
    "price_change": "-0.3%",
    "direction": "down",
    "pe": 14.6,
    "ev_ebitda": 8.9,
    "dividend_yield": 0.024,
    "ebitda_margin": 0.231,
    "revenue_growth_5y_cagr": 0.042
  },
  "fundamentals": {
    "symbol": "WTB.L",
    "name": "Whitbread PLC",
    "currency": "GBP",
    "years": [{"fiscal_year": 2024, "revenue": 2964.0, "ebitda": 685.0, ...}],
    "ratios": {"pe": 14.6, "ev_ebitda": 8.9, ...},
    "provider": "openbb-yfinance",
    "error": null
  },
  "comps": {
    "target_symbol": "WTB.L",
    "target_name": "Whitbread PLC",
    "rows": [
      {"symbol": "WTB.L", "name": "Whitbread PLC", "fiscal_year": 2024, ...},
      {"symbol": "IHG.L", "name": "IHG plc", ...}
    ],
    "note_path": null,
    "provider": "openbb",
    "warnings": []
  },
  "news": {
    "symbol": "WTB.L",
    "items": [{"title": "...", "url": "...", "published": "2026-05-28", "source": "Reuters"}],
    "provider": "openbb-yfinance",
    "error": null
  },
  "note_path": "<vault>/Companies/Whitbread PLC.md",
  "provider": "openbb",
  "warnings": []
}
```

**Vault note write** (when `write_note=True`, the default): the routine
APPENDS a `## Equity research · YYYY-MM-DD · auto-generated` section to
`Companies/<safe_filename(target.name)>.md` (stubs the file if missing).
The new section contains five subsections: `### Snapshot`, `### 5-year
financials`, `### Comps`, `### News · last 14 days`, `### Analyst
commentary` (with empty `Thesis / Risks / Catalysts` bullets — operator
fills). Audit row to `runs/tool.equity-research.jsonl`.

**What the route does NOT produce** (so the skill does not report it):
a drafted Thesis / Risks / Catalysts narrative (Iron Law clause 3 — the
empty bullet IS the deliverable for those slots), a per-section
operator-action recommendation, or a "key takeaway" sentence
synthesised from the data. The routine returns data; the operator
synthesises.

## Citations Required

Multi-section profile; every BLOCK in the result carries provenance via
its `provider` field. The dated section header `## Equity research ·
YYYY-MM-DD` stamps the as-of timestamp implicitly.

| Field | Required source type | Acceptable form |
|---|---|---|
| `snapshot` (via underlying `Quote.provider`) | The quote-fetch provenance | String e.g. "yfinance" / "openbb-yfinance"; surfaced indirectly through the comps block's `provider` |
| `fundamentals.provider` | The fundamentals-fetch provenance | String |
| `comps.provider` | The comps sub-leaf's provenance | String — inherited from the markets adapter |
| `news.provider` | The news-fetch provenance | String |
| `news.items[*].source` + `.url` + `.published` | Per-item news provenance | All three optional per item; Anton surfaces what's present |
| `note_path` | The vault write target (absolute path) | Surfaced so operator can click through |
| `warnings` | Per-stage failure surface | List of strings; surface verbatim |

## Cost Envelope

```
cost_ceiling_tokens: 3000   # 5 section narrations; no LLM in the routine
cost_ceiling_seconds: 90    # provider chain cold-cache is the dominant wall-clock
```

**Where the budget goes:**
- Provider chain: 0 tokens; 30-60s cold wall-clock (5 fan-out calls +
  comps sub-leaf which itself fans out to peer-fundamentals).
- Anton narration: ~2000-2500 tokens (verify Iron Law clauses × 3 +
  surface section summaries + provider chip + note_path chip).
- Headroom: ~500 tokens for one guardrail retry.

**The 3000 token / 90s ceiling vs prior skills:**
       sector-news   -> 6000 / 300s  (per-item LLM loop + network)
       LBO           -> 8000 / 90s   (engine subprocess)
       vault-health  -> 2000 / 60s   (pure file-walk + write report)
       deal-tracker  -> 3000 / 60s   (single Ollama call)
       bd-decay      -> 1500 / 30s   (pure file-walk, no write, no LLM)
       recall-query  -> 1500 / 30s   (pure SQLite query, no write, no LLM)
       equity-research -> 3000 / 90s (5-section narration + provider chain)

Equity-research has the HIGHEST token-narration surface of any non-
engine skill — 5 sections to summarise + chips, no synthesis.

> **Calibration status:** first-pass estimate. Recalibrate to
> `1.25 × observed` after first real runs. The provider chain may be
> faster than 30s on a warm cache (24h fundamentals + 60s quotes),
> dropping the wall-clock budget pressure.

## Verification Checklist (before declaring done)

- [ ] The route returned an `EquityResearchResult` (no exception propagated; partial pipelines surface as `warnings`)
- [ ] Every result block carries a `provider` tag (Iron Law clause 1): snapshot via underlying Quote, fundamentals.provider, comps.provider, news.provider
- [ ] If `note_path` is populated, the writer succeeded — surface as a `file://` chip for click-through
- [ ] The new `## Equity research · YYYY-MM-DD` section was APPENDED (Iron Law clause 2 — prior content preserved verbatim)
- [ ] The `### Analyst commentary` subsection bullets are EMPTY (Iron Law clause 3) — surface as "[empty; operator-filled]"
- [ ] Anton's narration does NOT draft Thesis / Risks / Catalysts content
- [ ] `warnings` are surfaced verbatim (per-stage provider failures, sub-leaf dropped peers, write failures)
- [ ] Audit row exists in `runs/tool.equity-research.jsonl` with `status: "ok"`
- [ ] Final chat bubble surfaces per-section spot-check + `[[<note_path>]]` wikilink for click-through
