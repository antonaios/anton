"""Memory promotion routine — plan §6 W4 D4 + §14.

Walks active projects + the cross-project layer, identifies items worth
promoting or compacting, writes a proposal file to `Routines/memory-promotion/`
for operator review. **Never auto-applies.** Two-step pattern: routine
proposes; operator approves by running `memory-promote apply <run-id>`.

What it looks for (MVP):
    1. Duplicate decisions — same decision text appearing in multiple notes
       across a project, suggesting consolidation
    2. Stale open actions — `- [ ] ...` checkboxes in meeting notes older
       than N days that haven't been ticked
    3. Cross-project lesson candidates — `13 Lessons Learned.md` entries
       tagged `promote-to-register` (or with a "Patterns worth promoting"
       section that has bullets)

Out of scope (Phase 2):
    - Cross-project pattern detection across multiple deals
    - Note-similarity-based de-duplication via embeddings
    - Auto-archival of closed projects
"""
