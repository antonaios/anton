"""Skill-cap lookup — re-export of the canonical registry (#61).

#67 shipped this module as a STUB whose ``get_active_skill_cap`` returned
``None`` for every skill (counter-only, no gating). #61 landed the real
frontmatter-reading registry at ``routines/skills/registry.py`` — the ONE
canonical skills-registry surface.

To avoid two registries, this module now simply re-exports
``get_active_skill_cap`` from there. The hook in ``llm_call_cap.py`` and the
``_runtime`` package ``__init__`` import from this path unchanged; the
behaviour is now real (reads ``cost_ceiling_<key>`` from SKILL.md) instead of
the no-op stub.

``CapKey`` stays here for the type alias callers reference.
"""

from __future__ import annotations

from routines.skills.registry import get_active_skill_cap

CapKey = str  # "llm_calls" | "tokens" | "seconds" — open enum

__all__ = ["get_active_skill_cap", "CapKey"]
