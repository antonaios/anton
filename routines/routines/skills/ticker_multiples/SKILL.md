---
name: ticker-multiples
description: |
  Use for a lightweight, current trading-multiples quick-look on one or more
  PUBLIC tickers — a dashboard snapshot / data helper, NOT a deliverable build.
  Pulls each ticker (plus a peer set) via the markets adapter and returns the
  current multiples (EV/EBITDA, P/E, ND/EBITDA, EBITDA margin, revenue, dividend
  yield) sourced from the provider. Triggers: ticker multiples, "what are X
  trading at", "quick multiples on <ticker>", trading snapshot, /comps <ticker>
  (the lightweight snapshot — distinct from /comps-build <deal>). Inputs: one or
  more public tickers + optional peer limit. Output: a per-ticker snapshot
  (target + peers) with every figure carrying its provider tag. FIREWALLED from
  the valuation Comps template, the precedent-transactions tracker, and the deal
  Valuation folder — this skill never stamps a workbook and (by default) writes
  nothing to the vault.
version: 0.1.0
license: proprietary
allowed_tools:
  - vault_read           # optional Companies/<X>.md table append (write_note opt-in)
  - vault_write          # only when write_note=True — Companies/<X>.md, NEVER the template/tracker/deal folder
capabilities:                        # #61-capabilities — declared surface, validated at boot
  vault_read:  ["Companies/**"]        # only the Companies note it may (optionally) append to
  vault_write: ["Companies/**"]        # opt-in table append ONLY (write_note=True); firewalled from Sectors/Projects/tracker
  fs_roots:    []                      # NEVER writes the deal Valuation folder / OS Templates — the firewall, declared
  network:                             # internal tier ⇒ provider hosts allowed (same narrow set as equity-research)
    - "query1.finance.yahoo.com"       # OpenBB → YFinance backend (trading data)
    - "query2.finance.yahoo.com"
metadata:
  sensitivity: internal              # public trading multiples — NO MNPI by construction; the markets adapter only accepts public tickers
  workspace_scope: any               # dashboard quick-look — workspace-independent; NOT deal-bound (that's comps-build, project-scoped)
  tile_label: "Ticker multiples"     # matches the relabeled dashboard tile (WS1, 2026-06-01) — see Frontend follow-up note
  cost_ceiling_tokens: 1500          # ties recall-query/bd-decay for smallest — Anton's narration just frames the snapshot; zero LLM in the routine
  cost_ceiling_seconds: 60           # per-ticker provider calls (target + peers); 60s covers a few tickers comfortably
  guardrails:
    - every_figure_sourced            # Iron Law — every populated figure carries the provider tag it came from
    - never_invent_a_figure           # Iron Law — a missing figure stays null; the snapshot fabricates nothing
    - firewalled_from_valuation       # NEVER stamps the template / tracker / deal Valuation folder
  guardrail_max_retries: 1
---

<!--
§14 FLEX NOTES (ticker-multiples vs prior migrated skills)

