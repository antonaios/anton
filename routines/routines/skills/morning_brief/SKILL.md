---
name: morning-brief
description: |
  Use when generating or regenerating today's morning brief — walk
  Projects/ meeting notes + decision logs + Registers/Actions.md for
  open actions (last N days), gather sector newsletters from the
  active_sectors in profile.md, run two local Ollama calls (qwen3:14b)
  to (1) classify actions into ovd/due/open buckets and (2) compose the
  reflective "Anton suggests" paragraph, write the structured brief to
  Routines/morning-briefs/<date>.md with the full payload mirrored into
  the frontmatter as JSON. Triggers: morning brief, regenerate today's
  brief, "what's on my plate today", refresh the brief after I closed
  X, /morning-brief Cmd-K. Inputs: optional vault override, optional
  date override (default: today UTC), optional days_lookback (default
  7), optional Ollama model (default qwen3:14b), optional dry_run flag.
  Output: the MorningBrief JSON payload (date + source + needsYou +
  sectorThisWeek + antonSuggests) AND the written note path (absent if
  dry_run=True) AND the ollama_state (ok | unreachable | fallback).
version: 0.1.0
license: proprietary
allowed_tools:
  - vault_read
  - vault_write
  - llm_local           # local Ollama qwen3:14b — first skill with llm_local AND zero external network
capabilities:                        # #61-capabilities
  vault_read:  ["Projects/**", "Registers/Actions.md", "Resources/Newsletters/**", "_claude/profile.md"]
  vault_write: ["Routines/morning-briefs/**"]   # bounded to the brief's own folder
  fs_roots:    []                               # vault-only
  network:     []                               # ZERO external network — Ollama is local, listen-only on localhost:11434
# Per-skill default system prompt for the gated llm() helper (#llm-skill-system-prompt).
# The skill-level BASE persona for morning-brief's local synthesis. When the two
# pipeline calls migrate from the bespoke OllamaClient path to the governed llm()
# helper (a #21 follow-up), each passes its own per-call `system=` override
# (JSON-mode classify vs reflective-prose suggest) ON TOP of this base. Inline
# form; a sibling-file form (llm_system_prompt_file:) is also supported.
llm_system_prompt: |
  You are Anton composing the operator's morning brief. Principal-side, en-GB,
  investor-grade, hedge-light. Surface only what is in the gathered vault context —
  never invent actions, deals, or sector news. If an input set is empty, say so
  plainly rather than fabricating filler. Keep the operator's day in view: what
  needs them, what moved in their active sectors, what to do first.
metadata:
  sensitivity: internal        # the brief surfaces operator-context actions + public sector news; not MNPI per-row; mirrors sector-news + vault-health tier
  workspace_scope: any         # firm-wide daily artefact; workspace-independent
  tile_label: "Morning Brief"
  cost_ceiling_tokens: 4000    # two LLM calls (classify_actions + anton_suggests) + Anton's surfacing narration; smaller than sector-news (6000) because the synthesis surface is smaller
  cost_ceiling_seconds: 180    # gather is sub-second; two qwen3:14b calls are ~30-60s each on consumer GPU; 180s = ~2x warm-cache headroom, ~1x cold-cache
  guardrails:
    - context_gathered_before_synthesis    # the synthesis ONLY runs after gather_context completes successfully — no LLM call on empty/failed context
    - ollama_state_surfaced                # the response surfaces whether Ollama was reachable; if not, the fallback path was used and the operator must know
    - data_frontmatter_complete            # the written file's frontmatter `data:` key contains the FULL brief.model_dump() payload (the dashboard read-path depends on this)
    - no_synthesis_without_inputs          # if BOTH needs_you AND sector_news are empty, the routine writes an empty-brief artefact with explicit "nothing to surface today" content — does NOT fabricate filler
  guardrail_max_retries: 1
---

<!--
§14 FLEX NOTES (morning-brief vs prior 9 migrated skills)

1. FIRST skill combining `llm_local` AND `network: []`. Distinct shape:
       LBO              -> numeric gate + XLSX deliverable (engine subprocess, no LLM in routine)
       sector-news      -> LLM + external network (web_search: Firecrawl/Tavily) + llm_local
       vault-health     -> deterministic sweep + report write, no LLM
       deal-tracker     -> single-shot LLM extract + Excel row append (llm_local + fs_roots)
       bd-decay         -> pure-return data sweep, no LLM, no network
       recall-query     -> deterministic hybrid retrieval, no LLM in routine
       equity-research  -> deterministic provider-data assembly + Companies/<X>.md write, no LLM in routine
       actions-decay    -> pure-return cross-project + multi-root sweep, no LLM
       lessons-suggest  -> deterministic frontmatter-driven leaf-suggest, no LLM
       morning-brief    -> LOCAL OLLAMA SYNTHESIS (two qwen3:14b calls) over VAULT-ONLY
                            inputs + STRUCTURED DAILY-ARTEFACT WRITE with JSON-in-
                            frontmatter for verbatim round-trip deserialisation
   Validates that the §14 capability manifest treats local-LLM and external-
   network as ORTHOGONAL surfaces — the LLM is the synthesis engine but the
   external network surface is empty (Ollama is on localhost:11434, not an
   external host).

