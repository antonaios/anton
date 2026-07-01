"""Project chat — per-deal conversational memory (OUTSTANDING #42).

A chat surface scoped to ONE active deal. Each turn:

  1. Reads the deal's prior ``Projects/<DEAL>/_chat.md`` history (episodic).
  2. Runs project-filtered recall over the deal's vault folder (semantic).
  3. Builds an LLM prompt (system + last-N turns + retrieved-sources block +
     the user message) and calls the local LLM.
  4. Atomic-appends both the user + assistant turns back to ``_chat.md``.

Memory-model placement (#41): reads semantic (project folder via recall) +
episodic (the deal's own ``_chat.md``); writes episodic (appends turns). The
audit lane field ``episodic_source`` = ``_chat.md + recall hits``; no
semantic/procedural targets unless the operator explicitly promotes a turn.

Sensitivity (CLAUDE.md §4): the chat endpoint reads the deal's
``00 Brief.md`` frontmatter ``sensitivity:`` and forces the LOCAL Ollama lane
for ``confidential`` / ``MNPI`` material — it never routes those to a cloud
lane regardless of ``AGENTIC_PLAN_TIER``.

This is the BACKEND half. The dashboard panel + types + api client are a
separate HARNESS session (see OUTSTANDING #42).
"""
