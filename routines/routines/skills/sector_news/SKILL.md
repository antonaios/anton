---
name: sector-news
description: |
  Use when generating a sector newsletter — scan recent news across an active
  sector, dedupe + score by relevance and materiality, synthesise a briefing,
  and write it to the vault. Triggers: sector news, newsletter, "what's
  happening in <sector>", weekly sector brief, news roundup, "catch me up on
  <sector>". Inputs: sector name, lookback days, result limit. Output: a
  markdown newsletter in Resources/Newsletters/ + optional M&A deal-tracker
  entries.
version: 0.1.0
license: proprietary
allowed_tools:
  - web_search          # Firecrawl / Tavily — FIRST skill with a network capability
  - llm_local           # Ollama (qwen3:8b score, qwen3:14b synthesis)
  - vault_read
  - vault_write
capabilities:                        # #61-capabilities — FIRST skill to declare a network host
  vault_read:  ["Sectors/**", "Resources/Newsletters/**"]   # sector defs + prior newsletters
  vault_write: ["Resources/Newsletters/**"]                 # the newsletter output (any-scope, not deal-bound)
  fs_roots:    ["<workspace-root>/Projects/_Trackers/**"]  # M&A deal-tracker auto-feed (Stage 3b)
  network:     ["api.firecrawl.dev", "api.tavily.com"]      # allowed: internal-tier skill MAY reach external hosts
metadata:
  sensitivity: internal        # public sector news — NOT confidential (cf. LBO)
  workspace_scope: any         # workspace-independent (cf. LBO's project)
  tile_label: "Sector News"
  cost_ceiling_tokens: 6000
  cost_ceiling_seconds: 300    # network + multi-article synthesis — longer than LBO's 90
  guardrails:
    - sources_cited            # every newsletter claim links to a source URL
    - dedupe_applied
  guardrail_max_retries: 1
---

<!--
§14 FLEX NOTES (sector-news vs LBO — the deliverable for the next ~12 migrations)
1. Iron Law: no hard numeric gate like LBO's S&U-tie. Instead "every newsletter
   claim links to a source URL; no synthesised fact without a citation" (§5.4
   no-invented-sources applied to news). A softer, sourcing-shaped Iron Law.
2. Core Pattern: 5 pipeline STAGES (fetch → dedupe → score → synthesise → write),
   not 8 compute/verify phases. The STOP-gates are softer — a failed fetch
   degrades gracefully (scraped sources still flow); only "no sources at all"
   is a hard stop. This is routine-backed, so Anton verifies the routine's
   PipelineResult, it does not do the work itself.
3. Output Contract: a markdown newsletter in <vault>/Resources/Newsletters/, not
   an XLSX in a project Valuation folder. No cell-map / colour conventions.
4. Cost Envelope: routine-backed skills with external calls (Firecrawl/Tavily +
   per-item Ollama) need LONGER ceilings — 300s here vs LBO's 90s.
5. Sensitivity/lane: as an `internal` skill, the registry's confidential/MNPI
   lane rules do NOT apply (registry.py only gates confidential/MNPI). So
   `web_search` is permitted and `llm_local` is not mandatory. web_search is the
   first network allowed-tool in any skill. As of #61-capabilities the
   `capabilities.network` block now DECLARES the external hosts
   (`api.firecrawl.dev` / `api.tavily.com`) and the validator EXPLICITLY allows
   them for this internal tier — the confidential/MNPI `network: []` cross-check
   passes vacuously here because the tier is `internal`. This is the systemic,
   declarative form of §5.2: a confidential skill authored with the same network
   block would hard-fail the bridge at boot.
6. References: score.py has NO source-credibility / outlet-tier model (only
   LLM-judged relevance × materiality), so a references/source-quality.md would
   be net-new fiction, not documentation of existing logic — deliberately omitted
   (cf. LBO, which is methodology-heavy). Routine-backed content-aggregation
   skills may legitimately ship with zero references/.
