---
name: vault-health
description: |
  Use when running a vault freshness / orphan-link / speculation sweep — scan
  Sectors/<X>/*.md for stale claim files (90-day warning, 180-day auto-bump),
  walk the vault for orphan wikilinks, and (when implemented) check
  `(speculation, YYYY-MM-DD)` expiry markers. Triggers: vault health, vault
  sweep, stale claims, orphan wikilinks, freshness check, "is anything stale",
  weekly vault audit. Inputs: vault path (defaults), optional `--today`
  override for testing, report format. Output: a markdown report at
  Routines/vault-health/<date>-<kind>.md (status: pending-review).
version: 0.1.0
license: proprietary
allowed_tools:
  - vault_read
  - vault_write
capabilities:                        # #61-capabilities
  vault_read:  ["Sectors/**", "Companies/**", "People/**", "Projects/**", "Routines/**", "_claude/**"]   # whole vault — orphan-link scan walks everything
  vault_write: ["Routines/vault-health/**"]                # only its own report dir
  fs_roots:    []                                          # vault-only; no fs writes outside the vault
  network:     []                                          # internal tier, but no external endpoints needed (zero-network sweep)
metadata:
  sensitivity: internal        # vault metadata sweep — NOT confidential (no deal data leaves the vault)
  workspace_scope: any         # workspace-independent — the sweep is vault-global
  tile_label: "Vault Health"
  cost_ceiling_tokens: 2000    # tiny — pure file-walk + frontmatter parse; no LLM calls in the routine today
  cost_ceiling_seconds: 60     # both sweeps complete in seconds on a ~1k-note vault
  guardrails:
    - report_written           # status="ok" + output_path set
    - sweep_completed          # no partial sweeps (the routine doesn't retry — either it finishes or it errors)
  guardrail_max_retries: 1
---

<!--
§14 FLEX NOTES (vault-health vs sector-news — the third migrated skill)

1. Iron Law is ENUMERATION-HONESTY, not sourcing (sector-news) or a numeric
   gate (LBO). The routine is deterministic file-walk + frontmatter parse; no
   LLM, no network. The load-bearing failure mode is silent over-count
   (`links.py` regex change flags every footnote as an orphan) or silent
   under-count (a frontmatter parser bug skips a malformed `.md`). A clean
   sweep that completed != a clean sweep that ran. Anton's job is to surface
   counts honestly and flag any non-zero error field — never narrate a
   partial walk as a pass.

2. `capabilities.network: []` — first skill to genuinely have ZERO external
   network surface (sector-news declares Firecrawl + Tavily; LBO declares
   `[]` for confidentiality; vault-health declares `[]` because it doesn't
   NEED any). The validator's confidential-⇒-no-network cross-check passes
   vacuously here (the tier is `internal`); the empty list is the
   declarative form of "this routine genuinely never reaches out".

3. Cost envelope an ORDER OF MAGNITUDE lower than prior skills (2000 tokens
   / 60s vs sector-news 6000/300, LBO 8000/90). Routine-backed read-only
   sweeps with no LLM calls are the cheapest skill shape. The 2000 token
   ceiling is headroom for any future narration loop Anton wraps the routine
   in; the routine itself uses zero tokens today.

4. Output Contract is a markdown REPORT (operator-actionable, status:
   pending-review) rather than a SYNTHESIS (sector-news newsletter) or
   DELIVERABLE (LBO XLSX). Three distinct deliverable shapes now exercised
   across the migrated trio. Same-day rerun overwrites in place (no
   `00. OLD/` — these are regenerable, mirroring sector-news).

5. `vault_read` covers the WHOLE vault (Sectors + Companies + People +
   Projects + Routines + _claude), not a sector subset — because `links.py`
   walks every `.md` to extract `[[wikilinks]]`. `freshness.py` only reads
   Sectors/; the union is the vault. `vault_write` is strictly
   `Routines/vault-health/**` — the routine's own report dir.

6. No `captures_to_vault:` block — this skill writes its OWN deliverable
   (the report), not a derived semantic fact (cf. LBO captures returns to
   the Company note). The report path IS the operator-actionable surface.

7. The route is for ON-DEMAND operator fires (dashboard tile + Cmd-K). The
   existing CLI + cron jobs (`vault-health-freshness` Mon 08:00,
   `vault-health-links` Mon 08:30 — see routines/scheduler/jobs.py) are
   untouched and continue to run the CLI directly. The skill descriptor
   governs the on-demand surface only.

8. Central guard (#61): the route flows through `tool_call_hooks` so
   `enforce_skill_sensitivity` is on the path — but for an `any`-scope,
   non-MNPI skill the guard is a structural NO-OP (nothing to refuse).
   Wiring the route's tool_name to the skill name only adds audit
   recognition, not gating.
-->

# Vault Health

## Overview

Drives the existing `vault-health` routine (`routines/vault_health/`) — three
deterministic sweeps of the vault that surface decay before it spreads. The
routine walks `Sectors/<X>/*.md` for stale claim files (`freshness`), walks
every `.md` for unresolvable `[[wikilinks]]` (`links`), and (stub today)
checks `(speculation, YYYY-MM-DD)` expiry markers (`speculation`). Each sweep
emits a structured `ScanResult` (list of dataclasses) which the routine
renders as markdown and writes to `<vault>/Routines/vault-health/`. **Anton's
job is to invoke the routine, verify it completed cleanly, surface the report
path + headline counts + 3-5 spot-check rows, and flag the operator** —
Anton does not re-summarise the report (the markdown IS the deliverable;
rewriting it strips the per-file path detail the operator needs to action).

The routine makes ZERO LLM calls and ZERO network requests today — it's
pure file-walk + frontmatter parse + regex match. The cost envelope
(2000 tokens / 60s) is headroom for a future narration loop, not current usage.

## When to Use

**Mandatory triggers** — fire the skill, do not just discuss:
- Operator types `/vault-health` (Cmd-K or composer)
- Operator clicks the Vault Health drawer tile
- The Mon 08:00 / 08:30 cron fires `vault-health-freshness` /
  `vault-health-links` (the scheduler owns this; the SKILL.md governs
  on-demand fires only — see Output Contract)

**Optional triggers** — propose firing the skill, ask first:
- Operator asks "is anything stale in the vault?"
- Operator asks "any broken wikilinks?" / "show me the orphans"
- Operator asks "weekly vault audit" mid-week (cron data goes stale by Wed)

**Don't use** — refuse and explain why:
- Operator wants to scan a SINGLE note or a single sector subset — this is a
  vault-global sweep; for a single-file check, just open the file. The
  routine has no `--scope` flag and inventing one is out of contract.
- Operator wants a HISTORICAL view ("how has the orphan count trended?") —
  the routine writes one report per run; cross-run diffing is a separate
  skill (not yet shipped). Surface the current sweep + note that historical
  diffing isn't available.
- Operator wants the routine to AUTO-FIX orphans / auto-bump confidence —
  the routine SURFACES, the operator ACTIONS. The report's `status:
  pending-review` is the operator's review queue.

## The Iron Law

> **NO REPORT IS SURFACED WITHOUT A WRITTEN STATUS LINE PER SWEEP. A SWEEP
> THAT EXITS WITH ERRORS OR PARTIAL RESULTS IS NEVER NARRATED AS A CLEAN
> PASS.**

This is non-negotiable and sits on top of the universals at
[universal-iron-laws](<vault>/CLAUDE.md#universal-iron-laws).
Softer than LBO's S&U-tie (no numeric gate) and softer than sector-news'
sourcing law (no synthesised content here). The load-bearing discipline is
**enumeration honesty** — a report that says "0 stale claims" must mean the
sweep COMPLETED across every file, not that it skipped a malformed file mid-
walk. If `freshness.scan()` or `links.scan()` raises, the partial list is NOT
a clean report — surface the exception verbatim, do not paper over.

> **Routine-reality note (2026-05-29 baseline).** The routine never calls an
> LLM today; it's deterministic file-walk + frontmatter parse. The failure
> mode that matters is **silent over-counting** (a regex change in `links.py`
> that suddenly flags every footnote as an orphan) or **silent under-
> counting** (a frontmatter parser bug that skips a malformed `.md` without
> raising). The deterministic guarantee is that any unparseable file is
> LOGGED (see `freshness.py:65 log.warning` and `links.py:97 log.warning`)
> rather than crashing — Anton must check the routine's logs / `outputs`
> shape for non-zero skip counts before reporting "0 stale". Flag if you'd
> phrase the Iron Law differently.

## Core Pattern — 3 sweeps (freshness + links + speculation)

The routine runs as a single call per sweep (`freshness.scan` /
`links.scan` / `speculation.scan`) or subprocess
(`python -m routines.vault_health.cli {freshness|links|speculation|all}`).
Each sweep is a deterministic pass; there are no internal STOP gates because
each pass either completes or raises. The verification checkpoints Anton
applies are POST-RUN sanity checks on the returned counts + emitted report.

### Sweep 1 — Freshness

- Walks `<vault>/Sectors/<X>/*.md` (skips dirs starting with `_` and the
  view files `_Index.md` / `BD.md` / `People.md`).
- For each file with `type: sector-claim` frontmatter, parses
  `last_refreshed:` and computes `days_since_refresh = today - last_refreshed`.
- Classifies: `warning` (≥90 days, no auto-action) / `auto-bump` (≥180 days,
  suggests confidence bump-down via `_bump_down`).
- Returns `list[StaleClaim]` (one dataclass per stale file).
- **Sanity check.** Confirm the routine emitted a count, not an exception.
  A vault with 500+ Sectors notes returning 0 stale is suspicious — log-grep
  for `freshness: failed to read` warnings before reporting "all fresh".

### Sweep 2 — Links

- Walks EVERY `.md` under the vault (skipping `.obsidian/`, `.trash/`,
  `.recall-index/`, `_Trackers/` directories and `Templates/` / `_template/`
  unless `include_templates=True`).
- Builds a `set[str]` of every existing note's full relative path + basename
  (lowercase, Obsidian-style resolution).
- Walks again; for each line outside a fenced code block or inline-code
  span, runs the `_WIKILINK_RE` regex; for each `[[target]]` not in the
  existing set and not matching a placeholder pattern
  (`_PLACEHOLDER_TARGETS` / `_PLACEHOLDER_PATTERNS`), records an
  `OrphanLink(source_path, target, line_number)`.
- Returns `list[OrphanLink]`.
- **Sanity check.** A count that's an order of magnitude higher than last
  week is usually a regex change, not a sudden vault collapse — surface the
  delta + flag the routine for tuning, don't filter the noise yourself.

### Sweep 3 — Speculation (stub)

- Returns `[]` today. The full implementation is deferred (see
  `routines/vault_health/speculation.py` docstring).
- **Surface the stub status explicitly.** Hiding it is worse than
  acknowledging it — the operator needs to know speculation expiry tracking
  doesn't exist yet.

### Verification Anton applies before surfacing

1. Each invoked sweep returned a list (no exception).
2. Counts are coherent: non-negative days, no `None` paths, no duplicate
   orphan rows (mechanical: `len(set(o.source_path, o.target, o.line_number)
   for o in orphans) == len(orphans)`).
3. If `--write` was on and the count was non-zero, the report exists at
   `<vault>/Routines/vault-health/<date>-<kind>.md` with `status:
   pending-review` in frontmatter.
4. Surface the report path + the headline counts + 3-5 spot-check rows;
   do NOT dump the full report inline (the operator opens the .md file for
   the long list).

## Quick Reference

```
operator types /vault-health         (or clicks Vault Health tile / Mon cron fires)
  ↓
route fires routines.vault_health.cli via tool_call_hooks (before_tool_call stack)
  ↓  (enforce_skill_sensitivity present but a NO-OP: workspace_scope=any, sensitivity≠MNPI)
Sweep 1 freshness.scan(vault, today): walks Sectors/<X>/*.md, returns list[StaleClaim]
  ↓
Sweep 2 links.scan(vault): walks every .md, returns list[OrphanLink]
  ↓
Sweep 3 speculation.scan(vault): stub today, returns []
  ↓
Anton verifies each sweep returned cleanly (no exception); counts coherent     [enumeration honesty]
  ↓
report(s) land at
  <vault>/Routines/vault-health/<YYYY-MM-DD>-<kind>.md                         [side effect]
audit row written to runs/vault-health-{freshness,links}.jsonl                 [hook side effect]
```

## Common Rationalizations

> **Append-only** by the operator after each production run that surfaces a
> new shortcut (CLAUDE.md §14.3). The 5 rows below are the v1 seed for a
> routine-backed read-only sweep skill.

| Rationalization | Reality |
|---|---|
| "This runs on a cron, the on-demand fire is the same data" | The cron runs Mon 08:00 / 08:30 only — operator pulls on demand mid-week mean the cron data is stale by Wed. Re-fire on demand. |
| "0 stale claims means the vault is healthy" | 0 stale claims means the SWEEP completed AND the existing `last_refreshed:` frontmatter on each claim file is current. It does NOT mean nothing is wrong (orphan links is a separate sweep; speculation expiry is a third). Run `all`, not just `freshness`, when the operator says "health check". |
| "Orphan-link count looks high, but most are just old meeting-note refs" | The routine doesn't classify orphans by age — that's the operator's call to make per-orphan. Don't pre-filter; surface the full list. |
| "speculation is a stub, just report 'TODO'" | Surface the stub status with a one-line "speculation sweep not yet implemented; tracked at #54a / `routines/vault_health/speculation.py`" so the operator knows this is a known gap, not a routine failure. |
| "The report wrote, status was 'ok', that's all that matters" | `status: "ok"` means the FILE wrote. It does NOT mean the sweep results are interesting. Read the counts; if all three sweeps returned 0 in a vault with 500+ notes, that's suspicious — sanity-check before reporting "all clear". |

## Red Flags

> Quoted internal monologue that signals a violation. If you catch yourself
> thinking any of these, stop and re-read the relevant sweep section.

- *"freshness returned 0, so the vault is healthy"* — confirm the scan COMPLETED (no exception caught mid-walk) before reporting 0. A skipped-due-to-parse-error file looks identical to "fresh enough" in the output.
- *"links flagged 200 orphans, that can't be right — I'll filter the noisy ones"* — the routine's job is enumeration, Anton's job is surfacing. Don't hide signal. If 200 is genuinely too noisy, that's a routine tuning issue (raise a `#vault-health-tuning` follow-on), not an Anton filter.
- *"the report is ugly, I'll re-summarise it in prose"* — the markdown report IS the deliverable; rewriting it strips the per-file path detail the operator needs to action.
- *"speculation sweep is a stub, I'll skip mentioning it"* — surface the stub status explicitly. Hiding it is worse than acknowledging it.
- *"I'll merge this week's report into last week's so the operator sees a diff"* — the routine writes one report per run; cross-run diffing is a separate skill (not yet shipped). Don't invent.

## Anti-Patterns

- Re-summarising the markdown report inline in chat instead of surfacing
  the report path + headline counts — the per-file `[[wikilinks]]` are the
  operator-actionable detail.
- Filtering orphans by your own heuristic ("this looks like a meeting note,
  probably intentional") — the routine's `_PLACEHOLDER_TARGETS` /
  `_PLACEHOLDER_PATTERNS` are the placeholder filter; everything else is
  signal.
- Reporting "all clear" when only one sweep ran — `freshness == 0` does NOT
  imply `links == 0` or `speculation == 0`. Use the `all` subcommand or
  invoke all three.
- Auto-bumping confidence in a claim file as a side effect — the
  `auto-bump` classification is a SUGGESTION; the operator edits the
  frontmatter, not the routine.
- Inventing a speculation count when the sweep is a stub — the stub returns
  `[]`; reporting any non-zero count would be fabricated.
- Hand-fixing an orphan wikilink Anton encounters during the sweep — the
  report's `status: pending-review` is the operator's queue; Anton surfaces,
  operator actions.

## Example — "weekly vault audit, please" (worked end-to-end)

Single realistic example. One good example beats five half-examples.

**Operator input:** "Weekly vault audit — run all three sweeps and show me
the headline."

**Sweep 1 — Anton invokes the routine:** `python -m routines.vault_health.cli
all --vault <vault>` (or direct in-process call to
`freshness.scan(vault, today=date.today())`).

**Sweep 1 — Anton's chat line:** "Freshness: scanned `Sectors/<X>/*.md`,
**12 stale** (3 auto-bump ≥180d, 9 warning ≥90d); report wrote to
`Routines/vault-health/2026-05-29-freshness.md` (status: pending-review).
Sweep completed cleanly (no parse warnings)."

**Sweep 2 — Anton's chat line:** "Links: scanned every `.md`, **47 orphans
across 18 files**; report wrote to
`Routines/vault-health/2026-05-29-orphan-links.md`. Spot-check: 12 in
`Sectors/Travel/_Index.md` (likely a renamed claim file), 8 in `Companies/`
notes (probably stub-never-written). Full list in the report."

**Sweep 3 — Anton's chat line:** "Speculation: stub (`routines/vault_health/
speculation.py:36` — returns empty). Tracked at #54a; not yet implemented."

**Final output bubble** — a short lead ("3 sweeps; the freshness queue is
the live one — 3 auto-bumps need either a refresh or a confidence
demotion"), the headline counts (12 freshness / 47 orphans / 0 speculation),
and chips (Open freshness report [the .md] · Open orphans report [the .md] ·
speculation: stub).

## When Stuck

| Symptom | Diagnostic |
|---|---|
| `freshness.scan` returns 0 but the vault has 500+ Sectors notes | Grep the routine log for `freshness: failed to read` or `unparseable last_refreshed` warnings — a malformed YAML frontmatter is silently skipped. Fix the file or surface the count of skipped files alongside the "0 stale" report. |
| `links.scan` returns a sudden 10x jump in orphans | Almost always a regex change in `_WIKILINK_RE` or `_PLACEHOLDER_PATTERNS`, not a vault collapse. Compare against the previous report; surface the delta + flag for routine tuning. |
| Report doesn't write despite the count being non-zero | Confirm `--write` wasn't passed `--no-write`. Confirm `<vault>/Routines/vault-health/` is writable (the dir is auto-created by `atomic_write`). Audit row in `runs/vault-health-{kind}.jsonl` should show `status: "ok"`. |
| Same orphan flagged across many files | Likely a renamed/deleted note that was widely cross-referenced. Action: either restore the note OR run vault-wide find-and-replace on the `[[wikilink]]` — both are operator decisions, not routine fixes. |
| Auto-bump suggested for a file you just refreshed | The frontmatter `last_refreshed:` wasn't updated when the content was — operator-side discipline; fix the frontmatter, re-run. |
| `speculation` sweep returns `[]` always | That's the current contract — it's a stub. See `routines/vault_health/speculation.py` for the decision points blocking full implementation (dating convention + operator workflow + edit semantics). |

## Output Contract

The report lands at:

```
<vault>/Routines/vault-health/<YYYY-MM-DD>-<kind>.md
```

where `<kind>` is one of `freshness` | `orphan-links` | `speculation` (note
`orphan-links`, not `links`, in the filename — the CLI hardcodes this) |
`all` (the `all` subcommand prints to stdout per-sweep and writes each
sweep's report file independently; there is no combined `all` file today).

Filename: `<date>-<kind>.md`, where `<date>` is `date.today().isoformat()`.
The file is written via `atomic_write` (temp + rename); a same-day rerun
overwrites in place (no `00. OLD/` archive — these are regenerable, like
sector-news; unlike LBO).

**Markdown frontmatter the routine stamps** (`freshness.render_report` and
`links.render_report`):

| Key | Value |
|---|---|
| `type` | `vault-health-report` |
| `report_kind` | `freshness` | `orphan-links` |
| `sensitivity` | `internal` (matches this skill's tier) |
| `date` (freshness only) | ISO date |
| `stale_count` / `auto_bump_count` / `warning_count` (freshness) | Integers |
| `orphan_count` / `affected_files` (links) | Integers |
| `status` | `pending-review` (operator-actionable) |
| `tags` | `[vault-health, <kind>, routines]` |

**Return shape** (what the route returns, mirroring the LBO `JobStarted` /
direct-call patterns — direct call when fast, subprocess when slow):

```python
VaultHealthResult(
    status="ok",                  # "ok" | "error"
    kind="freshness",             # "freshness" | "links" | "speculation" | "all"
    output_path=Path(".../Routines/vault-health/2026-05-29-freshness.md"),  # None if no findings
    counts={
        "stale": 12,              # freshness only
        "auto_bump": 3,
        "warning": 9,
        # OR for links:
        # "orphans": 47, "affected_files": 18,
        # OR for speculation:
        # "markers": 0,
    },
    duration_ms=2400,
    error=None,                   # str on status="error"
)
```

**Side effects:** an audit row to `runs/vault-health-{freshness|links}.jsonl`
(written by the routine's own `audit.write_structured` call inside the CLI;
the route's `tool_call_hooks` adds `runs/tool.vault-health.jsonl` on top).

**What the routine does NOT produce** (so the skill does not report it): a
historical diff vs the previous sweep, a per-orphan classification (stub vs
typo vs renamed), or an auto-fix payload. The report is review-only.

## Citations Required

Vault-health is a vault-internal sweep — it cites NOTHING external. The
report's `[[wikilinks]]` ARE the citations: every flagged file is named by
its vault-relative path so the operator can open it directly. There is no
source-URL contract here (cf. sector-news, where every claim cites a fetched
item).

| Field | Required source type | Acceptable form |
|---|---|---|
| Each `StaleClaim.path` | Vault file path (Sectors/<X>/<file>.md) | `[[<vault-relative-path>]]` wikilink in the markdown report |
| Each `OrphanLink.source_path` | Vault file path (the file containing the orphan) | `## <vault-relative-path>` heading + `L<n>: \`[[<target>]]\`` rows |
| Routine version / sweep ts | The audit row | `runs/vault-health-{kind}.jsonl` (`run_id`, `inputs`, `outputs`) |

## Cost Envelope

The frontmatter declares `cost_ceiling_tokens: 2000` and
`cost_ceiling_seconds: 60`. Both are read from frontmatter by
`get_active_skill_cap("vault-health", "tokens" | "seconds")`; the per-skill
caps are enforced by the central hook stack (#61/#67).

**Where the budget goes** (entirely Anton's narration — the routine itself
is zero LLM):
- Anton's narration: surfacing + verifying the three sweep results
  (~500-1500 tokens depending on counts).
- File-walk + frontmatter parse: deterministic, ~1-3s on a ~1k-note vault
  (the cheapest routine in the repo per `jobs.py` line 67's comment).
- `atomic_write`: temp + rename, sub-second.

**The 2000 token / 60s ceiling vs sector-news' 6000/300:** vault-health is
the cheapest skill shape — pure file-walk + frontmatter parse + regex match,
ZERO LLM calls in the routine, ZERO network calls. 60s is generous headroom
for a multi-thousand-note vault; 2000 tokens is headroom for a future
narration loop that wraps the routine. Both ceilings will recalibrate after
the first real narrated production runs (cron runs don't produce token data
to calibrate against).

> **Calibration status:** 2000 tokens / 60s are first-pass estimates for the
> on-demand narration loop. The Mon cron runs the routine directly (no Anton
> narration), so it produces no token data to calibrate against. Recalibrate
> to `1.25 × observed` after the first real narrated runs.

## Verification Checklist (before declaring done)

- [ ] Each invoked sweep returned a list (no exception raised mid-walk)
- [ ] Counts are coherent (non-negative days, no `None` paths, no duplicate orphan rows)
- [ ] If counts were non-zero, the report exists at `<vault>/Routines/vault-health/<date>-<kind>.md`
- [ ] Report frontmatter declares `status: pending-review` + `report_kind: <kind>`
- [ ] No parse warnings (`freshness: failed to read` / `links: failed to read`) were silently swallowed
- [ ] Audit row exists in `runs/vault-health-{freshness|links}.jsonl` with `status: "ok"`
- [ ] No orphans were filtered by Anton's own heuristic (the routine's placeholder filter is the only filter)
- [ ] Speculation sweep status is surfaced (even if it's "stub, returns []")
- [ ] Final chat bubble surfaces the report PATH + headline counts + 3-5 spot-check rows, NOT a re-summary of the full report
- [ ] No auto-fix attempted — the report's `status: pending-review` is the operator's queue
