---
name: bd-decay
description: |
  Use when surfacing stale BD outreach — walk Companies/<X>.md for
  bd_state + bd_last_contact frontmatter, compute days since last contact,
  and flag entries past the per-state decay threshold. Triggers: BD decay,
  stale BD, "who haven't I contacted in a while", BD pipeline freshness,
  morning brief BD section, business development sweep. Inputs: vault path
  (defaults), optional `--today` override for testing, output format. Output:
  a markdown / JSON list of stale BD entries grouped by company state
  (watching / engaged / dormant) with days-since-contact + owner. Sticky
  states (dead / won / lost) never decay and are excluded.
version: 0.1.0
license: proprietary
allowed_tools:
  - vault_read
capabilities:                        # #61-capabilities
  vault_read:  ["Companies/**"]          # ONLY Companies/ — the routine walks nothing else
  vault_write: []                        # pure read; the routine returns data, does not write a report
  fs_roots:    []                        # vault-only
  network:     []                        # zero LLM, zero network — pure file-walk + date arithmetic
metadata:
  sensitivity: internal        # Companies/ frontmatter is operator-internal context; not confidential per-company
  workspace_scope: any         # firm-wide BD sweep, workspace-independent
  tile_label: "BD Decay"
  cost_ceiling_tokens: 1500    # smallest skill so far — pure narration loop, no extraction/synthesis
  cost_ceiling_seconds: 30     # sub-second routine + brief narration
  guardrails:
    - scan_completed             # the routine walked successfully (no exception caught mid-walk)
    - thresholds_documented      # the active DECAY_THRESHOLDS are surfaced in the report
  guardrail_max_retries: 1
---

<!--
§14 FLEX NOTES (bd-decay vs prior 4 migrated skills)

1. Iron Law is TWO-STATE-HONESTY: stale entries and untracked entries are
   distinct buckets that MUST NOT be collapsed. The narrowest Iron Law
   shape yet — not sourcing (sector-news), not a numeric gate (LBO), not
   enumeration-completeness (vault-health), not extraction fidelity
   (deal-tracker). It's TAXONOMY FIDELITY: every company file lands in
   exactly one of three buckets — stale (both fields present, past
   threshold), fresh (both fields present, within threshold), untracked
   (one or both fields missing). The third bucket is NOT "stale".
   Surfacing it as stale generates phantom BD work for entries the operator
   never elected to track. The routine handles this correctly; Anton's job
   is to surface the three counts honestly, not collapse them.

