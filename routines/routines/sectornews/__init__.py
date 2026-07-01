"""Sector newsletter routine.

Pipeline (per plan §7A):
    1. Pull articles per active sector — Firecrawl /search + optional /scrape
       on URLs listed in `Sectors/<X>.md` frontmatter `sources:`
    2. Deduplicate near-identical pieces (cosine similarity on titles)
    3. Filter / score for relevance + materiality (local Ollama qwen3:8b)
    4. Synthesise top items into a newsletter (qwen3:14b)
    5. Write to vault: Resources/Newsletters/<date>-<sector>.md

Sensitivity: public/internal — all sources are public web. Cloud lanes are
fine per CLAUDE.md §4 routing, but per profile.md heartbeat_preferences we
default to local Ollama for token-budget reasons (newsletter is a recurring
background routine; not worth burning Opus / Codex on it).

CLI: `sector-news run <sector> [--days 7] [--dry-run]`
     `sector-news run-all [--days 7]`        # all active sectors per profile.md
"""
