---
type: company
memory_kind: procedural
produces_kind: semantic   # instances written from this template are semantic memory
name:
status: target | buyer | advisor | listed-comp | portfolio | other
sector: "[[]]"
hq:
ticker:
website:
ownership:
last-revenue:
last-ebitda:
sensitivity: internal
# importance: 3              # optional 1-5 recall weight; unset = 3 neutral. See _claude/CLAUDE.md §3 rule 12 (#54a triad).
# expires: YYYY-MM-DD        # optional auto-stale date; recall weight ×0.5 after.
# provenance: "[[Registers/Sources#]]"   # optional explicit provenance wikilink.
# BD watch fields (Plan v3 §6.9 Phase 5) — optional; only set if the company
# is in active BD watch. Auto-rolls into Sectors/<sector>/BD.md view.
bd_state:            # watching | engaged | dormant | dead | won | lost (or empty)
bd_last_contact:     # YYYY-MM-DD of most recent meaningful contact
bd_owner:            # operator slug
bd_notes:            # multi-line notes; first line shows in BD view
tags: [company]
tldr:
---

# {Company Name}

## Snapshot
{1–2 sentences: what they do, scale, ownership}

## Business
- **Model:**
- **Key segments / brands:**
- **Geography:**
- **Customers:**

## Financials
- **Revenue:** {latest, prior period, source}
- **EBITDA / margin:** {latest, prior period, source}
- **KPIs:**

## Ownership and governance
- **Owners:**
- **Board:**
- **Key management:** [[]]

## Transaction history
- {YYYY-MM-DD} — {event} → [[]]

## Why we care
{Free text — why this company is on the radar, in what context}

## Mentions
*(populated automatically by the watcher)*
