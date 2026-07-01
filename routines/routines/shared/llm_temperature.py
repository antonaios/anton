"""LLM temperature register — #llm-routing-temperature (sweep part b, 2026-06-03).

ANTON's LLM calls use temperature deliberately, not at the provider default
(1.0). This file is the canonical register of every ``chat(...)`` call site
that sets temperature explicitly, with the rationale. New callers should add
their temperature here too.

Recommended defaults (per ``LLM-ROUTING-2026-06-02.md`` §Tier 2):

  Skill type                              Temperature   Rationale
  ────────────────────────────────────    ───────────   ────────────────────────────
  Deterministic JSON extraction           0.1           Structured-output extraction;
                                                        invention is a hard failure.
  Note / prose extraction                 0.2           Some interpretation; tolerable
                                                        variance in surface form.
  Source-everything research              0.2           Low enough to suppress
                                                        invention; high enough to
                                                        enumerate breadth.
  Search synthesis / recall               0.2           Same.
  Deliverable synthesis (CIM, teaser,     0.3-0.5       Some prose variance OK;
   IC memo)                                              structure constrained by
                                                        template.
  Numerical audit (audit-xls, lbo, dcf)   0.0           Strict deterministic;
                                                        calculation work.
  Interactive chat                        0.3           Balance of fluency +
                                                        correctness.

Current ANTON call sites (2026-06-03 sweep):

  ─────────────────────────────────────────────────────────────────────────────
  File                                              Temp    Justification
  ─────────────────────────────────────────────────────────────────────────────
  routines/intake/parse.py:173                      0.1     JSON document
                                                            extraction from
                                                            scanned PDF pages
                                                            (image path).
                                                            Structured output.
                                                            ALIGNED.
  routines/intake/parse.py:199                      0.1     JSON document
                                                            extraction from
                                                            text-extracted PDFs
                                                            (text path).
                                                            Sister call to :173;
                                                            same rationale.
                                                            ALIGNED.
                                                            (Codex-review SEV-3
                                                            fix 2026-06-03 —
                                                            missed in initial
                                                            sweep.)
  routines/dealtracker/extract.py:118               0.1     JSON deal-row
                                                            extraction from
                                                            sector news. ALIGNED.
  routines/sectornews/score.py:91                   0.1     JSON {is_ma,
                                                            urgency} classifier.
                                                            ALIGNED.
  routines/hinotes/extract.py:148                   0.2     Loose extraction
                                                            from voice-note
                                                            transcripts. ALIGNED.
  routines/recall/synthesise.py:106                 0.2     Short search
                                                            synthesis (300 tok).
                                                            ALIGNED.
  routines/recall/synthesise.py:142                 0.2     Long search
                                                            synthesis. ALIGNED.
  routines/sectornews/synthesise.py:95              0.3     Sector-summary
                                                            narrative (2000 tok).
                                                            Edge of synthesis
                                                            range; acceptable.
                                                            Consider 0.2 if
                                                            fabrication observed.
  routines/sessions/router.py:501                   0.3     Interactive chat
                                                            default. ALIGNED.
                                                            (Line number
                                                            shifted from :457
                                                            after the Tier 1
                                                            dispatcher insertion
                                                            — Codex-review SEV-3
                                                            fix 2026-06-03.)

Gaps / pending wire-ups:

  ─────────────────────────────────────────────────────────────────────────────
  Location                                          Status
  ─────────────────────────────────────────────────────────────────────────────
  routines/skills/comps/scripts/comps.py            STUB shims today (all return []).
   _shim_equity_research_screen                     When wired (Path B / Tier 1),
   _shim_investment_banking_buyer_list              should use 0.2 (source-everything).
   _shim_deep_research_cotrans                      EBITDAaL deep-research option
                                                    (iii) also 0.2 — see
                                                    SESSION-COMPS-ORCHESTRATION.md
                                                    §EBITDAaL Temperature note.
  Operator's Claude Code session (cockpit)          Outside this codebase. Brief
                                                    documents the recommendation;
                                                    operator discipline.
  Anthropic skills' internal temperature            Outside this codebase.

Tier 2 status (#llm-routing-temperature part a — DONE 2026-06-03):
  Per-skill ``llm_params: { temperature, max_tokens }`` in SKILL.md frontmatter
  now LANDED with Tier 2. The registry (``routines.skills.registry``) parses +
  validates the block (temperature ∈ [0.0, 1.0]); ``resolve_skill_provider``
  overlays the operator sidecar (``_claude/provider_overrides.yaml``) on top;
  and the cloud dispatcher (``routines.sessions.router._dispatch_cloud_llm`` →
  ``_tier2_sampling``) splats the resolved params into the chosen provider's
  ``chat()``. So a skill that routes a cloud LLM call through the dispatcher
  declares its temperature in frontmatter rather than hard-coding it.

  This register stays as the MIGRATION TRAIL: the 9 call sites below are direct
  ``chat(temperature=...)`` calls inside routines (intake / dealtracker /
  sectornews / recall / hinotes / chat default) that do NOT yet flow through the
  Tier 2 dispatcher — they keep their inline temperature until/unless they
  migrate to a SKILL.md + the dispatcher path. ``RECOMMENDED_TEMPERATURE`` below
  remains the source for the spec defaults a migrating skill should declare.

Maintaining this register:
  When a new ``chat(...)`` call lands, add a row here with the rationale.
  When a temperature changes, update both the call site comment + this register.
  As skills migrate their LLM calls onto the Tier 2 dispatcher, the frontmatter
  ``llm_params:`` slot subsumes the inline value — drop the row here once it
  does; the file stays as the audit trail of what moved when.
"""

from __future__ import annotations

from typing import Final

# Re-export the defaults table programmatically for any caller that wants to
# read the recommended temperature for a task type instead of hardcoding it.
# Future Tier 2 work will likely move this into SKILL.md frontmatter and read
# from the registry; this dict is the bridge.

RECOMMENDED_TEMPERATURE: Final[dict[str, float]] = {
    "json_extraction":         0.1,   # intake/parse, dealtracker/extract, sectornews/score
    "note_extraction":         0.2,   # hinotes/extract
    "source_research":         0.2,   # deep-research, equity-research:screen, etc.
    "search_synthesis":        0.2,   # recall/synthesise
    "deliverable_synthesis":   0.3,   # CIM, teaser, IC memo (Step 5 ports)
    "narrative_synthesis":     0.3,   # sectornews/synthesise
    "numerical_audit":         0.0,   # audit-xls, lbo, dcf (when LLM-mediated)
    "interactive_chat":        0.3,   # sessions/router default
}

__all__ = ["RECOMMENDED_TEMPERATURE"]