2. FIRST skill with a JSON-in-frontmatter `data:` block for verbatim
   round-trip deserialisation. The dashboard READ endpoint
   (`/api/morning-brief/today`) deserialises the frontmatter `data:` key
   directly into the same MorningBrief pydantic instance — no body re-
   parsing, no field drift. The Iron Law clause `data_frontmatter_complete`
   guards this byte-faithfulness. Compare:
       LBO              -> XLSX deliverable, no frontmatter contract
       sector-news      -> markdown body with [source](URL) links, no JSON round-trip
       deal-tracker     -> Excel row append, no frontmatter contract
       equity-research  -> Companies/<X>.md note with append-only "Valuation history" section
       morning-brief    -> markdown body + JSON-in-frontmatter for verbatim deserialisation

3. FIRST skill with an explicit DETERMINISTIC-FALLBACK PATH for an
   unreachable LLM. `synthesise._fallback_classify` produces a
   deterministic categorisation when Ollama is down; `_fallback_suggest`
   produces a deterministic suggestion paragraph; the brief is still
   written but with `ollama_state: "fallback"` surfaced so the operator
   knows the artefact is the fallback, not the LLM synthesis. Distinct
   from sector-news (which has a softer "PipelineResult.error" surface
   when the provider chain fails but no parallel deterministic fallback
   for the synthesis layer).

4. Iron Law's three clauses are about ARTEFACT FAITHFULNESS
   (data_frontmatter_complete), LANE TRANSPARENCY (ollama_state surfaced),
   and SYNTHESIS DISCIPLINE (no fabrication when inputs empty). FIRST Iron
   Law that explicitly governs ROUND-TRIP SERIALISATION as a discipline
   clause.

