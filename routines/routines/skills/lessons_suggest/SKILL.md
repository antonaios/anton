---
name: lessons-suggest
description: |
  Use when populating the "From prior similar deals" subsection (§6) of
  a new project brief — match every entry in Registers/Lessons.md against
  the target project's industry / sector / subsector, rank by specificity
  (same subsector > same sector > same industry > sector-agnostic), and
  return the top N as paste-ready markdown bullets pointing to each
  lesson's stable wikilink (`[[Registers/Lessons#lesson-<slug>]]`).
  Triggers: lessons suggest, prior deals, similar lessons, watch-outs for
  this deal, populate §6 of new brief, lessons-learned for <sector>,
  /lessons-suggest Cmd-K (once wired). Inputs: EITHER a project name
  (reads industry/sector/subsector from Projects/<X>/00 Brief.md
  frontmatter) OR explicit industry / sector / subsector overrides;
  optional limit (default 10); optional format (bullets | verbose).
  Output: a ranked list of Suggestion records (slug + title + score 0-3
  + reason) AND the paste-ready markdown bullets string ready to drop
  into the brief's §6.
version: 0.1.0
license: proprietary
allowed_tools:
  - vault_read
capabilities:                        # #61-capabilities
  vault_read:  ["Registers/Lessons.md", "Projects/**"]   # NARROW: the register itself + project briefs for sector-context derivation
  vault_write: []                       # pure read; no write surface — operator pastes the bullets manually
  fs_roots:    []                       # vault-only; no fs reads outside the vault
  network:     []                       # zero LLM, zero external network — pure register parse + frontmatter matching
metadata:
  sensitivity: internal        # Registers/Lessons.md is operator-internal knowledge; lessons are cross-deal but not per-deal-MNPI
  workspace_scope: project     # the input is a project CONTEXT (industry/sector/subsector or a project name); the output is meant to drop into ONE specific brief's §6 — workspace_scope=project is the honest declaration even though the read surface is workspace-independent
  tile_label: "Lessons"
  cost_ceiling_tokens: 1500    # ties bd-decay + recall-query + actions-decay for smallest — zero LLM, brief narration of top suggestions
  cost_ceiling_seconds: 30     # register parse + per-entry sector-context derivation is sub-second on a register with <100 entries; 30s is 60x headroom
  guardrails:
    - score_greater_than_zero    # the routine filters zero-score entries at line 100; the Iron Law is "no zero-score suggestion in the surfaced list"
    - reason_populated           # every Suggestion carries a human-readable `reason` so the operator can sanity-check why it ranked
    - matched_context_echoed     # the response echoes the (industry, sector, subsector) tuple the matcher actually used, including _norm normalisation
    - no_synthesis               # the bullets string is RENDERED FROM SLUGS — no paraphrasing of the lesson body, no LLM rewriting
  guardrail_max_retries: 1
---

<!--
§14 FLEX NOTES (lessons-suggest vs prior 8 migrated skills)

1. FIRST deterministic FRONTMATTER-DRIVEN LEAF-SUGGEST skill. Distinct
   shape:
       LBO              -> numeric gate + XLSX deliverable (engine subprocess)
       sector-news      -> sourcing + LLM synthesis (per-item loop)
       vault-health     -> enumeration sweep + report write
       deal-tracker     -> extraction fidelity + Excel row append
       bd-decay         -> taxonomy fidelity, pure-return sweep over Companies/
       recall-query     -> hybrid retrieval over indexed corpus, ranked hits
       equity-research  -> provider data -> Companies/<X>.md note creation
       actions-decay    -> cross-project + multi-root sweep, pure-return
       lessons-suggest  -> deterministic frontmatter-matching against a
                           single register, paste-ready bullets pointing to
                           stable wikilinks
   Validates that the §14 contract supports a register-matcher leaf with
   PROJECT-CONTEXT INPUT and PASTE-READY BULLETS OUTPUT. Zero LLM
   anywhere in the routine.

