---
name: deal-tracker
description: |
  Use when extracting one M&A deal record from a news article or press
  release and appending it to the canonical precedent transactions tracker.
  Triggers: track this deal, log this M&A, append to deal tracker,
  "add this announcement", extract deal from URL. Inputs: source URL +
  article body text (or text file), optional workbook override, dry-run flag.
  Output: one row appended to
  <workspace-root>/4. Research & data/Precedent transactions tracker/
  Precedent_transactions_tracker.xlsx (sheet "Precedent transactions",
  18-column lean schema: announced_date, target, target description, sector,
  subsector_slug, country, acquirer, seller, currency, EV (m), revenue (m),
  EBITDA (m), EV/Revenue, EV/EBITDA, deal description, strategic commentary,
  source, deal_id) OR a "skipped_duplicate" status if (announced_date,
  target_company) already exists.
version: 0.1.0
license: proprietary
allowed_tools:
  - llm_local           # Ollama qwen3:14b — single JSON-mode extraction call
  - vault_read          # source URL provenance only — extraction is from inline text, not vault scan
  - fs_write            # the canonical workbook lives OUTSIDE the vault on the corporate-finance research drive
capabilities:                        # #61-capabilities
  vault_read:  []                                           # extraction is from inline text + URL; no vault scan
  vault_write: []                                           # canonical tracker lives outside the vault
  fs_roots:                                                 # the lean-18-col canonical tracker
    - "<workspace-root>/4. Research & data/Precedent transactions tracker/**"
  network:     []                                           # Ollama is local; no external HTTP
metadata:
  sensitivity: internal        # public news + audited M&A facts — NOT confidential (no deal-side MNPI)
  workspace_scope: any         # workspace-independent (single shared tracker workbook for the firm)
  tile_label: "Deal Tracker"
  cost_ceiling_tokens: 3000    # one qwen3:14b JSON-mode call + Anton's narration; small
  cost_ceiling_seconds: 60     # extract (~5-10s warm Ollama) + openpyxl append (<1s); generous headroom
  guardrails:
    - target_company_extracted   # the routine warns "no target extracted — likely not an M&A announcement" — Anton must surface this
    - no_computed_multiples      # extractor SYSTEM_PROMPT forbids inferring multiples; verify on output
    - dedupe_checked             # confirm the routine's dedupe ran (status in {"appended", "skipped_duplicate"})
  guardrail_max_retries: 1
---

<!--
§14 FLEX NOTES (deal-tracker vs prior 3 migrated skills)

1. Iron Law has TWO clauses (target_company non-empty + no-computed-multiples),
   distinct from sector-news' single sourcing-shaped law and from
   vault-health's enumeration-honesty law. The second clause
   (no-computed-multiples) is the [no-llm-maths] universal applied to deal
   records — the extractor's SYSTEM_PROMPT is the bind (it instructs the
   model to return null when the source did not state the multiple), Anton's
   verification is the second-pass check. If a multiple is non-null and the
   source text doesn't quote it, the model drifted — Anton flags, does not
   append.

2. openpyxl-based append, NOT Excel COM. Distinct from LBO which depends on
   xlwings + a live Excel instance for full-template population and circular-
   reference breaks. Deal-tracker's `workbook.append_deal` is a pure-python
   write — no headless-context blocker. The test surface mirrors this:
   workbook tests use `tmp_path` openpyxl files (no Excel runtime required).

3. SINGLE Ollama call per fire (extract one record), distinct from
   sector-news' per-item scoring loop (one call per fetched article) plus
   synthesis call. Cost ceiling 3000 tokens / 60s reflects this: ~500-800
   tokens for the extraction prompt + ~200 output + ~1000-2000 tokens for
   Anton's narration loop + headroom for one guardrail retry.

4. Output Contract is a single Excel ROW APPEND + a structured JSON status
   payload — four distinct deliverable shapes now exercised across the
   migrated quartet:
       LBO          → populated XLSX (full template, COM-driven)
       sector-news  → markdown newsletter (synthesis)
       vault-health → markdown report (enumeration sweep)
       deal-tracker → single row append + JSON status
   This validates that §14's contract is shape-independent.

