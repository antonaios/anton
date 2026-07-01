# Subsector taxonomy — Stage 0 scoping

> Loaded on demand by Anton when proposing subsectors for a comps build.

## Source of truth

`<vault>/_claude/profile.md` carries the canonical subsector taxonomy:

| Field | Purpose |
|---|---|
| `active_sectors:` (line 16) | Top-level sector list (e.g. `Travel`, `Leisure`, `Hospitality`, `Telecoms`) |
| `sector_sub_lens:` (line 21) | Sub-sector taxonomy descriptors per sector |

Example for `Hospitality`:
- `Hotels (full-service / limited-service / lifestyle / boutique)`

Slug convention (from `Topics/Architecture/sector-expertise.md` §3):
- Profile entries are **title-case** for readability (`Telecoms`, `Hospitality`)
- Folder slug + frontmatter `sector:` field are **lowercased + hyphenated** (`telecoms`, `hospitality`)
- Sub-sector slugs derive from `sector_sub_lens:` descriptors (`hotels-full-service`, `hotels-limited-service`, `hotels-lifestyle`, `hotels-boutique`)
- Routines convert via `sector_slug = active_sector.lower().replace(" ", "-")`

## Stage 0 algorithm

1. Read `Projects/<deal>/00 Brief.md` frontmatter (`target`, `sector`, optional `subsector`).
2. Read `_claude/profile.md` and parse `sector_sub_lens.<parent-sector>` into a candidate list.
3. Apply judgment:
   - **Pure-play target** (single business line) → propose ONE subsector.
   - **Hybrid target** (multiple business lines, materiality split) → propose 2-3 subsectors with a rough weight (informational; the template doesn't blend the blocks).
   - **Conglomerate** (4+ material lines) → propose 3-4 subsectors max; the operator may split into multiple comps runs.
4. Surface rationale per proposed subsector (one line referencing the target's mix from the brief / CIM).

## What NOT to do

- DO NOT propose a subsector not in `sector_sub_lens` for the parent sector — that's inventing taxonomy. If a subsector is missing, surface the gap and ask the operator to either (a) pick the closest, or (b) update `profile.md sector_sub_lens` first.
- DO NOT skip the operator gate. "Obvious" subsector calls are exactly where the operator catches a wrong frame.
- DO NOT propose a subsector belonging to a different parent sector ("the target is technically Leisure but also Travel-adjacent, so I'll mix") — surface the cross-sector question to the operator; they decide whether to split.

## Profile.md absent — fallback

If the parent sector isn't in `active_sectors:`, surface the gap. The operator either updates `profile.md` (preferred — sectors compound) or runs comps as a `workspace_scope: any` ad-hoc (future follow-on, not the current build).

## Cross-refs

- [universal-iron-laws](<vault>/CLAUDE.md#universal-iron-laws) — propose/approve discipline
- `<vault>/Topics/Architecture/sector-expertise.md` — slug + folder convention
- `peer-identification.md` (this skill) — Stage 1 algorithm
