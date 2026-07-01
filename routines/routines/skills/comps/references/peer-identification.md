# Peer identification — Stage 1 (CoCo + CoTrans)

> Loaded on demand by Anton when proposing peers + deals for a comps build.

## Two parallel identification paths per approved subsector

### Path A — CoCo peers (listed comparables)

Orchestrate THREE sources in parallel + reconcile:

1. **`equity-research:screen`** (Anthropic cloud skill) — sector + size + region screen. Pass the parent sector + approved subsector slug + target's size band (mkt cap). Returns ~10-15 candidates with one-line "why".
2. **`investment-banking:buyer-list`** (Anthropic cloud skill) — strategic buyer / financial sponsor universe. Useful when the target is itself listed (peers ARE potential acquirers) or for cross-checking the screen.
3. **`provider.get_peers(symbol)`** (markets adapter) — provider's native peer list (Yahoo's "similar tickers"; OpenBB-equivalent). Acts as a sanity check on (1)+(2).

Dedup by ticker; surface a 5-8 peer SHORT LIST per subsector with:
- Ticker + name + country
- Why this peer (one line referencing the screen / buyer-list rationale)
- Source: which of (1)/(2)/(3) surfaced it

### Path B — CoTrans deals (precedent transactions)

Two-step:

1. **Tracker query.** Read the canonical precedent tracker at
   `<workspace-root>/4. Research & data/Precedent transactions tracker/Precedent_transactions_tracker.xlsx`,
   sheet `Precedent transactions` (19-col lean schema, incl. "Acquirer type" after "Acquirer"). Filter by `subsector_slug` + last 5 years (or operator-specified window). **Deal-status filter (Q6):** the pool defaults to `announced_or_closed` — status-less / deep-research rows are treated as announced (included); explicit terminated/withdrawn/rumoured rows are excluded.
2. **Gap-fill via deep-research.** Fire `deep-research` (Anthropic cloud skill) with a structured prompt: "list precedent M&A deals in <subsector> last 5y where target is <size band>". Dedup against the tracker query results by `(announced_date, target)`. For NEW deals not in the tracker:
   - WRITE BACK to the tracker via `routines.dealtracker.workbook.append_deal()` with a fully populated `DealRecord` (announced_date, target, acquirer, **acquirer_type**, EV, EV/Rev, EV/EBITDA, source_url, subsector_slug).
   - **Acquirer type (Q5):** classify the buyer as `"Strategic"` (corporate/trade) or `"Financial"` (PE/sponsor/fund) via `schema.classify_acquirer_type()`; leave blank + flag when ambiguous. Never guessed.
   - Auto-generated `deal_id = PT-<announced-date>-<target-slug>`.
   - Source field = the deep-research source URL (cited press release / IR release).

The write-back is MANDATORY — the tracker is the single source of truth for precedent deals; locally-used-and-forgotten deals defeat the compounding-knowledge contract.

## Operator approval (Stage 1 gate)

Return both lists per subsector in a single approval payload:

```json
{
  "subsector_slug": "hotels-limited-service",
  "coco_proposed": [
    {"ticker": "IHG.L", "name": "IHG plc", "country": "UK",
     "why": "global limited-service operator; ~40% revenue mix matches target",
     "found_by": ["equity-research:screen", "provider.get_peers"]}
    // ... 4-7 more
  ],
  "cotrans_proposed": [
    {"announced_date": "2026-04-15", "target": "ABC", "acquirer": "XYZ",
     "ev_m": 180.0, "source": "tracker:PT-2026-04-15-abc"}
    // ... more from tracker + new from deep-research
  ],
  "tracker_writes_planned": [
    {"deal_id": "PT-2026-04-15-abc", "...": "..."}
    // deals deep-research found that aren't in the tracker yet
  ]
}
```

Operator can:
- Approve as-is → both lists go to Stage 2
- Edit either list (drop rows, add tickers/deals) → Anton re-fires Stage 1 with the edits
- Reject + ask for re-source → Anton fires a different screen prompt

## Anti-patterns

- DO NOT pre-filter the screen / buyer-list output for "obvious" peers; surface the full list and let the operator narrow.
- DO NOT call deep-research without first querying the tracker — the tracker has 5y of curated evidence, deep-research is for gaps.
- DO NOT use deep-research results without writing back to the tracker — that breaks the compounding-knowledge contract.
- DO NOT include the target itself in the CoCo list (the template stamps the target separately in row 0).

## Cross-refs

- [no-mnpi-to-cloud](<vault>/CLAUDE.md#no-mnpi-to-cloud) — sensitivity check fires BEFORE any cloud-skill call
- `subsector-taxonomy.md` — Stage 0 propose/approve
- `data-sourcing.md` — Stage 2 acquisition
- `routines.dealtracker.workbook.append_deal()` — tracker write-back