2. FIRST skill with `workspace_scope: project` AND `vault_write: []`.
   The project-scope is about the USE pattern (the bullets paste into
   ONE project's brief §6), not the write target (there is no write —
   the operator pastes manually). The registry validator accepts this
   combo because the project-scope vault_write guard at
   ``_validate_capabilities`` (line 370 of registry.py) only fires when
   vault_write is non-empty — an empty list passes vacuously. This
   validates that workspace_scope is a USE-discipline declaration, not a
   write-target requirement. Compare:
       LBO / sector-news / vault-health / deal-tracker -> bd-scope or
         general-scope, all have non-empty write surfaces.
       bd-decay / recall-query / actions-decay -> any-scope, all
         vault_write=[].
       equity-research  -> any-scope, vault_write=["Companies/**"].
       lessons-suggest  -> project-scope, vault_write=[].  ← NEW COMBO

3. Iron Law has THREE clauses: score > 0 + reason populated (the
   routine filters zero-score; the SKILL never surfaces them),
   matched-context echoed (the response surfaces the `_norm`-applied
   tuple so the operator can debug rank), no synthesis (bullets are
   built from slugs + titles + reasons by the routine's renderer; Anton
   must not paraphrase lesson bodies). FIRST Iron Law that explicitly
   enforces a DETERMINISTIC SCORE TIER as the discipline boundary.

