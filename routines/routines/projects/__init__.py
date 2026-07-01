"""Per-project routines — aggregators that walk the vault + Corp Finance trees
to surface project-scoped data (actions, brief frontmatter, etc.) to the
dashboard.

Per the action convention locked 2026-05-23 (see
``Topics/Architecture/workspace-write-policy.md``), action items are written
as ``- [ ] Task text [due:YYYY-MM-DD] [owner:slug] [urgent] [flag]`` inline in
any project markdown file. This package provides the reader.
"""
