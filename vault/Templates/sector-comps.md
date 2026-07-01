---
type: sector-claim
sector:
subsectors: [_all]
claim_type: comps
memory_kind: semantic
sensitivity: internal
last_refreshed:
source_count: 0
sources_independent: 0
weighted_independence: 0
confidence: medium
recall_priority: medium
operator_locked: false
comps_excel: "<workspace-root>/4. Research & data/Precedent transactions tracker/Precedent_transactions_tracker.xlsx"
tags: [sector-claim, comps, semantic-memory, precedent-transactions]
---

# {Sector} — comps

> Sector valuation memory. **Two content types live here, in separate sections:**
> **(1) Precedent transactions** — individual M&A deals (one deal's terms);
> permanent facts that do NOT go stale. **(2) Comps runs** — dated valuation
> *snapshots* (peer-set trading multiples as-of a date) produced by the `comps`
> skill. They are different things and must not be merged into one schema.
> The Excel master table at the `comps_excel` path above is the source of truth
> for precedent-transaction rows; this note is the storytelling + synthesis layer.

## Precedent transactions

> Individual M&A deals as `### comp-<id>` blocks, bucketed by `## <YYYY>` (newest
> year first). Fed by **#43 deal capture (CoTrans) + `sector-extract`** — NOT by
> the comps skill. Precedent transactions are permanent facts; they do NOT go
> stale. Block shape:
>
> ```
> ## <YYYY>
>
> ### comp-<yyyy-mm>-<slug>
> - **Date:** YYYY-MM-DD
> - **Target:** <target>
> - **Acquirer:** [[Companies/<acquirer>]]
> - **Consideration:** <headline price>
> - **Implied:** <EV + implied multiple>
> - **Subsector:** `<subsector-slug>`
> - **Structure:** <deal structure>
> - **Significance:** <what this comp anchors / teaches>
> - **Sources:** <provenance>
> - **Confidence:** high | medium | low
> ```

## Comps runs

> Dated comps-run **snapshots** — observed peer-set trading multiples as-of the
> run date. Fed by the `comps` skill's sector mirror (the #76 sibling) on
> operator Route — flat dated bullets (no `### comp-<id>` blocks). **Staleness
> signal = the as-of date in each bullet**: a sector note accrues many snapshots
> over time, so staleness is per-snapshot, NOT a note-level `expires:`. Bullet
> shape:
>
> ```
> - **YYYY-MM-DD** — <Subject> comps · <N> CoCo peers (<tickers>) + <M> CoTrans ·
>   median **EV/EBITDA <x>x**, **EV/Rev <x>x** · subsector: <slug> ·
>   → [`Project_<Deal>_COMPS_<date>_v<N>.xlsx`](<path>) — provenance: `runs:skill.comps.<run_id>`
> ```

## Multiples range summary

> *Curated / future-auto — template-only for v1.* Synthesised subsector ranges
> drawn from the comps-run snapshots above. It needs ~5 snapshots per subsector
> before there's anything worth synthesising, so for v1 this table is
> hand-curated (as in `Sectors/telecoms/Comps.md`). The auto-feed-from-snapshots
> is the tracked follow-up **`#43-sector-comps-range-autofeed`** — do NOT
> auto-build this table yet.

| Subsector | EV/EBITDA range | EV/Rev range | Notes |
|---|---|---|---|
|  |  |  |  |
