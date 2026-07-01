---
name: actions-decay
description: |
  Use when surfacing overdue or stale open actions across every active
  project — walk vault Projects/ AND every external_project_paths root
  from profile.md; for each discovered project, run the actions
  aggregator on its tree; flag rows where the due date is past
  (overdue) or where there is no due date AND the source file mtime is
  older than 90 days (stale). Sticky states (done / checked) are
  excluded. Triggers: "what's overdue", "show me my stale actions",
  cross-project decay, action-item decay, morning-brief actions
  section, follow-up sweep. Inputs: optional vault override, optional
  today-override for testing, optional format (summary | json | brief).
  Output: a list of StaleAction rows grouped into overdue + stale,
  PLUS the list of project names scanned, PLUS the list of root paths
  actually visited so the operator can sanity-check the multi-root
  resolution.
version: 0.1.0
license: proprietary
allowed_tools:
  - vault_read
capabilities:                        # #61-capabilities
  vault_read:  ["Projects/**", "Companies/**"]    # vault projects + Companies notes referenced from each project brief
  vault_write: []                                 # pure read; no write surface
  fs_roots:    ["{external_project_paths}/**/*.md"]  # PROFILE-DRIVEN: actual paths resolved at boot from _claude/profile.md::external_project_paths. The {...} placeholder is a documentation convention; the validator accepts the literal string.
  network:     []                                 # zero LLM, zero external network — pure file-walk + date arithmetic
metadata:
  sensitivity: internal        # action lines from project notes are operator-internal context; per-project sensitivity is inherited from each project's 00 Brief.md sensitivity (the walker respects skip rules but the SKILL tier is `internal` because the cross-project sweep aggregates across mixed-sensitivity projects)
  workspace_scope: any         # firm-wide sweep across every project; workspace-independent
  tile_label: "Actions Decay"
  cost_ceiling_tokens: 1500    # ties bd-decay + recall-query for smallest — zero LLM in the routine, brief narration of overdue+stale counts
  cost_ceiling_seconds: 60     # walk over a few dozen projects + their tree of .md files; sub-second to ~5s typical
  guardrails:
    - sweep_completed             # every discovered project was successfully aggregated (or its failure was logged + counted)
    - roots_surfaced              # the response's `roots_resolved` field enumerates EVERY path the walker visited
    - provenance_complete         # every StaleAction has project + source_file + source_line + task_hash
    - sticky_states_excluded      # `done` rows never surface in the overdue or stale buckets
  guardrail_max_retries: 1
---

<!--
§14 FLEX NOTES (actions-decay vs prior 7 migrated skills)

1. FIRST skill with PROFILE-DRIVEN fs_roots. The `capabilities.fs_roots:`
   block declares the SHAPE of what's read — the placeholder string
   `{external_project_paths}/**/*.md` is a documentation convention so the
   reader understands that the actual paths are resolved at BOOT from
   `_claude/profile.md::external_project_paths` (a list of absolute
   directories the operator maintains). The runtime walker in
   `routines/projects/decay.py::_discover_projects` reads that list
   directly; no glob-substitution happens at the capability layer. The
   registry validator accepts the literal placeholder string (it's a
   non-empty forward-slash glob — `_is_malformed_glob` is happy). Compare:
       LBO              → fs_roots = ["<workspace-root>/**"] (hard-coded, single root)
       sector-news      → fs_roots = ["<workspace-root>/Projects/_Trackers/**"] (hard-coded, single root, write target)
       vault-health     → fs_roots = [] (vault-only)
       deal-tracker     → fs_roots = ["<workspace-root>/Projects/_Trackers/**"] (hard-coded, single root)
       bd-decay         → fs_roots = [] (vault-only)
       recall-query     → fs_roots = [] (vault-only, read-only retrieval)
       equity-research  → fs_roots = [] (vault-only, provider→Companies/<X>.md)
       actions-decay    → fs_roots = ["{external_project_paths}/**/*.md"]  ← PROFILE-DRIVEN, MULTI-ROOT
   Validates that §14's capability manifest supports operator-configurable
   multi-root reads, not just statically-declared single roots.

2. Iron Law adds the `roots_surfaced` clause — the response MUST enumerate
   which paths the walker actually visited so the operator can sanity-
   check multi-root resolution. Distinct from bd-decay's TWO-STATE-HONESTY
   (stale ≠ untracked taxonomy): actions-decay's third clause is about
   RESOLUTION TRANSPARENCY at the response layer. The walker silently
   succeeding on a partial root set (one dismounted disk) is the failure
   mode this clause prevents.

