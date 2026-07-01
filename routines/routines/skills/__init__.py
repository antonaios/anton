"""Skills package — home for SKILL.md-style skill definitions (#21) and the
runtime substrate that supports them (#67).

The ``_runtime`` subpackage carries pieces that need to exist BEFORE the
SKILL.md mass migration in #21 — namely the per-skill ``llm_calls`` cap
enforcement that hangs off the existing ``@before_llm_call`` hook stack.

Public callers should import the runtime context-manager via:

    from routines.skills._runtime import skill_run

which scopes a ``run_id`` for ``llm_calls`` accounting around a skill
invocation. Today (pre-#21) nothing calls this — the chat lane is
intentionally ungoverned and the #57 budget gate still applies on top.
"""

from __future__ import annotations
