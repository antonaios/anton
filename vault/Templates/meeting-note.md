---
type: meeting-note
memory_kind: procedural
produces_kind: episodic   # dated meeting events — episodic memory
date: YYYY-MM-DD
duration:
attendees:
  - "[[]]"
project: "[[]]"
sensitivity: confidential
# importance: 3              # optional 1-5 recall weight; unset = 3 neutral. See _claude/CLAUDE.md §3 rule 12 (#54a triad).
# expires: YYYY-MM-DD        # optional auto-stale date; recall weight ×0.5 after.
# provenance: "[[Registers/Sources#]]"   # optional explicit provenance wikilink. Named `provenance:` to avoid clash with source-hash/source-file below.
source-hash:
source-file:
tags: [meeting]
tldr:
---

# YYYY-MM-DD — {meeting title}

## Summary
{2–3 sentence high-level summary, fully paraphrased}

## Key facts
- {Fact 1} → `[[Sources#xyz]]`
- {Fact 2} → `[[Sources#xyz]]`

## Decisions
- {Decision, owner, date} → promoted to `[[Registers/Decisions]]`

## Actions
<!--
Inline-tag convention per CLAUDE.md rule 11 + [[workspace-write-policy]] §7.
Tags (all optional): [due:YYYY-MM-DD] [owner:slug] [urgent] [flag] [done:YYYY-MM-DD]
Owner slug defaults to profile.md operator_slug when unset. Date format ISO.
The aggregator (`routines/projects/actions.py`) picks these up; the dashboard
Open Actions panel renders them; the toggle endpoint stamps [done:] on completion.
Examples (replace, don't keep):
-->
- [ ] Draft IC memo for committee [due:2026-05-27] [owner:operator] [urgent]
- [ ] Refresh comps post-earnings [due:2026-05-25] [owner:operator]
- [ ] Send buyer list v2 to counsel [due:2026-05-26] [owner:operator] [flag]
- [ ] Update LBO model with Q1 estimates [owner:operator]
- [x] Counsel call brief drafted [done:2026-05-22]

## Open questions
- {Open question 1}

## Mentions
- People: [[]]
- Companies: [[]]
- Sectors: [[]]

## Source transcript
[[Inbox/HiNotes/processed/]]
