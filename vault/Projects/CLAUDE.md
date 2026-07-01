---
type: claude-config
memory_kind: procedural
sensitivity: internal
tags: [claude-config, procedural-memory, rules, projects]
---

# Projects/CLAUDE.md — deal-room procedure

> Auto-pulls when a session first reads any file under `Projects/`. Vault-global
> rules (writing contract, sensitivity tiers, never-list) live in the vault root
> `CLAUDE.md` — this file carries only what is specific to deal rooms. Rules are
> single-sourced: link to the root anchors, never restate them.

## 1. Read the project brief first — always

Any session / subagent / chat / workflow starting work on a deal MUST load `Projects/<DEAL>/00 Brief.md` before anything else. The brief is the per-deal context layer (the project-level equivalent of `_claude/profile.md` at the operator level). It carries the mandate, client, target overview, industry/sector/subsector positioning, goals, and watch-outs — all the things needed to orient correctly before any deal-specific work. Watch-outs in particular (§6 of the brief) are the load-bearing prior: client-raised concerns + lessons from prior similar deals + operator's gut. Do not skip the brief in favour of jumping straight to recall or a workflow — recall has no per-claim weighting on what matters for *this* deal; the brief does.

## 2. Creating a new project

1. Scaffold from the template: `cp -r Projects/_template Projects/<DEAL>/`.
2. Fill in `00 Brief.md` **first** — every other file in the project depends on it.
3. Run `lessons-learned suggest --project <DEAL>` to auto-populate the brief's §6 "from prior similar deals" watch-outs with relevant entries from `Registers/Lessons.md` (matched by sector/subsector); the operator reviews + accepts before they land in the brief.

## 3. The deal-room map — what each file/folder is for

| Location | Contents |
|---|---|
| `00 Brief.md` | Per-deal context layer (see §1). Mandate, client, target, goals, watch-outs. |
| `01 Source Register.md` | Per-deal claim→source mapping. **Updated with every cited source in any deliverable.** Cross-skill: one register per deal, used by LBO + comps + DCF + teaser alike. |
| `02 Meeting Notes/` | Meeting notes (verbatim records; structured notes paraphrase per root §3 rule 4). |
| `03 Emails/` | Email records. |
| `04 VDR Documents/` | Verbatim VDR source documents — the quote-keeping location root §3 rule 4 points at. |
| `05 Research/` | Deep company profiles and research outputs (project-scoped). |
| `06 Valuation/` | Valuation working area; per-skill inputs files per each skill's Output Contract. |
| `07 Buyer Universe/` | Buyer lists and outreach mapping. |
| `08 Assumptions Register.md` | Deal assumptions, dated. |
| `09 Decision Log.md` | Deal-level decisions, dated. |
| `10 Model Register.md` | Engine runs / model versions for this deal. |
| `11 People and Relationship Map.md` | Deal-team and counterparty map (Person files stay in `People/`). |
| `12 Outputs/` | **Deliverables land here** (one-pagers, proposals, IC memos — per root §7 table). |
| `13 Lessons Learned.md` | Deal lessons; feed `Registers/Lessons.md` at close-out. |
| `14 Issues & Outstanding.md` | The per-deal issues register (v2, 2026-06-10). |

## 4. The `[issue:ISS-NN]` convention

Gating action items link to their issue via an inline tag on the checkbox line:

```markdown
- [ ] Confirm change-of-control consent list [issue:ISS-03] [due:2026-06-20]
```

`ISS-NN` is the issue's ID in this project's `14 Issues & Outstanding.md`. Parsed since #issues-register v2 (2026-06-10); the dashboard's Open Actions panel groups gating items by issue. The full inline-tag convention (all tags, skip rules) is root §3 rule 11 + [[workspace-write-policy]] §7 — don't restate it here.

## 5. Deliverable outputs — where and what else updates

- Deliverables go to `12 Outputs/` (root §7 table is the authority on shape + location).
- **Every cited source** in a deliverable gets a row in `01 Source Register.md` in the same pass — no orphan citations (root [no-invented-sources](<vault>/CLAUDE.md#no-invented-sources)).
- Deep profiles auto-populate the cross-reference layer (`Companies/<X>.md`, `Sectors/<sector>.md`, key People stubs) per `Templates/CLAUDE.md`.
- Skill-produced deliverable conclusions flow back to the vault only via the operator-gated `captures_to_vault` proposal lifecycle — never auto-written.