5. `capabilities.network: []` — the routine does NOT fetch the URL itself.
   The operator pastes the article body inline (or supplies a `text_file`);
   the URL is provenance only. If a future "auto-fetch URL" mode is added,
   that would require an explicit `network` declaration listing the fetch
   host (and likely a sensitivity rethink for paywall-protected outlets).

6. Citations Required is more STRUCTURED than the prior three skills:
   four explicit fields (source_url, extracted_by_run_id, deal_description,
   announced_date), each with required source type + acceptable form. The
   appended row CARRIES its provenance as Excel cells — `source_url` +
   `extracted_by_run_id` are the operator's audit handles for any row.

7. ON-DEMAND only governance. The sector-news pipeline's Stage 3b auto-feed
   continues to call `dealtracker.workbook.append_deal` DIRECTLY (no skill
   dispatch) for M&A-looking items it identifies. This SKILL.md governs the
   human-paste path: operator hits Cmd-K with a press release, the
   `/api/workflows/deal-tracker` route fires, Anton verifies + surfaces.
   The cron-driven auto-feed has its own iron laws (sector-news') and does
   not need this skill's gate.

8. Central guard (#61): the route flows through `tool_call_hooks` so
   `enforce_skill_sensitivity` is on the path — but for an `any`-scope,
   `internal`-tier skill the guard is a structural NO-OP for the common
   case. The only firing path is the cross-skill MNPI gate: if a caller
   somehow flags `workspace_sensitivity=MNPI` on the request, the guard
   refuses with `SkillScopeRefused` (mapped to HTTP 403). Tested.

9. The route does NOT support a `text_file` argument (the CLI does, via
   `--text-file`); operators paste the article body directly into the
   request payload. The route is the operator-facing surface; the CLI
   keeps the file path because shell paste is awkward. If the dashboard
   gains a drag-and-drop article-file affordance, the route gets an
   explicit `text` body that the dashboard fills from the file — not an
   `fs_roots` declaration (the file isn't a deal artefact).
-->

# Deal Tracker

## Overview

Drives the existing `dealtracker` routine (`routines/dealtracker/`) — a single
LLM extraction call that turns one M&A press release into a structured
`DealRecord` and appends it to the firm's canonical precedent transactions
tracker at `<workspace-root>/4. Research & data/Precedent transactions
tracker/Precedent_transactions_tracker.xlsx` (sheet "Precedent transactions",
18-column lean schema — post-2026-06-01 retarget per
`COMPS-REDESIGN-2026-06-01.md`; the prior 26-col `Projects/_Trackers/M&A
Deals.xlsx` is SUPERSEDED and archived). The routine makes ONE Ollama
(`qwen3:14b`, JSON-mode) call against a strict SYSTEM_PROMPT that forbids
inference and forbids computing multiples; the extractor returns a partially
populated `DealRecord` (many fields legitimately null when the source did
not state them). The workbook write is pure openpyxl — no Excel runtime
required, no headless-context risk (distinct from LBO). The routine
deduplicates by `(announced_date, target_company)`; same key → skipped.
**Anton's job is to invoke the extraction + append, verify the Iron Law's
two clauses, and surface the row number + structured deal preview + any
warnings** — Anton does not extract values itself, does not compute any
multiple the source did not state, and never bypasses the dedupe check.

## When to Use

**Mandatory triggers** — fire the skill, do not just discuss:
- Operator types `/deal-tracker` or `/track-deal` (Cmd-K or composer)
- Operator clicks the Deal Tracker drawer tile
- Operator pastes an M&A press release + a phrase like "log this", "track
  this deal", "add this announcement", "extract this deal"

**Optional triggers** — propose firing the skill, ask first:
- Operator pastes a news article and asks "is this an M&A deal?" — propose
  running the extraction, surface the extracted target/bidder for
  confirmation BEFORE appending
- Operator references "the Apollo deal yesterday" — propose appending IF
  they supply the article body (the routine does NOT fetch URLs)

**Don't use** — refuse and explain why:
- Operator wants to batch-track multiple deals from a daily wire — the skill
  is ONE deal per fire. The sector-news pipeline's Stage 3b is the cron-
  driven auto-feed; batch-tracking is a separate routine (not yet shipped).
- Operator wants to UPDATE an existing row (fill in a missing EV after the
  filing closes) — append-only contract; updating is an operator-side Excel
  edit. Surface the prior row number from the dedupe result so they can
  open + edit directly.
- Operator wants the routine to FETCH the article from a URL — the routine
  does not have network capability declared (`network: []`). Refuse with
  "paste the article body; the routine doesn't fetch URLs (Iron Law on
  provenance — the human paste is the provenance)".
- Source text is missing or empty — refuse with 422; provenance is required.

## The Iron Law

> **NO DEAL RECORD IS APPENDED UNLESS THE TARGET COMPANY EXTRACTED IS
> NON-EMPTY, AND NO MULTIPLE IS POPULATED THAT THE SOURCE DID NOT EXPLICITLY
> STATE.**

This is non-negotiable and sits on top of the universals at
[universal-iron-laws](<vault>/CLAUDE.md#universal-iron-laws).
Two clauses, both hard:

1. **Target company must be extracted.** If `deal.target_company` is empty
   after extraction, the article likely is NOT an M&A announcement — it
   could be a corporate-action piece, a sector commentary, a fundraise, a
   results release. The routine WARNS but does not refuse; **Anton STOPS,
   surfaces "no target extracted — confirm this is an M&A announcement
   before appending", and asks the operator to re-paste a cleaner excerpt or
   confirm-anyway.** The route here is stricter than the CLI — it returns
   422 + the warning, refusing to append. Skipping this guard pollutes the
   tracker with non-deals.
2. **No computed multiples.** The `SYSTEM_PROMPT` in `extract.py` is
   explicit: "Multiples: only populate if the source explicitly states them.
   Do NOT compute them from EV ÷ Revenue." This is
   [no-llm-maths](<vault>/CLAUDE.md#no-llm-maths) applied
   to deal records. If a multiple is non-null after extraction and Anton
   cannot find an explicit quote of that multiple in the source text Anton
   can see, the extractor drifted — Anton flags, does not append.

> **Routine-reality note (2026-05-29 baseline).** The dedupe check
> (`(announced_date, target_company)` tuple match, case- and whitespace-
> insensitive on the target) is permissive — same target with a different
> announced date counts as a new deal (deliberately, to capture multi-stage
> acquisitions like the various Just Eat / Wagamama timelines). The
> `existing_row` field on a `skipped_duplicate` status tells the operator
> where the prior row is; surface that link so the operator can decide
> whether to MERGE (edit the prior row in Excel) or accept the dedupe.
> Anton does NOT auto-update the prior row — append-only is the contract.

## Core Pattern — 4 stages (extract → validate → dedupe → append)

The routine runs as a single CLI call (`deal-tracker add`) or in-process via
the bridge route (`POST /api/workflows/deal-tracker`). Anton's verification
points map to the routine's stages; there are no internal STOP gates between
stages because the routine completes or raises. Anton's job is the POST-RUN
sanity pass.

### Stage 1 — Extract

- Input: pasted article body text + source URL (URL for provenance only;
  the routine does NOT fetch).
- Truncates text to 8k chars (qwen3:14b context is generous, but extraction
  quality drops on noise).
- Calls `OllamaClient.chat(model='qwen3:14b', json_mode=True, temperature=0.1)`
  with the SYSTEM_PROMPT enforcing the two iron-law clauses.
- Parses the JSON response into a `DealRecord` Pydantic-like dataclass
  (26 fields + provenance: `source_url`, `source_excerpt`, `extracted_by_run_id`).
- **Sanity check.** The JSON must parse; an `OllamaError` is unrecoverable
  (Anton surfaces verbatim, does NOT retry with a different model).

### Stage 2 — Validate (Anton's guard)

- Confirm `deal.target_company` is non-empty (Iron Law clause 1). If empty,
  the route returns 422 + the warning chip; Anton surfaces and asks the
  operator to re-paste.
- Confirm every non-null `reported_*_multiple_y1` field is traceable to an
  explicit quote in the source text Anton can see (Iron Law clause 2). If
  Anton cannot find the quote, flag in the warnings list — the route still
  appends if target_company is present, but Anton's chat bubble surfaces
  the unverified-multiple chip prominently.

### Stage 3 — Dedupe

- `workbook.append_deal()` loads (or creates with header) the workbook,
  walks data rows, and checks `(announced_date, target_company)` (lower-
  cased, stripped) against existing rows.
- If matched, returns `{"status": "skipped_duplicate", "existing_row": "N"}`.
- If announced_date is null in the new record, dedupe is SKIPPED — every
  no-date row is permitted, since a missing announced_date is a real-world
  occurrence (rumour-stage deals) and the operator can dedupe manually.

### Stage 4 — Append

- If no dupe and Stage 2 passed, the routine writes a new row (18 columns
  via `to_row()`; provenance fields stay on the instance) and `wb.save()`s.
- Returns `{"status": "appended", "row": "N"}` where N is the 1-indexed
  row number (`ws.max_row`).
- Workbook is openpyxl-based — NO Excel COM, NO headless-context risk.

### Verification Anton applies before surfacing

1. Extraction returned a `DealRecord` (no `OllamaError` raised).
2. `target_company` is non-empty (or 422 surfaced and Anton stopped).
3. Every non-null multiple is supported by an explicit quote (or warning
   surfaced).
4. The route returned `status in {"appended", "skipped_duplicate"}` —
   anything else is a routine failure to surface verbatim.
5. Audit row exists in `runs/tool.deal-tracker.jsonl` with the run_id +
   inputs + outputs.

## Quick Reference

```
operator pastes article body + URL              (Cmd-K / Deal Tracker tile)
  ↓
route fires routines.dealtracker.extract.extract_deal via tool_call_hooks
  ↓  (enforce_skill_sensitivity present; NO-OP for any-scope + internal)
Stage 1 extract: Ollama qwen3:14b JSON-mode → DealRecord                       [Iron Law clause 2: SYSTEM_PROMPT bind]
  ↓
Stage 2 validate: target_company non-empty + no unsourced multiples            [Iron Law clause 1: route-side guard]
  ↓
Stage 3 dedupe: workbook.append_deal scans (announced_date, target_company)    [idempotency]
  ↓
Stage 4 append: openpyxl row write + wb.save                                   [append-only]
  ↓
Anton surfaces row number + deal preview + warnings                            [chat bubble]
audit row written to runs/tool.deal-tracker.jsonl                              [hook side effect]
```

## Common Rationalizations

> **Append-only** by the operator after each production run that surfaces a
> new shortcut (CLAUDE.md §14.3). The 5 rows below are the v1 seed for a
> single-shot extraction + append skill.

| Rationalization | Reality |
|---|---|
| "This is a press release from a tier-1 outlet, the extraction will be fine" | Outlet tier ≠ structured-data quality. A FT lede may state "Apollo to buy Worldpay" without ever stating the EV; the extractor returns null EV; Anton must NOT compute one. Verify each non-null field against the source text Anton can see. |
| "It's a sector-news auto-feed, so the human extract path doesn't need its own guards" | Wrong direction. The SKILL.md governs the ON-DEMAND fire (operator pastes via Cmd-K). The auto-feed runs through `dealtracker.workbook.append_deal` directly with the sector-news-extracted record — that path has sector-news' own iron laws. This skill guards the human path, which is distinct and stricter (route-side 422 on empty target). |
| "EV is missing but the bidder, target, and date are all there — append anyway" | Append, yes (target + date present). But surface "EV not stated in source" as an explicit chip in the warnings list so the operator knows the gap is REAL (provenance miss), not extractor failure. Otherwise the operator opens the workbook later and assumes Anton overlooked it. |
| "The dedupe said skipped, but the prior row's bidder is empty — I should re-append" | Skipped means dedupe by `(announced_date, target)` hit. Updating the prior row (filling the empty bidder) is a different action — surface `existing_row` + ask the operator if they want to open the workbook and edit, don't append a near-duplicate. Append-only is the contract. |
| "Source URL is a paywall, but the text is pasted, so the URL doesn't matter" | Source URL is the provenance field per §5.4 [no-invented-sources](<vault>/CLAUDE.md#no-invented-sources). Empty URL → empty provenance → an undefendable row 6 months later when someone asks "where did this come from". The route accepts empty URL but Anton must surface "no source URL captured — paste the URL for future-you" as a chip. |

## Red Flags

> Quoted internal monologue that signals a violation. If you catch yourself
> thinking any of these, stop and re-read the relevant Iron Law clause.

- *"Target came back empty but I can see it's DemoTelco in the headline —
  I'll fill it in"* — the extractor decided the article wasn't structured
  enough; substituting your own extraction defeats the routine's
  deterministic surface. Re-paste a cleaner article excerpt, or refuse the
  append. Filling fields Anton sees but the model missed = Anton extracting,
  not the routine — violates the single-source-of-extraction contract.
- *"The source mentions £5bn EV and £400m revenue, so the rev multiple is
  12.5x — I'll populate that field"* — Iron Law clause 2 breach. The
  extractor deliberately leaves the multiple null. Computing it yourself =
  LLM-maths in violation of §5.1.
- *"This is the third Apollo deal this month, I'll batch-summarise them"* —
  the skill is ONE deal per fire. Batching is a different (not-yet-shipped)
  routine. Fire the skill three times if there are three deals; surface
  three row numbers.
- *"Skipped_duplicate, fine, move on"* — surface `existing_row` to the
  operator. They may want to OPEN the prior row to merge new information
  (EV finally disclosed, completed_date now known), or they may have meant
  to append a new stage of the same deal under a new announced_date.
- *"The Ollama output looks plausible, I won't double-check the values"* —
  qwen3:14b drift on rare names (mid-cap Italian targets, Asian
  conglomerates, hyphenated entity names) is real. Spot-check
  `target_company` + `bidder_company` + `announced_date` against the source
  text Anton can see in context.

## Anti-Patterns

- Computing any multiple field from EV ÷ Revenue / EV ÷ EBITDA — Iron Law
  clause 2. The routine's SYSTEM_PROMPT explicitly forbids it; Anton's
  verification is the second pass.
- Filling missing fields by inference ("the article didn't say USD or EUR
  but Apollo is US-based so it's USD") — extractor returned blank
  deliberately; honour the blank.
- Re-appending after a `skipped_duplicate` because "the prior row was
  incomplete" — the right action is operator-side Excel edit, not a near-
  duplicate row.
- Batch-firing the skill in a loop over a daily wire — Cmd-K → ONE press
  release at a time. Sector-news auto-feed is the batch path.
- Editing the workbook's existing rows via the skill — the routine has no
  update path; the only side-effect is APPEND. Updates are operator-side.
- Treating the warnings list as cosmetic — every entry is a real-world gap
  the operator needs to know about ("EV not stated", "no source URL",
  "unverified multiple"). Surface them as visible chips, not buried text.

## Example — "log this Hilton/Bluegreen deal" (worked end-to-end)

Single realistic example. One good example beats five half-examples.

**Operator input:** "Track this — Hilton agreed to buy Bluegreen for $1.5bn
yesterday. Article body pasted below: [Hilton Worldwide Holdings has agreed
to acquire Bluegreen Vacations Holdings Corp for approximately $1.5 billion
in an all-cash transaction announced on May 28, 2026. The deal values
Bluegreen at roughly 8.5x trailing EBITDA, expanding Hilton's timeshare
exposure. The transaction is subject to regulatory approvals and is expected
to close in Q4 2026...]"

**Stage 1 — Anton invokes the route:** `POST /api/workflows/deal-tracker`
with `{url: "https://...", text: "<article body>"}`.

**Stage 2 — Anton's chat line:** "Extracted: target = Bluegreen Vacations
Holdings Corp, bidder = Hilton Worldwide Holdings, announced 2026-05-28,
EV ~$1,500m USD. **Stated multiple: EBITDA 8.5x** (article: 'roughly 8.5x
trailing EBITDA' — verified). Revenue / EBIT multiples null (not stated)."

**Stage 3 — Anton's chat line:** "Dedupe checked — no prior row for
(2026-05-28, Bluegreen Vacations Holdings Corp). Appending."

**Stage 4 — Anton's chat line:** "Appended to row 142 of
`<workspace-root>/4. Research & data/Precedent transactions tracker/
Precedent_transactions_tracker.xlsx` (sheet: Precedent transactions). Run ID:
`a3f7b9c1`."

**Final output bubble** — the row number + a 3-line preview (target /
bidder / EV+multiple) + warnings (none here — EV stated, multiple verified,
source URL present), and chips (Open workbook · View run audit · Spot
another deal).

## When Stuck

| Symptom | Diagnostic |
|---|---|
| Extraction returns empty `target_company` | Article likely isn't an M&A announcement (could be a fundraise / corporate action / sector commentary). Surface the warning + ask operator to re-paste a cleaner excerpt OR confirm-anyway (route refuses, CLI warns). Do NOT fill it yourself. |
| Non-null multiple but Anton cannot find the quote | Extractor drifted; the SYSTEM_PROMPT failed to bind on this article. Strip the multiple from Anton's surfaced preview, flag in warnings, and CONSIDER refusing the append (operator-judgement call — the row is still useful even if a multiple is dubious; surface the source excerpt prominently). |
| `OllamaError` from extraction | Ollama isn't running, or the model isn't pulled. Surface verbatim ("ollama not reachable at localhost:11434" / "model qwen3:14b not found"). Don't retry with a different model — drift between models would silently change extraction shape. |
| Workbook file is locked (operator has it open in Excel) | openpyxl raises `PermissionError`. Surface: "workbook locked — close Excel and retry". The routine does NOT auto-retry; race conditions on a shared workbook are operator-side. |
| Dedupe says `skipped_duplicate` but operator insists it's a new deal | Likely the prior row's announced_date is the same (maybe a multi-stage acquisition: initial bid, revised bid, completion). Suggest changing announced_date to the new milestone date if that's accurate, or note that the prior row should be updated in Excel directly. |
| Extracted EV is in mixed currencies (e.g., "$5bn EV with €4bn revenue") | The SYSTEM_PROMPT explicitly handles this: currency is left BLANK and the mismatch is noted in `deal_description`. Surface "currency blank — mixed reporting per source" in warnings. |

## Output Contract

One row appended to (and the workbook auto-created if missing):

```
<workspace-root>/4. Research & data/Precedent transactions tracker/
  Precedent_transactions_tracker.xlsx                 (sheet: Precedent transactions)
```

18 columns per `routines/dealtracker/schema.py::COLUMNS` (lean canonical
schema; post-2026-06-01 per `COMPS-REDESIGN-2026-06-01.md`). Created on first
append with the header row; subsequent appends are pure data rows. The live
filename is STABLE — date-stamped snapshots live under `./Archive/` and are
produced by a separate weekly snapshot job, not by every append.

**Cell-level mapping** — column order is fixed; `DealRecord.to_row()`
produces an 18-element list matching `COLUMNS` exactly. Provenance fields
(`source_url`, `source_excerpt`, `extracted_by_run_id`) are NOT written as
sheet columns — they live on the `DealRecord` instance and surface via the
route's JSON response + the audit row. The lean schema's `Source` column
carries the article URL (`source_url` mirror) so each row's primary
provenance lives in a cell.

**JSON return shape** (what the route returns):

```json
{
  "status": "appended" | "skipped_duplicate" | "error",
  "run_id": "<8-hex audit id>",
  "deal": {
    "target_company": "...",
    "bidder_company": "...",
    "seller_company": "...",
    "announced_date": "YYYY-MM-DD",
    "enterprise_value_m": 1500.0,
    "currency": "USD",
    "reported_revenue_multiple_y1": null,
    "reported_ebit_multiple_y1": null,
    "reported_ebitda_multiple_y1": 8.5,
    "target_sector": "Leisure",
    "deal_description": "...",
    "source_url": "https://..."
  },
  "workbook_path": "<workspace-root>/4. Research & data/Precedent transactions tracker/Precedent_transactions_tracker.xlsx",
  "row": 142,                       // present on "appended"
  "existing_row": 17,               // present on "skipped_duplicate"
  "warnings": ["EV not stated in source"]
}
```

**Side effects:** an audit row to `runs/tool.deal-tracker.jsonl` (written
by the `audit_tool_call` after-hook on the same `tool_call_hooks` path).
The CLI also writes `runs/dealtracker.jsonl` via `audit.write_structured`;
the route inherits the hook-stack audit instead (the central guard's
after-hook is the canonical surface).

**What the routine does NOT produce** (so the skill does not report it):
a per-row update payload, a multi-deal batch result, a fetched-article body
(the operator pastes), or any Excel formatting / formula cells. The
workbook is a flat data store.

## Citations Required

Every appended row CARRIES its provenance on the `DealRecord` instance —
`source_url` + `extracted_by_run_id` + `source_excerpt` are populated by
the extraction. Anton's narration must surface the URL + run_id in the
chat bubble so the operator can trace any row back to the source.

| Field | Required source type | Acceptable form |
|---|---|---|
| `source_url` | The press-release / article URL | `https://...` — required for full provenance; route accepts empty but Anton surfaces a "no source URL captured" warning chip |
| `extracted_by_run_id` | Routine's audit run id | 8-hex; resolves to a `runs/tool.deal-tracker.jsonl` row + the `routines/dealtracker/extract.py` SYSTEM_PROMPT version |
| `deal_description` | Source text | 3-6 sentences paraphrasing the source's strategic rationale + conditional / approval status (extractor enforces this length range via SYSTEM_PROMPT) |
| `announced_date` | Source text | ISO date stated in the article; **null if not stated** (do NOT infer from the article's publish date — the article might be reporting on a deal announced a week ago) |

## Cost Envelope

The frontmatter declares `cost_ceiling_tokens: 3000` and
`cost_ceiling_seconds: 60`. Both are read from frontmatter by
`get_active_skill_cap("deal-tracker", "tokens" | "seconds")`; the per-skill
caps are enforced by the central hook stack (#61/#67).

**Where the budget goes:**
- Ollama qwen3:14b extraction call: ~500-800 tokens for the prompt (article
  body + SYSTEM_PROMPT + SCHEMA_HINT) + ~200 tokens output (the JSON
  record). Truncation at 8k chars keeps the prompt bounded.
- Anton's narration: ~1000-2000 tokens (verify Stage 2 + surface the
  result + chips). The narration is the dominant variable cost.
- Headroom: ~500 tokens for one guardrail retry (e.g., re-prompt the
  extractor if the first JSON didn't parse — though `parse_json_response`
  already does light cleanup).

**The 3000 token / 60s ceiling vs sector-news' 6000/300:** deal-tracker
runs ONE Ollama call (vs sector-news' per-item scoring loop + synthesis
call). The wall-clock dominator is the extract: warm Ollama returns in
~5-10s, cold load is ~30s, so 60s gives 2× cold-extract headroom. The
openpyxl append is sub-second on any reasonable workbook.

> **Calibration status:** first-pass estimate. Recalibrate to `1.25 ×
> observed` after the first real narrated production runs. The dominant
> uncertainty is Anton's narration length on the warnings path (multiple
> unverified-multiple flags would inflate output tokens).

## Verification Checklist (before declaring done)

- [ ] Extraction returned a `DealRecord` (no `OllamaError` raised)
- [ ] `target_company` is non-empty (Iron Law clause 1) — route returned
      200, not 422
- [ ] Every non-null `reported_*_multiple_y1` is traceable to an explicit
      quote in the source text (Iron Law clause 2)
- [ ] Route returned `status in {"appended", "skipped_duplicate"}` —
      anything else is a routine failure
- [ ] On `skipped_duplicate`, `existing_row` was surfaced so the operator
      can open + edit the prior row directly
- [ ] On `appended`, the row number was surfaced and the workbook path
      was included in chips
- [ ] Audit row exists in `runs/tool.deal-tracker.jsonl` with the run_id +
      inputs + outputs
- [ ] No multiple was computed by Anton from EV / revenue / EBITDA
- [ ] Warnings list (EV not stated / no source URL / unverified multiple)
      was surfaced as visible chips, not buried text
- [ ] No prior row was edited as a side effect — append-only contract
      respected
