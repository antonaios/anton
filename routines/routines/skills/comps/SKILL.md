---
name: comps
description: |
  Use when building a comparable-companies (CoCo) + comparable-transactions
  (CoTrans) deliverable for a live deal — propose subsectors from the target,
  propose CoCo peers + CoTrans deals per subsector (operator approves both
  lists), acquire data WITH SOURCES (provider for trading data, IR pages /
  deep-research for net debt + LFY+1 + deal multiples), and stamp a populated
  copy of the operator's v2 Comps template. Triggers: comps, comparable
  companies, comparable transactions, CoCo, CoTrans, trading comps, precedent
  transactions, valuation comps, "build comps for <deal>", "/comps-build".
  Inputs: deal name + target name + parent sector (subsectors are PROPOSED by
  the skill, APPROVED by the operator). Output: a populated XLSX in the deal's
  Valuation folder + a captured headline-multiples fact on Companies/<target>.md
  + a refreshed Sectors/<sector>/Comps.md mirror.
version: 0.1.0
license: proprietary
allowed_tools:
  - llm_local
  - llm_cloud           # internal-tier — deep-research / xlsx engine skills run cloud-side
  - vault_read
  - vault_write
  - web_search          # IR pages, results releases, deal announcements
capabilities:                        # #61-capabilities — declared surface, validated at boot
  vault_read:  ["Sectors/**", "Companies/**", "Projects/**"]   # target profile + sector universe + prior comps
  vault_write: ["Projects/**"]                                 # project-scoped per the validator (#61). BOTH Companies/<target>.md AND
                                                               # Sectors/<sector>/Comps.md flow through operator-gated #76 deliverable-
                                                               # outcome proposals at Routines/deliverable-outcomes/ — there's NO direct
                                                               # runtime write into Sectors/** or Companies/** from this skill (closes
                                                               # the #61 capability gap; see flex note 10).
  fs_roots:                                                    # operator-side workspace + tracker
    - "<workspace-root>/1. Projects/**"                    # deal Valuation folder lives here per workspace-write-policy
    - "<workspace-root>/4. Research & data/Precedent transactions tracker/**"   # CoTrans source + write-back
    - "os-templates/**"                                     # the v2 template (read-only)
  network:                                                     # internal tier ⇒ external hosts allowed (cf. sector-news)
    - "query1.finance.yahoo.com"                               # OpenBB → YFinance backend (trading data)
    - "query2.finance.yahoo.com"
    - "api.firecrawl.dev"                                      # IR / results-release scrape
    - "api.tavily.com"                                         # search fallback
    # Plus the operator's full IR domain set — deliberately UNBOUNDED below the
    # internal tier. See §Honest network note below.
captures_to_vault:                   # #76 — capture the headline multiples back to Companies/<target>.md
  target: "Companies/{target}.md"
  fields: [headline_ev_ebitda_median, headline_ev_revenue_median, peer_count, deal_count, as_of, provider, template_path]
  headline: "{target} comps · {peer_count} CoCo / {deal_count} CoTrans · median EV/EBITDA {headline_ev_ebitda_median}x, EV/Revenue {headline_ev_revenue_median}x · as of {as_of} ({provider})"
  section: "Comps history"           # LOCKED per operator decision 2026-06-01 — distinct from LBO's "Valuation history"
metadata:
  sensitivity: internal              # public multiples / announced deals — NO MNPI. The DEAL note may be confidential; the multiples table is not.
  workspace_scope: project           # LOCKED per operator decision 2026-06-01 — deal-bound (the deliverable lands in the deal Valuation folder)
  tile_label: "Comps (Build)"        # distinct from the "Ticker multiples" tile that drives /api/workflows/comps (the legacy snapshot)
  cost_ceiling_tokens: 18000         # generous — deep-research per CoTrans gap + per-subsector synthesis + narration loop
  cost_ceiling_seconds: 600          # 10min — multi-stage operator-gated pipeline + per-peer provider calls + IR scrapes + xlsx stamp
  guardrails:
    - every_figure_sourced                  # Iron Law clause 1 — no populated cell without a Source column entry
    - operator_approves_subsectors          # Stage 0 gate — Anton proposes, operator approves the subsector list
    - operator_approves_peers_and_deals     # Stage 1 gate — both CoCo peers + CoTrans deals
    - lfy1_approved_unless_connector        # Stage 2 — forward-year consensus needs operator approval unless a connector feed supplied it
    - section_append_not_overwrite          # #76 capture appends to Companies/<target>.md "Comps history"; never overwrites
  guardrail_max_retries: 1
---

<!--
§14 FLEX NOTES (comps vs the prior ten migrated skills)

1. Iron Law is OPERATOR-GATED + SOURCING-SHAPED — not a numeric gate like LBO,
   not a pure-sourcing gate like sector-news, not a three-clause write-behaviour
   gate like equity-research. The comps Iron Law has THREE pieces all glued
   together: (a) every populated cell carries a Source, (b) Anton PROPOSES /
   operator APPROVES subsectors + peers + deals + LFY+1, (c) the Comps-history
   write is append-only. The mechanical test is "every populated CoCo + CoTrans
   row in the output workbook has a non-empty Source column" — that's the part
   we can assert; the propose/approve discipline is enforced at the route layer
   (the route REFUSES to finalize without operator-approval tokens).

