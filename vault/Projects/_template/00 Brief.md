---
type: project-brief
memory_kind: semantic
workspace-type: project                    # project | bd  — canonical, machine-read by vault scanner (#6c dual-scan). MUST stay in sync with the matching tag in `tags:` below (endpoint flips both on BD scaffold create).
project:                                   # codename / shortname, e.g. FALCON
client: "[[Companies/]]"                   # who we're acting for (distinct from target)
client-side: buy | sell | advisory
status: live | paused | won | lost | archived
sensitivity: confidential
# importance: 3              # optional 1-5 recall weight; unset = 3 neutral. See _claude/CLAUDE.md §3 rule 12 (#54a triad).
# expires: YYYY-MM-DD        # optional auto-stale date; recall weight ×0.5 after. For project briefs, typically left unset (active deals don't stale).
# provenance: "[[Registers/Sources#]]"   # optional explicit provenance wikilink (briefs typically use the per-project `01 Source Register.md` instead).
deal-type: M&A | refinancing | minority | divestment | jv | partnership-with-equity | research | other
codename:

# Industry layering — three levels, deepest is most queryable
industry:                                  # broad — e.g. Consumer, Industrials, Financials, TMT, Real Estate
sector: "[[Sectors/]]"                     # vault sector page, e.g. [[Sectors/Hospitality]]
subsector:                                 # narrow free text — e.g. "Pubs with rooms", "Hotels — limited-service"

# Counterparties
target: "[[Companies/]]"                   # who's being bought/sold/financed
counterparty: "[[Companies/]]"             # who's on the other side of the deal

# Lifecycle
opened: YYYY-MM-DD
closed:                                    # set on close/archive

# Workspace overview — fields the dashboard's Project Overview tile reads
owner:                                     # operator on this side; default to vault operator
stage:                                     # pitch | kick-off | DD | bid-1 | bid-2 | signing | close

# Parseable timeline — single source of truth for the Project Overview tile + recall.
# Mirror of `1. Process Management/4. Timeline/*.xlsx` on the file-system side.
# `state` is one of: done | next | future. Exactly one entry should be `next` at a time.
key-dates:
  - { label: "Kick-off",   date: YYYY-MM-DD, state: future }
  - { label: "Bid 1",      date: YYYY-MM-DD, state: future }
  - { label: "Bid 2",      date: YYYY-MM-DD, state: future }
  - { label: "IC",         date: YYYY-MM-DD, state: future }
  - { label: "Final",      date: YYYY-MM-DD, state: future }

# Pre-populated by `lessons-learned suggest --project <X>` when the brief is created
relevant-lessons:                          # list of [[Registers/Lessons#lesson-id]] suggested for this deal
  - "[[Registers/Lessons#]]"

tldr:                                      # 2 sentences capturing the load-bearing story of this deal
tags: [project, brief, semantic-memory]   # leading tag MIRRORS workspace-type above; endpoint swaps "project" → "bd" on BD scaffold create. Operator searches #project / #bd in Obsidian tag pane to browse by type.
---

# {Project Name} — Brief

> **Read this file first when working on this project.** This is the project-context layer — the per-deal equivalent of `_claude/profile.md`. Per `_claude/CLAUDE.md` §6 operating procedure: any subagent / chat / workflow starting work on this deal loads this brief before anything else.

## 1. Mandate

What we're being asked to do. Buy-side / sell-side / advisory? For whom? On what timeline? What's the success condition?

## 2. Client

Who they are (one paragraph). What they do (one paragraph — business model, scale, geographies). Why they engaged us — what made our positioning right for this mandate.

## 3. Target / counterparty

- **Target company:** [[Companies/]]
- **Counterparty:** [[Companies/]] (if known)
- **Light overview of the target:** {1–2 paragraphs framing the business — link out to [[Companies/<X>]] for the deep profile. The brief carries enough that a subagent doesn't need to follow the wikilink to orient.}
- **Why interesting:** {what makes this deal worth doing — strategic logic, valuation gap, structural opportunity}

## 4. Industry · sector · subsector

- **Industry:** {broad context — Consumer / Industrials / Financials / TMT / Real Estate / etc. Used by recall for cross-sector queries.}
- **Sector:** [[Sectors/]] {wikilink to the sector page — the source of truth for sector-level multiples, value drivers, tailwinds/headwinds}
- **Subsector:** {narrow positioning — "Hotels — limited-service", "Pubs with rooms", "Leisure parks", "Visitor attractions". Forces clarity on the niche we're actually operating in; prevents sector-file claims from blurring across niches.}
- **Why the subsector matters for this deal:** {1 paragraph — what's specific to this niche that shapes the analysis, e.g. RevPAR/ADR/Occ for hotels, LFL/ATV for pubs}

## 5. Goals

Numbered list of what we want to achieve on this mandate. Concrete + checkable + ordered by priority.

1. **Primary objective:** {the single thing that defines success}
2. **Secondary objectives:** {supporting outcomes — material but not load-bearing}
3. **Constraints we must meet:** {non-negotiables — timing, valuation floor, regulatory, confidentiality, governance approvals}

## 6. Watch-outs — things to keep top of mind throughout the process

Three sources of watch-outs, each tagged so a subagent can weight them differently:

### Client-raised
{Concerns / sensitivities / hard requirements the client has flagged. Direct from them. Highest weight — never violate.}
- {watch-out} _(client-raised, 2026-MM-DD)_

### From prior similar deals
{Auto-suggested by `lessons-learned suggest --project <this>` when the brief is created; the operator reviews + accepts. Each entry should link to the durable lesson in `Registers/Lessons.md` so the trace is intact.}
- {watch-out} -> [[Registers/Lessons#lesson-id]]

### Operator's gut
{Tacit concerns — things you've noticed don't quite add up but aren't yet anchored to a source. Lowest formal weight but often the most important. Promote to a lesson register entry if a second deal corroborates.}
- {watch-out} _(operator's gut, 2026-MM-DD)_

## 7. Stakeholders

### On our side
- {Name} — {role} → [[People/]]

### On the other side
- {Name} — {role} → [[People/]]

## 8. Process

- **Stage:**
- **Key dates:**
- **Critical path:**

## 9. Working hypothesis

What we currently think the answer is. Update as you learn.

## 10. Open questions

Distinct from §6 watch-outs — these are things we don't yet know the answer to, not things to be careful of.

-

## 11. Where things live

- This brief: this file
- Sources: `01 Source Register.md`
- Meeting notes: `02 Meeting Notes/`
- Emails: `03 Emails/`
- VDR: `04 VDR Documents/`
- Research: `05 Research/`
- Valuation: `06 Valuation/`
- Buyer universe: `07 Buyer Universe/`
- Assumptions: `08 Assumptions Register.md`
- Decisions: `09 Decision Log.md`
- Models: `10 Model Register.md`
- People map: `11 People and Relationship Map.md`
- Outputs: `12 Outputs/`
- Lessons: `13 Lessons Learned.md` — patterns from this deal worth promoting to `[[Registers/Lessons]]` and (Plan v3 §6.8, deferred) to `[[Sectors/]]`
- Issues: `14 Issues & Outstanding.md` — running register of live deal issues (status / priority / owner / gating items); gating items surface on the dashboard Open Actions panel and will feed `/agenda`
