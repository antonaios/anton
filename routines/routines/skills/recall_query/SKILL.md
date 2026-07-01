---
name: recall-query
description: |
  Use when retrieving the most-relevant notes from the vault for a natural-
  language query — hybrid retrieval (BM25 + vector cosine + frontmatter
  importance triad with expires-decay) over the indexed corpus. Triggers:
  recall, "what do we know about", "have I written about X", "find notes on",
  pre-call prep retrieval, Cmd-K /recall, leaf for /pitch and /pre-call-qa
  composites. Inputs: a natural-language query, optional limit, optional
  frontmatter filter (tags, sensitivity, since/until dates). Output: a ranked
  list of NoteHit objects — vault-relative path, headline excerpt, and the
  FULL score decomposition (vector_score / fts_score / importance /
  expires_decay / final_score) so the operator can see WHY each hit ranked.
version: 0.1.0
license: proprietary
allowed_tools:
  - vault_read
capabilities:                        # #61-capabilities
  vault_read:  ["**"]                  # the index covers the whole vault — any note is a valid hit
  vault_write: []                      # pure read; no write surface
  fs_roots:    []                      # vault-only; no fs writes outside the vault
  network:     []                      # zero LLM call, zero external network — pure SQLite query against the local index
metadata:
  sensitivity: internal        # hits include note paths + excerpts; respects each hit's own frontmatter sensitivity (filter applies)
  workspace_scope: any         # workspace-independent retrieval; project-bound recall is a filter not a separate skill
  tile_label: "Recall"
  cost_ceiling_tokens: 1500    # smallest synthesis surface yet — Anton's narration just frames the hit list; zero LLM in the routine
  cost_ceiling_seconds: 30     # SQLite query against ~1k-note vault completes in <500ms; 30s is 60x headroom
  guardrails:
    - every_hit_scored          # NoteHit must carry the full score decomposition (not just final_score)
    - filter_applied            # if a filter was requested, confirm it was applied (sensitivity gate, since/until)
    - no_synthesis              # the skill returns HITS, not a narrative — synthesis is a separate skill
  guardrail_max_retries: 1
---

<!--
§14 FLEX NOTES (recall-query vs prior 5 migrated skills)

1. Iron Law is TWO-CLAUSE: every hit carries full score decomposition
   (explainability) + retrieval != narration (skill-boundary discipline).
   The FIRST Iron Law that explicitly enforces a skill BOUNDARY rather
   than a content rule. Distinct shapes:
       LBO           -> numeric gate (S&U tie)
       sector-news   -> sourcing (every claim cited)
       vault-health  -> enumeration honesty (sweep that errored is not clean)
       deal-tracker  -> extraction fidelity (target non-empty, no inferred multiples)
       bd-decay      -> taxonomy fidelity (stale != untracked)
       recall-query  -> explainability + skill boundary (every hit scored
                        AND retrieval is not narration; the moment Anton
                        paraphrases the hits, this is no longer recall-query)

2. FIRST read-only RETRIEVAL skill. Distinct from sweep (vault-health),
   extract (deal-tracker), pure-data-return (bd-decay). Returns ranked
   HITS with score components. Anton's job is to surface the hits + the
   per-component scoring rationale; NOT to summarise the content.