2. FIRST OPERATOR-GATED MULTI-STAGE SKILL. All prior skills are single-shot
   (call route → result lands). Comps is a 4-stage pipeline (Stage 0-3) where
   the operator approves between stages 0→1 and 1→2 (and again at 2→3 for any
   unsourced LFY+1). This means the route returns "approval-pending" payloads
   between stages, not a final result, and the operator re-fires the route with
   the approval tokens. Pattern adopted from the proposals lifecycle (#8/#58).
   First skill to surface gating MID-PIPELINE rather than at refuse-or-go entry.

3. FIRST DELIBERATE BROAD-NETWORK SKILL. equity-research declared
   `query1/2.finance.yahoo.com` (narrow YFinance backend); sector-news declared
   `api.firecrawl.dev` + `api.tavily.com` (narrow search providers). Comps adds
   THE OPERATOR'S FULL IR DOMAIN SET on top — DemoTelco.com/investors,
   investors.iag.com, etc. — and the brief is explicit that this is NOT the
   narrow-host story. The internal sensitivity tier permits it (the network⇔
   sensitivity validator only forbids confidential/MNPI from declaring any host
   at all). Documented in the frontmatter — the operator's reading of the
   capabilities block should not be misled into expecting a narrow allow-list.

4. FIRST SKILL ORCHESTRATING ANTHROPIC CLOUD SKILLS as engines. Stage 1 calls
   equity-research:screen / investment-banking:buyer-list for peer identification;
   Stage 1+2 call deep-research for CoTrans gap-fill + IR extraction; Stage 3
   calls anthropic-skills:xlsx for the populated workbook stamp. The skill CODE
   declares these as orchestration LAYER (the route never calls them directly —
   it goes through a thin shim that mocks cleanly in tests). NO MNPI to cloud
   skills — the route's sensitivity-check fires BEFORE any cloud-skill call.

5. block-flex (LOCKED per operator 2026-06-01): the v2 template is built as a
   clean SINGLE CoCo block + CoTrans section UNIT; the skill stamps ONE CoCo
   block PER APPROVED SUBSECTOR (insert block, re-base Mean/Median/75th/25th/
   Min/Max ranges) and groups CoTrans by subsector. Handles 1..N subsectors
   without a fixed-row template. Tests use a tmp synthetic xlsx that mirrors
   the v2 structure — NOT the real os-templates/Project_x_Comps_v2.xlsx
   file (it's operator-controlled and the layout may still be in review).

6. workspace_scope = project (LOCKED per operator 2026-06-01). The deliverable
   lands in the deal Valuation folder per workspace-write-policy. Ad-hoc
   "build comps for a research idea without a deal" is a deferred follow-on
   (would need workspace_scope: any + a different write target).

7. captures_to_vault section = "Comps history" (LOCKED per operator 2026-06-01).
   Distinct from LBO's "Valuation history" — comps is a separate kind of
   conclusion (multiples panel, not a valuation point estimate) and should
   not co-mingle with the LBO/DCF dated bullets.

8. Central guard (#61): project-scoped, internal-tier. The guard refuses any
   call from a non-project workspace (no operator-pull "build comps on
   /general"); the only other firing path is the MNPI cross-skill gate (which
   a comps call should never carry — comps is public multiples by construction).

9. The new route is at /api/workflows/comps-BUILD (with the dash). The legacy
   /api/workflows/comps route in routes/markets.py is the ticker-multiples
   surface (per the redesign rename) — comps-build is the deliverable skill.
   Disambiguated at the route path so Cmd-K + dashboard tiles can wire to
   distinct skills without overloading the route key.

10. workspace_scope=project + the SECTOR MIRROR write: the registry validator
   (_validate_capabilities) refuses a project-scoped skill that declares
   vault_write outside Projects/**. The skill conceptually writes to
   Sectors/<sector>/Comps.md (the mirror) AND to Companies/<target>.md (the
   #76 capture). BOTH are routed through Routines/deliverable-outcomes/ as
   operator-gated proposals — neither is a direct runtime write into
   Sectors/** or Companies/**:
     (a) Companies/<target>.md is written via the standard #76
         emit_deliverable_proposal — proposal lands at
         Routines/deliverable-outcomes/<date>-<deal>-comps.md, operator's
         Route action appends the dated fact via _route_deliverable_outcome.
     (b) Sectors/<sector>/Comps.md mirror is ALSO written via a
         deliverable-outcome proposal (sibling shape) —
         _emit_sector_mirror_proposal writes
         Routines/deliverable-outcomes/<date>-<deal>-comps-sector-mirror.md
         with target=Sectors/<sector>/Comps.md and section="Comps runs"
         (a valuation-snapshot section, distinct from the precedent-transaction
         "## Precedent transactions" blocks — #43-sector-template-align).
         The operator's Route action does the actual Sectors/ append using
         the same _route_deliverable_outcome path. This closes the #61
         capability gap: there's NO ungated runtime write into Sectors/**
         from this skill. (Pre-fix the mirror wrote DIRECTLY to Sectors/;
         updated 2026-06-01 per the comps-skill review.)
   The only writes left for this skill's runtime are: (1) the deal
   Valuation XLSX under <workspace-root>/1. Projects/** (covered by
   fs_roots), and (2) the two operator-gated proposal files under
   Routines/deliverable-outcomes/ (workflow tool runtime, exempt from
   enforce_workspace_policy — same pattern as the #76 Companies capture).
-->

# Comps (Build)

## Overview

Drives the operator's `Project_x_Comps_v2.xlsx` template (CoCo + CoTrans
blocks, one block per approved subsector, 75th/25th percentile rows alongside
Mean/Median/Min/Max). The skill is an OPERATOR-GATED 4-stage pipeline:
Stage 0 understand the target + propose subsectors; Stage 1 propose CoCo peers
+ CoTrans deals per approved subsector; Stage 2 acquire each populated figure
WITH A SOURCE (provider for trading data; IR pages / deep-research for net
debt + LFY+1 + CoTrans EV/multiples); Stage 3 stamp the v2 template, save to
the deal's `2. Valuation/01. COMPS/` folder per workspace-write-policy
(archive prior versions to `00. OLD/`), fire the #76 capture into
`Companies/<target>.md "## Comps history"`, and refresh the
`Sectors/<sector>/Comps.md` mirror.

**Comps is a research / judgment / sourcing problem, not an engine-maths
problem** (the template's only maths is SUM + divide + mean/median/percentile).
The valuation engine adds nothing here, and the redesign explicitly scrapped
the "engine-comps-template" framing. The maths is left to the template's
formulas; the skill's job is OPERATOR-GATED IDENTIFICATION + SOURCED
ACQUISITION + TEMPLATE STAMP.

**Anton's job** is to PROPOSE the subsectors (Stage 0), PROPOSE the peers +
deals (Stage 1), FETCH the figures with their sources (Stage 2), and STAMP
the template (Stage 3) — pausing at each gate for operator approval. Anton
NEVER invents a number, NEVER picks a peer without operator sign-off, and
NEVER writes a populated cell without a Source. Sits on top of
[no-invented-sources](<vault>/CLAUDE.md#no-invented-sources)
+ [no-llm-maths](<vault>/CLAUDE.md#no-llm-maths).

## When to Use

**Mandatory triggers** — fire the skill, do not just discuss:
- Operator types `/comps-build <deal>` (Cmd-K or composer) — distinct from
  `/comps <ticker>` which routes to the ticker-multiples legacy snapshot.
- Operator clicks the "Comps (Build)" drawer tile (gated on a Project workspace).
- A composite (`/ic-memo`, `/pitch`) calls the comps step in its DAG and the
  deal has a v2 template slot.

**Optional triggers** — propose firing the skill, ask first:
- Operator asks "what are <target> trading at vs peers?" inside a Project chat —
  propose `/comps-build <deal>` AND surface that the lightweight
  `/comps <ticker>` is faster if the operator only wants a quick multiples
  panel without the full template stamp.
- Operator asks "what precedent deals are there in <subsector>?" — propose
  starting at Stage 1 with the subsector already approved.

**Don't use** — refuse and explain why (the route enforces these as hard gates):
- Workspace is NOT a Project — comps-build is project-scoped and writes to the
  deal Valuation folder. (403: "comps-build requires a project workspace; use
  /comps for an ad-hoc ticker-multiples snapshot".)
- Workspace is MNPI tier — refuse. Comps is public multiples by construction;
  if you have MNPI on the target, that's a different workflow (
  [no-mnpi-to-cloud](<vault>/CLAUDE.md#no-mnpi-to-cloud)).
- No target named — refuse with "name the target before running; comps on
  nothing isn't comps".
- Operator hasn't approved subsectors yet but is calling Stage 1 — refuse with
  the approval-pending payload from Stage 0 (the gate IS the contract).
- Operator hasn't approved peers + deals but is calling Stage 2/3 — same: the
  approval token is mandatory between stages.

## The Iron Law

> **NO FIGURE IS WRITTEN WITHOUT A SOURCE OR AN EXPLICIT OPERATOR-APPROVED
> ASSUMPTION. ANTON PROPOSES subsectors, peers, deals, AND LFY+1; THE
> OPERATOR APPROVES. APPEND-ONLY HISTORY IS NEVER OVERWRITTEN.**

This is non-negotiable, and it sits on top of the universals at
[universal-iron-laws](<vault>/CLAUDE.md#universal-iron-laws),
in particular
[no-invented-sources](<vault>/CLAUDE.md#no-invented-sources)
and the §3-rule-9 append-only contract.

Three pieces glued together:

1. **Every populated cell carries a Source.** The CoCo Source column (X) and
   the CoTrans Source field must be non-empty for any row the skill stamps.
   If a number can't be sourced (Yahoo has it, but you couldn't verify it
   against the filing), it's flagged as an explicit operator-approved
   ASSUMPTION and the Source cell carries `operator-approved:<date>` —
   never silently filled. The mechanical test asserts this on every
   populated row.
2. **Anton PROPOSES, operator APPROVES** at Stage 0 (subsectors), Stage 1
   (peers + deals), and Stage 2 (any LFY+1 not sourced from a connector).
   The route REFUSES to finalize without the approval tokens. There is no
   "implicit best-guess" path — the gate IS the contract.
3. **The `## Comps history` section on `Companies/<target>.md` is
   APPEND-ONLY.** Each fire writes a NEW dated bullet via the #76 capture
   loop (operator-gated); prior bullets are preserved verbatim. The writer
   never modifies prior entries. Mirrors LBO's `## Valuation history`
   pattern but in its own section so the kinds of conclusions don't
   co-mingle.

> **Routine-reality note (2026-06-01 baseline).** The mechanical test
> (`tests/skills/comps/test_iron_law.py`) asserts piece 1 — every populated
> output row has a Source. Pieces 2 + 3 are route-layer + capture-layer
> enforcement, tested in `test_comps_skill.py` (the route refuses Stage 1+
> calls without the prior approval token; the capture appends and never
> overwrites). Flag if you'd phrase the Iron Law differently.

## Core Pattern — 4 stages (operator-gated)

Each stage has a STOP gate. **STOP markers are not advisory** — if a stage's
verification doesn't have an explicit written result + operator approval token,
do not proceed to the next stage.

### Stage 0 — Understand + scope

- Read the target's business from `Projects/<deal>/00 Brief.md` (the
  operator's mandate + target overview), the deal's CIM if present, and the
  parent sector from the brief or operator input.
- Propose subsectors by READING `_claude/profile.md sector_sub_lens` for the
  parent sector and APPLYING JUDGMENT (may be a single subsector for a pure-
  play target; may be 2-3 for a conglomerate).
- **STOP — 🛑 Operator approval gate.** Return an `approval_pending` payload
  carrying the proposed subsector list + Anton's rationale. The operator
  approves a subset (or edits the list); the approval token is the signed
  list that the route requires to advance to Stage 1.

### Stage 1 — Identify (CoCo peers + CoTrans deals)

- For each approved subsector:
  - **CoCo peers.** Orchestrate `equity-research:screen` /
    `investment-banking:buyer-list` (Anthropic cloud skills) + the markets
    provider's `get_peers()` as a sanity check. Propose 5-8 peers per
    subsector with one-line rationale per peer.
  - **CoTrans deals.** Query the precedent tracker (canonical workbook at
    `<workspace-root>/4. Research & data/Precedent transactions tracker/
    Precedent_transactions_tracker.xlsx`, sheet `Precedent transactions`)
    by `subsector_slug + last 5y`. For gaps, fire `deep-research` (Anthropic
    cloud skill) — dedupe deduped-by-`(announced_date, target)`, and
    **write any new deals BACK into the tracker** via
    `routines.dealtracker.workbook.append_deal()` (lean 18-col, sourced).
- **STOP — 🛑 Operator approval gate.** Return an `approval_pending` payload
  carrying the proposed peers + deals per subsector. The operator approves
  both lists (or edits). The signed lists are the Stage 2 entry tokens.

### Stage 2 — Acquire data with sources

Per CoCo row (each approved peer + the target itself):
- Market cap + price + currency via `provider.get_quotes()`.
- Trading currency (from the provider) vs functional/FS currency (from the
  filing). **FLAG when trading-currency ≠ FS-currency** — operator decides
  whether to display in £ vs €, or stamp both — never silently fill.
- Net debt — NOT in Yahoo's `Fundamentals.ratios`; fetch from IR / results
  release (Firecrawl scrape + cited URL).
- Revenue LFY / LFY+1, EBITDA LFY / LFY+1 — LFY from
  `Fundamentals.years[0]` (or the filing); **LFY+1 requires operator
  approval UNLESS sourced from a connector** (CapIQ/FactSet/LSEG/PitchBook
  consensus, when licensed). For Yahoo gap-fill, surface the LFY+1 candidate
  + the assumption type and ASK before stamping.
- Source column populated per row: `<provider>:<as_of_date>` for
  provider rows; `<filing_url>` for IR-scraped rows; `operator-
  approved:<date>` for operator-confirmed assumptions.

Per CoTrans row (each approved deal):
- EV + EV/Revenue + EV/EBITDA + strategic commentary — from the
  precedent tracker if present; from `deep-research` (cited press release
  / IR release URL) for tracker gaps. Strategic commentary is a deep-
  research synthesis bullet; operator can edit post-stamp.
- Source field non-empty for every row.

**STOP — 🛑 Surface ALL unsourced + approval-pending figures together.**
Operator approves (or rejects + asks for re-source) each. Approved
assumptions get `operator-approved:<date>` in the Source cell; rejected
rows are dropped from the stamp.

### Stage 3 — Populate + deliver

- Stamp the v2 template (one CoCo block per approved subsector — block-flex
  pattern: insert block, re-base Mean/Median/75th/25th/Min/Max ranges per
  block — and CoTrans grouped by subsector). Driven by
  `anthropic-skills:xlsx`; the formulas in the template compute the
  multiples + stats from the stamped inputs.
- Save the populated workbook to
  `<workspace-root>/1. Projects/<Deal>/3. Financials & analysis/
  2. Valuation/01. COMPS/Project_<Deal>_COMPS_<date>_v<N>.xlsx` per
  workspace-write-policy. **Archive prior same-day same-skill version to
  `00. OLD/`** (never delete).
- Fire the #76 capture: `emit_deliverable_proposal()` writes a
  `kind: deliverable-outcome` proposal carrying the headline median EV/
  EBITDA + EV/Revenue + peer/deal counts + provider + template_path; on
  operator Route, a dated bullet appends under `## Comps history` of
  `Companies/<target>.md` (NEVER overwriting prior bullets — Iron Law
  piece 3).
- Emit the `Sectors/<sector>/Comps.md` mirror via a SIBLING deliverable-
  outcome proposal (also at `Routines/deliverable-outcomes/`, with
  `target: Sectors/<sector>/Comps.md` + `section: Comps runs`). On
  operator Route, a dated comps-run **snapshot** bullet (medians · peer/deal
  counts · subsector · → deliverable link) appends under `## Comps runs` of
  the sector note — a valuation snapshot, NOT a precedent transaction — with no
  direct Sectors/** write from this skill's runtime (closes #61 capability gap).

Only after Stage 3 completes (with Stages 0/1/2 approval tokens all on
the request) does Anton produce the final chat bubble with the headline
multiples + chips (Open workbook · Open Companies/<target>.md · Open
Sectors/<sector>/Comps.md).

## Quick Reference

```
operator types /comps-build <deal>           (or clicks Comps (Build) tile)
  ↓
route refuses if workspace ≠ project OR sensitivity = MNPI OR no target named   [hard gate: 403/422]
  ↓
Stage 0  read target → propose subsectors from profile.md sector_sub_lens
  ↓        (return approval_pending payload to operator)
  ↓        🛑 operator approves subsector list → approval token signed
  ↓
Stage 1  per approved subsector: propose CoCo peers (equity-research:screen / investment-banking:buyer-list / provider)
           + CoTrans deals (tracker query + deep-research gap-fill, write-back to tracker)
  ↓        (return approval_pending payload to operator)
  ↓        🛑 operator approves peers + deals per subsector → approval tokens signed
  ↓
Stage 2  acquire data with sources: provider (CoCo trading), IR scrape (net debt + LFY+1), deep-research (CoTrans gaps)
           flag FS-ccy ≠ trading-ccy; LFY+1 requires operator-approval unless connector
  ↓        🛑 operator approves any unsourced LFY+1 / assumption → approval tokens signed
  ↓
Stage 3  stamp v2 template (1 CoCo block / subsector + CoTrans grouped) via anthropic-skills:xlsx
  ↓        save to Project_<Deal>_COMPS_<date>_v<N>.xlsx per workspace-write-policy
  ↓        prior same-day version → 00. OLD/                                                   [archive]
  ↓
#76 capture: emit deliverable-outcome proposal → operator Route → appended bullet under
       Companies/<target>.md "## Comps history"                                                  [append-only — Iron Law piece 3]
  ↓
Sectors/<sector>/Comps.md mirror — sibling deliverable-outcome proposal → operator Route        [operator-gated]
  ↓
audit row written to runs/tool.comps-build.jsonl                                                [hook side effect]
```

## Common Rationalizations

> **Append-only** by the operator after each production run that surfaces a
> new shortcut (CLAUDE.md §14.3). The 6 rows below are the v1 seed.

| Rationalization | Reality |
|---|---|
| "The target is obviously hotels-full-service, I'll skip the Stage 0 gate" | The propose/approve pattern IS the Iron Law's spine. "Obvious" subsector calls are exactly where the operator catches a wrong frame (e.g. operator wants hotels-LIMITED-service in the peer set, not full-service). Never skip the gate. |
| "Yahoo has the LFY+1 forecast — that's a source, no need to ask the operator" | Yahoo's forward consensus is a SCRAPE of broker consensus; it's not the source register entry the operator's IC memo will cite. LFY+1 needs `operator-approved:<date>` unless a real connector (CapIQ/FactSet) supplied it. The brief is explicit: `lfy1_approved_unless_connector`. |
| "This CoCo row's net debt isn't in Yahoo — I'll estimate from market cap and EV" | Estimating net debt as a residual breaks the source contract — the EV-mkt cap residual IS the net debt, by identity, so there's no real source. Fetch from the filing (press release / annual report). If unavailable, flag as `operator-approved:<date>` after the operator confirms. |
| "The precedent tracker has 3 deals in this subsector but they're all >3y old — I'll add the latest 2 from deep-research without writing them back to the tracker" | Writing back IS the contract. The tracker is the SINGLE source of truth for precedent deals; if deep-research finds something not in the tracker, that's a tracker gap to FILL via `append_deal()`, not to use locally and forget. Next-time-someone-runs-comps deserves the same evidence. |
| "Trading currency is GBp, FS currency is GBP — that's a unit issue not a currency mismatch, I'll just divide by 100" | The `flag FS-ccy ≠ trading-ccy` gate fires on UNITS too. Surface it — the operator may want the cell stamped in £ or in p; do not silently convert. |
| "The prior comps workbook is already in `00. OLD/`, I'll just overwrite the live one to keep it tidy" | Archive-then-write is the contract per workspace-write-policy. Prior versions are preserved in `00. OLD/` (not deleted, not overwritten) so the operator can diff. The Iron Law's append-only piece extends to the workbook lineage too. |

## Red Flags

> Quoted internal monologue that signals a violation. If you catch yourself
> thinking any of these, stop and re-read the relevant stage.

- *"I've got Yahoo for everything, I can skip the IR scrape"* — Stage 2 fail. Yahoo has trading data; it does NOT have a single citable source for net debt (it's a derived residual) nor a usable LFY+1 (it's a scraped consensus). The IR scrape IS the source.
- *"The operator said 'use the obvious peers', I'll skip the propose/approve step"* — Iron Law piece 2 breach. "Obvious peers" is exactly the trap; surface the proposed list anyway so the operator can edit a row.
- *"This LFY+1 is well-sourced (broker consensus), I don't need explicit operator sign-off"* — broker consensus is NOT a connector. Connector = CapIQ/FactSet/LSEG/PitchBook contracted feed. Broker-consensus scrapes need `operator-approved:<date>`.
- *"Found a new CoTrans deal via deep-research — I'll add it to the comps table without writing to the tracker"* — Stage 1 contract breach. Write-back is mandatory.
- *"The operator has fired this comps three times today, I'll overwrite the prior bullet in `## Comps history` to keep it clean"* — Iron Law piece 3 breach. Append-only. Multiple bullets per day is correct; the operator can prune manually if they want.
- *"Anthropic cloud skill failed on the deep-research call — I'll fabricate the strategic commentary from what I know about the deal"* — fabrication. Surface the deep-research failure as a warning; leave strategic commentary blank; operator fills.

## Anti-Patterns

- Stamping a row with a Source cell containing the literal text "tbd" or "see filing" — both are unsourced. Sources are URLs, source-register IDs, or `operator-approved:<date>` markers.
- Calling Stage 2 or Stage 3 directly (bypassing the propose/approve gates) by passing a hand-built peer list to the route. The route refuses without the prior-stage approval tokens.
- Stamping the live template `Project_x_Comps_v2.xlsx` instead of a copy — the template is operator-controlled and read-only. Always copy-then-stamp.
- Running comps-build on a workspace flagged MNPI to "warm up" the template — refuse. MNPI inputs are never permitted; comps is public multiples by construction.
- Inferring a CoCo row's currency from the country instead of from the provider's `Quote.currency` — currency is the provider's call, not ours.
- Re-numbering the v2 template's CoCo block ranges by hand instead of going through `anthropic-skills:xlsx` block-flex helpers — manual range edits drift; the engine-skill helper is the one place ranges re-base.
- Writing to `Companies/<target>.md` directly from this skill bypassing the #76 capture loop — the capture proposal is the operator gate; direct write breaks Iron Law piece 3.

## Example — DemoDeal (worked end-to-end, dry-run with stub provider)

Single realistic example. One good example beats five half-examples.

**Operator input:** "Build comps for the DemoDeal deal. The target is a UK
pubs-with-rooms operator — pull peers + precedent transactions."

**Stage 0 — Anton:** "Read `Projects/DemoDeal/00 Brief.md`: target sector
`hospitality`. Read `profile.md sector_sub_lens.hospitality`: 4 candidate
subsectors — `hotels-full-service`, `hotels-limited-service`,
`hotels-lifestyle`, `hotels-boutique`. DemoDeal is a pubs-with-rooms
hybrid → propose **hotels-limited-service** (~70% of comps weight) +
**leisure-pubs** (~30%, for the F&B angle). Returning approval_pending."

**🛑 Gate 0:** operator approves `[hotels-limited-service, leisure-pubs]`,
signs the approval token.

**Stage 1 — Anton:** "Per `hotels-limited-service`: orchestrating
`equity-research:screen` → 6 CoCo candidates (PMI.PA, ACS.PA, MAR, HLT,
IHG.L, WTB.L); deep-research surfaced 3 CoTrans deals not in the tracker
(2026-04-15 ABC/XYZ £180m; 2025-11-02 DEF/GHI £95m; 2025-06-10 JKL/MNO
£210m) — writing all 3 back to the tracker via `append_deal()`.

Per `leisure-pubs`: 3 CoCo candidates (JDW.L, MAB.L, MARS.L); 4 CoTrans
deals already in tracker (2024-09, 2023-07, 2023-03, 2022-11). Returning
approval_pending."

**🛑 Gate 1:** operator approves the peers + deal lists (drops 1 CoTrans
as not comparable), signs the tokens.

**Stage 2 — Anton:** "Per CoCo peer × subsector: provider `get_quotes()`
+ `get_fundamentals()`; net debt scraped from IR pages (cited URLs).
2 currency-mismatches flagged: ACS.PA (FS=EUR, trading=EUR — clean) vs
MAR (FS=USD, trading=USD — clean) vs JDW.L (FS=GBP, trading=GBp — UNIT
mismatch, surfacing). 3 LFY+1 values came from Yahoo consensus, NOT a
connector — surfacing 3 approval-pendings.

Per CoTrans: 7 of 10 deals fully sourced from tracker; 3 from
deep-research with cited press releases. Returning approval_pending with
the 4 figures needing operator sign-off."

**🛑 Gate 2:** operator approves 3 of the 4 LFY+1 numbers + the JDW.L
unit (stamp in £); rejects the 4th LFY+1 as not material (row dropped).

**Stage 3 — Anton:** "Stamping the v2 template. Block 1
(`hotels-limited-service`): 6 CoCo rows + 11 CoTrans rows. Block 2
(`leisure-pubs`): 3 CoCo rows + 3 CoTrans rows. Mean/Median/75th/25th/
Min/Max ranges re-based per block. Saved to
`Project_DemoDeal_COMPS_2026-06-01_v1.xlsx`; prior `_v0.xlsx` moved to
`00. OLD/`. #76 capture proposal emitted (headline: 9 CoCo / 14 CoTrans;
median EV/EBITDA 8.4x / EV/Rev 1.9x). `Sectors/hospitality/Comps.md`
mirror refreshed.

Iron Law check: ALL 23 populated rows have non-empty Source cells (16
provider:<as_of>, 4 IR-URL, 3 operator-approved:2026-06-01)."

**Final output bubble** — KPIs (9 CoCo / 14 CoTrans, median EV/EBITDA
8.4x, median EV/Revenue 1.9x); commentary (2-3 lines on the
hotels-limited vs leisure-pubs spread); chips (Open workbook ·
Companies/DemoDeal.md `## Comps history` · Sectors/hospitality/Comps.md ·
2 unit/ccy flags surfaced).

## When Stuck

| Symptom | Diagnostic |
|---|---|
| Route returns 422 "subsectors approval token missing" on a Stage 1 call | Operator hasn't approved Stage 0 yet (or the token is stale). Re-fire Stage 0 to surface the approval payload; operator signs; re-fire Stage 1 with the token. |
| Route returns 422 "peers/deals approval tokens missing" on a Stage 2/3 call | Same shape as above but at the Stage 1→2 boundary. The route's stage-gate refusal is by design. |
| The xlsx stamp succeeded but the Source column for some CoCo rows is blank | Iron Law breach upstream — the row entered Stage 3 without a sourced figure. The pre-stamp guard SHOULD have surfaced this; if it didn't, file a `#comps-source-guard-drift` follow-on. Do not ship the workbook. |
| The xlsx stamp fails with "block-flex range re-base error" | The v2 template's named-range conventions don't match what `anthropic-skills:xlsx` expects. Could mean the operator edited the template; re-read `os-templates/Project_x_Comps_v2.xlsx` shape, surface the diff. |
| #76 capture proposal didn't appear in `/api/proposals/pending` | The deliverable succeeded but the capture failed (best-effort, by design — see capture.py). Check the bridge log for "comps-build: deliverable→vault capture failed"; the workbook is still valid. |
| `Sectors/<sector>/Comps.md` mirror didn't update | Path-not-found (sector not in `_claude/profile.md active_sectors`?) OR write-permission issue. Surface the warning; the workbook is still valid. |
| Operator says "this comps is wrong — the peers are off" | This is what Stage 1's approval gate is for — re-fire from Stage 0 (or Stage 1 if the subsectors are still correct) and have the operator edit the proposed list. |
| deep-research call returned a CoTrans deal already in the tracker but with different EV figures | Dedup-by-(date, target) means the duplicate is skipped; surface the figure diff to operator. Operator decides whether to update the tracker row or treat the discrepancy as noise. |

## Output Contract

The populated workbook lands at:

```
<workspace-root>/1. Projects/<Deal>/3. Financials & analysis/
  2. Valuation/01. COMPS/
    Project_<Deal>_COMPS_<YYYY-MM-DD>_v<N>.xlsx
```

Filename: `Project_<Deal>_COMPS_<YYYY-MM-DD>_v<N>.xlsx` (skill UPPERCASE).
Prior same-(Deal, COMPS, date) versions MOVE to `00. OLD/` per
workspace-write-policy; never deleted.

**v2 template shape** (block-flex, one CoCo block per approved subsector):

| Block | Cols (CoCo) | Rows |
|---|---|---|
| Per CoCo subsector | B Ticker · C Company · D CCY · E MktCap · F NetDebt · G EV (=SUM(E:F)) · H YE · I/J Revenue LFY/LFY+1 · K/L EBITDA LFY/LFY+1 · N/O/P EV/EBITDA (LFY, LFY+1, LTM) · R/S/T EV/Revenue (LFY, LFY+1, LTM) · V P/E · X Source | N peer rows + 6 stat rows (Mean, Median, 75th, 25th, Min, Max — RE-BASED PER BLOCK) |
| Per CoTrans subsector | A Date · B Target · C Acquirer · D Country · E Target description · F EV · G EV/Revenue · H EV/EBITDA · K Strategic commentary · X Source | N deal rows |

**Color conventions** (existing template; the stamp does not alter):
**blue = input** (the cells the skill writes — figures + Source URLs),
**black = formula** (the cells the template computes — multiples + stats).

**JSON return shape** (what the bridge returns):

```json
{
  "ok": true,
  "stage": "complete",
  "deal_name": "DemoDeal",
  "target": "DemoDeal",
  "run_id": "8-hex audit id",
  "approved_subsectors": ["hotels-limited-service", "leisure-pubs"],
  "blocks": [
    {
      "subsector_slug": "hotels-limited-service",
      "coco_rows": [
        {"symbol": "IHG.L", "name": "IHG plc", "currency": "GBP", "market_cap_m": 12345.0,
         "net_debt_m": 2103.0, "revenue_lfy_m": 4567.0, "ev_ebitda_lfy_x": 9.4,
         "source": "https://www.ihgplc.com/-/media/.../fy25-results.pdf"},
        // ...
      ],
      "cotrans_rows": [
        {"announced_date": "2026-04-15", "target": "ABC", "acquirer": "XYZ",
         "ev_m": 180.0, "ev_revenue_x": 1.5, "ev_ebitda_x": 8.2,
         "strategic_commentary": "expansion into UK budget hotel segment",
         "source": "https://example.com/abc-xyz-press-release"},
        // ...
      ],
      "ccy_flags": [],
      "unsourced_lfy1": []
    }
    // ... one block per approved subsector
  ],
  "headline_ev_ebitda_median": 8.4,
  "headline_ev_revenue_median": 1.9,
  "peer_count": 9,
  "deal_count": 14,
  "as_of": "2026-06-01",
  "provider": "openbb-yfinance + deep-research + IR-scrape",
  "template_path": "<workspace-root>/1. Projects/DemoDeal/3. Financials & analysis/2. Valuation/01. COMPS/Project_DemoDeal_COMPS_2026-06-01_v1.xlsx",
  "prior_archived_path": "<workspace-root>/1. Projects/DemoDeal/3. Financials & analysis/2. Valuation/01. COMPS/00. OLD/Project_DemoDeal_COMPS_2026-06-01_v0.xlsx",
  "mirror_refresh_path": "Sectors/hospitality/Comps.md",
  "tracker_writes": [
    {"deal_id": "PT-2026-04-15-abc", "status": "appended", "row": "42"},
    {"deal_id": "PT-2025-11-02-def", "status": "appended", "row": "43"}
  ],
  "warnings": [],
  "iron_law_assertion": {"all_rows_sourced": true, "rows_checked": 23}
}
```

**Approval-pending return shape** (between stages):

```json
{
  "ok": true,
  "stage": "approval_pending",
  "stage_just_completed": 0,
  "approval_payload": {
    "kind": "subsectors",
    "proposed": ["hotels-limited-service", "leisure-pubs", "hotels-boutique"],
    "rationale": {"hotels-limited-service": "70% revenue mix per CIM", ...}
  },
  "approval_token_to_sign": "approve_subsectors_for=<deal>:<run_id>",
  "deal_name": "DemoDeal",
  "run_id": "8-hex"
}
```

## Citations Required

Every cell-stamped figure maps to a Source. There is no exception. Maps to
[no-invented-sources](<vault>/CLAUDE.md#no-invented-sources).

| Field | Required source type | Acceptable form |
|---|---|---|
| CoCo `market_cap_m` / `price` / `currency` | The markets provider | `<provider-name>:<as_of_date>` (e.g. `openbb-yfinance:2026-06-01`) |
| CoCo `net_debt_m` | IR / results release / annual report | URL to the source filing OR `src:<source-register-id>` |
| CoCo `revenue_lfy_m` / `ebitda_lfy_m` | Filing OR provider fundamentals | URL OR `<provider>:<as_of_date>` |
| CoCo `revenue_lfy1_m` / `ebitda_lfy1_m` | Connector consensus (preferred) OR operator approval | `<connector>:<as_of_date>` OR `operator-approved:<date>` |
| CoTrans `ev_m` / `ev_revenue_x` / `ev_ebitda_x` | Press release / IR release / tracker row | URL OR `tracker:<deal_id>` (canonical tracker reference) |
| CoTrans `strategic_commentary` | Deep-research synthesis from cited sources | Operator-editable; Source cell carries the underlying URL |
| All sourcing | Per row in the workbook's column X | Non-empty string; the pre-stamp guard refuses blank Source cells |

The deal's `Projects/<Deal>/01 Source Register.md` should index the IR URLs
the skill scraped — the route's audit row carries the list so a later
`/recall` can find them.

## Cost Envelope

The frontmatter declares `cost_ceiling_tokens: 18000` and
`cost_ceiling_seconds: 600`. Generous because the pipeline orchestrates
multiple cloud skills (deep-research per CoTrans gap; xlsx stamp) AND
maintains operator-gated state across 4 stages.

**Where the budget goes:**

- ~3,000 tokens — Stage 0 + Stage 1 propose payloads + rationale
- ~6,000 tokens — Stage 2 per-row narration (provider+IR fetch + source
  surfacing + flag-and-ask for unsourced)
- ~5,000 tokens — Stage 3 narration (stamp summary + capture proposal +
  mirror refresh)
- ~4,000 tokens — headroom for one guardrail retry + deep-research
  per-deal back-and-forth

**The 600s ceiling:** the dominant wall-clock is the orchestrated cloud
skills + per-peer provider calls + IR scrapes. 600s covers ~10 peers + ~15
CoTrans gap-fills + the xlsx stamp comfortably.

> **Calibration status:** first-pass estimate. Recalibrate to
> `1.25 × observed` after the first real production runs.

## Verification Checklist (before declaring done)

- [ ] Stage 0 returned a subsector list + operator approval token signed
- [ ] Stage 1 returned peer + deal lists per subsector + both approval tokens signed
- [ ] Stage 2 surfaced every ccy-mismatch + every unsourced LFY+1 for operator approval (never silently filled)
- [ ] Every populated CoCo row has a non-empty Source (column X) — mechanical assertion
- [ ] Every populated CoTrans row has a non-empty Source — mechanical assertion
- [ ] Workbook saved at `…/2. Valuation/01. COMPS/Project_<Deal>_COMPS_<date>_v<N>.xlsx`
- [ ] Prior same-day version moved to `00. OLD/` (not deleted)
- [ ] Deep-research surfaced new CoTrans deals are written back to the canonical tracker (`append_deal()` returns `appended` or `skipped_duplicate`)
- [ ] #76 capture proposal emitted (best-effort — workbook still valid if capture fails)
- [ ] `Sectors/<sector>/Comps.md` mirror refreshed
- [ ] Audit row exists in `runs/tool.comps-build.jsonl` with `status: "ok"`
- [ ] No MNPI in inputs / output / capture proposal
- [ ] Final chat bubble carries headline median multiples + per-block summary + chips, NOT a "preliminary" qualifier
