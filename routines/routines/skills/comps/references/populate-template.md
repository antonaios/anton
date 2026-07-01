# Populate template — Stage 3 (stamp + archive + capture + mirror)

> Loaded on demand by Anton during the Stage 3 stamp loop.

## The v2 template

`os-templates/Project_x_Comps_v2.xlsx` — operator-controlled, READ ONLY.
Built fresh (NOT an openpyxl resave of the live legacy file — that would
strip the operator's threaded source comments). Shape:

| Section | Columns (CoCo) | Stats rows |
|---|---|---|
| CoCo subsector block | B Ticker · C Company · D CCY · E MktCap · F NetDebt · G EV (=SUM(E:F)) · H YE · I Revenue LFY · J Revenue LFY+1 · K EBITDA LFY · L EBITDA LFY+1 · N/O/P EV/EBITDA (LFY/LFY+1/LTM) · R/S/T EV/Revenue (LFY/LFY+1/LTM) · V P/E · X Source | Mean · Median · 75th · 25th · Min · Max — **RE-BASED PER BLOCK** |
| CoTrans subsector block | A Date · B Target · C Acquirer · **C′ Buyer Type (Strategic/Financial, Q5)** · D Country · E Target description · F EV · G EV/Revenue · H EV/EBITDA · K Strategic commentary · X Source | (no stats — point-in-time deals; **no time-weighting (Q3) / no control-premium adjustment (Q4)** — operator applies judgment in Excel) |

`blue = input` (cells the skill stamps), `black = formula` (cells the
template computes). The skill writes ONLY blue cells; never overwrites
black formula cells.

## Block-flex pattern (LOCKED per operator 2026-06-01)

The template ships with ONE CoCo block + ONE CoTrans block as a UNIT.
The skill stamps ONE CoCo block PER APPROVED SUBSECTOR by:

1. **Insert the block** below the previous one (copy-paste the block range, ~30-40 rows).
2. **Re-base the Mean/Median/75th/25th/Min/Max formulas** to point at the new block's data range (not the original block's). The stats row formulas live in the X+1 column of each row; the range arg gets re-pointed.
3. **Insert the matching CoTrans block** (filtered by the subsector_slug from the approved deal list).

Driven by `anthropic-skills:xlsx` block-flex helpers. The skill code mocks this in tests via a `_stamp_template_v2()` shim.

## Save path (workspace-write-policy compliant)

```
<workspace-root>/1. Projects/<Deal>/3. Financials & analysis/
  2. Valuation/01. COMPS/Project_<Deal>_COMPS_<YYYY-MM-DD>_v<N>.xlsx
```

Versioning:
- `<N>` increments on same-day re-fires: `_v1.xlsx`, `_v2.xlsx`, ...
- Prior `_v<N-1>.xlsx` MOVES (not copies) to `00. OLD/` BEFORE the new write — this is the archive contract.
- Different-day re-fires get a new date stamp; the prior day's `_v<N>` stays in place.

## Pre-stamp guard

BEFORE writing the workbook, iterate the populated row payloads and
assert `source` is non-empty on every row. If ANY row lacks a Source:
- Surface as an error (NOT a warning) with the offending row index +
  subsector.
- Do NOT write the workbook.
- This is the Iron Law piece 1 mechanical enforcement.

## #76 capture (post-stamp)

After the workbook is saved, fire `routines.skills._runtime.capture.emit_deliverable_proposal()`:

- Target: `Companies/{target}.md`
- Section: `Comps history` (LOCKED — distinct from LBO's `Valuation history`)
- Fields: `headline_ev_ebitda_median, headline_ev_revenue_median, peer_count, deal_count, as_of, provider, template_path`
- Headline template: `"{target} comps · {peer_count} CoCo / {deal_count} CoTrans · median EV/EBITDA {headline_ev_ebitda_median}x, EV/Revenue {headline_ev_revenue_median}x · as of {as_of} ({provider})"`

The capture is BEST-EFFORT — a failure is logged but never fails the
deliverable (the workbook already succeeded). The operator's Route action
on the proposal appends the dated bullet to `## Comps history` (append-only).

## Sectors/<sector>/Comps.md mirror

After the Companies/<target>.md capture, emit a SECOND deliverable-outcome
proposal — the SECTOR MIRROR SIBLING — at
`<vault>/Routines/deliverable-outcomes/<date>-<deal>-comps-sector-mirror.md`
with `target: Sectors/<sector>/Comps.md` + `section: Comps runs`.
The skill does NOT write directly into `Sectors/**` — that would escape
the project-scoped vault_write declaration (#61 capability gap).

A comps run is a **valuation snapshot** (peer-set trading multiples as-of a
date), NOT a precedent transaction (one deal's terms) — so the bullet is a
FLAT dated snapshot under `## Comps runs`, never forced into the
`### comp-<id>` precedent-transaction blocks (those live under
`## Precedent transactions`, fed by #43 deal capture). The snapshot bullet
carries: date · subject deal · peer set · median EV/EBITDA + EV/Rev ·
subsector · → deliverable link · provenance (#43-sector-template-align §2).

On operator Route, the existing `_route_deliverable_outcome` handler
appends the dated bullet to the sector mirror under `## Comps runs`, creating
the sector note from the `sector-comps` template if missing (the route's
create-from-template is path-aware). The Companies + Sectors proposals are
independent: the operator can route, skip, or reject each separately.
The mirror is the sector-level pointer so a later sector-wide review
surfaces the latest comps panels.

## Anti-patterns

- DO NOT edit the live `Project_x_Comps_v2.xlsx` — copy first, stamp the copy.
- DO NOT skip the block re-base after a block insert — the stats rows would point at the wrong data and produce wrong medians.
- DO NOT save the populated workbook ANYWHERE other than the `…/2. Valuation/01. COMPS/` folder for THIS deal — the policy path is load-bearing.
- DO NOT delete prior `_v<N>` files; always move to `00. OLD/`.
- DO NOT bypass the #76 capture loop and write directly to `Companies/<target>.md` — the capture proposal is the operator gate.

## Cross-refs

- `data-sourcing.md` — Stage 2 (the Source values the stamp asserts on)
- `routines.skills._runtime.capture.emit_deliverable_proposal` — #76 capture loop
- `<vault>/Topics/Architecture/workspace-write-policy.md` — write target convention
- [no-llm-maths](<vault>/CLAUDE.md#no-llm-maths) — the template's formulas do the maths, not the skill
