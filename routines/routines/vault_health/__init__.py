"""Vault health checks — Plan v3 §6.9 Phase 6 decay defences.

Four routines:

1. `freshness` — for each claim file under `Sectors/<X>/`, check
   `last_refreshed:`. If > 180 days, auto-bump `confidence` down one
   tier. If > 90 days, flag as warning. Configurable via
   `_claude/profile.md` `decay_thresholds:` block (future).

2. `links` — walk all `.md` files in vault, find `[[wikilinks]]` whose
   target file doesn't exist (orphans). Flag in a report.

3. `speculation` — find any `(speculation)` markers in claim files;
   if associated date > 180 days ago, suggest re-classification.
   STUB — implementation deferred until operator demand becomes clear
   (claim files may not consistently date their speculation markers).

4. (deferred — incremental re-index lives in routines.recall as a
   future enhancement; not part of this module)
"""