2. Capabilities: NARROWEST surface yet. vault_read is exactly
   ["Companies/**"] — the routine reads NOTHING else. vault_write,
   fs_roots, network are all []. The declarative manifest demonstrates
   that capability granularity scales DOWN — a tiny skill declares a tiny
   surface, not a wildcard. Compare:
       sector-news   → Sectors/**, network: Firecrawl + Tavily
       LBO           → Projects/**, fs_roots: <workspace-root>/**
       vault-health  → whole vault read, Routines/vault-health/** write
       deal-tracker  → Projects/_Trackers/** read+write
       bd-decay      → Companies/** read only — nothing else
   The validator's confidential-⇒-no-network cross-check passes vacuously
   here (tier is `internal`); the empty network list is the declarative
   form of "this routine genuinely never reaches out".

3. NO WRITE SURFACE AT ALL — first skill that is pure-return data.
   Distinct from vault-health (writes its own report), deal-tracker
   (appends an Excel row), sector-news (writes a newsletter), LBO
   (populates an XLSX). Five distinct deliverable shapes now exercised
   across the migrated five:
       LBO          → populated XLSX (full template, COM-driven)
       sector-news  → markdown newsletter (synthesis)
       vault-health → markdown report (enumeration sweep)
       deal-tracker → single Excel row append + JSON status
       bd-decay     → pure-return JSON + rendered markdown (no file write)
   The dashboard / morning-brief consumer renders; the skill returns.
   Validates §14 contract is shape-independent.

4. LOWEST cost envelope yet (1500 tokens / 30s) vs vault-health 2000/60s,
   deal-tracker 3000/60s, LBO 8000/90s, sector-news 6000/300s. Routine is
   ZERO LLM, ZERO network, sub-second walk over a few hundred Companies/
   files. The 1500-token ceiling is headroom for Anton's brief narration
   loop ("12 stale entries; 3 urgent across [list of names]"); the routine
   itself uses zero tokens.

5. The ON-DEMAND skill governs the route fire only. The 06:30 morning-
   brief cron continues to call `bd.decay.scan` DIRECTLY (no skill
   dispatch) — it's a cron job, not an operator-triggered fire, and has
   its own iron laws (morning-brief's). This SKILL.md exists so the
   operator-pulled on-demand path ("show me my stale BD outreach") has a
   §14 descriptor + a Cmd-K-reachable route. The cron path is untouched.

6. Sticky-state handling. The routine excludes `dead` / `won` / `lost`
   companies from the stale list (DECAY_THRESHOLDS = -1 → sticky, never
   decays). This is by design — a "won" deal stays won; surfacing it as
   stale would be operator noise. Anton's report header MUST surface
   which states are sticky alongside the active thresholds, so the
   operator can sanity-check "stale" matches expectation.

7. Central guard (#61): the route flows through `tool_call_hooks` so
   `enforce_skill_sensitivity` is on the path — but for an `any`-scope,
   `internal`-tier skill the guard is a structural NO-OP for the common
   case. The only firing path is the cross-skill MNPI gate: if a caller
   somehow flags `workspace_sensitivity=MNPI` on the request, the guard
   refuses with `SkillScopeRefused` (mapped to HTTP 403). Tested.

8. No `captures_to_vault:` block — this skill is pure-return data, not a
   derived semantic fact (cf. LBO captures returns to the Company note).
   The stale-list IS the operator-actionable surface; nothing to capture
   back to the vault.
-->

# BD Decay

## Overview

Drives the existing `bd-decay` routine (`routines/bd/decay.py`) — a single
deterministic sweep of `Companies/<X>.md` that surfaces stale BD outreach
before it becomes phantom pipeline. The routine walks every Companies file
with a `bd_state` frontmatter field, parses the matching `bd_last_contact:`
date, computes `days_since_contact = (today - bd_last_contact).days`, and
classifies against the per-state `DECAY_THRESHOLDS` constant. Returns a
`list[StaleEntry]` (the routine's dataclass) which the route renders as
JSON + a markdown snippet via `format_stale_for_morning_brief()`. **Anton's
job is to invoke the routine, verify the scan completed cleanly, surface
the counts (stale / fresh / untracked) + the active thresholds + 3-5
spot-check rows, and flag the operator** — Anton does NOT collapse
"untracked" into "stale" (Iron Law).

The routine makes ZERO LLM calls and ZERO network requests — pure
file-walk + frontmatter parse + date arithmetic. The cost envelope
(1500 tokens / 30s) is headroom for Anton's narration loop, not current
routine usage.

## When to Use

**Mandatory triggers** — fire the skill, do not just discuss:
- Operator types `/bd-decay` (Cmd-K or composer)
- Operator clicks the BD Decay drawer tile
- The 06:30 morning-brief cron consumes `bd.decay.scan` directly (the
  cron owns this path; the SKILL.md governs on-demand fires only — see
  Output Contract)

**Optional triggers** — propose firing the skill, ask first:
- Operator asks "who haven't I contacted in a while?" / "show me stale
  BD" / "what's gone cold in the BD pipeline?"
- Operator asks "BD pipeline freshness check" mid-day (cron data is
  06:30; mid-day BD edits aren't reflected until next morning unless the
  on-demand path fires)
- Operator asks "weekly BD review" — appropriate on-demand fire

**Don't use** — refuse and explain why:
- Operator wants to scan a SINGLE company file — open it directly; the
  routine has no `--scope` filter and inventing one is out of contract.
- Operator wants to BUMP a `bd_last_contact:` date — the routine SURFACES,
  the operator ACTIONS. Bumping a date without an actual contact is
  fabrication.
- Operator wants AUTO-OUTREACH (draft an email to every stale entry) —
  that is `bd-outreach`, a separate (not-yet-shipped) skill. Surface the
  stale list + chips; do not synthesise outreach copy.
- Operator wants a HISTORICAL trend ("how has stale-count moved over
  weeks?") — the routine returns the current count; cross-run diffing is
  a separate skill (not yet shipped).

## The Iron Law

> **NO BD ENTRY IS REPORTED STALE UNLESS BOTH `bd_state` AND
> `bd_last_contact` FRONTMATTER FIELDS ARE PRESENT AND PARSE-VALID. A
> MISSING-FRONTMATTER ENTRY IS NOT THE SAME AS A STALE ENTRY.**

This is non-negotiable and sits on top of the universals at
[universal-iron-laws](<vault>/CLAUDE.md#universal-iron-laws).
Softer than LBO (no numeric gate to fail) and softer than sector-news (no
synthesised content). The load-bearing discipline is **two-state honesty**:
every Company file lands in exactly one of three buckets:

  * **stale** — both fields present, parse-valid, past threshold
  * **fresh** — both fields present, parse-valid, within threshold
  * **untracked** — one or both fields missing OR sticky state
    (dead/won/lost)

The third bucket is NOT "stale". Surfacing it as stale generates fake BD
work for entries the operator never elected to track — or for entries the
operator deliberately marked sticky (a "won" deal stays won). The routine
handles this correctly: `bd_state` missing → skip; sticky state → skip;
`bd_last_contact` missing → routine treats as immediately stale BUT marks
`bd_last_contact: "(unset)"` so the operator can audit. Anton's job is to
surface the three counts honestly, not collapse them.

> **Routine-reality note (2026-05-29 baseline).** The `DECAY_THRESHOLDS`
> constant in `routines/bd/decay.py` defines per-state day cutoffs:
> `watching: 180`, `engaged: 60`, `dormant: 90`, and `dead`/`won`/`lost`
> as `-1` (sticky, never decays). Anton MUST surface the active thresholds
> in the report header so the operator can sanity-check whether "stale"
> matches their current expectation. If `_claude/profile.md` ever overrides
> the thresholds (future per-operator config), surface the EFFECTIVE
> values, not the constants. Flag if you'd phrase the Iron Law differently.

## Core Pattern — 2 stages (scan → format)

The routine is single-shot, sub-second, deterministic. No internal STOP
gates because the walk either completes or raises.

### Stage 1 — Scan

`routines.bd.decay.scan(vault_root, today=None)` walks `Companies/<X>.md`:

  * Skips files without `bd_state` frontmatter (these are "untracked").
  * Skips files with sticky `bd_state` (dead / won / lost — threshold=-1).
  * For each remaining file, parses `bd_last_contact:` (ISO date).
    * If missing → `last_contact_str="(unset)"`, treated as immediately
      stale.
    * If unparseable → logged via `log.warning`, file skipped (routine
      counts this as "untracked" by virtue of skip; Anton flags any
      non-zero skip count alongside the report).
  * Computes `days_since_contact = (today - bd_last_contact).days`.
  * If `days_since_contact > threshold_days` → records a `StaleEntry`.

Returns `list[StaleEntry]`. Bounded by file count × frontmatter parse
latency; no LLM, no network. Sub-second on a typical 500-company vault.

### Stage 2 — Format

The route + Anton's narration surface:
  * The headline counts (`stale`, `untracked`, `fresh`, `scanned`).
  * The active `DECAY_THRESHOLDS` (so "stale" is interpretable without
    grepping `decay.py`).
  * The rendered markdown via `format_stale_for_morning_brief()` (sorts
    by `days_over` desc, caps at top 10 + "...and N more" tail).
  * 3-5 spot-check rows so the operator can sanity-check.

Anton's verification: confirm the routine's reported counts are coherent
(no negative `days_since_contact`, untracked count distinct from stale
count, no duplicate entries by `company_path`).

### Verification Anton applies before surfacing

1. The scan returned a list (no exception caught mid-walk).
2. `counts.stale + counts.fresh + counts.untracked == counts.scanned`
   (taxonomy fidelity — every scanned file went into exactly one bucket).
3. All `days_since_contact >= 0` (no future-dated `bd_last_contact:`).
4. The active `DECAY_THRESHOLDS` are surfaced in the report header.
5. Spot-check rows include `company_path` so the operator can click
   through to each Company note.

## Quick Reference

```
operator types /bd-decay         (or clicks BD Decay tile / asks "show me stale BD")
  ↓
route fires routines.bd.decay.scan via tool_call_hooks (before_tool_call stack)
  ↓  (enforce_skill_sensitivity present but a NO-OP: workspace_scope=any, sensitivity=internal)
Stage 1 scan(vault, today): walks Companies/<X>.md, returns list[StaleEntry]
  ↓
Stage 2 format_stale_for_morning_brief(stale): renders markdown (sorted by days_over desc, top 10 + tail)
  ↓
Anton verifies scan returned cleanly (no exception); counts coherent     [taxonomy fidelity]
  ↓
JSON response: {status, counts: {scanned, stale, fresh, untracked}, active_thresholds, stale: [...], rendered_markdown}
  ↓
audit row written to runs/tool.bd-decay.jsonl                            [hook side effect]
```

## Common Rationalizations

> **Append-only** by the operator after each production run that surfaces
> a new shortcut (CLAUDE.md §14.3). The 5 rows below are the v1 seed for
> a pure-return-data sweep skill.

| Rationalization | Reality |
|---|---|
| "Untracked = stale, same difference" | False. Untracked = the operator didn't elect to track BD state on this company (or the field is missing). Surfacing it as stale generates phantom BD work. Split into two counts. |
| "The morning brief already ran this, no need to re-fire" | Morning brief fires 06:30; mid-day on-demand pulls return current data (a Company file edited at 09:00 is reflected in an 11:00 fire). Re-fire when asked. |
| "The thresholds are in the code, no need to mention them in the report" | Operator may not remember the active values; surface them in the report header so "stale" is interpretable without grepping `decay.py`. |
| "I'll filter by sector to make the list shorter" | Don't pre-filter; the routine returns all stale entries. If the list is genuinely too long, surface the top-N by `days_since_contact` and link to the full JSON, but DO NOT silently drop rows. |
| "Companies/<X>.md was edited yesterday so `bd_last_contact` should be auto-bumped" | File-modified-time is NOT the same as last contact. The operator updates `bd_last_contact` deliberately (or via a separate routine). Routine respects the field verbatim. |

## Red Flags

> Quoted internal monologue that signals a violation. If you catch
> yourself thinking any of these, stop and re-read the relevant stage.

- *"15 entries are 'untracked', I'll lump them with stale to make the action list richer"* — Iron Law breach. Untracked ≠ stale; an untracked entry is an operator decision NOT to track it (or a gap in the data discipline, which is a separate intervention).
- *"The thresholds look conservative, I'll bump them down for this report"* — the thresholds live in `decay.py` (and `_claude/profile.md` in future). Don't override per-report; surface the active threshold + note "this is what 'stale' means right now".
- *"3 stale entries — that's it for the week, easy"* — confirm the SCAN COMPLETED (no exception caught mid-walk). 3 stale on a 500-company vault is plausible; 3 stale because the parser skipped malformed files is wrong. Cross-check `counts.scanned` against your sense of the vault size.
- *"This company's `bd_last_contact` is 4 years ago, must be a typo, I'll auto-bump"* — the routine reports verbatim. Inferring a typo is operator's call.
- *"The owner is empty, but I know it's me, I'll fill it in"* — surface "(unset)" honestly. Inferring ownership pollutes the report.
- *"The 'won' deal showed 200 days since contact — should be stale"* — sticky states (dead/won/lost) never decay. That's by design; surfacing them as stale would generate noise on closed pipeline.

## Anti-Patterns

- Collapsing the three-bucket count ("stale + untracked = needs action") —
  the buckets MUST stay distinct; conflating them generates phantom work.
- Filtering stale entries by sector / owner / "looks like a stub" — the
  routine returns the full list; Anton surfaces, operator filters.
- Auto-drafting outreach emails for each stale entry — that's a separate
  (not-yet-shipped) `bd-outreach` skill. bd-decay surfaces, doesn't
  outreach.
- Modifying `bd_last_contact:` in a Companies file as a side effect — the
  operator updates the field when an actual contact happens; the routine
  reads, never writes.
- Reporting "all fresh" when only the scan COUNT was checked — confirm
  `counts.untracked` separately. A vault where every company is
  "untracked" reports 0 stale, but that means nothing is being tracked,
  not that everything is current.
- Inventing thresholds the routine doesn't use — surface the active
  `DECAY_THRESHOLDS` verbatim. Even tighter (or looser) thresholds the
  operator has in mind are NOT what "stale" means until the constant or
  `_claude/profile.md` changes.

## Example — "show me stale BD" (worked end-to-end)

Single realistic example. One good example beats five half-examples.

**Operator input:** "Quick BD sweep — anything gone cold?"

**Stage 1 — Anton invokes the routine:** POST `/api/workflows/bd-decay`
with `{}` (defaults: vault path = configured `AGENTIC_VAULT`, today =
`date.today()`, format = both).

**Stage 1 — Anton's chat line:** "Scanned 487 Companies files. **12
stale** past threshold (out of 330 tracked); 157 untracked (no `bd_state`
field — sector notes, dormant prospects never logged, etc.). Active
thresholds: watching 180d / engaged 60d / dormant 90d; dead+won+lost are
sticky (never decay). Scan completed cleanly."

**Stage 2 — Anton's spot-check bubble:** "Top 5 by days-overdue —

  * `[[Companies/Acme]]` (engaged, sector Leisure) — last contact
    2026-01-15, 134d ago (threshold 60d, +74d over). Owner: Operator.
  * `[[Companies/Bluegreen Vacations]]` (engaged, sector Leisure) —
    last contact 2026-02-01, 117d ago (threshold 60d, +57d over). Owner:
    Operator.
  * `[[Companies/Carthago]]` (dormant, sector Leisure) — last contact
    2026-01-20, 129d ago (threshold 90d, +39d over). Owner: (unset).
  * `[[Companies/Dometic]]` (watching, sector Leisure) — last contact
    2025-10-01, 240d ago (threshold 180d, +60d over). Owner: Operator.
  * `[[Companies/Erwin Hymer]]` (engaged, sector Leisure) — last contact
    2026-03-15, 75d ago (threshold 60d, +15d over). Owner: Operator.

…and 7 more in the full JSON. Open the response payload for the rest."

**Final output bubble** — a short lead ("12 stale on the engaged/dormant
sleeve; Leisure dominates"), the headline triple (12 stale / 318 fresh /
157 untracked), and chips (Re-fire after action · Refresh thresholds in
decay.py · Open Companies/ for any clicked row).

## When Stuck

| Symptom | Diagnostic |
|---|---|
| `scan` returns 0 stale on a 500-company vault | Cross-check `counts.untracked`. If untracked dominates, BD state is barely tracked anywhere — that's an operator-discipline gap, not a "clean vault". |
| Same company flagged stale twice | Should never happen — `scan` returns one StaleEntry per file. If you see a duplicate, the routine bug is `Companies/<X>.md` containing two files differing only by case on a case-insensitive filesystem; flag the routine. |
| `days_since_contact` reported as negative | Future-dated `bd_last_contact:`. Surface the row + flag the typo; do NOT auto-correct. |
| The scan raises mid-walk | Iron Law: a partial list is NOT a clean report. Surface the exception verbatim — typically a malformed YAML frontmatter on one Companies file. Operator opens the file, fixes the YAML, re-runs. |
| A 'won' deal shows up as stale | Should never happen — sticky state filter in `decay.py`. If you see this, either the state was mistyped (`Won` vs `won` — the routine lowercases, so this shouldn't bite) or `DECAY_THRESHOLDS` was edited. Flag. |
| `bd_owner` shows "(unset)" for many | Operator-discipline gap, not a routine fail. Surface alongside the stale list as a chip: "N entries missing owner — consider adding". |
| The morning brief shows different counts | The 06:30 cron snapshots Companies/ at that moment; on-demand fires use current state. Mid-day BD edits explain the delta. |

## Output Contract

The route returns JSON:

```json
{
  "status": "ok",
  "run_id": "<8-hex audit id>",
  "today": "YYYY-MM-DD",
  "active_thresholds": {
    "watching": 180,
    "engaged": 60,
    "dormant": 90,
    "dead": -1,
    "won": -1,
    "lost": -1
  },
  "counts": {
    "scanned": 487,
    "stale": 12,
    "fresh": 318,
    "untracked": 157
  },
  "stale": [
    {
      "company_path": "Companies/Acme.md",
      "company_name": "Acme",
      "sector": "leisure",
      "bd_state": "engaged",
      "bd_last_contact": "2026-01-15",
      "bd_owner": "Operator",
      "days_since_contact": 134,
      "threshold_days": 60,
      "days_over": 74
    }
  ],
  "rendered_markdown": "## BD watch -- stale entries\n\n_12 companies past decay threshold._\n\n- [[Companies/Acme]] ...",
  "duration_ms": 240
}
```

**No file write** — distinct from vault-health (which writes a report),
deal-tracker (which appends an Excel row), sector-news (which writes a
newsletter). bd-decay returns data; the dashboard / morning-brief
consumer renders.

**Side effects:** an audit row to `runs/tool.bd-decay.jsonl` written by
the `@after_tool_call` central guard. The CLI's own `audit.write_structured`
call (writing to `runs/bd-decay.jsonl`) is separate — that's the cron-fired
path; the route path's audit comes from the central hook stack (#60
substrate).

**What the routine does NOT produce** (so the skill does not report it):
a historical diff vs the previous sweep, a per-entry classification
(typo vs stale vs renamed vs operator-noted), an auto-outreach payload,
or a sector roll-up. The stale list is review-only.

## Citations Required

bd-decay is a vault-internal sweep — it cites NOTHING external. The data
IS its own citation: every stale entry carries `company_path` so the
operator can open the Company note directly.

| Field | Required source type | Acceptable form |
|---|---|---|
| `company_path` | The Companies/<X>.md file | Vault-relative POSIX path; Anton surfaces as `[[Companies/<name>]]` wikilink in the chat bubble |
| `bd_state` / `bd_last_contact` / `bd_owner` | The file's frontmatter | Parsed verbatim (the routine never infers) |
| `active_thresholds` | `DECAY_THRESHOLDS` constant (or `_claude/profile.md` override in future) | Surface in the report header verbatim |
| Routine version / scan ts | The audit row | `runs/tool.bd-decay.jsonl` (`run_id`, `inputs`, `outputs`) |

## Cost Envelope

The frontmatter declares `cost_ceiling_tokens: 1500` and
`cost_ceiling_seconds: 30`. Both are read from frontmatter by
`get_active_skill_cap("bd-decay", "tokens" | "seconds")`; the per-skill
caps are enforced by the central hook stack (#61/#67).

**Where the budget goes** (entirely Anton's narration — the routine is
zero LLM):
- Routine: 0 tokens (pure file-walk + frontmatter parse + date
  arithmetic). Sub-second on a 500-company vault.
- Anton narration: ~800-1200 tokens (verify counts coherent, surface 3-5
  spot rows + active thresholds + chips).
- Headroom: ~300 tokens for one guardrail retry.

**The 1500 token / 30s ceiling vs prior skills:**
       sector-news   → 6000 / 300s  (per-item LLM loop)
       LBO           → 8000 / 90s   (engine subprocess)
       vault-health  → 2000 / 60s   (pure file-walk, write report)
       deal-tracker  → 3000 / 60s   (single Ollama call)
       bd-decay      → 1500 / 30s   (pure file-walk, no write, no LLM)

The smallest skill shape sets the lowest declared envelope; the routine
is genuinely zero-LLM and the narration loop is shorter than vault-
health's (no sweep-by-sweep structure to surface — single bucketed list).

> **Calibration status:** 1500 tokens / 30s are first-pass estimates.
> Likely TIGHTER than 1500 once real data lands (operator narration is
> reliably short on bucket sweeps). Recalibrate to `1.25 × observed`
> after the first few narrated runs.

## Verification Checklist (before declaring done)

- [ ] The scan returned a list (no exception raised mid-walk)
- [ ] `counts.stale + counts.fresh + counts.untracked == counts.scanned` (taxonomy fidelity)
- [ ] All `days_since_contact >= 0` (no future-dated bd_last_contact)
- [ ] No duplicate entries by `company_path` (routine bug if any)
- [ ] The active `DECAY_THRESHOLDS` (or `_claude/profile.md` overrides) are surfaced in the report header
- [ ] The three buckets are NOT collapsed (untracked stays distinct from stale)
- [ ] Sticky states (dead/won/lost) are excluded from `stale` AND surfaced as "sticky, never decay" in the threshold header
- [ ] Audit row exists in `runs/tool.bd-decay.jsonl` with `status: "ok"`
- [ ] No outreach copy drafted, no `bd_last_contact:` edits proposed — surface only
- [ ] Final chat bubble surfaces 3-5 spot-check rows with `[[Companies/<name>]]` wikilinks for click-through
