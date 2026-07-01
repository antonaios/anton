---
type: issues-register
memory_kind: episodic
project: "[[]]"
sensitivity: confidential
tags: [register, issues]
---

# Issues & Outstanding — {Project Name}

> Running register of live issues on this deal — picked up from emails, meeting notes, FDD/VDD reports, chat sessions — and continuously updated through the deal's life. One `## ISS-NN` section per issue. Never delete an issue: set `status: closed` and fill `resolution:` — the section is the audit trail. Gating items use the checkbox convention (`_claude/CLAUDE.md` §3 rule 11) tagged `[issue:ISS-NN]`, so they surface on the dashboard's Open Actions panel automatically. **Close-out rule:** a closed issue carries no unchecked boxes — before setting `status: closed`, check every gating item (`- [x] … [done:YYYY-MM-DD]`) or rewrite abandoned ones as plain bullets (`- dropped: …`); the action parser ignores issue status, so an unchecked box under a closed issue would keep surfacing as an open action. When `/agenda` ships, every non-closed issue (open / monitoring / blocked) feeds it directly (#issues-register v3).

## Issue shape

Copy the block below for each new issue (kept in a code fence so the action parser ignores the placeholder checkbox):

```markdown
## ISS-01 — {short title, e.g. "FDD: working-capital adjustment risk"}
- **status:** open | monitoring | blocked | closed
- **priority:** P1 | P2 | P3
- **owner:** {lead} (+ {supporting parties / sub-processes, e.g. counsel, FDD team})
- **raised:** YYYY-MM-DD — {source: [[source:xyz]] / `03 Emails/...` / `02 Meeting Notes/...`}
- **affects:** {downstream artefacts this must flow into — e.g. SPA → completion-accounts mechanism}
- **resolution:** {set when closed — what settled it, with source}
- **gating items:**
  - [ ] {action to resolve / verify / chase} [due:YYYY-MM-DD] [owner:slug] [issue:ISS-01]
```

Conventions: `status: monitoring` = live risk being watched, no action currently possible (distinct from an open task). `affects:` is the linkage line — when the named artefact (SPA draft, IC memo, databook) is being prepared, scan this register for issues that name it. Source per §3 rule 8: every issue traces to where it was picked up.

---

## Issues

*(none yet)*
