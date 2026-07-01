---
name: lbo
description: |
  Use when modeling a leveraged buyout, sizing acquisition debt, building a
  sponsor IRR/MOIC sensitivity grid, sensitising entry/exit multiples, or
  producing a sources-and-uses + cap-structure breakdown for an IC memo.
  Triggers: LBO, MBO, take-private, sponsor model, debt sizing, IRR sensitivity,
  exit multiple grid, S&U, sources and uses, equity cheque, leverage. Inputs:
  target name, entry multiple + acquisition EBITDA, leverage (debt/EBITDA),
  minimum equity, hold period, fees. Output: a populated XLSX in the deal's
  Valuation folder + a narrative summary citing every assumption.
version: 0.3.0
license: proprietary
allowed_tools:
  - llm_local
  - vault_read
  - vault_write
  - engine_call
capabilities:                        # #61-capabilities — declared surface, validated at boot
  vault_read:  ["Projects/**", "Companies/**", "Registers/**"]   # deal + counterparties + source registers
  vault_write: ["Projects/<deal>/**"]                            # only the deal tree (project-scoped)
  fs_roots:    ["<workspace-root>/**", "<workspace-root>/**"]      # populated XLSX lands here (Output Contract)
  network:     []                                                # confidential ⇒ no external endpoints (§5.2)
captures_to_vault:                   # #76 — opt-in: capture the deliverable's CONCLUSION back to the vault
  target: "Companies/{deal_name}.md"   # where the dated valuation fact lands (operator-gated; templated by run context)
  fields: [irr_central_pct, moic_central_x, entry_multiple, exit_multiple, equity_cheque_m, hold_years]
  headline: "{deal_name}: {irr_central_pct}% IRR / {moic_central_x}x MOIC at {entry_multiple}x entry → {exit_multiple}x exit (equity £{equity_cheque_m}m, {hold_years}y hold)"
  section: "Valuation history"          # append-only history section on the Company note (§3 rule 9)
metadata:
  sensitivity: confidential
  workspace_scope: project
  tile_label: "LBO Model"
  cost_ceiling_tokens: 8000
  cost_ceiling_seconds: 90
  guardrails:
    - engine_validation_passes
    - sources_and_uses_ties
    - every_assumption_cited
  guardrail_max_retries: 1
---

# LBO Model

## Overview