1. Iron Law is SOURCE-EVERY-FIGURE + NEVER-INVENT + FIREWALL. The first two are
   the universal no-invented-sources discipline made mechanical for a pure
   data-passthrough surface: every figure surfaced carries the provider tag it
   was sourced from, and a figure the provider didn't return stays `null` (the
   snapshot NEVER fabricates a number to fill a gap). The third clause is a
   SKILL BOUNDARY (cousin to recall-query's retrieval≠narration): ticker-
   multiples is a quick-look, NOT a deliverable build — it must NEVER stamp the
   valuation Comps template, touch the precedent tracker, or write the deal
   Valuation folder. That work belongs to the `comps` (comps-build) skill.

2. RENAMED, NOT NEW LOGIC. This is the SESSION-27B `comps-pull` surface
   re-scoped per COMPS-REDESIGN-2026-06-01: `comps` = the research/judgment
   deliverable pipeline; `ticker-multiples` = this lightweight snapshot. The
   route logic reuses `markets/comps.py::build_comps` (the SAME provider chain
   the equity-research sub-leaf uses) — nothing in the markets layer is
   duplicated. The pre-existing `/api/workflows/comps` route in
   `routes/markets.py` is the legacy snapshot surface; THIS skill adds the
   §14-governed `/api/workflows/ticker-multiples` route on top of the same
   `build_comps` call, flowing through the central guard.

3. workspace_scope = any. A quick-look is workspace-independent — the operator
   asks "what's JDW.L trading at?" from anywhere. Distinct from `comps`
   (workspace_scope: project — deal-bound, writes the deal Valuation folder).

4. Capabilities are NARROW by firewall design. `fs_roots: []` is the firewall
   DECLARED: this skill must never reach `<workspace-root>/.../Valuation/`
   or `os-templates/`. `vault_write: ["Companies/**"]` covers ONLY the
   opt-in `write_note=True` table append to `Companies/<X>.md` (the same path
   the legacy snapshot used) — by DEFAULT `write_note=False` and the skill
   writes nothing. `network:` lists only the YFinance provider hosts (same
   narrow set as equity-research; no wildcard).

5. NO captures_to_vault block (deliberate). A quick-look snapshot is not a
   deliverable conclusion to capture into the vault's semantic memory — there's
   no headline-multiples-fact to append to a Company note's history section.
   The (optional) `write_note` table append is a convenience, not a #76
   capture. Distinct from `comps` (which captures the headline medians) and
   LBO (which captures IRR/MOIC).

6. Sensitivity = internal (not public). The MULTIPLES are public, but the
   markets-provider network call (YFinance) means the skill declares network
   hosts — which the §14 validator forbids on a confidential/MNPI tier but
   allows on internal/public. internal matches the sibling market skills
   (equity-research, the legacy snapshot) for lane consistency. No MNPI can
   reach it by construction: the markets adapter accepts only public tickers.
-->

# Ticker Multiples

## Overview

A LIGHTWEIGHT, current trading-multiples quick-look on one or more PUBLIC
tickers. Given a ticker (or a list), the skill pulls the ticker itself plus a
peer set via the markets adapter (`routines.markets.comps.build_comps`, which
sits on the OpenBB/Yahoo provider Protocol — CapIQ/FactSet/LSEG/PitchBook slot
in as providers when licensed) and returns a per-ticker snapshot of current
multiples: EV/EBITDA, P/E, ND/EBITDA, EBITDA margin, revenue, 5y CAGR, dividend
yield — every figure carrying the provider tag it came from.

This is a dashboard data helper, **NOT a deliverable build**. It is FIREWALLED
from the valuation Comps template, the precedent-transactions tracker, and the
deal Valuation folder: it never stamps a workbook, never touches the tracker,
and by default writes nothing to the vault. The returned snapshot IS the
deliverable. For the full operator-gated, template-stamping comps deliverable,
use the `comps` skill (`/comps-build <deal>`) instead — they are deliberately
distinct surfaces (COMPS-REDESIGN-2026-06-01).

**Anton's job** is to FETCH the multiples and SURFACE them with their provider
tags — never to invent a figure, never to stamp a template. Sits on top of
[no-invented-sources](<vault>/CLAUDE.md#no-invented-sources).

## When to Use

**Mandatory triggers** — fire the skill, do not just discuss:
- Operator types `/comps <ticker>` (Cmd-K or composer) — the lightweight
  snapshot, distinct from `/comps-build <deal>`.
- Operator clicks the "Ticker multiples" drawer tile.

**Optional triggers** — propose firing the skill, ask first:
- Operator asks "what's JDW.L trading at?" / "quick multiples on these three" —
  fire the snapshot. If they're inside a Project workspace and clearly want the
  full template build, propose `/comps-build <deal>` instead.

**Don't use** — refuse and explain why:
- The operator wants the populated v2 Comps template, the tracker write-back, or
  anything landing in the deal Valuation folder — that's `comps` (comps-build),
  not this skill. (This skill is firewalled from all three.)
- A non-public identifier (deal codename, target name, buyer name) is passed —
  the markets adapter accepts only public tickers; refuse.

## The Iron Law

> **EVERY FIGURE SURFACED CARRIES THE PROVIDER TAG IT WAS SOURCED FROM. A
> FIGURE THE PROVIDER DID NOT RETURN STAYS NULL — THE SNAPSHOT NEVER INVENTS A
> NUMBER. THE SKILL NEVER STAMPS THE VALUATION TEMPLATE, THE TRACKER, OR THE
> DEAL VALUATION FOLDER.**

This is non-negotiable, and it sits on top of the universals at
[universal-iron-laws](<vault>/CLAUDE.md#universal-iron-laws),
in particular
[no-invented-sources](<vault>/CLAUDE.md#no-invented-sources).

Three clauses:

1. **Source every figure.** Each snapshot row + the run carry the `provider`
   tag (`openbb-yfinance` / `stub` / a licensed feed). The figures are a
   verbatim provider passthrough — no derived or recomputed multiple is
   surfaced as if the provider supplied it.
2. **Never invent.** A multiple the provider didn't return is `null`, not a
   guess. The mechanical test asserts that no `null` provider figure is
   back-filled with a fabricated value. A stub/unlicensed provider returning
   empty fundamentals yields a snapshot of `null` multiples — honest about the
   gap, not papered over.
3. **Firewall.** The skill's `fs_roots` is `[]` and `vault_write` is
   `["Companies/**"]` (opt-in only). It structurally CANNOT write the deal
   Valuation folder, the tracker, or the OS Templates directory. Stamping the
   comps template is a different skill.

## Core Pattern — single-shot (fetch + surface)

The skill is a single-shot data pull (no operator-gated stages). The pattern is
the verification Anton applies to the returned snapshot before narrating.

### Phase 1 — Fetch
- For each requested ticker, call `build_ticker_multiples` → `build_comps`
  (target + peers via the markets provider Protocol). Deterministic;
  side-effect-free unless `write_note=True`.

### Phase 2 — VERIFY provenance + honesty
- **STOP — do not narrate a figure without its provider tag.**
- Confirm each snapshot carries a `provider` tag and each surfaced figure traces
  to it. A `null` multiple is reported AS null (the provider had no data) — never
  back-filled.

### Phase 3 — Surface
- Produce the quick-look bubble: per-ticker multiples table(s) + the provider +
  as-of stamp. If a provider warning fired (e.g. stub provider, unlicensed
  fundamentals), surface it verbatim — do not hide a thin snapshot behind
  invented numbers.

## Quick Reference

```
operator types /comps <ticker>            (or clicks Ticker multiples tile)
  ↓
route refuses if workspace_sensitivity = MNPI (central guard, §5.2)         [hard gate: 403]
  ↓
build_ticker_multiples → build_comps per ticker (target + peers)            [Phase 1]
  ↓        reuses markets/comps.py — NO duplicated provider plumbing
Anton verifies every snapshot has a provider tag + nulls are honest         [Phase 2 STOP]
  ↓
Anton surfaces per-ticker multiples + provider + as-of                      [Phase 3]
  ↓
(write_note=True only) optional table append → Companies/<X>.md             [opt-in side effect]
  ↓
audit row written to runs/tool.ticker-multiples.jsonl                       [hook side effect]
```

## Common Rationalizations

| Rationalization | Reality |
|---|---|
| "The provider didn't return EV/EBITDA but I can compute it from EV and EBITDA, that's basically sourced" | A recomputed multiple is NOT a provider figure. If the provider returned `null`, surface `null`. Deriving it silently breaks clause 1 (it's no longer the provider's number) and clause 2 (you invented the field). |
| "This is basically a comps build, I'll just stamp the template while I'm here" | No. ticker-multiples is firewalled from the template by construction (`fs_roots: []`). The template build is the `comps` skill with its operator gates. Quick-look ≠ deliverable. |
| "The stub provider returned empty fundamentals, I'll fill in plausible multiples so the snapshot isn't blank" | Fabrication — clause 2 breach. A thin snapshot is the honest answer when the provider has no data; surface the provider warning and the `null` cells. |
| "The operator asked for a snapshot, I'll write it into the deal's Valuation folder so it's saved" | Firewall breach. This skill writes nothing to the deal folder; the only (opt-in) write target is `Companies/<X>.md`. If the operator wants a saved deliverable, that's comps-build. |

## Red Flags

> Quoted internal monologue that signals a violation.

- *"I'll back-fill the missing P/E from a quick mental estimate"* — clause 2 breach; `null` stays `null`.
- *"I'll drop the provider tag, the operator knows it's Yahoo"* — clause 1 breach; every figure carries its source.
- *"I'll stamp the template since I already have the data"* — clause 3 breach; firewalled.

## Anti-Patterns

- Surfacing a derived/recomputed multiple as if the provider supplied it.
- Writing anything to `<workspace-root>/.../Valuation/`, `os-templates/`,
  or the precedent tracker — all out of scope, all firewalled.
- Treating a stub/unlicensed thin snapshot as a failure — it's a valid, honest
  result (status=ok with `null` multiples + a provider warning).
- Passing a deal codename / target name as a ticker — only public identifiers.

## Example — JD Wetherspoon quick-look (stub provider, deterministic)

**Operator input:** "Quick multiples on JDW.L."

**Phase 1 — Anton:** "Pulling JDW.L + peers via the markets adapter
(`build_comps`)."

**Phase 2 — Anton:** "Provider = `stub` (OpenBB not installed). JDW.L row +
5 peers returned; fundamentals empty on the stub, so the multiples columns are
`null` — surfacing the gap honestly, not back-filling."

**Phase 3 — final bubble:** per-ticker table (symbol · name · currency ·
EV/EBITDA · P/E · ND/EBITDA · …) with the `null` cells shown as `—`, the
provider tag (`stub`), the as-of date, and a "stub provider — install OpenBB
for live fundamentals" warning surfaced verbatim. With a live OpenBB provider
the same call returns populated multiples, each tagged `openbb-yfinance`.

## When Stuck

| Symptom | Diagnostic |
|---|---|
| The snapshot's multiples are all `null` | The provider returned empty fundamentals (stub / unlicensed). Surface the provider warning; do NOT invent figures. Install OpenBB (`pip install -e .[markets]`) for live data. |
| A ticker returns no row at all | The symbol wasn't recognised by the provider. Surface it as a per-ticker warning; never fabricate a row. |
| The operator wanted the template stamped | Wrong skill — that's `comps` (comps-build). This skill is firewalled from the template by design. |
| Route returns 403 | The central guard refused MNPI inputs (§5.2). Multiples are public by construction; an MNPI workspace input is a mis-route. |

## Output Contract

Pure-return JSON (no file write by default). One snapshot per requested ticker:

```json
{
  "status": "ok",
  "run_id": "8-hex audit id",
  "as_of": "2026-06-05",
  "provider": "openbb-yfinance",
  "snapshots": [
    {
      "target_symbol": "JDW.L",
      "target_name": "JD Wetherspoon",
      "provider": "openbb-yfinance",
      "note_path": null,
      "rows": [
        {"symbol": "JDW.L", "name": "JD Wetherspoon", "currency": "GBP",
         "ev_ebitda": 8.4, "pe": 12.1, "net_debt_ebitda": 2.1,
         "ebitda_margin": 0.11, "is_target": true},
        {"symbol": "MAB.L", "name": "Mitchells & Butlers", "ev_ebitda": 7.9, "is_target": false}
      ],
      "warnings": []
    }
  ],
  "warnings": [],
  "duration_ms": 120
}
```

`note_path` is `null` unless `write_note=True` was requested (then it is the
absolute path of the `Companies/<X>.md` table append — the ONLY write target).

## Citations Required

Every surfaced figure maps to the provider it came from. There is no exception.

| Field | Required source | Acceptable form |
|---|---|---|
| Any multiple / financial in a snapshot row | The markets provider | The snapshot/run `provider` tag (e.g. `openbb-yfinance`, `stub`) |
| A figure the provider didn't return | — | `null` (never a fabricated value) |

## Cost Envelope

`cost_ceiling_tokens: 1500` / `cost_ceiling_seconds: 60`. The token ceiling ties
recall-query / bd-decay for the smallest — the routine does ZERO LLM work; the
ceiling is for Anton's brief narration loop that frames the snapshot. The 60s
seconds ceiling covers a few tickers' worth of provider calls (each ticker pulls
target + peers).

> **Calibration status:** first-pass estimate. Recalibrate to `1.25 × observed`
> after the first real production runs.

## Verification Checklist (before declaring done)

- [ ] Every snapshot carries a `provider` tag
- [ ] Every surfaced figure traces to the provider; no recomputed multiple is presented as provider-sourced
- [ ] Every `null` multiple is reported as `null` (no back-fill)
- [ ] No write to the valuation template / tracker / deal Valuation folder
- [ ] `note_path` is null unless `write_note=True` was explicitly requested
- [ ] Provider warnings (stub / unlicensed) surfaced verbatim, not hidden
- [ ] Audit row exists in `runs/tool.ticker-multiples.jsonl`
- [ ] No MNPI in inputs (public tickers only)
