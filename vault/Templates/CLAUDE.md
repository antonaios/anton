---
type: claude-config
memory_kind: procedural
sensitivity: internal
tags: [claude-config, procedural-memory, rules, templates]
---

# Templates/CLAUDE.md — deliverable discipline

> Auto-pulls when a session reads any template — i.e. exactly when a deliverable
> is being produced. Shape *selection* (which verb → which shape → which
> template) lives in the vault root `CLAUDE.md` §7; this file carries the
> discipline that applies while producing the deliverable. `Templates/` is
> excluded from recall indexing — rules live here, content lives in notes.

## 1. Templates are the schema

Frontmatter fields in a template are the contract (root §3 rule 2): every note instantiated from a template carries all of its fields. If a field is missing or doesn't fit, the template is wrong — propose a template change (and capture the signal via `learn note`, root §6); do not silently skip fields.

## 2. Discipline that applies to every deliverable shape

1. **Research recency.** Search for events specifically dated to the last 14 days as a separate query. The headline news a company's M&A team is working on *right now* is the highest-value fact in the profile and the easiest to miss with a generic search. Always cross-check with a "<Company> news <YYYY-MM>" query.
2. **Three-year financial baseline minimum** for any profile shape that includes financials. Single-year snapshots mislead — direction matters more than level.
3. **Comparable peers.** If the company has listed comparables, surface 5–7 trading multiples for the peer set. Be honest about EBITDAaL vs EBITDA conventions in the relevant sector (telecoms, REITs, towers, etc. all have non-GAAP norms).
4. **Sources traceable per claim.** Every non-trivial number has a source register entry. The Sources section at the bottom of the profile maps claim-level superscripts to the register.
5. **Sector-specific accounting nuances.** Each sector has 2–4 accounting wrinkles that change how multiples and metrics work (telecoms: EBITDAaL, IFRS 16, hyperinflation; REITs: EPRA NTA, debt LTV; banks: CET1, ROTE; etc.). Surface them when relevant; don't pretend GAAP-only valuation is sufficient.
6. **Currency hygiene.** State denomination on every number. £ vs € vs $ vs ZAR — telcos especially mix. Use the company's own functional currency for P&L; convert separately if showing UK retail price points.
7. **The deep profile auto-populates the cross-reference layer:** `Companies/<X>.md` enriched, `Sectors/<sector>.md` enriched (or stubbed if missing), key People stubbed (CEO, CFO at minimum). Wikilinks must resolve.

## 3. Out of scope without an explicit ask

Company-search outputs (one-pager, profile) do **not** include: investment recommendation, target price, DCF or multiples-derived fair value. Those require the Python valuation engine ([no-llm-maths](<vault>/CLAUDE.md#no-llm-maths)) and the IC-memo or thesis shape. The profile presents the data; the thesis takes a view.
