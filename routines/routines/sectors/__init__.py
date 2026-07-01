"""Sector expertise layer routines.

Per Plan v3 §6.9. Two-stage pipeline:

1. **Extract** (this module's `extract` + `extractors/`): walk vault inputs
   (closed projects, newsletters, meeting notes, research, BD), produce
   proposals in `Routines/sector-extraction/<date>-<sector>.md` with
   `status: pending-review`. Operator applies via REVIEW chip; applied
   extracts append to `Sectors/<X>/_sources/from-*.md` provenance files.
2. **Synthesize** (Phase 4 — `routines.sectors.synthesize`): read
   provenance + claim files, apply weighted-independence + confidence
   formula, propose updates to claim files. Cron Sat 02:30.

Full schema reference: `Topics/Architecture/sector-expertise.md` in the vault.
"""