7. Central guard (#61): the route flows through `tool_call_hooks`
   (`before_tool_call`), so `enforce_skill_sensitivity` is on the path — but for
   an `any`-scope, non-MNPI skill the guard is a structural NO-OP (nothing to
   refuse). Wiring the route's tool_name to the skill name would only add audit
   recognition, not gating; deferred to operator (would rename the audit JSONL).
-->

# Sector News

## Overview

Drives the existing `sector-news` routine (`routines/sectornews/`) — a fetch →
dedupe → score → synthesise → write pipeline that produces a weekly sector
newsletter. The routine fetches recent items via a web-search provider
(Firecrawl, falling back to Tavily), dedupes near-duplicates, scores each item
for relevance + materiality with a local Ollama model (qwen3:8b), synthesises a
themed narrative briefing (qwen3:14b), and writes a markdown newsletter to
`<vault>/Resources/Newsletters/`. It also auto-feeds M&A-looking items into the
deal tracker. **Anton's job is to pick the sector + window from operator
context, invoke the routine, verify the returned `PipelineResult`, and surface
the newsletter with its source links intact** — Anton does not re-summarise or
re-rank the items itself (the routine owns that; Anton must not invent facts or
sources beyond what the routine cited, per
[no-invented-sources](<vault>/CLAUDE.md#no-invented-sources)).

## When to Use

**Mandatory triggers** — fire the skill, do not just discuss:
- Operator types `/sector-news` (Cmd-K or composer)
- Operator clicks the Sector News drawer tile
- The Mon–Fri 07:00 cron fires `run-all` (the scheduler owns this; the SKILL.md
  governs on-demand fires only — see Output Contract)

**Optional triggers** — propose firing the skill, ask first:
- Operator asks "what's happening in <sector> this week?"
- Operator asks "give me a news roundup on <sector> for the last fortnight"

**Don't use** — refuse and explain why:
- No sector named, and none resolvable from context — refuse with "name a
  sector; a newsletter on nothing isn't a newsletter".
- No search provider configured (neither `FIRECRAWL_API_KEY` nor
  `TAVILY_API_KEY{,S}`) — the routine exits 2 at setup; surface that, don't
  pretend to have fetched.
- The request is for a single company's confidential/MNPI deal news — that's not
  sector news; route to the relevant project workflow. Sector news is `internal`
  (public sources) and must not be used to launder MNPI into a public newsletter
  ([no-mnpi-to-cloud](<vault>/CLAUDE.md#no-mnpi-to-cloud)).

## The Iron Law

> **NO CLAIM APPEARS IN THE NEWSLETTER WITHOUT A SOURCE URL. EVERY SYNTHESISED
> FACT TRACES TO ONE OF THE FETCHED, SCORED ITEMS — NO FACT IS INVENTED, NO
> SOURCE IS FABRICATED.**

This is non-negotiable and sits on top of the universals at
[universal-iron-laws](<vault>/CLAUDE.md#universal-iron-laws),
in particular
[no-invented-sources](<vault>/CLAUDE.md#no-invented-sources).
Unlike the LBO skill, sector-news has no hard numeric gate (no S&U tie, no
validation exit code) — the load-bearing discipline is **sourcing**. If a
sentence in the briefing can't be traced to a fetched item's snippet, it does
not belong in the newsletter. The routine enforces this softly (the synthesis
prompt requires inline `[source](URL)` links and forbids inventing specifics;
the deterministic fallback emits a `[title](url)` link per item). Anton's job is
to NOT add unsourced colour on top — if the routine produced zero material items,
report that honestly; do not pad with general-knowledge commentary.

> **Routine-reality note (2026-05-29 baseline).** The Iron Law here is a sourcing
> contract, not a mechanical equality. The deterministic guarantee is that every
> item the routine emits in the fallback list and the "Other items" appendix
> carries a `[title](url)` markdown link (mechanically tested — see
> `tests/skills/sector_news/test_iron_law.py`). The LLM-synthesised narrative is
> prompt-bound to cite, not code-bound; treat an uncited claim in the narrative
> as a routine bug to surface, not to silently fix. Flag if you'd phrase the
> Iron Law differently.

## Core Pattern — 5 stages (fetch → dedupe → score → synthesise → write)

The routine runs as a single call (`run_for_sector`) or subprocess
(`python -m routines.sectornews.cli run <sector>`); it does not pause between
stages. These are the verification checkpoints Anton applies to the one returned
`PipelineResult` **before** surfacing the newsletter. STOP markers here are
softer than LBO's (most failures degrade gracefully) — but a STOP without a
written result still means: do not surface a newsletter.

### Stage 1 — Fetch
- Load the sector config (`Sectors/<sector>.md`: aliases + explicit source URLs).
- Scrape each explicit source URL, then run a provider `/search` for the sector
  query over the lookback window (`--days`). A failed individual scrape is
  logged and skipped; a failed search still returns scraped sources. **Only if
  BOTH yield nothing does the routine raise** — that is the one hard stop.

### Stage 2 — Dedupe
- Near-duplicate items are collapsed (`dedupe`, similarity threshold 0.7). The
  `dedupe_applied` guardrail records that this ran.

### Stage 3 — Score
- One Ollama call per item (qwen3:8b) returns relevance 0–10 + materiality 0–10
  + a one-line rationale. Composite = √(relevance × materiality).
- **STOP — sanity check.** Confirm `items_scored > 0`. All-zero scores usually
  mean the model or window is wrong, not that the sector is quiet; surface it.

### Stage 4 — Synthesise
- Items above the composite threshold (default 4.0, top 10) are grouped into 2–4
  themes by qwen3:14b with inline `[source](URL)` links; the rest go to an
  "Other items worth a look" appendix. If the model fails, a deterministic
  ranked list is emitted instead (still fully cited).

### Stage 5 — Write
- The full markdown (frontmatter + title + body) is written atomically to
  `<vault>/Resources/Newsletters/<YYYY-MM-DD>-<Sector>.md`.
- **STOP — verify the `sources_cited` guardrail.** Confirm
  `result.status == "ok"` and `result.output_path` is set (or, on `--dry-run`,
  that the would-write path was logged). Surface the newsletter WITH its links;
  never strip the citations to "tidy" it.

## Quick Reference

```
operator types /sector-news <sector>     (or clicks the Sector News tile / 07:00 cron run-all)
  ↓
route fires routines.sectornews.cli via tool_call_hooks (before_tool_call stack)
  ↓  (enforce_skill_sensitivity present but a NO-OP: workspace_scope=any, sensitivity≠MNPI)
Stage 1  fetch: scrape explicit sources + provider /search over --days window
  ↓        (both empty → routine raises → NO newsletter; surface verbatim)
Stage 2  dedupe (threshold 0.7)                                   [dedupe_applied]
  ↓
Stage 3  score each item (qwen3:8b): relevance×materiality        [STOP: items_scored>0]
  ↓
Stage 3b auto-feed M&A candidates → Projects/_Trackers/M&A Deals.xlsx  (side effect)
  ↓
Stage 4  synthesise themed briefing (qwen3:14b) + "Other items" appendix, all cited
  ↓
Stage 5  atomic_write markdown                                    [STOP: sources_cited]
  ↓
newsletter lands at
  <vault>/Resources/Newsletters/<YYYY-MM-DD>-<Sector>.md          [side effect]
audit row written to runs/sectornews.jsonl (+ tool.<name>.jsonl)  [hook side effect]
```

## Common Rationalizations

> **Append-only** by the operator after each production run that surfaces a new
> shortcut (CLAUDE.md §14.3). The rows below are the v1 seed for a routine-backed
> content skill.

| Rationalization | Reality |
|---|---|
| "The sector's quiet this week, I'll add some general context to fill it out" | Padding with unsourced general knowledge breaks the Iron Law. If the routine returned no material items, report "no material items this period" — that IS the answer. |
| "The narrative reads well, I don't need to check the links" | A well-written uncited claim is the dangerous failure mode. Every claim must trace to a fetched item; an uncited line is a routine bug to flag, not a feature. |
| "Firecrawl failed, I'll just summarise what I already know about the sector" | The routine falls back to Tavily, then to scraped sources. If everything fails it raises — surface that. Do not substitute your own recall for fetched sources. |
| "Two items look similar, I'll merge them in the summary myself" | Dedupe is the routine's job (threshold 0.7). Hand-merging risks dropping a distinct story; trust the pipeline or fix `dedupe`, don't paper over it. |
| "This is the weekly run, the defaults are fine, skip the sanity check" | All-zero scores or an empty fetch look like "a quiet week" but usually mean a broken provider key or wrong window. The Stage 3 sanity check is cheap; a silently empty newsletter is not. |
| "It's public sector news, citations are optional" | Citations are MORE central here than in LBO, not less — the whole artefact is a claim-per-source briefing. |

## Red Flags

> Quoted internal monologue that signals a violation. If you catch yourself
> thinking any of these, stop and re-read the relevant stage.

- *"I'll round out the briefing with what I know about the sector"* — Iron Law breach; only fetched, scored items belong in the newsletter.
- *"The links are ugly, I'll drop them and keep the prose"* — the citations ARE the deliverable; never strip them.
- *"Search returned nothing, I'll write a plausible roundup anyway"* — a fabricated newsletter is worse than an honest "no material items".
- *"items_scored is 0 but the prose came back, so it's fine"* — zero scored items means there's nothing to cite; investigate the provider/window, don't ship empty-but-pretty.
- *"This headline implies a deal; I'll state the deal value"* — only state specifics present in the snippet; do not infer numbers the source didn't give.

## Anti-Patterns

- Writing a newsletter when the routine returned `status="error"` or zero items — surface the error/empty result, don't synthesise around it.
- Stripping or rewriting the `[source](URL)` links for readability.
- Adding general-knowledge background paragraphs not tied to a fetched item.
- Inventing a credibility/tier ranking of outlets — the routine scores
  relevance × materiality only; there is no source-tier model to cite.
- Running sector-news to produce news about a single confidential/MNPI target —
  that's a project workflow, not a public sector newsletter.
- Re-ranking the items by your own judgement over the routine's composite score.

## Example — "what's happening in Travel this week?" (worked end-to-end)

Single realistic example. One good example beats five half-examples.

**Operator input:** "What's happening in Travel this week? Give me the
newsletter."

**Stage 1–2 — Anton invokes the routine:** `sector-news run Travel --days 7
--limit 15` (or `run_for_sector("Travel", days=7, fetch_limit=15, ...)`). The
routine scrapes the 3 explicit sources in `Sectors/Travel.md` + a provider
search; dedupe collapses 18 → 14 items.

**Stage 3 — Anton's chat line:** "Scored 14 items (9 non-zero); top composite
6.5. items_scored > 0 ✓."

**Stage 4–5 — Anton's chat line:** "Synthesised 8 items into 3 themes (M&A,
Capacity, Regulatory) + a 6-item appendix; status=ok; wrote
`Resources/Newsletters/2026-05-29-Travel.md` (sources-count 14). Every item
carries a `[source]` link ✓."

**Final output bubble** — a short lead ("3 themes this week in Travel; the
M&A thread is the live one"), the themed briefing with inline `[source](URL)`
links exactly as the routine produced them, the "Other items worth a look"
appendix, and chips (Open newsletter [the .md] · 14 sources · 2 M&A items
auto-fed to the deal tracker).

## When Stuck

| Symptom | Diagnostic |
|---|---|
| Routine exits 2 at startup: "Search provider setup failed" | Neither `FIRECRAWL_API_KEY` nor `TAVILY_API_KEY{,S}` is set. Run `sector-news health` to confirm; set a key via Windows User-scope `setx` (HANDOFF.md §3). No fetch happened — do not synthesise. |
| `status="error"`, both fetch paths failed | Provider outage or all source URLs dead. Surface the error; retry later or widen `--days`. |
| `items_scored == 0` but items were fetched | Usually Ollama not reachable (qwen3:8b) or the window caught only off-topic items. Run `sector-news health` for Ollama; check `Sectors/<sector>.md` aliases. |
| Newsletter says "no material items this period" | All items scored below the composite threshold (4.0). Widen `--days`, add explicit sources to `Sectors/<sector>.md`, or accept that it was a quiet week. |
| Narrative has a claim with no `[source]` link | Synthesis-model drift. Surface as a routine bug; do not hand-add a source you didn't fetch. The deterministic fallback always cites — a rerun may land on it. |
| Duplicate stories still appear | The 0.7 dedupe threshold missed a near-duplicate. Note it; tightening the threshold is a routine change, not a SKILL.md fix. |

## Output Contract

The newsletter lands at:

```
<vault>/Resources/Newsletters/<YYYY-MM-DD>-<Sector>.md
```

Filename: `<date>-<sector>.md`, where spaces in the sector name become hyphens
(`cfg.name.replace(' ', '-')`) and `<date>` is `date.today().isoformat()`. The
file is written via `atomic_write` (temp + rename); a same-day rerun overwrites
in place (no `00. OLD/` archive — unlike LBO; newsletters are regenerable).

**Markdown frontmatter the routine stamps** (`build_full_newsletter_md`):

| Key | Value |
|---|---|
| `type` | `newsletter` |
| `sector` | the sector name |
| `date` | ISO date |
| `sensitivity` | `internal` (matches this skill's tier) |
| `run-id` | the routine's audit run id |
| `sources-count` | number of source URLs used |
| `items-scored-mean-composite` | mean composite across scored items |
| `tags` | `["newsletter", "sector-news", <sector>.lower()]` |
| `tldr` | one-line auto-summary |

**`PipelineResult` return shape** (what `run_for_sector` returns; the CLI prints
a summary, the route returns a `JobStarted` PID for the long-running subprocess):

```python
PipelineResult(
    status="ok",            # "ok" | "skipped" | "error"
    sector="Travel",
    output_path=Path(".../Resources/Newsletters/2026-05-29-Travel.md"),  # None on dry-run/error
    items_fetched=18,
    items_deduped=14,
    items_scored=14,
    deals_appended=2,       # rows added to the M&A deal tracker
    deals_skipped=1,        # dedupe hits (already tracked)
    deals_filtered=3,       # items that matched the M&A pre-filter
    duration_ms=42100,
    error=None,             # str on status="error"
    fed_urls=[...],         # URLs auto-fed to the deal tracker
)
```

**Side effects:** an audit row to `runs/sectornews.jsonl` (+ the hook stack's
`tool.<name>.jsonl`), and — when `feed_deals=True` (default) and not a dry-run —
appended rows in `Projects/_Trackers/M&A Deals.xlsx` for M&A-looking items.

**What the routine does NOT produce** (so the skill does not report it): a
source-credibility / outlet-tier ranking, a sentiment score, or a per-claim
citation index. Scoring is relevance × materiality only. Reporting a tier rank
would mean inventing one — forbidden by
[no-invented-sources](<vault>/CLAUDE.md#no-invented-sources).

## Citations Required

Sourcing is the Iron Law here, so the citation contract IS the skill. Every
claim in the briefing maps to a fetched item's URL.

| Field | Required source type | Acceptable form |
|---|---|---|
| Every narrative claim | A fetched, scored item | Inline `[source](URL)` markdown link |
| Each "Other items" entry | A fetched item | `[title](URL)` markdown link |
| `sources-count` frontmatter | Count of distinct source URLs used | Integer ≥ items cited |
| M&A deal-tracker row | The item the deal was extracted from | `source_url` on the appended row |
| Sector definition (aliases, seed URLs) | `Sectors/<sector>.md` | Vault note path |

Sources are EXTERNAL news URLs, so citations are markdown `[source](URL)` links,
NOT vault wikilinks (the routine's synthesis prompt enforces this distinction).

## Cost Envelope

The frontmatter declares `cost_ceiling_tokens: 6000` and
`cost_ceiling_seconds: 300`. Both are read from frontmatter by
`get_active_skill_cap("sector-news", "tokens" | "seconds")`; the per-skill caps
are enforced by the central hook stack (#61/#67).

**Where the budget goes** (the routine, not Anton's narration):
- Network: provider `/search` + per-source scrape — the dominant wall-clock,
  variable with `--limit` and source count.
- Ollama scoring: one qwen3:8b call per item (~5s warm × N items).
- Ollama synthesis: one qwen3:14b call (max_tokens 2000).
- Anton's narration: surfacing + verifying the `PipelineResult` (~1–2k tokens).

**The 300s ceiling vs LBO's 90s:** routine-backed skills that make external
network calls AND per-item LLM calls need a longer wall-clock than a single
engine subprocess. 300s covers ~15 items (scrape + score + synthesise) with
headroom; if a provider hangs, the subprocess/timeout fires and the run is
marked `error`.

> **Calibration status:** 6000 tokens / 300s are first-pass estimates for the
> on-demand narration loop. The cron `run-all` runs the routine directly (no
> Anton narration), so it produces no token data to calibrate against.
> Recalibrate to `1.25 × observed` after the first real narrated runs.

## Verification Checklist (before declaring done)

- [ ] Routine returned `status == "ok"` (not `error` / `skipped`)
- [ ] `items_scored > 0` (Stage 3 sanity check)
- [ ] `output_path` is set (or the dry-run would-write path was logged)
- [ ] Every narrative claim carries a `[source](URL)` link (Iron Law)
- [ ] The "Other items" appendix entries are all linked
- [ ] No unsourced general-knowledge padding was added on top of the routine
- [ ] Newsletter exists at `<vault>/Resources/Newsletters/<date>-<sector>.md`
- [ ] Audit row exists in `runs/sectornews.jsonl` with `status: "ok"`
- [ ] No confidential/MNPI single-target news laundered into the public newsletter
- [ ] Final chat bubble surfaces the briefing WITH citations + chips, not a re-summary
