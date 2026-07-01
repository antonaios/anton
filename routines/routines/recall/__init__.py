"""Hierarchical-retrieval skill for the Agentic OS vault.

Implements plan §9 hierarchical retrieval:
    1. Frontmatter filter (Python-side, exact / range)
    2. Semantic top-k via local Ollama nomic-embed-text
    3. TLDR-first read
    4. Map-reduce on bodies (optional --synthesise)

CLI: `recall index | query | health`. Built as a separate stack from Smart
Connections (which uses bge-micro-v2 for in-Obsidian browsing) so retrieval
quality and indexing are under our control. Both stacks running in parallel
is fine — they serve different surfaces.
"""