4. `vault_read` is NARROWEST yet — ["Registers/Lessons.md",
   "Projects/**"]. recall-query is ["**"]; bd-decay is ["Companies/**"];
   actions-decay is ["Projects/**", "Companies/**"]. lessons-suggest
   declares the register itself PLUS Projects/** (for the sector-
   context derivation step: the matcher walks each lesson's
   first_seen_projects and reads their 00 Brief.md frontmatter).

5. TIES bd-decay + recall-query + actions-decay for smallest cost
   envelope (1500 tokens / 30s). Zero LLM in the routine; narration
   loop is short (top-N bullet list + matched-context echo + chips).

6. The ON-DEMAND skill governs the NEW `/api/workflows/lessons-suggest`
   route only. The existing CLI (`lessons-learned suggest --project
   <X>`) stays live and continues to call `routines.lessons.suggest`
   directly — unaffected by this migration. Same separation as bd-decay
   + actions-decay: CLI / cron stays direct; SKILL governs the
   operator-pulled dashboard/Cmd-K path. The OTHER lessons command
   (`lessons-learned scan`) is the LLM-driven proposal flow; out of
   scope here (operator-coupled, not autonomous).

7. NO `captures_to_vault:` block — this skill is pure-return data, not
   a derived semantic fact (cf. LBO captures returns to the Company
   note, equity-research captures the consensus snapshot). The bullets
   string IS the operator-actionable surface; nothing to capture back
   to the vault — the operator pastes the bullets into §6 of the brief
   manually.

8. Central guard (#61): for a project-scope, internal-tier skill the
   guard fires on every call (it cross-checks workspace_type against
   the declared scope). For the common case where the caller passes
   workspace_type="project", the guard is a structural NO-OP. The two
   firing paths are: (a) workspace_type != "project" on a project-scope
   skill (SkillScopeRefused -> 403); (b) workspace_sensitivity=MNPI on
   any skill (the §5.2 cross-skill gate). Tested.
-->

# Lessons Suggest

## Overview

Drives the existing `lessons.suggest` routine
(`routines/lessons/suggest.py::suggest` + `suggest_for_project`) — a
deterministic register-matcher that walks `Registers/Lessons.md`,
parses every `## lesson-<slug> — <title>` entry, derives each entry's
sector context by walking the `First seen:` project's `00 Brief.md`
frontmatter (cached), and ranks against the target project's
industry / sector / subsector using a 4-tier ladder (subsector=3,
sector=2, industry=1, sector-agnostic=1). Returns a `list[Suggestion]`
with score + reason; the routine's `render_brief_bullets` renderer
builds the paste-ready markdown bullets string.

**Anton's job is to invoke the routine via the workflow route, verify
the matched context was applied correctly (the `_norm` normalisation
echo), surface the top-N suggestions with their score + reason, and
hand over the bullets string for the operator to paste into §6 "From
prior similar deals" of the brief.** Anton does NOT paraphrase the
lesson bodies (Iron Law clause 3 — that's a `recall-narrate`
boundary, not lessons-suggest territory).

The routine makes ZERO LLM calls and ZERO network requests — pure
register parse + frontmatter matching + dict cache. The cost envelope
(1500 tokens / 30s) is headroom for Anton's brief narration, not
current routine usage.

## When to Use

**Mandatory triggers** — fire the skill, do not just discuss:
- Operator types `/lessons-suggest` (Cmd-K or composer, once wired)
- Operator clicks the Lessons drawer tile (when wired)
- The project-brief-create flow auto-fires lessons-suggest after the
  brief frontmatter is populated (future; not yet wired)

**Optional triggers** — propose firing the skill, ask first:
- Operator asks "what watch-outs do we have for this deal?" / "any
  lessons from prior similar deals?" / "populate §6 of the brief"
- Operator asks "what cross-cutting lessons might apply?" — fire with
  no sector/subsector OR pass the brief's industry only, get the
  sector-agnostic + same-industry hits
- Operator opens a new `Projects/<X>/00 Brief.md` and asks Anton to
  pre-fill §6 — fire with the project name; the routine reads the
  brief's frontmatter

**Don't use** — refuse and explain why:
- Operator wants a NARRATIVE summary of all lessons for a sector —
  that's `recall-narrate` territory (un-migrated). lessons-suggest
  surfaces ranked WIKILINKS + a one-line reason, not summaries.
- Operator wants to ADD a new lesson to the register — that's
  `lessons-learned scan` (the LLM-driven proposal flow, operator-
  coupled). lessons-suggest READS the register; the scan flow PROPOSES
  additions.
- Operator wants to filter lessons by date / author / "stuff from the
  last 6 months" — the matcher is sector-only. Cross-cutting filters
  are future flags, not a silent reinterpretation.
- Operator wants to BUMP a lesson's rank ("I think this one is more
  important than the score says") — Iron Law breach. The score tier
  is the operator's ranking DISCIPLINE; collapsing it with a per-fire
  override breaks the discipline. If the score tiers are wrong, that's
  a scoring-policy revision in `suggest.py`, not a per-fire override.

## Iron Law

> **NO SUGGESTION IS SURFACED WITHOUT A NON-ZERO SCORE AND A POPULATED
> `reason`. THE RESPONSE ECHOES THE (industry, sector, subsector)
> TUPLE THE MATCHER ACTUALLY USED — INCLUDING `_norm` NORMALISATION
> OF WIKILINK-WRAPPED VALUES — SO THE OPERATOR CAN SANITY-CHECK WHICH
> CONTEXT DROVE THE RANK. THE RENDERED BULLETS STRING IS BUILT FROM
> SLUGS + TITLES, NEVER FROM PARAPHRASED LESSON BODIES.**

Three clauses on top of the universals at
[universal-iron-laws](<vault>/CLAUDE.md#universal-iron-laws):

1. **Score > 0 + reason populated** — the routine already filters
   zero-score entries at line 100 of `suggest.py` (`if score > 0:
   suggestions.append(...)`); the SKILL contract is to NEVER surface a
   zero-score entry even if Anton thinks it's "still relevant". The
   score tiers (1 = sector-agnostic OR same-industry; 2 = same-sector;
   3 = same-subsector) are the operator's ranking discipline;
   collapsing them with a "this is still useful" override breaks the
   discipline. Every surfaced Suggestion ALSO carries a non-empty
   `reason` string (the routine constructs this at line 102:
   `Suggestion(lesson=entry, score=score, reason=reason)`); the SKILL
   refuses to surface a row with an empty reason (would indicate a
   routine drift).
2. **Matched context echoed** — the response surfaces the
   `_norm`-applied (industry, sector, subsector) tuple alongside the
   raw inputs, so the operator can see that `[[Sectors/Telecoms]]` was
   normalised to `telecoms` before matching. Without this echo, a
   wikilink-wrapped input that silently mis-normalises produces ranked
   output the operator cannot debug. The route layer surfaces both
   `input_echo` (the verbatim inputs) AND `matched_context` (the
   `_norm`-applied form).
3. **No synthesis** — the deliverable is the bullets string built by
   `render_brief_bullets` (line 107 of suggest.py). Anton's narration
   MUST NOT paraphrase the lesson bodies (those live in
   `Registers/Lessons.md` and are operator-curated; restating them is
   fiction risk). Anton MAY narrate "5 suggestions, top match is X
   (same subsector)" — that's metadata, not synthesis. If Anton drafts
   a sentence that reads "the lesson on Y warns about Z", that's a
   boundary breach into `recall-narrate` territory (un-migrated).

> **Routine-reality note (2026-05-31 baseline).** The scoring is a
> 4-tier ladder (subsector=3, sector=2, industry=1, agnostic=1) with
> deterministic tie-break on `lesson.slug` (line 103 of suggest.py:
> `suggestions.sort(key=lambda s: (-s.score, s.lesson.slug))`).
> `_norm` strips wikilink wrappers via `_WIKILINK_RE` (line 272). A
> lesson with NO derivable sector context (no `first_seen_projects`
> OR their briefs have no industry/sector/subsector) scores as
> `_SCORE_AGNOSTIC` regardless of target context — the "cross-cutting
> lesson always shows" behaviour at line 263. Flag if you'd phrase
> the Iron Law differently after reading the suggest.py code in full.

## Core Pattern — 4 stages (parse register -> derive sector context -> score per entry -> sort + truncate + render)

The pipeline is implemented in `routines/lessons/suggest.py::suggest`
(lines 77-104). Anton's verification points map to the routine's
stages.

### Stage 1 — Parse register

`_parse_register(vault_root)` (line 134) reads
`Registers/Lessons.md`, splits the body by `## lesson-<slug>`
headings (honouring fenced code blocks so the register's own schema
example doesn't get treated as a real entry), and returns a
`list[LessonEntry]` with `slug`, `title`, `body`, and
`first_seen_projects` (extracted by `_FIRST_SEEN_RE` +
`_PROJECT_LINK_RE` from the body).

Anton verifies: the parser ran cleanly (no exception); the register
exists (line 137 handles missing register gracefully -> returns `[]`,
which surfaces in the response as `register_state.exists: false`).

### Stage 2 — Derive sector context

`_annotate_with_sector_context(entries, vault_root)` (line 211) walks
each entry's `first_seen_projects`, loads each project's `00 Brief.md`
frontmatter (cached in `brief_cache: dict[str, dict[str, str | None]]`),
and unions the project's industry/sector/subsector into the entry's
`industries` / `sectors` / `subsectors` lists.

Anton verifies: every entry that has `first_seen_projects` got
annotated (the brief existed AND was parseable); entries whose briefs
are missing OR have no sector fields end up with empty lists (they
score as `_SCORE_AGNOSTIC` at Stage 3).

### Stage 3 — Score per entry

`_score_entry(entry, target_industry, target_sector, target_subsector)`
(line 250) applies the 4-tier ladder: subsector match (3) > sector
match (2) > industry match (1) > sector-agnostic (1) > no match (0).

Anton verifies: every returned Suggestion has `score > 0` (zero-score
entries are filtered at line 100); every Suggestion has a populated
`reason` string (line 102: `Suggestion(lesson=entry, score=score,
reason=reason)`).

### Stage 4 — Sort + truncate + render

Sort descending by score with tie-break on slug (line 103); truncate
to `limit` (line 104); `render_brief_bullets(suggestions)` (line 107)
builds the paste-ready markdown bullets string.

Anton verifies: `len(suggestions) <= limit`; the rendered string
contains exactly one bullet per suggestion (no header, no preamble,
no trailing summary); if `format=verbose`, the response includes
score + reason per row in addition to the bullets string.

### Verification Anton applies before surfacing

1. The matcher returned a list (no exception caught mid-parse).
2. Every Suggestion has `score > 0` AND a non-empty `reason` AND a
   non-empty `slug` (Iron Law clause 1).
3. `matched_context.{industry,sector,subsector}_norm` echoes the
   `_norm`-applied form of each input (Iron Law clause 2).
4. The `bullets` string is byte-identical to a direct call of
   `render_brief_bullets(suggestions)` on the same Suggestion list —
   no Anton-side reformatting (Iron Law clause 3).
5. `register_state` carries `exists` + `entries_parsed` so the
   operator can sanity-check the corpus the matcher saw.
6. `len(suggestions) <= limit` (the operator's cap is honoured).

## Quick Reference

```
operator types /lessons-suggest --project DemoDeal     (or asks "any prior deals like this?")
  ↓
route fires routines.lessons.suggest.suggest_for_project via tool_call_hooks (before_tool_call stack)
  ↓  (enforce_skill_sensitivity present; for project-scope+internal, NO-OP on workspace_type=project / non-MNPI)
Stage 1 _parse_register(vault): reads Registers/Lessons.md, splits by ## lesson-<slug>
  ↓
Stage 2 _annotate_with_sector_context: walks first_seen_projects, reads 00 Brief.md frontmatter (cached)
  ↓
Stage 3 _score_entry per entry: 4-tier ladder (subsector=3, sector=2, industry=1, agnostic=1)
  ↓
Stage 4 sort by (-score, slug); truncate to limit; render_brief_bullets builds bullets string
  ↓
Anton verifies: score>0 + reason populated; matched_context echoes _norm; bullets byte-identical to renderer
  ↓
JSON response: {status, input_echo, matched_context, register_state, suggestions: [...], bullets, counts}
  ↓
audit row written to runs/tool.lessons-suggest.jsonl                     [hook side effect]
```

## Common Rationalizations

> **Append-only** by the operator after each production run that
> surfaces a new shortcut (CLAUDE.md §14.3). The 5 rows below are the
> v1 seed for a deterministic frontmatter-matcher leaf.

| Rationalization | Reality |
|---|---|
| "The operator asked for a project, I'll derive industry/sector/subsector myself from the project name" | Use `suggest_for_project()` — it reads the brief frontmatter properly. If the brief doesn't exist OR has no sector fields, that's an explicit "no project context" condition; surface it honestly, do not pattern-match the project name. |
| "Limit was passed but I see 12 strong matches — I'll surface them all" | Honour the limit. The operator's `limit=10` is the contract; ignoring it produces a longer bullets string than the operator can comfortably paste. |
| "The bullets string is plain markdown — I'll add a header `## From prior similar deals` for the paste" | DON'T add the section header — the operator pastes the bullets INTO the existing section. Adding the header pastes "header + bullets" which the operator then has to delete. Bullets only. |
| "Two suggestions tied on score (both subsector matches) — I'll re-order by my judgement" | The deterministic tie-break is on `lesson.slug` (line 103). Preserve. The operator can re-order post-paste if they care. |
| "The format param was 'verbose' but the operator clearly wants bullets — I'll render bullets anyway" | Honour the format. If the operator passed `format=verbose`, they want score + reason + slug per row for debugging. Don't second-guess. |

## Red Flags

> Quoted internal monologue that signals a violation. If you catch
> yourself thinking any of these, stop and re-read the relevant stage.

- *"Only 2 suggestions came back, I'll loosen the matcher to surface more"* — the matcher returns what scored > 0; if 2 entries scored, the register doesn't have more relevant entries. Surface "2 matches for sector=telecoms; consider also browsing Registers/Lessons.md directly for cross-cutting entries", do NOT silently loosen.
- *"The top suggestion's title is awkward — `Fallback to Markdown when Excel is closed.` — I'll clean it up before pasting"* — DON'T. The `render_brief_bullets` renderer does the standard cleanup (`title.rstrip(". ")` at line 117); further rewording is fiction. The lesson's stable wikilink IS the canonical reference; the operator can edit the bullet's wording post-paste if they want to.
- *"This lesson has score=1 (sector-agnostic) but it's clearly the most important watch-out for this deal — I'll bump it to rank 1"* — Iron Law breach. The score is the operator's ranking discipline. If sector-agnostic lessons are systematically more useful than sector-specific ones, that's a scoring-policy revision, not a per-fire override.
- *"The matched_context echo shows `sector: ''` because the project brief has no sector — I'll just guess from the project name"* — wrong. Surface the empty echo honestly; the matcher returned what it returned. If the operator passed no sector AND the project brief has no sector AND the matcher returned only sector-agnostic hits, that's the truth.
- *"The render_brief_bullets output uses `->` — I'll convert to the prettier `→` for the response"* — DON'T. The renderer's choice (line 117 comment: "Use ASCII `->` rather than `→` so the renderer's output prints cleanly on Windows cp1252 terminals") is deliberate. Pass through verbatim.
- *"The operator pasted no inputs — I'll default to a generic firm-wide rank"* — wrong. With no inputs (no project, no industry, no sector, no subsector), the matcher returns only sector-agnostic hits (those with no derivable sector context). Surface that honestly + flag "no context provided; only cross-cutting hits returned".

## Anti-Patterns

- Paraphrasing the lesson body into the bullet ("Asset-light pubs
  trade rich because their multiples are insensitive to working-
  capital swings") — the bullet is the title + wikilink + reason,
  built by the routine's renderer; paraphrasing the body is
  `recall-narrate` territory.
- Dropping the `reason` from the bullet for brevity — the reason is
  the operator's visible explanation of WHY each lesson ranked;
  removing it forces the operator to grep `suggest.py` to interpret
  the rank.
- Re-ordering suggestions by Anton's judgement after the routine
  returns — the deterministic sort is `(-score, lesson.slug)` (line
  103); re-ordering breaks the contract.
- Silently re-formatting the bullets string ("I'll switch `->` to
  `→`, looks nicer") — the renderer's choice is deliberate (Windows
  cp1252 terminal-safe). Pass through verbatim.
- Adding a section header to the bullets string ("## From prior
  similar deals\n\n- ...") — the operator pastes the bullets INTO
  the existing section; adding the header pastes "header + bullets"
  which the operator then has to delete.
- Inferring sector from the project name when the brief frontmatter
  has none — if the brief has no sector, that's an operator-
  discipline gap. Surface "no sector in brief; only agnostic hits"
  honestly.

## Example — "lessons for DemoDeal" (worked end-to-end)

Single realistic example. One good example beats five half-examples.

**Operator input:** "DemoDeal is a Leisure / Pubs roll-up; any prior
deals like this in the register?"

**Stage 1 — Anton invokes the routine:** POST
`/api/workflows/lessons-suggest` with `{"project": "DemoDeal",
"limit": 5}` (the brief frontmatter carries `industry: "Consumer"`,
`sector: "[[Sectors/Leisure]]"`, `subsector: "Pubs"`).

**Stage 1 — Anton's chat line:** "Read 24 entries from
`Registers/Lessons.md`; 19 had derivable sector context. Matched
context: industry=consumer, sector=leisure, subsector=pubs.
**5 suggestions** (3 subsector matches, 1 sector match, 1 sector-
agnostic). Top match: `lesson-leisure-asset-light-multiple-expansion`
(same subsector: pubs)."

**Stage 2 — Anton's bullets bubble:** "Ready to paste into §6 'From
prior similar deals':

```markdown
- Asset-light leisure operators trade at premium EV/EBITDA -> [[Registers/Lessons#lesson-leisure-asset-light-multiple-expansion]] _(matched: same subsector (pubs))_
- Pub roll-ups stall on integration unless ops director is hired pre-LOI -> [[Registers/Lessons#lesson-pubs-integration-ops-director-first]] _(matched: same subsector (pubs))_
- Capex-cycle timing dominates valuation in pubs vs general leisure -> [[Registers/Lessons#lesson-pubs-capex-cycle-timing]] _(matched: same subsector (pubs))_
- Leisure carve-outs need TSA scope locked before due-diligence kickoff -> [[Registers/Lessons#lesson-leisure-carveout-tsa-scope-first]] _(matched: same sector (leisure))_
- Deal narrative drift between IC and final paper is the single biggest exec-summary failure mode -> [[Registers/Lessons#lesson-narrative-drift-ic-to-final]] _(matched: sector-agnostic / cross-cutting)_
```

Paste under §6, then edit the wording if you want (the wikilinks are
the canonical references)."

**Final output bubble** — a short lead ("5 suggestions, 3 pubs-
specific dominating the rank; 1 sector-wide leisure carve-out hit
might apply if there's a TSA layer; 1 cross-cutting narrative-drift
warning always shows"), and chips (Re-fire with limit=10 · Paste
to brief now · Open Registers/Lessons.md for the full body).

## When Stuck

| Symptom | Diagnostic |
|---|---|
| `suggestions: []` on a real register | Cross-check `register_state.exists` AND `register_state.entries_parsed > 0` AND `matched_context` echoes the inputs. If the register has entries but none scored, the inputs may have NO overlap with any entry's derivable sector context — that's the truth; surface "register has N entries but none matched (industry, sector, subsector)". |
| `suggestions` only contains sector-agnostic hits | Means no entry in the register has derivable sector context matching the target. Either (a) the inputs are off (typo in sector name), (b) the register doesn't yet have entries for this sector, or (c) entries exist but their `First seen:` projects' briefs have no sector frontmatter. Surface the `matched_context` echo + the `register_state.entries_with_sector_context` count so the operator can debug. |
| `matched_context.sector_norm` is empty when the input was `[[Sectors/Leisure]]` | Routine bug — `_norm` should strip the wikilink wrapper to "leisure". If you see empty norm output for a non-empty input, flag it: surface the raw input + the empty norm + the resulting "only agnostic hits". |
| `len(suggestions) > limit` | Route-wrapper bug. The routine truncates at line 104; if the route shipped more rows, the truncation didn't run. Surface the discrepancy. |
| The bullets string contains the section header `## From prior similar deals` | Route-wrapper bug. The renderer at line 107 of suggest.py builds bullets ONLY; adding a header is a route-side mistake. Refuse the response + flag. |
| Two suggestions have the same slug | Routine bug — the register's parser should never emit two entries with the same slug (the heading regex requires `lesson-<...>`; duplicates indicate the operator put the same heading twice in the register, OR the parser mis-handled a code fence). Surface verbatim + flag. |
| `register_state.exists: false` | The vault does not contain `Registers/Lessons.md`. Surface `suggestions: []` + the routine's "no matching lessons" string in `bullets` (line 111 of suggest.py). This is `status: "ok"`, NOT an error — an empty register is a valid state. |

## Output Contract

The route returns JSON:

```json
{
  "status": "ok",
  "run_id": "<8-hex audit id>",
  "input_echo": {
    "project": "DemoDeal",
    "industry": null,
    "sector": null,
    "subsector": null,
    "limit": 5,
    "format": "bullets"
  },
  "matched_context": {
    "industry_norm": "consumer",
    "sector_norm": "leisure",
    "subsector_norm": "pubs"
  },
  "register_state": {
    "path": "Registers/Lessons.md",
    "exists": true,
    "entries_parsed": 24,
    "entries_with_sector_context": 19
  },
  "suggestions": [
    {
      "slug": "lesson-leisure-asset-light-multiple-expansion",
      "title": "Asset-light leisure operators trade at premium EV/EBITDA",
      "score": 3,
      "reason": "same subsector (pubs)",
      "wikilink": "[[Registers/Lessons#lesson-leisure-asset-light-multiple-expansion]]"
    }
  ],
  "bullets": "- Asset-light leisure operators trade at premium EV/EBITDA -> [[Registers/Lessons#lesson-leisure-asset-light-multiple-expansion]] _(matched: same subsector (pubs))_\n- ...",
  "counts": {
    "total_entries": 24,
    "scored": 7,
    "returned": 5
  },
  "duration_ms": 18
}
```

**No file write** — operator pastes the `bullets` string into
`Projects/<X>/00 Brief.md` §6 manually. Audit row to
`runs/tool.lessons-suggest.jsonl` (via central hook stack; #60
substrate).

**What the route does NOT produce** (so the skill does not report
it): a paraphrased summary of the lesson bodies, a narrative
synthesis across suggestions, an auto-edit of the project brief, or
a cross-run diff against the previous fire. The bullets + the ranked
Suggestion list are the only deliverables.

## Citations Required

The wikilink IS the citation — every Suggestion carries the stable
`[[Registers/Lessons#lesson-<slug>]]` reference. The operator clicks
through to read the full lesson body.

| Field | Required source type | Acceptable form |
|---|---|---|
| `slug` | The lesson's stable identifier | String matching `lesson-<...>` (regex `_HEADING_RE` at line 129 of suggest.py) |
| `wikilink` | The Obsidian cross-reference | `[[Registers/Lessons#lesson-<slug>]]` verbatim |
| `score` | The matcher's 4-tier ranking | Integer 1-3 (zero-score entries are filtered before return) |
| `reason` | Human-readable why-it-matched | String: `"same subsector (<X>)"` / `"same sector (<X>)"` / `"same industry (<X>)"` / `"sector-agnostic / cross-cutting"` |
| `matched_context.{industry,sector,subsector}_norm` | The `_norm`-applied normalisation of the input | Lowercase string with wikilink wrappers stripped |
| `register_state.path` + `entries_parsed` | The register source + parse count | Vault-relative path + integer |

## Cost Envelope

The frontmatter declares `cost_ceiling_tokens: 1500` and
`cost_ceiling_seconds: 30`. Both are read from frontmatter by
`get_active_skill_cap("lessons-suggest", "tokens" | "seconds")`;
the per-skill caps are enforced by the central hook stack (#61/#67).

**Where the budget goes** (entirely Anton's narration — the routine
is zero LLM):
- Routine: ~0 tokens (pure Python + frontmatter parse + dict cache).
  Wall-clock <500ms on a register with <100 entries.
- Anton narration: ~700-1000 tokens (matched-context echo + top-N
  suggestions with score + reason + "paste into §6" follow-up chip +
  propose `format=verbose` follow-up if any scores are tied).
- Headroom: ~500 tokens for one guardrail retry (e.g. re-fire with
  a wider context if matched_context is empty AND zero suggestions
  returned).

**The 1500 token / 30s ceiling vs prior skills:**
       sector-news       -> 6000 / 300s  (per-item LLM loop)
       LBO               -> 8000 / 90s   (engine subprocess)
       equity-research   -> 4000 / 120s  (provider + Companies write)
       deal-tracker      -> 3000 / 60s   (single Ollama call)
       vault-health      -> 2000 / 60s   (pure file-walk, write report)
       recall-query      -> 1500 / 30s   (hybrid retrieval, no writes)
       bd-decay          -> 1500 / 30s   (pure file-walk, no write, no LLM)
       actions-decay     -> 1500 / 60s   (pure file-walk + external-tree, no write, no LLM)
       lessons-suggest   -> 1500 / 30s   (pure register parse + frontmatter match, no write, no LLM)

Ties bd-decay + recall-query for smallest ceiling on BOTH axes.
Routine work is genuinely zero-LLM and sub-second; narration is a
ranked-list summary + a paste prompt.

> **Calibration status:** 1500 tokens / 30s are first-pass estimates.
> Likely TIGHTER than 1500 once real data lands (operator narration
> is reliably short on ranked-list surfaces). Recalibrate to `1.25
> x observed` after the first few narrated runs.

## Verification Checklist (before declaring done)

- [ ] The matcher returned a list (no exception raised mid-parse; route surfaces 500 on raise)
- [ ] Every Suggestion has `score > 0` AND non-empty `reason` AND non-empty `slug` (Iron Law clause 1)
- [ ] `matched_context.{industry,sector,subsector}_norm` echoes the `_norm`-applied form of each input (Iron Law clause 2)
- [ ] The `bullets` string is byte-identical to a direct call of `render_brief_bullets(suggestions)` on the same Suggestion list (Iron Law clause 3)
- [ ] No Suggestion field carries a paraphrased lesson body — the bullet text is title + wikilink + reason, full stop
- [ ] `len(suggestions) <= limit` (operator's cap honoured)
- [ ] `register_state.exists` + `entries_parsed` populated so the operator can sanity-check the corpus
- [ ] `register_state.exists: false` returns `status: "ok"` with empty suggestions + the routine's "no matching lessons" bullets string (NOT an error)
- [ ] Audit row exists in `runs/tool.lessons-suggest.jsonl` with `status: "ok"`
- [ ] No section header (`## From prior similar deals`) injected into the bullets string
- [ ] Final chat bubble surfaces the bullets in a code fence so the operator can copy-paste directly