3. COUSIN shape to bd-decay (pure-return decay sweep) but with cross-vault
   + external-tree scope. bd-decay reads `Companies/**` only;
   actions-decay reads `Projects/**` + every `external_project_paths`
   root + Companies/<target>+<counterparty> per project brief. Same
   pure-return contract (no file write), same zero-LLM routine, same
   morning-brief consumer pattern.

4. TIES bd-decay + recall-query for smallest cost envelope
   (1500 tokens / 60s). The routine is genuinely zero-LLM and zero-
   network; the narration loop is short (counts + 3-5 spotlight rows per
   bucket). 60s ceiling (vs bd-decay's 30s) is the only delta — file-walk
   over external trees on a slow disk can take a few seconds longer than
   the in-vault Companies/ sweep.

5. NO `captures_to_vault:` block — this skill is pure-return data, not a
   derived semantic fact (cf. LBO captures returns to the Company note,
   equity-research captures the consensus snapshot). The overdue + stale
   list IS the operator-actionable surface; nothing to capture back to
   the vault.

6. The ON-DEMAND skill governs the route fire only. The 06:45 daily-cron
   continues to call `routines.projects.decay.scan` DIRECTLY (no skill
   dispatch) — it's a cron job with its own iron laws (morning-brief's).
   This SKILL.md exists so the operator-pulled on-demand path ("show me
   my overdue actions") has a §14 descriptor + a Cmd-K-reachable route.
   Same separation as bd-decay: cron stays direct; SKILL governs
   operator-pulled.

7. Central guard (#61): the route flows through `tool_call_hooks` so
   `enforce_skill_sensitivity` is on the path — but for an `any`-scope,
   `internal`-tier skill the guard is a structural NO-OP for the common
   case. The only firing path is the cross-skill MNPI gate: if a caller
   somehow flags `workspace_sensitivity=MNPI` on the request, the guard
   refuses with `SkillScopeRefused` (mapped to HTTP 403). Tested.

8. ROUTINE-SURFACE DELTA: the existing `decay.scan` returns a
   `DecaySweep` carrying `projects_scanned` + `overdue` + `stale`. The
   ROUTE LAYER (not the routine) computes the additional `roots_resolved`
   + `roots_unresolved` + `projects_failed` fields by re-loading the
   profile and existence-checking each declared external path. This
   keeps `decay.py` untouched (the cron path is unaffected) while
   delivering the Iron Law's `roots_surfaced` clause on the on-demand
   surface.
-->

# Actions Decay

## Overview

Drives the existing `actions-decay` routine
(`routines/projects/decay.py::scan`) — a deterministic cross-project sweep
that walks vault `Projects/` AND every `external_project_paths` root from
`_claude/profile.md`, runs the per-project actions aggregator on each
discovered project, and returns the set of overdue + stale open actions.
The routine returns a `DecaySweep` (`projects_scanned`, `overdue`, `stale`);
the bridge route wraps that with `roots_resolved` + `roots_unresolved` +
`projects_failed` so the operator can sanity-check the multi-root
resolution. **Anton's job is to invoke the routine, verify the sweep
completed cleanly + every row carries full provenance + every visited
root is surfaced, surface the headline counts + thresholds + 3-5
spotlight rows per bucket, and flag the operator.**

The routine makes ZERO LLM calls and ZERO network requests — pure
file-walk + frontmatter parse + date arithmetic. The cost envelope
(1500 tokens / 60s) is headroom for Anton's narration loop, not current
routine usage.

## When to Use

**Mandatory triggers** — fire the skill, do not just discuss:
- Operator types `/actions-decay` (Cmd-K or composer)
- Operator clicks the Actions Decay drawer tile (when wired)
- The 06:45 daily-cron consumes `routines.projects.decay.scan` directly
  (the cron owns this path; the SKILL.md governs on-demand fires only —
  see Output Contract)

**Optional triggers** — propose firing the skill, ask first:
- Operator asks "what's overdue?" / "show me my stale actions" / "what
  follow-ups have I dropped?"
- Operator asks for a mid-day open-actions sweep (cron data is from
  06:45; mid-day edits aren't reflected until next morning unless the
  on-demand path fires)
- Operator asks "weekly sweep — am I behind on anything?"

**Don't use** — refuse and explain why:
- Operator wants to scan a SINGLE project's actions — the per-project
  endpoints at `GET /api/projects/{project}/actions` are the right
  surface; this skill is the cross-project sweep.
- Operator wants to TOGGLE a specific action to done — that is
  `POST /api/projects/{project}/actions/toggle`, an existing route.
  actions-decay SURFACES, the operator ACTIONS.
- Operator wants a historical trend ("how has overdue-count moved over
  weeks?") — the routine returns the current state; cross-run diffing
  is a separate skill (not shipped).
- Operator wants to silently filter archived projects — surface them
  with their overdue counts. A dead project's actions ARE stale; that's
  the truth.

## Iron Law

> **NO STALE OR OVERDUE ROW IS SURFACED WITHOUT FULL PROVENANCE
> (project + source_file + source_line + task_hash). THE
> `roots_resolved` FIELD IN THE RESPONSE MUST ENUMERATE EVERY PATH THE
> WALKER ACTUALLY VISITED — vault `Projects/` PLUS EVERY
> `profile.external_project_paths` ENTRY. STICKY STATES (`done`) ARE
> NEVER COUNTED IN THE OVERDUE OR STALE BUCKETS.**

Three clauses on top of the universals at
[universal-iron-laws](<vault>/CLAUDE.md#universal-iron-laws):

1. **Provenance complete** — every `StaleAction` in the response carries
   the project name (from `_discover_projects`), the `source_file`
   absolute path, the `source_line` (1-indexed hint), and the
   `task_hash` (8-char sha1 of normalised title for stable identity
   across re-runs). Without these, the operator cannot click through to
   fix the row OR even verify the row is real (false-positive regex
   matches happen). The routine already constructs each row via
   `StaleAction.from_action(project, a)` (lines 40-52 of `decay.py`);
   the source `Action` dataclass (lines 77-89 of `actions.py`) carries
   all the fields.
2. **Roots surfaced** — the response surfaces the FULL list of paths
   visited. The vault `Projects/` is always in the list; each
   `external_project_paths` entry that resolves to an existing
   directory is in the list; entries that don't resolve are surfaced as
   `roots_unresolved` with the operator's profile.md value as the
   source (so the operator can see "you set this path in profile but it
   doesn't exist anymore"). A silent skip on disk failure gives the
   operator false confidence the sweep was full.
3. **Sticky states excluded** — `done` rows NEVER count as overdue or
   stale. The routine already does this at the status-mapping layer
   (lines 312-319 of `actions.py`); Anton's job is to refuse the
   response if any `done`-marked row appears in either bucket
   (indicates a routine drift).

> **Routine-reality note (2026-05-31 baseline).** The routine's
> `STALE_AFTER_DAYS = 90` constant (line 70 of `actions.py`) is the only
> threshold. The response surfaces `thresholds_applied.stale_after_days`
> so the operator can sanity-check the active value. If a future revision
> makes the threshold operator-configurable via profile.md, surface the
> EFFECTIVE value (not the constant). Flag if you'd phrase the Iron Law
> differently.

## Core Pattern — 4 stages (discover roots → enumerate projects → aggregate per project → bucket by status)

The pipeline is implemented in `routines/projects/decay.py::scan` (lines
98-126). Anton's verification points map to the routine's stages.

### Stage 1 — Discover roots

`_discover_projects(vault, profile)` (lines 67-95) reads
`profile.external_project_paths` from `_claude/profile.md`, walks vault
`Projects/` AND every external path, dedupes by canonical name
(`_canonical(name)` = `name.lower().replace(' ', '-')`), retains
first-seen original casing for display. Returns a sorted list of project
names.

Anton verifies: every external path the operator declared is enumerated
in `roots_resolved`, OR is reported in `roots_unresolved` with its
profile.md value + reason.

### Stage 2 — Per-project aggregator

For each discovered project, `actions_mod.aggregate(vault, project,
profile, today)` (line 370 of `actions.py`) runs `action_sources(...)`
to enumerate the .md files for that project (vault tree + external
trees + `Companies/<target>` + `Companies/<counterparty>` from the
brief), then `_scan_file(...)` on each, then dedupes by
`(source_file, task_hash)`.

Anton verifies: per-project enumeration didn't silently fail; if it did,
the project lands in `projects_failed` with reason.

### Stage 3 — Bucket by status

The aggregator returns a `list[Action]` with `status: 'open' | 'overdue'
| 'stale' | 'done'` already computed (lines 312-319 of `actions.py`).
The `decay.scan` layer (lines 116-119) appends overdue rows to
`sweep.overdue` and stale rows to `sweep.stale`.

Anton verifies: no `done` row in either bucket; `open` rows are excluded
from both (they're not decayed yet).

### Stage 4 — Surface with roots_resolved

The route wrapper surfaces the `DecaySweep` shape as JSON, augmented
with a `roots_resolved` list (vault `Projects/` + each existing external
path) and a `roots_unresolved` list (profile entries that didn't
resolve).

Anton verifies: the `roots_resolved` count equals
(`len(external_project_paths)` + 1 for vault `Projects/`) MINUS
`len(roots_unresolved)`; the operator can do this arithmetic mentally
as a sanity check.

### Verification Anton applies before surfacing

1. The sweep returned a `DecaySweep` (no exception caught mid-walk).
2. Every row in `overdue` + `stale` has non-null `project`,
   `source_file`, `source_line`, `task_hash` (Iron Law clause 1).
3. `roots_resolved` enumerates every path the walker visited;
   unreachable paths are in `roots_unresolved` (Iron Law clause 2).
4. No `done`-status row appears in `overdue` or `stale` (Iron Law
   clause 3).
5. `thresholds_applied.stale_after_days` echoes the routine's
   `STALE_AFTER_DAYS` constant (90).
6. `projects_failed` is empty OR every entry has a `reason` string.

## Quick Reference

```
operator types /actions-decay      (or clicks Actions Decay tile / asks "what's overdue")
  ↓
route fires routines.projects.decay.scan via tool_call_hooks (before_tool_call stack)
  ↓  (enforce_skill_sensitivity present but a NO-OP: workspace_scope=any, sensitivity=internal)
Stage 1 _discover_projects(vault, profile): walks vault Projects/ + every external_project_paths root
  ↓
Stage 2 per project: actions_mod.aggregate(vault, project, profile, today) → list[Action]
  ↓
Stage 3 bucket by status: overdue → sweep.overdue; stale → sweep.stale; done/open → excluded
  ↓
Stage 4 route wrapper: compute roots_resolved + roots_unresolved + projects_failed
  ↓
Anton verifies sweep returned cleanly; provenance complete; roots surfaced; no done in buckets
  ↓
JSON response: {status, today, thresholds_applied, roots_resolved, roots_unresolved,
                projects_scanned, projects_failed, overdue: [...], stale: [...], counts}
  ↓
audit row written to runs/tool.actions-decay.jsonl                       [hook side effect]
```

## Common Rationalizations

> **Append-only** by the operator after each production run that surfaces
> a new shortcut (CLAUDE.md §14.3). The 5 rows below are the v1 seed.

| Rationalization | Reality |
|---|---|
| "The operator only cares about today's overdue — I'll filter out stale" | Both buckets are surfaced because both decay (overdue = past due-date; stale = no due-date + file untouched 90d). The morning-brief and the on-demand SKILL fire have the same contract: BOTH buckets. |
| "task_hash is an implementation detail — I'll drop it from the surfaced row" | Iron Law breach. The task_hash is the operator's ONLY stable identifier for re-running the toggle endpoint (`POST /api/projects/{project}/actions/toggle`) against the same row after a file edit shifts the line number. Always surface. |
| "An external root is missing (`/mnt/y/...` doesn't exist on Windows) — I'll silently coerce to vault-only" | Iron Law breach (roots surfaced). Surface the unresolved root with its profile.md value AND continue with the rest of the sweep. Partial sweeps are fine; silent partial sweeps are not. |
| "The aggregator failed for one project (frontmatter parse error in its brief) — I'll re-try with a relaxed parser" | The aggregator already logs the failure (line 113-114 of decay.py) and skips that project. Surface the failure in `projects_failed` with reason; do NOT silently swallow OR re-parse. |
| "The stale-threshold is 90 days but I think 30 is more useful — I'll override" | The threshold is set by the routine (line 70 of actions.py: `STALE_AFTER_DAYS = 90`). Surface the threshold in `thresholds_applied` of the response header; do NOT silently change it. If the operator wants 30-day staleness, that's a future flag, not a silent drop. |

## Red Flags

> Quoted internal monologue that signals a violation. If you catch
> yourself thinking any of these, stop and re-read the relevant stage.

- *"12 overdue rows came back, I'll list the top 3 with their titles — operator can click through if they want more"* — Iron Law breach (provenance complete clause). Every row in the surfaced list must include the source path; the operator's "click through" path is the path string itself. Surface 3-5 rows fully (project · title · source_file:line · age), not 3 titles.
- *"`Projects/SomeOldDeal/` came back with 47 overdue actions — that's an archive project, I'll skip it"* — wrong. The walker discovered it; surface it. If the operator wants archived projects excluded, that's a future filter knob (e.g. read `00 Brief.md` `status: closed`), not a silent drop. A dead project's actions ARE stale; that's the truth.
- *"One of the external_project_paths is unreachable (disk dismounted), I'll quietly skip and not mention"* — Iron Law breach (roots surfaced clause). Surface the unresolved root explicitly with its profile.md value so the operator knows the sweep was incomplete. A silent skip gives false confidence.
- *"The sweep took 8 seconds because there are 31 projects — I'll cache the result for the next call"* — no caching at the SKILL layer. The cron caches; the SKILL fire is on-demand and must show the current state. (The dashboard's #69 rollup endpoint has its own cache; the SKILL is upstream of that.)
- *"3 of 12 overdue rows look like duplicates (same title, different project) — I'll dedupe"* — DON'T. The aggregator dedupes WITHIN a project by `(source_file, task_hash)` (line 396 of actions.py); cross-project dupes are LEGITIMATE (the same action assigned to two deals is two rows). Surface as-is.
- *"The aggregator raised on one project — I'll re-throw and abort the whole sweep"* — the routine's design is partial-success-tolerant: it logs + skips the failing project (lines 113-115 of decay.py). The route surfaces the failure in `projects_failed` so the operator knows; the other projects' rows still surface. Aborting the whole sweep would be a regression.

## Anti-Patterns

- Collapsing the two buckets into one count ("20 actions to triage") —
  overdue and stale are DIFFERENT failure modes; surface them
  separately so the operator can decide which sleeve to attack first.
- Filtering by owner / project / "looks like a stub" — the routine
  returns the full list; Anton surfaces, operator filters.
- Auto-toggling a row to done as a side effect — the operator decides
  what's actually done; this skill READS, the toggle endpoint WRITES.
- Reporting "0 overdue, 0 stale" without confirming `projects_scanned`
  > 0 AND `roots_resolved` covers the operator's expected paths — an
  empty result on a misconfigured profile (zero external paths
  resolved) means the sweep was vacuous, not that nothing is overdue.
- Inventing a threshold the routine doesn't use — surface
  `STALE_AFTER_DAYS = 90` verbatim in `thresholds_applied`. If the
  operator wants 30, that's a future flag, not a silent reinterpretation.
- Dropping `task_hash` from the surfaced row "for brevity" — the
  task_hash is the only stable identifier the toggle endpoint can match
  after a line-number shift. Always surface.

## Example — "what's overdue?" (worked end-to-end)

Single realistic example. One good example beats five half-examples.

**Operator input:** "Quick sweep — anything I've let slip?"

**Stage 1 — Anton invokes the routine:** POST `/api/workflows/actions-decay`
with `{}` (defaults: vault = configured `AGENTIC_VAULT`, today =
`date.today()`, format = both).

**Stage 1 — Anton's chat line:** "Scanned 11 projects across 2 roots
(vault `Projects/` + 1 external). **12 overdue, 8 stale.** Active
threshold: stale ≥ 90 days untouched, no due date. 1 project
(`BadBrief`) failed (frontmatter parse error on 00 Brief.md); rest
walked clean."

**Stage 2 — Anton's spotlight bubble:** "Top 5 overdue —

  * **DemoDeal** · Send NDA to Heartwood Collection · due 2026-05-15
    (16d over) · `<workspace-root>/Projects/DemoDeal/02 Meeting Notes/2026-05-10.md:47`
    · hash a3f8d12b · owner Operator
  * **Heartwood** · Follow up on counter-bid · due 2026-05-18 (13d over)
    · `Projects/Heartwood/03 Term Sheet/notes.md:22` · hash 7c1b4e9d
    · owner Operator
  * **Falcon** · Send updated IM to second-round bidders · due
    2026-05-20 (11d over) · `Projects/Falcon/05 IM Drafts/v3.md:88`
    · hash b7e1c4d9 · owner Operator
  * **DemoDeal** · Schedule follow-up call with founder · due
    2026-05-22 (9d over) · `Projects/DemoDeal/00 Brief.md:67`
    · hash 4e9a1c8f · owner Operator
  * **Heartwood** · Send revised LOI · due 2026-05-25 (6d over) ·
    `Projects/Heartwood/03 Term Sheet/loi-v2.md:14` · hash 9d3f7a21
    · owner (unset)

…and 7 more overdue + 8 stale in the full JSON. Open the response
payload for the rest.

Roots resolved: `vault://Projects/`,
`<workspace-root>/Projects/`. Profile declares 2 external paths;
both resolved cleanly."

**Final output bubble** — a short lead ("12 overdue dominated by
DemoDeal + Heartwood — both deals in active sleeve"), the headline
pair (12 overdue / 8 stale across 11 projects, 1 failed), and chips
(Open DemoDeal overdue · Open Heartwood overdue · Re-fire after
toggle).

## When Stuck

| Symptom | Diagnostic |
|---|---|
| `scan` returns 0 overdue + 0 stale on a real vault | Cross-check `projects_scanned` > 0 AND `roots_resolved` covers the expected paths. An empty result on a misconfigured profile (zero external paths resolved) means the sweep was vacuous. |
| Same action flagged in 2 projects | Cross-project dupes are LEGITIMATE — the same action assigned to two deals is two rows. Surface both with their respective `project` field; don't dedupe. |
| `source_line` reported as 0 or negative | Routine bug — file with the action couldn't be re-read after enumeration. Surface verbatim + flag. |
| `roots_resolved` shorter than expected | Compare `len(roots_resolved) + len(roots_unresolved)` to `len(profile.external_project_paths) + 1` (vault). If equal, all paths were processed; the missing ones are in `roots_unresolved`. If less, the route wrapper has a bug — surface the discrepancy. |
| `projects_failed` lists a project with no `reason` | Route-wrapper bug. The reason should always be populated (e.g. "frontmatter parse error on 00 Brief.md" or "exception: <e>"); a None reason means the wrapper isn't carrying through the routine's log line. |
| A `done`-status row appears in overdue or stale | Routine drift. The status-mapping layer (lines 312-319 of actions.py) should never emit `done` to either bucket. Refuse the response + surface the row + flag. |
| The sweep raises mid-walk | Iron Law: a partial sweep is NOT a clean report. The route should surface as 500 with the exception detail — typically a malformed YAML on one project's `00 Brief.md`. Operator opens the file, fixes the YAML, re-fires. |

## Output Contract

The route returns JSON:

```json
{
  "status": "ok",
  "run_id": "<8-hex audit id>",
  "today": "2026-05-31",
  "thresholds_applied": {
    "stale_after_days": 90
  },
  "roots_resolved": [
    "vault://Projects/",
    "<workspace-root>/Projects/"
  ],
  "roots_unresolved": [
    {"profile_path": "/mnt/y/Old External", "reason": "directory does not exist"}
  ],
  "projects_scanned": ["DemoDeal", "Heartwood", "Falcon"],
  "projects_failed": [
    {"project": "BadProject", "reason": "frontmatter parse error on 00 Brief.md"}
  ],
  "overdue": [
    {
      "project": "DemoDeal",
      "title": "Send NDA to Heartwood Collection",
      "status": "overdue",
      "due": "2026-05-15",
      "owner": "Operator",
      "urgent": true,
      "flag": false,
      "source_file": "<workspace-root>/Projects/DemoDeal/02 Meeting Notes/2026-05-10.md",
      "source_line": 47,
      "task_hash": "a3f8d12b"
    }
  ],
  "stale": [
    {
      "project": "Falcon",
      "title": "Review IM draft",
      "status": "stale",
      "due": null,
      "owner": "Operator",
      "urgent": false,
      "flag": false,
      "source_file": "Projects/Falcon/05 IM Drafts/v3.md",
      "source_line": 12,
      "task_hash": "b7e1c4d9"
    }
  ],
  "counts": {
    "overdue": 12,
    "stale": 8,
    "projects_scanned": 11,
    "projects_failed": 1
  },
  "duration_ms": 240
}
```

**No file write** — pure-return data, same shape as bd-decay. Audit row
to `runs/tool.actions-decay.jsonl` (via central hook stack; #60
substrate).

**What the route does NOT produce** (so the skill does not report it):
a historical diff vs the previous sweep, auto-toggle of any row, a
per-project sentiment score, or a synthesised "what to do next" plan.
The list is review-only.

## Citations Required

Every row carries its source file + line + task_hash as the citation.
The `source_file` path IS the citation — the operator clicks through to
verify.

| Field | Required source type | Acceptable form |
|---|---|---|
| `source_file` | The .md file containing the action line | Absolute path; never a vault-relative path here because the external trees ARE outside the vault and need to be unambiguously addressable |
| `source_line` | The 1-indexed line number | Integer (hint; the toggle endpoint re-verifies via task_hash) |
| `task_hash` | The stable identity of the task | 8-char sha1 of the normalised title (see `_hash_title` in actions.py) |
| `project` | The discovered project name | String; matches `projects_scanned`; first-seen canonical casing retained |
| `roots_resolved` (response-level) | The paths the walker actually visited | List of strings; `vault://Projects/` for the vault path; absolute paths for each external root |
| `roots_unresolved` (response-level) | Profile entries that didn't resolve | List of `{profile_path, reason}` records |
| `projects_failed` (response-level) | Per-project aggregator exceptions | List of `{project, reason}` records |

## Cost Envelope

The frontmatter declares `cost_ceiling_tokens: 1500` and
`cost_ceiling_seconds: 60`. Both are read from frontmatter by
`get_active_skill_cap("actions-decay", "tokens" | "seconds")`; the
per-skill caps are enforced by the central hook stack (#61/#67).

**Where the budget goes** (entirely Anton's narration — the routine is
zero LLM):
- Routine: ~0 tokens (pure Python file-walk + frontmatter parse + date
  arithmetic). Wall-clock 1-5s typical, 10-30s on slow disk or 50+
  projects.
- Anton narration: ~800-1200 tokens (surface counts + thresholds + 3-5
  spotlight rows per bucket + roots_resolved sanity-check + propose
  follow-up `/projects/<X>/actions/toggle` chip for the top overdue).
- Headroom: ~300 tokens for one guardrail retry.

**The 1500 token / 60s ceiling vs prior skills:**
       sector-news       → 6000 / 300s  (per-item LLM loop)
       LBO               → 8000 / 90s   (engine subprocess)
       equity-research   → 4000 / 120s  (provider + Companies write)
       deal-tracker      → 3000 / 60s   (single Ollama call)
       vault-health      → 2000 / 60s   (pure file-walk, write report)
       recall-query      → 1500 / 60s   (hybrid retrieval, no writes)
       bd-decay          → 1500 / 30s   (pure file-walk, no write, no LLM)
       actions-decay     → 1500 / 60s   (pure file-walk + external-tree, no write, no LLM)

Ties bd-decay + recall-query for smallest token ceiling. 60s wall-clock
(vs bd-decay's 30s) is the only delta — the external-tree walk on a slow
disk can take a few seconds longer than the in-vault Companies/ sweep.

> **Calibration status:** 1500 tokens / 60s are first-pass estimates.
> Likely TIGHTER than 1500 once real data lands (operator narration is
> reliably short on bucket sweeps). Recalibrate to `1.25 × observed`
> after the first few narrated runs.

## Verification Checklist (before declaring done)

- [ ] The sweep returned a `DecaySweep` (no exception raised mid-walk; route surfaces 500 on raise)
- [ ] Every row in `overdue` + `stale` has non-null `project`, `source_file`, `source_line`, `task_hash` (Iron Law clause 1)
- [ ] `roots_resolved` enumerates the vault `Projects/` + every existing external root (Iron Law clause 2)
- [ ] `roots_unresolved` carries any profile entry that didn't resolve, with reason (Iron Law clause 2)
- [ ] `projects_failed` carries any project whose aggregator raised, with reason (no silent swallow)
- [ ] No `done`-status row appears in `overdue` or `stale` (Iron Law clause 3)
- [ ] `thresholds_applied.stale_after_days` echoes `STALE_AFTER_DAYS = 90` (operator-verifiable)
- [ ] The two buckets are NOT collapsed (overdue and stale stay distinct)
- [ ] Audit row exists in `runs/tool.actions-decay.jsonl` with `status: "ok"`
- [ ] No row dropped for being a cross-project dupe (legitimate; surface as-is)
- [ ] Final chat bubble surfaces 3-5 spotlight rows per bucket with `source_file:line` for click-through