5. Deliverable is the daily artefact at
   `Routines/morning-briefs/<date>.md`. Same-date refire is DELIBERATELY
   OVERWRITE (the brief IS today's snapshot; regenerating gives a new
   snapshot — the cron produces 06:30's snapshot, an on-demand fire
   produces the operator's mid-day snapshot after closing a deal or adding
   actions). Distinct from LBO (which archives to `00. OLD/` on
   regenerate) and sector-news (which overwrites same-day too, mirrors
   the operator's "regenerable" rationale).

6. The cron stays canonical. The 06:30 daily cron fires the routine
   directly via the CLI (no SKILL.md governance, no operator narration);
   the SKILL governs ONLY the on-demand `/api/workflows/morning-brief`
   regeneration fire from the dashboard or Cmd-K. Same separation as
   bd-decay + actions-decay + lessons-suggest: CLI / cron stays direct;
   SKILL governs the operator-pulled path.

7. `workspace_scope: any` — the brief is a firm-wide daily artefact (it
   aggregates open actions across EVERY project + sector news across all
   active_sectors in profile.md). Not project-bound, not BD-bound. The
   central guard fires on every call; for `any`-scope + `internal`
   sensitivity the only non-NOOP path is `workspace_sensitivity: MNPI`
   (§5.2 cross-skill gate -> SkillScopeRefused -> 403). Tested.

8. NO `captures_to_vault:` block — the SKILL itself writes the daily
   artefact (Routines/morning-briefs/<date>.md). There is no SECONDARY
   semantic-fact capture to a Companies/ note (cf. LBO captures returns
   to the deal's Companies note; equity-research captures the consensus
   snapshot). The brief IS the captured artefact.

9. FIRST skill to declare a per-skill `llm_system_prompt:` (#llm-skill-
   system-prompt) — the base persona the gated `llm()` helper applies in
   place of the generic chat persona (`_ANTON_SYSTEM_PROMPT`). This is
   FORWARD-LOOKING: morning-brief's two synthesis calls
   (`synthesise.classify_actions` / `synthesise.anton_suggests`) still run
   on the bespoke `OllamaClient` pipeline with their own `_CLASSIFY_SYSTEM`
   / `_SUGGEST_SYSTEM` prompts (UNCHANGED this session — see the route
   docstring's "NOT moved to the L3 llm() helper"). The declaration is the
   ready persona for when those calls migrate to `llm()` (a #21 follow-up);
   at that point each call passes its own per-call `system=` override (the
   JSON-mode classify system vs the reflective-prose suggest system) layered
   on top of this skill-level base. Declaring it now (a) proves the
   declaration -> registry -> wrapper -> gateway -> dispatch plumbing end-to-
   end and (b) gives the migration a single source of truth for the skill's
   voice. No behaviour change today: nothing in the body calls `llm()`, so
   the key is inert until the synthesis migrates.
-->

# Morning Brief

## Overview

Drives the existing `morning_brief` routine
(`routines/morning_brief/cli.py::generate_cmd` and the underlying
`pull.gather_context` -> `synthesise.classify_actions` ->
`synthesise.anton_suggests` -> `writer.write_brief` pipeline) — a
local-Ollama synthesis over vault-only inputs that produces today's
structured morning brief. The routine reads Projects/ meeting notes +
decision logs + Registers/Actions.md for open actions (last N days),
gathers sector newsletters from `Resources/Newsletters/` filtered by
`profile.active_sectors`, runs two LOCAL Ollama calls (qwen3:14b
default — classify_actions, then anton_suggests), and writes the
brief to `Routines/morning-briefs/<date>.md` with the full payload
mirrored into the frontmatter as JSON so the bridge endpoint can
serve it back verbatim to the dashboard.

**Anton's job is to pick the inputs (default: vault default + today
UTC + 7-day lookback), invoke the routine via the workflow route,
verify the returned `MorningBrief` payload + the written note path +
the `ollama_state`, and surface the brief honestly — if the LLM was
unreachable and the fallback path was used, Anton must say so.** The
on-demand SKILL governs the operator-pulled regeneration path; the
06:30 cron stays canonical and fires the routine directly.

The routine makes ZERO external network requests (Ollama is on
localhost:11434; the `capabilities.network: []` declaration is the
systemic form of "this skill has no external network surface"). The
LLM is the synthesis engine but the network surface is empty.

## When to Use

**Mandatory triggers** — fire the skill, do not just discuss:
- Operator types `/morning-brief` (Cmd-K or composer, once wired)
- Operator clicks the Morning Brief drawer tile to REGENERATE
  mid-day (the canonical READ path stays on the existing
  `/api/morning-brief/today` endpoint)
- Operator says "regenerate today's brief", "refresh the brief
  after I closed X", "rebuild today's brief"

**Optional triggers** — propose firing the skill, ask first:
- Operator asks "what's on my plate today?" — if a brief already
  exists for today, surface it via the read path first; offer
  regeneration ONLY if the operator says "no, generate a fresh one"
- Operator asks "did I get a brief this morning?" — surface the
  existing brief (read path); do NOT auto-fire regeneration

**Don't use** — refuse and explain why:
- Operator wants a brief for a SPECIFIC PROJECT (one deal's
  actions only) — that's not morning-brief territory; route to a
  project workspace query. The morning brief is firm-wide.
- Operator wants to RETRIEVE yesterday's brief — that's the read
  path (`GET /api/morning-brief/today?date=YYYY-MM-DD`), not a
  regeneration. Don't re-fire the routine for a past date unless
  the operator EXPLICITLY asks to regenerate it.
- Operator wants the brief to PERSIST EDITS made to the markdown
  body — wrong tool. The brief is regenerable; manual edits to the
  body are lost on next fire. If the operator wants persistent
  notes, route to the relevant project's decision log.
- `workspace_sensitivity: MNPI` is passed — the central guard
  refuses (§5.2 cross-skill gate -> SkillScopeRefused -> 403).
  Surface the refusal verbatim; the brief is `internal`-tier and
  cannot launder MNPI through the LLM call.

## The Iron Law

> **THE WRITTEN FILE'S FRONTMATTER `data:` KEY MUST CONTAIN THE FULL
> `brief.model_dump()` PAYLOAD (THE DASHBOARD READ-PATH DEPENDS ON
> THIS FOR VERBATIM DESERIALISATION). THE RESPONSE MUST SURFACE
> `ollama_state` HONESTLY — IF OLLAMA WAS UNREACHABLE AND THE
> FALLBACK PATH WAS USED, THE OPERATOR MUST KNOW THE BRIEF IS THE
> DETERMINISTIC FALLBACK, NOT THE LLM SYNTHESIS. IF BOTH `needs_you`
> AND `sector_news` ARE EMPTY, THE BRIEF IS WRITTEN WITH EXPLICIT
> "NOTHING TO SURFACE TODAY" CONTENT — NO LLM FABRICATION OF
> FILLER.**

Three clauses on top of the universals at
[universal-iron-laws](<vault>/CLAUDE.md#universal-iron-laws):

1. **`data:` frontmatter complete** — the writer's frontmatter
   `data:` key carries the FULL `brief.model_dump()` (see
   `_render_markdown` at line 43 of writer.py:
   `payload_json = json.dumps(brief.model_dump(), indent=2)` then
   injected as a `data: |` YAML block scalar). The dashboard READ
   endpoint (`reader.load_for_date` at routines/morning_brief/
   reader.py) deserialises this verbatim into a `MorningBrief`
   pydantic instance; truncating ANY field (`needsYou`,
   `sectorThisWeek`, `antonSuggests`, `date`, `source`) breaks the
   read path. The SKILL contract is byte-faithful round-trip: what
   was written must deserialise back into the same in-memory brief.
2. **Ollama state surfaced** — `synthesise.classify_actions` falls
   back to `_fallback_classify` on `OllamaError`;
   `synthesise.anton_suggests` falls back to `_fallback_suggest` on
   `OllamaError`. The fallback path is DETERMINISTIC categorisation
   + a deterministic suggestion paragraph (no LLM); the brief still
   gets written but the operator must know it's the fallback.
   Surface `ollama_state: "ok" | "unreachable" | "fallback"` on the
   response. The fallback brief is a legitimate artefact (the
   operator's machine GPU may be off, or the qwen3:14b model may
   not be pulled); silent fallback is operator-deceptive.
3. **No synthesis without inputs** — if `gather_context` returns
   `needs_you: []` AND `sector_news: []`, the writer writes a brief
   whose `antonSuggests` is the deterministic "nothing material on
   the desk" fallback string (see `_fallback_suggest` at line 199 of
   synthesise.py). The LLM is NOT called for `anton_suggests` when
   both inputs are empty (the deterministic fallback short-circuits
   the call) — the discipline is "no fact in the output that isn't
   in the input". Cost_ceiling stays generous (4000 tokens) so the
   fallback narration has room.

> **Routine-reality note (2026-05-31 baseline).** The `_render_markdown`
> at writer.py lines 36-85 ALWAYS emits the `data:` frontmatter key
> with `json.dumps(brief.model_dump(), indent=2)` indented under a
> `data: |` YAML block scalar. The dashboard endpoint reads this back
> via `reader.load_for_date` (verified — see line 40 of reader.py:
> `raw_data = post.metadata.get("data")`). The `_fallback_classify`
> path is at synthesise.py lines 121-139; `_fallback_suggest` at lines
> 189-199. Flag if you'd phrase the Iron Law differently after reading
> writer.py + reader.py + synthesise.py in full.

## Core Pattern — 4 stages (gather context -> classify actions (LLM #1) -> compose suggestion (LLM #2) -> write artefact)

The pipeline is implemented in `routines/morning_brief/cli.py::generate_cmd`
(lines 50-150); the SKILL route reuses these helpers directly
(`gather_context`, `classify_actions`, `anton_suggests`, `write_brief`
— the route orchestrates without re-implementing).

### Stage 1 — Gather context

`gather_context(vault_root, today, days_lookback, active_sectors,
newsletter_lookback_days)` (line 75 of pull.py) reads vault-only
inputs — Projects/ meeting notes (multiple naming conventions:
`02 Meeting Notes/`, `Meeting Notes/`, `meeting-notes/`), decision
logs (`09 Decision Log.md`, `Decision Log.md`, `decisions.md`),
`Registers/Actions.md` cross-project register, and
`Resources/Newsletters/<YYYY-MM-DD>-<Sector>.md` files filtered by
`profile.active_sectors`. Returns a `ContextBundle` with
`needs_you: list[ActionItem]` (capped at 30 for LLM context size) +
`sector_news: list[BriefRow]` (capped at 8). NO LLM, NO external
network at this stage.

Anton verifies: `gather_context` ran without exception; counts are
surfaced (`{"actions_gathered": N, "sector_news": M}`).

### Stage 2 — Classify actions (LLM #1)

`classify_actions(ctx.needs_you, today, client, model)` (line 67 of
synthesise.py) — local Ollama call (qwen3:14b default), JSON-mode,
per the `_CLASSIFY_SYSTEM` prompt at lines 36-64 of synthesise.py.
Categorises raw action candidates into `ovd` / `due` / `open` buckets
with cleaned text + sub; drops noise (meeting-prep boilerplate,
agenda items, duplicates). Aims for 4-6 brief rows total.

On `OllamaError` (transport / parse failure) OR JSON parse failure,
falls back to `_fallback_classify` (line 121): deterministic
categorisation of the top 6 actions, no cleanup, no noise-drop.

Anton verifies: LLM call succeeded OR fallback ran; `ollama_state`
is set accordingly (`"ok"` OR `"fallback"`). If the routine raised
`OllamaError` at `client.health()` (the route should health-check
BEFORE firing the pipeline), `ollama_state: "unreachable"` and the
route routes both stages through the deterministic fallback path.

### Stage 3 — Compose suggestion (LLM #2)

`anton_suggests(needs_you, ctx.sector_news, profile_context, client,
model)` (line 163 of synthesise.py) — local Ollama call, plain-text
mode, per the `_SUGGEST_SYSTEM` prompt at lines 144-160. Produces
the reflective paragraph (2-4 sentences, 50-90 words, en-GB voice,
hedge-light, principal-side) that opens the brief.

On `OllamaError` OR empty response, falls back to `_fallback_suggest`
(line 189): deterministic prose ("N overdue items — clear the oldest..."
/ "N items due today..." / "Nothing material on the desk..." per
input shape).

Anton verifies: text non-empty; for the empty-inputs case (both
`needs_you` AND `sector_news` empty), confirm the fallback returned
"Nothing material on the desk..." (the SKILL's no-synthesis-without-
inputs guarantee — the LLM was NOT called for fabricated filler).

### Stage 4 — Write artefact

`write_brief(vault_root, brief, the_date)` (line 27 of writer.py)
constructs the `MorningBrief` pydantic instance, atomic-writes
`Routines/morning-briefs/<date>.md` with the human-readable body
PLUS the YAML frontmatter carrying:
  - `type: morning-brief`
  - `sensitivity: internal`
  - `date: <ISO>`
  - `generated: <source string>`
  - `tags: [morning-brief, routines, auto-generated]`
  - `data: |` (the FULL `brief.model_dump()` JSON, indented under
    a YAML block scalar — the dashboard read-path's source of
    truth)

Anton verifies: file exists at the expected path; frontmatter `data:`
key contains the FULL `brief.model_dump()` payload (verify by
parsing the written file with `frontmatter.load` and deserialising
the `data:` key with `MorningBrief.model_validate_json` — the result
must equal the in-memory brief byte-faithfully).

### Verification Anton applies before surfacing

1. `gather_context` ran without exception; counts surfaced.
2. `ollama_state` honestly reflects the LLM lane state.
3. If `dry_run=False`: the file exists at the expected path;
   `frontmatter_data_complete: true` (the byte-faithful round-trip
   check).
4. If `dry_run=True`: no file written; `note_path: null`;
   `frontmatter_data_complete: null`.
5. `brief.needsYou` rows ALL have `marker in {ovd, due, open}`
   AND non-empty `text`. `brief.sectorThisWeek` rows ALL have
   `marker == "news"` AND non-empty `text`.
6. If `gather_context` returned empty `needs_you` AND empty
   `sector_news`: `brief.antonSuggests` is the deterministic
   fallback string (NOT an LLM-fabricated paragraph).

## Quick Reference

```
operator types /morning-brief                       (or asks "regenerate today's brief")
  ↓
route fires routines.morning_brief.* via tool_call_hooks (before_tool_call stack)
  ↓  (enforce_skill_sensitivity present; for any-scope + internal, NO-OP unless workspace_sensitivity=MNPI)
Stage 1 gather_context(vault, today, days_lookback, active_sectors): walks Projects/ + Registers/Actions.md + Resources/Newsletters/
  ↓     (vault-only; NO external network)
        OllamaClient.health() check
  ↓     (unreachable -> ollama_state="unreachable"; route through fallback path for both stages)
Stage 2 classify_actions (LLM #1, qwen3:14b, JSON-mode)               [or _fallback_classify on OllamaError]
  ↓     -> list[BriefRow] with markers ovd/due/open
Stage 3 anton_suggests (LLM #2, qwen3:14b, plain-text)                [or _fallback_suggest on OllamaError or empty inputs]
  ↓     -> reflective paragraph
Stage 4 MorningBrief assembled (date, source, needsYou, sectorThisWeek, antonSuggests)
  ↓
        write_brief: atomic_write to Routines/morning-briefs/<date>.md  [or skip if dry_run=True]
  ↓     (frontmatter data: contains FULL brief.model_dump() JSON for verbatim round-trip)
Anton verifies: ollama_state surfaced; frontmatter data: complete; no fabrication if inputs empty
  ↓
JSON response: {status, run_id, ollama_state, input_echo, context_counts, brief, note_path, frontmatter_data_complete}
  ↓
audit row written to runs/tool.morning-brief.jsonl                    [hook side effect]
```

## Common Rationalizations

> **Append-only** by the operator after each production run that
> surfaces a new shortcut (CLAUDE.md §14.3). The 5 rows below are
> the v1 seed for a local-LLM vault-only synthesis skill.

| Rationalization | Reality |
|---|---|
| "The brief is written, no need to surface the path in the response" | The response MUST include `note_path` (the vault-relative path of the written file). The operator's "open the brief" follow-up depends on this; without it the dashboard chip has nowhere to link. |
| "Two LLM calls is heavy — I'll merge classify_actions + anton_suggests into one call" | Don't refactor in the SKILL migration. Two calls is the routine's choice; the SKILL governs the existing pipeline. A merge would be a separate "morning-brief-streamlining" session with its own evaluation. |
| "The brief's `source` field says 'Generated · Local Ollama qwen3:14b' — that's awkward for the operator" | Pass through verbatim. The `source` field is provenance; the operator needs to know which model produced the synthesis. Don't rewrite. |
| "dry_run=True is set but I'll still write the file so the operator has it" | Honour dry_run. If the operator wants to preview, they want to PREVIEW — no side effect. Return the brief payload + `note_path: null`. |
| "Empty active_sectors in profile.md — I'll default to all newsletters to fill the brief" | Don't. Empty active_sectors means the operator hasn't declared any active sector; the brief should surface `sectorThisWeek: []` honestly. Defaulting to all newsletters is fiction. |

## Red Flags

> Quoted internal monologue that signals a violation. If you catch
> yourself thinking any of these, stop and re-read the relevant
> stage.

- *"Ollama is down, I'll just skip the LLM calls and write a minimal brief without telling the operator"* — Iron Law breach (clause 2). Surface `ollama_state: "unreachable"` + use the fallback explicitly. The operator decides whether to retry or accept the deterministic categorisation; that decision needs the disclosure.
- *"There are 47 raw action candidates but the LLM is going to drop most as noise — I'll pre-filter to the top 15 to save tokens"* — DON'T. The LLM's filter step (per the SYSTEM prompt at lines 36-64 of synthesise.py) is part of the contract — Anton's pre-filter is fiction. Pass all 47 to the LLM; let it drop noise per its prompt instructions.
- *"`antonSuggests` came back as 12 lines — I'll trim to 3 to keep the brief tight"* — DON'T. The LLM produces what the prompt asks for; trimming post-hoc breaks the synthesis contract. If the operator wants shorter briefs, that's a prompt-engineering revision (separate session), not a per-fire override.
- *"The brief for today already exists (the cron ran at 06:30) — I'll skip the regenerate to avoid overwriting"* — wrong. The SKILL fire is on-demand regeneration; overwrite is the contract. The operator EXPLICITLY asked for regeneration (mid-day after closing a deal or adding actions); honour the request. The deliberate-overwrite is documented in the §14 FLEX NOTES.
- *"The newsletter pull returned 0 items because Resources/Newsletters/ is empty — I'll quietly skip sector_news"* — surface honestly. `sectorThisWeek: []` is legitimate; silent omission would let the operator think nothing's happening in their active sectors.

## Anti-Patterns

- Writing a brief when `gather_context` raised (e.g. vault root
  unreadable) — surface the exception verbatim; do NOT synthesise
  around it. The synthesis runs ONLY after Stage 1 succeeds.
- Surfacing `ollama_state: "ok"` when the fallback path ran —
  silent fallback is operator-deceptive (Iron Law clause 2).
- Trimming or paraphrasing the `antonSuggests` paragraph post-hoc
  for "concision" — the LLM produced what its prompt asked for;
  trimming breaks the synthesis contract.
- Truncating the `data:` frontmatter payload to keep the file
  small — the dashboard read-path deserialises the FULL payload;
  truncation breaks the round-trip (Iron Law clause 1).
- Defaulting to all newsletters when `profile.active_sectors` is
  empty — empty means the operator hasn't declared sectors; the
  brief should reflect that honestly, not fabricate breadth.
- Hand-editing the brief body and re-saving — the brief is
  regenerable; manual edits are lost on next fire. The structured
  payload in `data:` is the source of truth.
- Skipping the regenerate because "today's brief already exists" —
  the SKILL fire is on-demand regeneration; overwrite is the
  contract.

## Example — "regenerate today's brief, I just closed Falcon" (worked end-to-end)

Single realistic example. One good example beats five half-examples.

**Operator input:** "I just closed Falcon — regenerate the brief so
the Falcon actions drop out."

**Stage 1 — Anton invokes the routine:** POST
`/api/workflows/morning-brief` with `{"date": "2026-05-31",
"days_lookback": 7}` (defaults: vault + qwen3:14b + dry_run=False).
The route reads `profile.active_sectors` (e.g.
`["Leisure", "Telecoms"]`), runs `gather_context`, finds 23 raw
action candidates across 4 projects (Falcon's last 2 actions are
already closed so they don't appear) + 4 sector_news rows from
this week's Leisure + Telecoms newsletters.

**Stage 1 — Anton's chat line:** "Gathered 23 action candidates +
4 sector news rows. Falcon's actions are absent (closed). Ollama
health check ✓ (qwen3:14b available)."

**Stage 2-3 — Anton's chat line:** "Classified 23 -> 5 brief rows
(2 overdue, 1 due today, 2 open). Composed 'Anton suggests' (87
words, principal voice). ollama_state: ok."

**Stage 4 — Anton's chat line:** "Wrote `Routines/morning-briefs/
2026-05-31.md` (overwrites the 06:30 cron's snapshot). Frontmatter
data: round-trips byte-faithfully (5 needsYou rows + 4
sectorThisWeek rows + antonSuggests deserialise back to the in-
memory brief)."

**Final output bubble** — the brief payload surfaced inline (5 rows
+ 4 news + the suggestion paragraph), and chips (Open the brief
[the .md] · Re-fire with days_lookback=14 · ollama_state: ok).

## When Stuck

| Symptom | Diagnostic |
|---|---|
| `ollama_state: "unreachable"` AND brief returned with `_fallback_classify` rows | Ollama is not reachable at localhost:11434. Check the Ollama service (`Get-Service Ollama` on Windows) OR the operator's machine GPU. The brief is still a legitimate artefact (the fallback is deterministic); surface honestly + suggest "retry once Ollama is up". |
| `ollama_state: "ok"` but `antonSuggests` is the deterministic fallback string | Both `needs_you` AND `sector_news` are empty AND the route short-circuited the LLM #2 call (Iron Law clause 3 — no synthesis without inputs). Surface "no actions, no sector news -> deterministic 'nothing material' note; LLM not called". |
| `note_path: null` despite `dry_run: false` | Route-wrapper bug. The writer was called but the path wasn't surfaced. Re-fire; if it persists, flag as a routine drift. |
| Brief written but `frontmatter_data_complete: false` | The Iron Law clause 1 check FAILED — the `data:` key did not round-trip. Surface the failure verbatim; do NOT silently accept the brief. The dashboard read-path will mis-deserialise. |
| `context_counts.actions_gathered: 0` despite recent meeting notes | Either `days_lookback` is too narrow (try `days_lookback: 14`) OR the meeting notes use a non-standard folder layout (the routine walks `02 Meeting Notes/` + `Meeting Notes/` + `meeting-notes/` only). Surface the empty count + propose widening. |
| Brief returned with `brief.needsYou: []` but `context_counts.actions_gathered > 0` | The LLM (or the fallback) dropped all candidates as noise. Surface the discrepancy: "23 candidates gathered, 0 brief rows — the LLM's noise filter was aggressive; consider widening days_lookback OR firing the routine in `--debug` mode to see the raw candidates". |
| Brief was written for today but no `Routines/morning-briefs/` exists in vault | The atomic_write creates the parent directory if missing (`path.parent.mkdir(parents=True, exist_ok=True)` at line 30 of writer.py). If the directory truly doesn't exist after the call, the vault root is wrong — check `VAULT` env. |

## Output Contract

The route returns JSON:

```json
{
  "status": "ok",
  "run_id": "<8-hex audit id>",
  "ollama_state": "ok",
  "input_echo": {
    "date": "2026-05-31",
    "days_lookback": 7,
    "model": "qwen3:14b",
    "dry_run": false
  },
  "context_counts": {
    "actions_gathered": 23,
    "actions_classified": 5,
    "sector_news": 4
  },
  "brief": {
    "date": "Sat · 31 May 2026 · UTC",
    "source": "Generated · Local Ollama qwen3:14b",
    "needsYou": [
      {"marker": "ovd", "text": "Send NDA to Heartwood", "sub": "Heartwood · overdue 27d"}
    ],
    "sectorThisWeek": [
      {"marker": "news", "text": "Leisure consolidation accelerating", "sub": "Leisure · 2026-05-29"}
    ],
    "antonSuggests": "Three things on your plate today: ..."
  },
  "note_path": "Routines/morning-briefs/2026-05-31.md",
  "frontmatter_data_complete": true,
  "duration_ms": 47812
}
```

**If `dry_run=True`:** `note_path: null`, `frontmatter_data_complete: null`,
brief returned but not written.

**If `ollama_state == "unreachable"`:** brief returned with
`_fallback_classify` results in `needsYou`; `antonSuggests` is the
deterministic fallback string ("N overdue items..." / "Nothing
material on the desk..." per the input shape); `brief.source`
remains "Generated · Local Ollama qwen3:14b" (the model field is
provenance about what WOULD have run, not what DID — the
`ollama_state` field is the truth-surface for "did the LLM
actually fire"); `note_path` is still populated (the fallback brief
IS written so the dashboard has something to read).

**Side effects:** an atomic write to
`<vault>/Routines/morning-briefs/<date>.md` (overwrite on same-
date refire — deliberate); an audit row to
`runs/tool.morning-brief.jsonl` (via central hook stack; #60
substrate).

**What the route does NOT produce** (so the skill does not report
it): a multi-day brief, a per-project brief, a brief for a date
other than the requested one, a hand-edited body that overrides
the LLM synthesis. The deliverable is today's structured brief +
the written artefact + the ollama_state truth-surface.

## Citations Required

Every action row carries its source via the `sub` field (the `sub`
typically includes the project name + age/due hint). The
`source_path` of each underlying `ActionItem` is captured by
`_extract_from_file` (line 195 of pull.py) but is intentionally NOT
surfaced per-row in the brief — the brief's audience is the operator
scanning their day, not a citation-checker. The frontmatter `data:`
key carries the full structured payload for verbatim round-trip
(the source-path provenance is in `runs/tool.morning-brief.jsonl`
audit row for after-the-fact debugging).

| Field | Required source type | Acceptable form |
|---|---|---|
| `brief.needsYou[*]` | Action rows from `_gather_actions` | BriefRow with marker + text + sub; the `sub` typically includes the project name + age/due |
| `brief.sectorThisWeek[*]` | Newsletter rows from `_gather_sector_news` | BriefRow with marker=news + text + sub (sub = sector name + date) |
| `brief.antonSuggests` | LLM #2 output OR deterministic fallback | Plain-text paragraph; the deterministic "Nothing material on the desk..." fallback IFF both inputs were empty |
| `brief.source` | Provenance | String including the LLM model name; combined with `ollama_state` on the response, the operator knows whether the LLM actually fired |
| `note_path` | The written file location | Vault-relative path; the dashboard's "open the brief" chip uses this |
| `ollama_state` | The LLM lane state | One of `"ok"`, `"unreachable"`, `"fallback"` |
| `frontmatter_data_complete` | The Iron Law clause 1 verification | Boolean; True iff parsing the written file's frontmatter `data:` key yields a deserialisable `MorningBrief` matching the in-memory brief |

## Cost Envelope

The frontmatter declares `cost_ceiling_tokens: 4000` and
`cost_ceiling_seconds: 180`. Both are read from frontmatter by
`get_active_skill_cap("morning-brief", "tokens" | "seconds")`; the
per-skill caps are enforced by the central hook stack (#61/#67).

**Where the budget goes:**
- Gather: ~0 tokens (pure Python file-walk + frontmatter parse).
  Wall-clock sub-second on a vault with <50 projects.
- Classify (LLM #1): ~1200-1800 tokens output (~30-60s wall-clock
  on consumer GPU).
- Anton suggests (LLM #2): ~800-1500 tokens output (~20-40s wall-
  clock).
- Anton's surfacing narration: ~600-1000 tokens (verify Iron Law
  clauses + surface the 3-5 most actionable rows + propose
  dashboard chip).
- Headroom: ~500 tokens for one guardrail retry (e.g. re-fire LLM
  #2 if first output is suspiciously short).

**The 4000 token / 180s ceiling vs prior skills:**
       LBO              -> 8000 / 90s   (engine subprocess)
       sector-news      -> 6000 / 300s  (per-item LLM loop + external network)
       equity-research  -> 4000 / 120s  (provider data + Companies/<X>.md write)
       morning-brief    -> 4000 / 180s  (two qwen3:14b calls + write artefact)
       deal-tracker     -> 3000 / 60s   (single Ollama call)
       vault-health     -> 2000 / 60s   (pure file-walk, write report)
       recall-query     -> 1500 / 30s   (hybrid retrieval, no writes)
       bd-decay         -> 1500 / 30s   (pure file-walk, no write, no LLM)
       actions-decay    -> 1500 / 60s   (pure file-walk + external-tree, no write, no LLM)
       lessons-suggest  -> 1500 / 30s   (pure register parse, no write, no LLM)

Ties equity-research on tokens (both 4000). Wall-clock is GENEROUS
(180s) because two qwen3:14b calls on consumer GPU can each take
30-60s; 180s = ~2x warm-cache headroom, ~1x cold-cache. The cron
has a separate timeout governed by the scheduler.

> **Calibration status:** first-pass estimate. Recalibrate to
> `1.25 x observed` after first real narrated runs on the
> operator's actual machine. Likely needs adjustment for the
> operator's GPU (faster GPU = tighter seconds ceiling; slower =
> looser).

## Verification Checklist (before declaring done)

- [ ] `gather_context` ran without exception; counts surfaced in `context_counts`
- [ ] `ollama_state` honestly reflects the LLM lane state (`ok` | `unreachable` | `fallback`)
- [ ] If `dry_run=False`: the file exists at `Routines/morning-briefs/<date>.md`
- [ ] If `dry_run=False`: `frontmatter_data_complete: true` (the Iron Law clause 1 byte-faithful round-trip check)
- [ ] If `dry_run=True`: no file written; `note_path: null`; `frontmatter_data_complete: null`
- [ ] `brief.needsYou` rows ALL have `marker in {ovd, due, open}` AND non-empty `text`
- [ ] `brief.sectorThisWeek` rows ALL have `marker == "news"` AND non-empty `text`
- [ ] If `gather_context` returned empty `needs_you` AND empty `sector_news`: `brief.antonSuggests` is the deterministic fallback (NOT an LLM-fabricated paragraph)
- [ ] `note_path` populated (vault-relative path the dashboard chip can link to)
- [ ] Audit row exists in `runs/tool.morning-brief.jsonl` with `status: "ok"`
- [ ] No `workspace_sensitivity: MNPI` was accepted (the central guard refuses with 403)
- [ ] Final chat bubble surfaces the brief honestly — if the fallback path ran, the operator is told