Drives the operator's `Project_x_LBO_date.xlsx` (v4, DemoDeal-shape) template
via the valuation engine (`engine run lbo`). The engine copies the template,
writes the assumption cells, triggers Excel recalc, runs the post-recalc
hardcoding ritual that breaks the exit-multiple circular reference, reads the
returns + S&U outputs back, validates them, and saves the populated workbook to
`<workspace-root>/<Deal>/3. Financials & analysis/2. Valuation/`. **Anton's job is
to pick inputs from operator context (brief, recall hits, source register),
call the engine, verify the engine's validation gate passed, and narrate the
returns with citations** — Anton does not do the maths (see
[no-llm-maths](<vault>/CLAUDE.md#no-llm-maths)).

## When to Use

**Mandatory triggers** — fire the skill, do not just discuss:
- Operator types `/lbo` (Cmd-K or composer)
- Operator clicks the LBO drawer tile (gated on a Project workspace)
- A composite (`/pitch`, `/ic-memo`) calls the LBO step in its DAG

**Optional triggers** — propose firing the skill, ask first:
- Operator asks "what's the IRR if we lever at 6x?" inside a Project chat
- Operator asks "show me the entry/exit sensitivity for this deal"

**Don't use** — refuse and explain why (the bridge route enforces these as a
hard 403/422 gate):
- Workspace is BD or General — the LBO template is project-scoped; the engine
  refuses unless `<workspace-root>/<Deal>/` **and** `<vault>/Projects/<Deal>/`
  both exist. (403: "workspace is general; LBO requires a project workspace".)
- Workspace is MNPI tier — no pre-announce results; wait for embargo lift per
  [no-mnpi-to-cloud](<vault>/CLAUDE.md#no-mnpi-to-cloud).
  (403: "LBO does not run on MNPI inputs".)
- No citations supplied — every assumption needs a source. (422.)
- Operator hasn't named a target — refuse with "name the target before running;
  IRR on nothing isn't a number".

## The Iron Law

> **NO RETURN FIGURE IS REPORTED UNTIL THE ENGINE'S VALIDATION GATE PASSES
> (`status == "ok"`) AND THE SOURCES & USES TIE-CHECK CELL IS TRUE.**

This is non-negotiable, and it sits on top of the universals at
[universal-iron-laws](<vault>/CLAUDE.md#universal-iron-laws).
If the engine exits non-zero (validation gate failed) or the S&U tie-check
(`sources_and_uses.ties`) is false, the engine has returned garbage or refused.
Suppress the IRR / MOIC bubble in chat, surface the engine's failure message
verbatim, and stop. Do not show a "best-effort" IRR with a warning chip — that
signal gets ignored under time pressure and a wrong IRR drives a wrong
recommendation.

> **Engine-reality note (2026-05-29 baseline).** In the v4 template, Sources =
> Uses is a *structural identity* — the tie-check cell (`G97`, the last row of
> `sources_and_uses`) is TRUE by construction and cannot be broken via inputs.
> The load-bearing failure surface is therefore the engine's own validation
> gate: FTEV > 0, sponsor equity > 0, stub period in [0, 1], and **the IRR-grid
> centre cell being a real number** (it comes back blank when `holding_period`
> exceeds the template's projection horizon — ~5 years for a mid-2026
> acquisition). When that gate fails the engine exits 2 and no returns exist to
> report. The Iron Law wording above is operator-confirmable — flag if you'd
> phrase it differently.

## Core Pattern — 4 phases (compute + verify)

The engine runs as a single subprocess; it does not pause between internal
steps. These phases are the verification checkpoints Anton applies to the one
returned payload **before** narrating. **STOP markers are not advisory** — if a
verification phase doesn't have an explicit written result, do not proceed to
narration.

### Phase 1 — Assemble inputs and call the engine
- Read `00 Brief.md` frontmatter (`target`, `sector`, `sensitivity`); read the
  most recent comps run if present; read source-register entries tagged
  `acq_multiple` / `acq_ebitda` / `target_financials`.
- Populate the 20 required engine inputs (entry multiple, acquisition EBITDA,
  debt/EBITDA, minimum equity %, TLA split, RCF quantum, fees, tax, dates, hold
  period) + any optionals (scenario switches, cap-structure splits, min cash).
- Optionally attach the deal's operating model as `client_fs`
  (`ClientFSBlock`: dates + Client_FS row arrays, VERBATIM full-currency
  values — NOT £m). The wrapper ships it to the engine as a separate JSON on
  `--client-fs`; the engine writes it into `Client_FS` before the input cells
  and refuses formula (total-row) targets. Omit → the run uses whatever
  operating model sits in the template's `Client_FS` sheet.
- Call `engine run lbo --output-json`.

#### Intake mode (#63 suspend/resume — the dashboard path, 2026-06-09)
`POST /api/workflows/lbo` also accepts `{mode: "intake", deal_name,
workspace_*, deal_context, prefill?}`: the route SUSPENDS (202) with the
deal-assumption **boxes manifest** in `options` (20 fields; conventions carry
defaults, deal-specific fields don't; `prefill` overrides a default). The
operator answers via `POST /api/skills/{run_id}/resume` with
`{"boxes": {...}, "citations": [...]}` — a fixable answer (missing citations /
failed validation) RE-SUSPENDS the same run with a fresh token instead of
burning it; a valid answer assembles the full `LBOInput` and runs Phases 1-4
unchanged. Intake TTL = 7 days. The one-shot full-payload call is untouched.
NB building the deal's operating model is upstream of intake (the chat-agent
leg; see OUTSTANDING `#lbo-dashboard-wiring`) — but the result can now ride
the run: an intake fire may carry a `client_fs` block (checkpointed in the
suspension state), and a resume answer may supply/override one
(`{"boxes": …, "citations": …, "client_fs": …}`); the assembled `LBOInput`
forwards it to the engine. Absent a block, the engine still runs on whatever
operating model sits in the template's `Client_FS` sheet.

### Phase 2 — VERIFY the engine gate and S&U tie
- **STOP — do not narrate any return figure until this passes.**
- Confirm `engine_status == "ok"` (engine exited 0) and
  `validation.sources_and_uses_ties` is True. If the engine exited non-zero,
  surface its verbatim message; there are no returns to report.
- Write a one-line acknowledgement: "Engine gate OK; S&U ties: Sources £XXX.Xm
  (= FTEV), tie-check TRUE." — this is the audit trail that Phase 2 ran.

### Phase 3 — VERIFY headline sanity
- **STOP — do not proceed to returns narration.**
- Confirm the engine's headline values are coherent: FTEV > 0; sponsor equity >
  0; stub period in [0, 1]; `net_debt_at_close ≈ tla_quantum + tlb_quantum −
  min_cash`. These mirror the engine's own validation rules — restate them in
  writing so the verification is visible, don't assume.
- Out of range → the engine should have refused; treat as an engine bug and
  surface the `run_id`.

### Phase 4 — VERIFY returns are real numbers
- **STOP — do not narrate any return figure until this passes.**
- Confirm the IRR-grid centre cell (`returns.irr_central_pct`) is a number, not
  null. Null means the IRR calc failed — almost always `holding_period` beyond
  the projection horizon. Suppress the returns bubble; surface verbatim.
- Confirm the entry/exit axes parsed from the Output Summary table (a non-empty
  `sensitivity.entry_axis` / `exit_axis`). If `warnings` flags axis-parse
  drift, the template layout moved — narrate the central case only and flag the
  grid as unavailable; do not invent axis values.

Only after Phase 4 passes does Anton produce the chat bubble with returns +
commentary + chips.

## Quick Reference

```
operator types /lbo               (or clicks LBO drawer tile)
  ↓
route refuses if workspace ≠ project OR sensitivity = MNPI OR no citations   [hard gate: 403/422]
  ↓
Anton assembles the 20 engine inputs from brief + recall + sources           [Phase 1]
  ↓
engine run lbo --output-json  (copies template, recalcs, hardcodes, validates, saves)
  ↓
Anton verifies engine_status == ok AND sources_and_uses_ties == True         [Phase 2 STOP]
  ↓  (engine exit ≠ 0  →  502  →  NO returns bubble; surface verbatim)
Anton verifies FTEV>0, sponsor_equity>0, stub∈[0,1], net-debt identity       [Phase 3 STOP]
  ↓
Anton verifies IRR-grid centre is numeric + axes parsed                      [Phase 4 STOP]
  ↓
Anton produces chat bubble: IRR/MOIC central + sensitivity grid + chips      [outputs]
  ↓
populated XLSX lands at
  <workspace-root>/<Deal>/3. Financials & analysis/2. Valuation/
    Project_<Deal>_LBO_<YYYY-MM-DD>_v<N>.xlsx                                [side effect]
prior same-day same-skill version moves to 00. OLD/                          [archive]
audit row written to runs/tool.lbo.jsonl                                     [hook side effect]
```

## Common Rationalizations

> **Append-only** by the operator after each production run that surfaces a new
> shortcut (CLAUDE.md §14.3). The 8 rows below are the v1 seed; the 2026-05-29
> baseline ran as an engine subprocess (no model narration), so none were
> replaced — they stand as institutional guidance for the narration loop.

| Rationalization | Reality |
|---|---|
| "This is a vanilla buyout, defaults are fine" | Defaults reflect the last template author's view, not the current deal — sector mix, cap structure, and exit window all drift. Re-source inputs from the brief + recall, even on a "boring" deal. |
| "Sponsor returns look reasonable, skip the sensitivity grid" | "Reasonable" is the failure mode — 19% IRR off a wrong entry multiple is more dangerous than a clearly-broken 4%. The grid IS the deliverable; the operator can't see the range without it. |
| "The engine exited 0, so the numbers are right" | Exit 0 means the engine's *internal* rules passed (S&U tie, FTEV>0, IRR-grid numeric). It does NOT validate that your *inputs* match the deal. Garbage-but-self-consistent inputs return clean. Re-check the inputs against the brief. |
| "Entry multiple of 10x is the operator's house view, no need to cite" | House views still need a source-register entry pointing to the deal's comps run or sector note — otherwise the IC memo's "how did we get to 10x?" has no answer. |
| "S&U is off by a rounding penny, that's noise" | S&U is a structural identity here — it always ties (G97 TRUE). If you *think* you see an S&U mismatch, you're misreading the output; re-read it. A real mismatch would mean an engine bug, not rounding. |
| "The leverage looks high but the engine clamped it, so it's fine" | The `min_equity` floor silently clamps over-leverage to the equity minimum. The engine returning clean does NOT mean your debt/EBITDA was sensible — it means the floor caught it. Confirm the intended leverage actually applied. |
| "Acquisition EBITDA is £X per the management deck, that's the source" | Management decks are management views, not audited filings. Distinguish reported (audited) from adjusted (management) from forecast — CLAUDE.md §4a finance-grade sourcing applies. |
| "This is the third LBO this week, I can move faster" | The third LBO is exactly when the discipline erodes; each LBO is its own deal. The phases are cheap; a wrong IRR in an IC memo is not. |

## Red Flags

> Quoted internal monologue that signals a violation. If you catch yourself
> thinking any of these, stop and re-read the relevant phase.

- *"The engine errored but the inputs look fine, I'll just rerun with a shorter hold and report that"* — Phase 2/4 fail; surface the validation message first, then diagnose. Don't quietly mutate inputs to force a clean run.
- *"The IRR grid centre came back blank but the other cells look populated"* — Phase 4 fail; `holding_period` almost certainly exceeds the horizon. No central return exists.
- *"I'll narrate the central case and skip the grid"* — the grid IS the deliverable.
- *"The brief is thin so I'll fill in plausible assumptions"* — every assumption needs a source-register entry, including "operator confirmed verbally on YYYY-MM-DD" if that's the source.
- *"Sponsor equity came back tiny but positive, leverage must be fine"* — the `min_equity` floor may have clamped it; confirm the intended debt/EBITDA actually drove the structure.
- *"The chip says engine warning but the IRR looks right"* — an IRR off a failed gate is meaningless; suppress the bubble, do not annotate.

## Anti-Patterns

- Reporting "approximate IRR" or "preliminary returns" — there are no preliminary returns. Either the gate passes and you report, or it fails and you don't.
- Showing a returns chart with an "engine validation failed" warning chip — see the Iron Law.
- Forcing an entry/exit multiple to make IRR hit the operator's hurdle. The model populates from the brief + comps; if IRR doesn't hit, surface that fact + the implied break-even exit multiple from the grid.
- Quoting a MOIC from the formatted summary grid while ignoring that the numeric IRR grid is the authoritative source — the formatted "IRR%/MOICx" cells are display rounding.
- Inventing a numeric MOIC grid or a return bridge — the v4 engine does not produce them (see Output Contract). Report only what the engine returns.
- Running the skill on an MNPI workspace via a sensitivity override. The route refuses; do not try to bypass.

## Example — DemoDeal take-private (worked end-to-end, real 2026-05-29 run)

Single realistic example. One good example beats five half-examples.

**Operator input:** "Run an LBO on DemoDeal. Entry 10.0x adjusted EBITDA of
£13.5m; lever at 5.73x; minimum equity 35%; £25m revolver; 5-year hold; M&A
fees £2m. Show me the IRR/MOIC grid."

**Phase 1 — Anton's inputs** (the 20 engine inputs, abbreviated):
```json
{
  "deal_name": "DemoDeal",
  "acq_multiple": 10, "acq_ebitda": 13.5, "ebitda_basis": "Adj",
  "debt_ebitda": 5.73, "min_equity": 0.35, "tla_split": 1, "rcf_quantum": 25,
  "ma_fees": 2, "step_multiple": 0.5, "holding_period": 5,
  "acq_date": "2026-06-30", "fye_post_acq": "2026-12-31", "tax_rate": 0.25,
  "pref_interest": 0.10, "currency": "GBP", "scenario_provider": "Management",
  "first_fye": "2024-12-31", "project_name": "DemoDeal", "rcf_switch": 0,
  "existing_net_debt": 0
}
```

**Phase 2 — Anton's chat line:** "Engine gate OK (`status=ok`); S&U ties:
Sources £137.9m = FTEV, tie-check TRUE (net debt 43.9% / equity 56.1%)."

**Phase 3 — Anton's chat line:** "Headline sane: FTEV £137.9m; sponsor equity
£77.2m; TLA £60.6m, TLB £0.0m; net debt at close £60.6m (= 60.6 + 0.0 − 0.0
min-cash); stub 0.5y. All within tolerance."

**Phase 4 — Anton's chat line:** "IRR-grid centre numeric: 9.6% / 1.6x at entry
10.0x / exit 10.0x. Entry axis 8.0x–12.0x, exit axis 9.0x–11.0x, step 0.5x.
Grid parsed cleanly (no warnings)."

**Final output bubble** — KPIs (IRR central **9.6%**, MOIC **1.6x**, equity
cheque **£77.2m**, hold 5y); commentary (3–4 lines: at the central 10.0x/10.0x
the deal returns sub-hurdle; the grid shows IRR ranges 0.4%–20.8% across
entry 8–12x × exit 9–11x; de-leveraging from 5.73x is the main return driver
given flat multiples); chips (Open in Excel [populated XLSX] · IRR/MOIC grid
[show 5×9 range] · S&U [ties at £137.9m] · Source register [2 cites]).

## When Stuck

| Symptom | Diagnostic |
|---|---|
| Engine exits 2 with "IRR grid centre is not a number" | `holding_period` exceeds the template horizon. Max safe is ~5 for a mid-2026 acquisition (9-period timeline). Lower the hold or extend the template (engine team). |
| Engine exits 1 "project … not fully scaffolded" | `<workspace-root>/<Deal>/` or the vault `Projects/<Deal>/` is missing. Run `engine new-project <Deal>` to bootstrap both atomically; `engine project-status <Deal>` shows which side is missing. |
| Engine returns `TemplateHashMismatch` (exit 2) | `os-templates/Project_x_LBO_date.xlsx` was hand-edited. Run `engine validate lbo` to confirm the cell map still resolves; if it does, re-hash with `--rehash`. If validation fails, the cell map is stale — surface to operator; do not auto-fix. |
| Sponsor equity came back at exactly `min_equity` of FTEV | The `min_equity` floor clamped the requested leverage. Your `debt_ebitda` implied more debt than the floor allows; the structure used the floor instead. Confirm that's intended. |
| `warnings` flags "entry axis not found" / "central MOIC not parseable" | The Output Summary table layout shifted (rows inserted above B101). The numeric `irr_grid` is still authoritative; narrate the central IRR + flag the formatted grid as unavailable. Engine team: re-confirm the `Output_Summary_Range` registration. |
| Excel COM error ("Cannot run macros / file locked") | Another Excel instance has the template open. Close all Excel instances; rerun. The engine tears down its own COM session in a finally block; this only fires on an external lock. |

## Output Contract

The populated workbook lands at:

```
<workspace-root>/<Deal>/3. Financials & analysis/2. Valuation/
  Project_<Deal>_LBO_<YYYY-MM-DD>_v<N>.xlsx
```

Filename: `Project_<Deal>_LBO_<YYYY-MM-DD>_v<N>.xlsx` (skill UPPERCASE). Prior
same-(Deal, LBO, date) versions MOVE to `00. OLD/` per engine archive policy;
never deleted.

**Sheets the engine reads from** (per `engine/templates/templates.yaml`):

| Sheet | Purpose | Key ranges read |
|---|---|---|
| `LBO` | Main model — assumptions, S&U, returns grids | `B101:Q120` (Output Summary table, formatted IRR/MOIC grid + entry-multiple ladder), `Y187:AG195` (9×9 numeric IRR grid), `G89:H97` (S&U bridge + tie-check `G97`), headline cells `I76`/`G90`–`G95` |
| `Client_FS` | Client financials — generic deal slot (overwrite per deal) | — |
| `OpModel_Link` | Operating-model bridge | — |
| `1PageOutput`, `GBPSONIA` | Output formatting + rates | — |

**Color conventions** (existing template; engine does not alter): Blue =
hardcoded input · Black = formula · Green = cross-sheet link · Red = hardcode
override (the `S118 = M103` paste-special circular break).

**Axes are read from the workbook, never computed** (CLAUDE.md §14 Q7): the
entry axis comes from the `"x"` row of the Output Summary table; the exit axis
from the `"Exit at N.Nx …"` row labels. Both centre on the chosen multiple at
`step_multiple` spacing. Do not hard-code `I114:Q118` — rows shift per project.

**JSON return shape** (what the bridge returns; mirrors the engine's real v4
outputs — `returns` derived, `headline`/`sensitivity` passed through):

```json
{
  "ok": true,
  "deal_name": "DemoDeal",
  "run_id": "8-hex audit id (shared with runs/tool.lbo.jsonl)",
  "output_xlsx_path": "<workspace-root>/DemoDeal/3. Financials & analysis/2. Valuation/Project_DemoDeal_LBO_2026-05-29_v1.xlsx",
  "duration_ms": 9616,
  "convergence_iters": 1,
  "returns": {
    "irr_central_pct": 9.58,        // irr_grid centre × 100
    "moic_central_x": 1.6,          // parsed from the formatted summary grid; null if unparseable
    "equity_cheque_m": 77.23,       // sponsor equity (new cheque at close)
    "hold_years": 5                 // echo of holding_period
  },
  "headline": {
    "ftev_m": 137.91, "entry_multiple": 10.0, "exit_multiple": 10.0,
    "tla_quantum_m": 60.56, "tlb_quantum_m": 0.0, "net_debt_at_close_m": 60.56,
    "sponsor_equity_m": 77.23, "management_equity_m": 0.12, "total_equity_m": 77.34,
    "stub_period": 0.5
  },
  "sensitivity": {
    "irr_grid": [[...9×9 decimal...]],
    "entry_axis": [8.0, 8.5, 9.0, 9.5, 10.0, 10.5, 11.0, 11.5, 12.0],
    "exit_axis":  [9.0, 9.5, 10.0, 10.5, 11.0],
    "summary_grid": [["15.3%/2.0x", "..."], ["..."]],
    "moic_grid": null               // NOT produced by the v4 engine — do not fabricate
  },
  "sources_and_uses": [[0.0,0.0],[60.56,0.439],[0.0,0.0],[60.56,0.439],[77.23,0.560],[0.12,0.001],[77.34,0.561],[137.91,1.0],[true,null]],
  "validation": {
    "engine_status": "ok",
    "engine_rules_passed": true,
    "sources_and_uses_ties": true
  },
  "warnings": [],
  "citations": [
    {"field": "acq_ebitda",   "source_id": "src:DemoDeal-fy25-results"},
    {"field": "acq_multiple", "source_id": "operator-verbal:2026-05-29"}
  ]
}
```

**What the v4 engine does NOT produce** (and the skill therefore does not
report): a numeric MOIC grid, a return-attribution bridge (entry→exit equity by
component), covenant-headroom paths, or a balance-sheet-balance check. These
were in the original aspirational template; they require engine work (#18) and
are out of scope for this thin IO migration. Reporting them would mean
fabricating numbers — forbidden by
[no-llm-maths](<vault>/CLAUDE.md#no-llm-maths).

## Citations Required

Every input maps to a row in the deal's `Projects/<Deal>/01 Source Register.md`
AND a row in this skill's `citations` return field. Any field that can't trace
to a source is surfaced as an explicit ASK chip; never silently defaulted.

| Field | Required source type | Acceptable form |
|---|---|---|
| `acq_ebitda` | Filing OR vendor data feed | `src:<id>` citing the filing line item OR feed pull date |
| `acq_multiple` | Recent comps run OR operator-explicit | `runs:comps.<deal>.<date>` OR `operator-verbal:<date>` |
| `debt_ebitda` | Indicative debt term sheet OR operator | `src:<id>` to term sheet OR `operator-verbal:<date>` |
| `min_equity` | Sponsor mandate / fund policy | `src:<id>` OR `operator-verbal:<date>` |
| `tla_split` / `rcf_quantum` | Debt package indicative term sheet | `src:<id>` to term sheet |
| `ma_fees` | Engagement-letter / deal-cost estimate | `src:<id>` OR `operator-verbal:<date>` |
| `holding_period` | Operator | `operator-verbal:<date>` |
| `step_multiple` | Convention (sensitivity ladder spacing) | `operator-verbal:<date>` (note as convention) |

## Cost Envelope

The frontmatter declares `cost_ceiling_tokens: 8000` and `cost_ceiling_seconds:
90`. The per-skill caps are enforced by the central hook stack once the
frontmatter-reader lands (today `get_active_skill_cap` is a stub returning None
— telemetry only; see #61/#67); the route also bounds the engine subprocess at
a 90s hard timeout.

**Where the token budget goes** (the narration loop, not the engine):
- ~2,000 tokens — assembling inputs from brief + recall + comps run
- ~1,000 tokens — Phase 2 / 3 / 4 verification narration
- ~3,000 tokens — final commentary + grid explanation
- ~2,000 tokens — headroom for one guardrail retry

**The 90s ceiling:** the engine itself is the dominant wall-clock — the
2026-05-29 baseline measured **~8–9.6s per run** (cold Excel COM launch +
recalc + 1 convergence pass), NOT the ~1.8s an earlier note assumed. 90s leaves
comfortable headroom; if Excel hangs (COM lockup) the subprocess timeout fires
and the run is marked `error` in `runs/tool.lbo.jsonl`.

> **Calibration status:** the token ceiling (8000) is still the first-pass
> guess — the baseline ran as an engine subprocess with no LLM narration, so it
> produced no token data to calibrate against. Recalibrate to `1.25 ×
> observed` after the first real narrated production runs.

## Verification Checklist (before declaring done)

- [ ] Engine exited 0 (`validation.engine_rules_passed == true`)
- [ ] S&U tie-check cell TRUE (`validation.sources_and_uses_ties == true`)
- [ ] FTEV > 0; sponsor equity > 0; stub period in [0, 1]
- [ ] IRR-grid centre is a number (`returns.irr_central_pct` not null)
- [ ] Entry/exit axes parsed (`sensitivity.entry_axis` / `exit_axis` non-empty); `warnings` empty
- [ ] Every input has a citation row
- [ ] Populated XLSX exists at the canonical path
- [ ] Prior same-day version is in `00. OLD/` (not deleted)
- [ ] Audit row exists in `runs/tool.lbo.jsonl` with `status: "ok"`
- [ ] No raw MNPI in the inputs file or output workbook
- [ ] Final chat bubble carries 4 KPIs + commentary + chips, NOT a "preliminary" qualifier