3. Capabilities: vault_read = ["**"] is the BROADEST READ surface yet.
   The index covers the whole vault by design (#54a/b: every note
   frontmatter triad is indexed). Narrower would be wrong: recall must
   be able to surface hits from any reachable path. Per-hit
   confidentiality enforcement is via the filter.sensitivity_max knob,
   not via the read scope.
       sector-news   -> Sectors/** + Newsletters/** (write surface)
       LBO           -> Projects/** + fs_roots <workspace-root>/**
       vault-health  -> whole vault read + Routines/vault-health/** write
       deal-tracker  -> Projects/_Trackers/** read+write
       bd-decay      -> Companies/** read only
       recall-query  -> ** read only (the WHOLE vault, hits anywhere)

4. NO WRITE SURFACE — ties bd-decay for the pure-return shape. Returns
   data; the dashboard / chat client renders. No file write, no Excel
   append, no markdown report.

5. TIES bd-decay for the LOWEST cost envelope (1500 tokens / 30s). The
   routine is ZERO LLM (pure SQLite + numpy cosine + frontmatter parse);
   the embed call is local (Ollama-resident nomic-embed-text) and
   excluded from this skill's token budget per the "routine work is not
   Anton's token spend" principle. Anton's narration is brief.

6. The NoteHit IS its own citation. First skill where the deliverable
   carries its own provenance inline (path + excerpt + scoring
   rationale). Every NoteHit returned has vault-relative path, the
   routine's deterministic excerpt, and the FULL score decomposition.

7. The ON-DEMAND skill governs the route fire only (POST
   /api/workflows/recall-query). The existing POST /api/recall stays
   live as the canonical retrieval endpoint; the new workflow route is
   the SKILL-governed surface that flows through the before_tool_call
   central guard. Downstream consumers that call recall.retrieve.query()
   directly (morning_brief, LBO /pitch composite, /pre-call-qa) are
   UNTOUCHED by this skill.

8. Central guard (#61): for an any-scope, internal-tier skill the guard
   is a structural NO-OP for the common case. The only firing path is
   the cross-skill MNPI gate: if a caller flags
   workspace_sensitivity=MNPI on the request, the guard refuses with
   SkillScopeRefused -> HTTP 403. Tested.
-->

# Recall Query

## Overview

Drives the existing `recall` routine (`routines/recall/retrieve.py`) — the
#54b hybrid retrieval pipeline (vector cosine + FTS5 BM25 + frontmatter
importance/expires triad). On-demand fires from `/recall` Cmd-K and the
dashboard surface go through this skill; downstream code-path consumers
(morning-brief, the LBO `/pitch` composite, `/pre-call-qa`) call
`recall.retrieve.query()` directly and are not governed by this skill.

The routine returns a `list[NoteHit]`; each hit carries the vault-
relative path, a deterministic excerpt, and the FULL score decomposition
(`vector_score`, `fts_score`, `importance`, `expires_decay`, `final_score`).
**Anton's job is to invoke the routine via the workflow route, verify the
scan completed cleanly, surface the hits WITH their score components, and
report index state (last reindex, FTS sidecar present) so the operator can
sanity-check freshness** — Anton does NOT paraphrase the hits into a
narrative (Iron Law, clause 2: retrieval is not narration; that's a
separate, un-migrated `recall-narrate` skill).

The routine makes ZERO LLM calls and ZERO external network requests. The
embed call is local (Ollama-resident `nomic-embed-text`) and excluded from
this skill's token budget. The cost envelope (1500 tokens / 30s) is
headroom for Anton's brief narration loop only.

## When to Use

**Mandatory triggers** — fire the skill, do not just discuss:
- Operator types `/recall <query>` (Cmd-K or composer)
- Operator clicks the Recall drawer tile / dashboard recall surface
- Operator asks "have I written about X?" / "find notes on Y" / "what do
  we know about Z?"

**Optional triggers** — propose firing the skill, ask first:
- Pre-call prep: "I'm meeting with X at 4pm, what do we have?"
- Composite-skill leaf: `/pitch <deal>` and `/pre-call-qa <name>` call
  `recall.retrieve.query()` directly as a sub-step; the SKILL.md governs
  ONLY the on-demand operator-pulled fires.

**Don't use** — refuse and explain why:
- Operator wants a NARRATED summary of the hits — that's
  `recall-narrate` (not yet migrated). recall-query returns HITS with
  scores; the operator's chat client renders them.
- Operator wants to REINDEX the vault — that's `/recall index` (a
  separate admin op via POST `/api/recall/index`). Borderline per the
  #21 audit; outside this skill's scope.
- Operator wants to SEARCH external sources — recall is vault-only.
  Use `sector-news` (Firecrawl/Tavily) for external news.
- No `query` text was supplied — the route refuses with 422
  ("query must be a non-empty string"). Don't invent a query.

## The Iron Law

> **CLAUSE 1 — EVERY HIT CARRIES ITS FULL SCORE DECOMPOSITION
> (`vector_score`, `fts_score`, `importance`, `expires_decay`,
> `final_score`) AND ITS VAULT-RELATIVE PATH. NO HIT IS SURFACED WITHOUT
> THE EXPLANATION OF WHY IT RANKED.**
>
> **CLAUSE 2 — RETRIEVAL != NARRATION. THE SKILL RETURNS HITS; IT DOES
> NOT PARAPHRASE OR SUMMARISE THEM. THE MOMENT ANTON DRAFTS A SENTENCE
> THAT RE-WORDS THE HIT CONTENT, THIS IS NO LONGER THE recall-query
> SKILL — IT IS THE (un-migrated) recall-narrate SKILL.**

This is non-negotiable and sits on top of the universals at
[universal-iron-laws](<vault>/CLAUDE.md#universal-iron-laws).
Sourcing-shape Iron Law applied to retrieval: where sector-news says
"every claim cited" and LBO says "every assumption cited", recall says
"every HIT explainable". The triad (`vector_score` + `fts_score` +
`importance`) IS the explanation; truncating any component breaks the
operator's ability to sanity-check rank order. The second clause —
RETRIEVAL != NARRATION — is the discipline boundary. Surface hits; do
not summarise them.

> **Routine-reality note (2026-05-29 baseline).** The hybrid formula is
> `0.5*vector + 0.3*fts + 0.2*(importance/5)` with a `*0.5` decay
> multiplier when `expires < today_utc`. If the `recall_fts` sidecar is
> absent (un-reindexed vault), the FTS contribution falls to 0 and the
> formula degrades gracefully to `0.5*vector + 0.2*(importance/5)`. The
> `expires_decay` field on `NoteHit` is `1.0` (current) or `0.5`
> (expired) — surface it; halved-score hits are still legitimate, just
> downweighted. Flag if you'd phrase the Iron Law differently.

## Red Flags

> Quoted internal monologue that signals a violation. If you catch
> yourself thinking any of these, stop and re-read the relevant clause.

- *"5 hits came back, I'll paraphrase the top 3 into a quick summary"* —
  Iron Law clause 2 breach. Recall-query returns HITS. If the operator
  wanted a summary, they'd ask `/recall-narrate` (not yet migrated).
  Surface paths + excerpts + scores; let the operator click through.
- *"final_score is what matters, I'll drop the per-component scores to
  keep the response clean"* — Iron Law clause 1 breach. The score
  components ARE the citation; without them, "rank 1 of 5" is
  undefendable. Always surface the triad + decay.
- *"FTS lane returned 0 for every hit because the index is stale — I'll
  re-run with vector-only and not mention"* — wrong. Surface the
  stale-index condition explicitly (`fts_score: 0.0` on every hit for a
  populated query is the operator's cue to fire `/recall index`). Do NOT
  paper over.
- *"This hit's `expires_decay` is 0.5, must be a bad note, I'll drop
  it"* — expired != wrong. The 0.5 multiplier is the routine's
  deliberate downweight; an expired note may still be the best
  available (e.g. a 6-month-old industry overview is still useful even
  after the speculation expiry). Surface, do not filter.
- *"The query returned 0 hits, I'll loosen it and re-run"* — broaden
  the query in conversation with the operator (suggest alternatives),
  do NOT silently rewrite. A zero-hit return IS a result; surface it as
  "0 hits for '<query>'; want me to try '<variant>' instead?".

## Common Rationalizations

> **Append-only** by the operator after each production run that surfaces
> a new shortcut (CLAUDE.md §14.3). The 5 rows below are the v1 seed for
> a read-only retrieval skill.

| Rationalization | Reality |
|---|---|
| "Top hit is obviously the right answer, I don't need to surface the others" | The top hit is `final_score 0.62`; the second is `final_score 0.58`. That's a TIE in operator-decision space. Surface 5+ hits whenever the top tier is close. |
| "Filter was passed but the implementation looks tricky, I'll skip it for this fire" | Iron Law breach (filter_applied guardrail). If a filter was requested and not honoured, the response is misleading. Either apply the filter or refuse with "filter not supported: <which one>". |
| "The recall index hasn't been reindexed in a week, but it'll still return useful hits" | Surface the index-age (the route exposes `index_state.last_indexed_at`). Stale-index hits are still valid but the operator should know. |
| "The user asked 'have I written about X' — I'll just say yes/no" | The skill RETURNS hits; the surface is the hits + count + scores. "Yes" is not the contract; "5 hits, here they are with scores" is. |
| "Per-hit excerpt is messy, I'll re-format it" | The excerpt is the routine's deterministic surface; re-formatting risks losing the matched-term highlight. Pass through verbatim. |

## Core Pattern — 4 stages (embed -> vector + FTS in parallel -> blend -> decay & truncate)

The pipeline is documented in `routines/recall/retrieve.py` lines 1-35
(the "Mnemosyne pattern" referenced in CLAUDE.md §3 rule 12). These are
the verification checkpoints Anton applies to the returned `list[NoteHit]`
before surfacing.

### Stage 1 — Embed

The query is embedded by the same model used at index time (locked via
`_claude/profile.md::recall_embedding_model`, default
`nomic-embed-text`). Anton verifies the model is set and the embed call
returns a non-null vector — if the query magnitude is zero, the routine
returns `[]` immediately.

### Stage 2 — Two lanes in parallel

- **Vector lane:** cosine-rank against per-chunk embeddings, best chunk
  per file wins. Falls back to whole-file note embeddings when the
  `chunks` table is empty (legacy index).
- **FTS lane:** BM25 over the `recall_fts` sidecar; top `3 * limit`
  candidates; ranks normalised to [0, 1]. Either lane can return 0; the
  blend is robust to a missing lane.

### Stage 3 — Frontmatter filter

Applied Python-side over the `notes_by_path` candidate set. Honours
`types`, `sensitivity_max`, `project`, `sectors`, `modified_after`,
`modified_before`, `path_prefix`, `exclude_path_prefix`.

### Stage 4 — Blend + expires-decay + sort + truncate

The formula: `0.5 * vector + 0.3 * fts + 0.2 * (importance / 5)`;
multiply by 0.5 if `expires < today_utc`; sort descending by
`final_score`; truncate to `limit`. **Anton's verification:** confirm
`len(hits) <= limit`, every hit's `final_score` is in [0, 1], every
component score is non-null (or explicit-zero with a documented reason
like "FTS sidecar absent").

### Verification Anton applies before surfacing

1. The route returned a list (no exception caught mid-query).
2. Every hit has `vector_score`, `fts_score`, `importance`,
   `expires_decay`, `final_score`, `path`, `excerpt` populated (Iron
   Law clause 1).
3. `len(hits) <= limit_applied`.
4. The response's `filter_applied` echoes the requested filter (if any).
5. The response's `index_state` is surfaced so the operator can sanity-
   check index freshness (last_indexed_at, notes_indexed, fts_present).
6. Anton's surface does NOT paraphrase the hit content (Iron Law clause
   2). Score components + path + raw excerpt only.

## Quick Reference

```
operator types /recall <query>      (or clicks Recall tile / asks "have I written about X?")
  |
route fires routines.recall.retrieve.query via tool_call_hooks (before_tool_call stack)
  |  (enforce_skill_sensitivity present but a NO-OP: workspace_scope=any, sensitivity=internal)
Stage 1  embed query (local Ollama nomic-embed-text)
  |       (qmag == 0 -> [] returned immediately, no further work)
Stage 2  two lanes in parallel:
           vector lane: per-chunk cosine, best chunk per file, oversample 3x
           fts lane:    BM25 over recall_fts sidecar (top 3*limit, normalised)
  |
Stage 3  apply frontmatter filter Python-side (types / sensitivity / since / until / project / path)
  |
Stage 4  blend 0.5v + 0.3f + 0.2(importance/5); expires-decay x0.5 if expired; sort; truncate to limit
  |
JSON response: {status, run_id, query, limit_applied, filter_applied, index_state, hits: [...], warnings}
  |
audit row written to runs/tool.recall-query.jsonl                          [hook side effect]
```

## Anti-Patterns

- Paraphrasing the hits into a "here's what we know about X" narrative
  — Iron Law clause 2. recall-query surfaces hits + scores; narration
  is a separate skill.
- Dropping per-component scores to "keep the response clean" — Iron Law
  clause 1. The triad IS the citation.
- Silently rewriting the query when 0 hits return — surface "0 hits"
  + suggest alternatives in conversation; do not retry with a different
  query without consent.
- Filtering expired hits (`expires_decay == 0.5`) — expired != wrong;
  the routine's decay multiplier already downweights them; surface,
  don't filter.
- Re-ranking hits by your own judgement over the routine's `final_score`
  — the hybrid formula is locked (CLAUDE.md §3 rule 12); don't override
  per-query.
- Calling the canonical `/api/recall` route from this skill — the
  workflow route exists precisely so this skill flows through the
  central guard; don't bypass it.

## Example — "have we covered Leisure consolidation?" (worked end-to-end)

Single realistic example. One good example beats five half-examples.

**Operator input:** "Have we written about leisure sector consolidation?
Show me what's in the vault."

**Stage 1 — Anton invokes the workflow route:** POST
`/api/workflows/recall-query` with `{"query": "leisure sector
consolidation", "limit": 10}`.

**Stage 2-4 — Anton's chat line (post-response):** "10 hits returned
for 'leisure sector consolidation' (limit_applied: 10). Index state:
last reindexed 2026-05-28 (3 days ago), 1247 notes indexed, FTS sidecar
present. Top 5 by `final_score`:

  * 1. `[[Sectors/Leisure/_overview.md]]` — `final_score 0.69` (vector
    0.71 / fts 0.62 / importance 4 / decay 1.0). 'Leisure sector
    consolidation accelerated in Q4...'
  * 2. `[[Companies/Bluegreen Vacations.md]]` — `final_score 0.58`
    (vector 0.61 / fts 0.55 / importance 3 / decay 1.0). 'Wyndham's
    spinoff into Travel + Leisure left a mid-cap...'
  * 3. `[[Resources/Newsletters/2026-04-01-Leisure.md]]` —
    `final_score 0.54` (vector 0.49 / fts 0.71 / importance 3 / decay
    0.5 — EXPIRED). 'M&A thread of the week: three Leisure deals in
    flight...'
  * 4. `[[Companies/Carnival.md]]` — `final_score 0.51` (vector 0.55 /
    fts 0.40 / importance 4 / decay 1.0). 'Cruise consolidation
    pressure from CCL's leverage...'
  * 5. `[[Companies/Marriott.md]]` — `final_score 0.48` (vector 0.52 /
    fts 0.38 / importance 3 / decay 1.0). 'Brand-portfolio
    consolidation playbook applied to mid-scale...'

5 more hits in the full response payload. The #3 hit (Apr newsletter)
is on the expired-decay multiplier — it's still surfaced because the
content is relevant, but flagged. Want me to narrate any of these in
detail (separate skill), or open the top hit?"

**Final output bubble** — short lead ("10 hits; the Sectors overview
is the canonical entry"), the top-5 list with score components,
chips (Open top hit · Open full response payload · Narrate these
[separate skill]).

## When Stuck

| Symptom | Diagnostic |
|---|---|
| Route returns 0 hits but the operator expected matches | Cross-check `index_state.last_indexed_at`. If stale (>1 week), suggest `/recall index`. Cross-check `fts_present`; if false, the FTS lane is degraded. |
| Every hit has `fts_score: 0.0` | The `recall_fts` sidecar is absent or empty — index needs a rebuild (`/recall index --rebuild`) or the query has no usable tokens (all punctuation). |
| Hits include notes from a sensitivity tier the operator wanted filtered out | The `filter.sensitivity_max` knob was not passed, OR the filter was applied but the gate sits between sensitivity tiers (e.g. `sensitivity_max: "internal"` excludes confidential + MNPI). Confirm in `filter_applied`. |
| `final_score` for the top hit is below 0.3 | All-low scores usually mean the query terms don't match the corpus well — suggest a query variant. The routine never refuses on low scores; the operator decides if the hits are useful. |
| Route raises 500 with "Index not found" | The vault hasn't been indexed yet (no `.recall-index/index.db`). Operator runs `/recall index` first. |
| Same path appears twice in the hit list | Should never happen — `query()` returns one hit per path (best chunk wins). If you see it, flag as a routine bug. |
| `expires_decay` is 0.5 for many hits | The vault has stale dated content past its `expires:` frontmatter. Surface alongside the list as a chip ("N hits are on expired-decay; consider refreshing or re-dating"). |

## Output Contract

The route returns JSON (mirrors existing `RecallResponse` shape with
explainability fields):

```json
{
  "status": "ok",
  "run_id": "<8-hex audit id>",
  "query": "the original query string verbatim",
  "limit_applied": 10,
  "filter_applied": {
    "types": null,
    "sensitivity_max": null,
    "project": null,
    "sectors": null,
    "modified_after": null,
    "modified_before": null,
    "path_prefix": null,
    "exclude_path_prefix": null
  },
  "index_state": {
    "last_indexed_at": "2026-05-28T11:42:00Z",
    "notes_indexed": 1247,
    "chunks_indexed": 4891,
    "fts_present": true
  },
  "hits": [
    {
      "rank": 1,
      "path": "Sectors/Leisure/_overview.md",
      "excerpt": "Leisure sector consolidation accelerated in Q4 ...",
      "vector_score": 0.71,
      "fts_score": 0.62,
      "importance": 4,
      "expires_decay": 1.0,
      "final_score": 0.69
    }
  ],
  "duration_ms": 240,
  "warnings": []
}
```

**No file write** — distinct from vault-health (writes report),
sector-news (writes newsletter), deal-tracker (appends Excel row),
LBO (populates XLSX). recall-query returns data; the dashboard / chat
client renders. Audit row to `runs/tool.recall-query.jsonl` (via central
hook stack; #60 substrate).

**What the route does NOT produce** (so the skill does not report it):
a synthesised narrative summary of the hits (that's `recall-narrate`,
separate skill), a per-hit operator action recommendation, or a
re-ranked hit order based on its own judgement. The hits are surfaced
with their routine-computed scores; the operator decides what's useful.

## Citations Required

The HIT IS the citation. Every `NoteHit` carries the vault-relative
path that is itself the source for any downstream claim built on it.
Anton's narration must surface the vault-relative path AND the score
decomposition for every spot-checked hit so the operator can:
1. Click through to the source note
2. Sanity-check why this hit ranked where it did (vector strong? FTS
   strong? both? boosted by importance?)

| Field | Required source type | Acceptable form |
|---|---|---|
| `path` | The note that ranked | Vault-relative POSIX path; never a URL or workspace-tree path |
| `excerpt` | Source-note text | The routine's deterministic 1-3 line context window (tldr / body_excerpt / best_chunk_text) — NOT a paraphrase |
| `final_score` + components | The blender output | All 4 components surfaced: `vector_score`, `fts_score`, `importance`, `expires_decay` |
| `index_state.last_indexed_at` | The index sidecar metadata | ISO-8601 UTC; surfaced in the response header so operator can sanity-check freshness |

## Cost Envelope

The frontmatter declares `cost_ceiling_tokens: 1500` and
`cost_ceiling_seconds: 30`. Both are read from frontmatter by
`get_active_skill_cap("recall-query", "tokens" | "seconds")`; the
per-skill caps are enforced by the central hook stack (#61/#67).

**Where the budget goes** (entirely Anton's narration — the routine is
zero LLM in the recall-query path):
- Routine: 0 tokens (pure Python + SQLite + numpy cosine). The embed
  model fire is local (Ollama-resident `nomic-embed-text`); excluded
  from this skill's token budget per the "routine work is not Anton's
  token spend" principle.
- Anton narration: ~800-1200 tokens (verify Iron Law components +
  surface top 5 hits with score components + propose follow-up "want
  me to narrate these?" chip).
- Headroom: ~300 tokens for one guardrail retry (e.g. re-fire with
  `limit=20` if initial 10 hits are all low-score).

**The 1500 token / 30s ceiling vs prior skills:**
       sector-news   -> 6000 / 300s  (per-item LLM loop + network)
       LBO           -> 8000 / 90s   (engine subprocess)
       vault-health  -> 2000 / 60s   (pure file-walk + write report)
       deal-tracker  -> 3000 / 60s   (single Ollama call)
       bd-decay      -> 1500 / 30s   (pure file-walk, no write, no LLM)
       recall-query  -> 1500 / 30s   (pure SQLite query, no write, no LLM)

Ties bd-decay for the smallest envelope.

> **Calibration status:** first-pass estimate. Recalibrate to
> `1.25 * observed` after first real narrated runs. Likely TIGHTER
> than 1500 in practice.

## Verification Checklist (before declaring done)

- [ ] The route returned a list (no exception caught mid-query)
- [ ] Every hit carries `vector_score`, `fts_score`, `importance`, `expires_decay`, `final_score`, `path`, `excerpt`, `rank` (Iron Law clause 1)
- [ ] `len(hits) <= limit_applied`
- [ ] The response's `filter_applied` echoes the requested filter
- [ ] The response's `index_state` surfaces `last_indexed_at`, `notes_indexed`, `fts_present`
- [ ] Anton's narration surfaces hits + score components + paths — NOT a paraphrased summary (Iron Law clause 2)
- [ ] No `summary` or `narrative` field is invented and added to the response payload
- [ ] Audit row exists in `runs/tool.recall-query.jsonl` with `status: "ok"`
- [ ] Zero-hit return is reported as a valid result ("0 hits for '<query>'"), not as an error
- [ ] Final chat bubble surfaces 3-5 spot-check hits with `[[<path>]]` wikilinks for click-through
