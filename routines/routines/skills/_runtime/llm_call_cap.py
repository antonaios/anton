"""Per-skill ``llm_calls`` cap — #67 CREWAI §2.4 pattern.

Hook into the #22 ``@before_llm_call`` chain. Each LLM call belonging to
a skill dispatch increments a per-``run_id`` counter; once the count
would exceed the skill's declared ``cost_ceiling.llm_calls`` cap, the
hook raises ``LLMCallsCapExceeded`` and the dispatcher aborts the skill
mid-run.

Defence in depth on top of #57 budget gating:
  * #57 catches **runaway cost** — money spent before reset.
  * #67 catches **runaway loops** — N LLM calls before wall-clock fires.

The two are complementary: a tight loop that fires 100 cheap calls in 4
minutes hits #67 cap long before #57 sees the daily cap.

Today (pre-#21) every skill's cap reads as ``None`` (see
``registry.get_active_skill_cap``). The counter still increments — it's
useful telemetry / debug surface — but no call is blocked. Once #21
SKILL.md frontmatter ships, skills can opt in by declaring
``cost_ceiling.llm_calls: <N>``.

The chat lane (no ``run_id`` bound) is intentionally ungoverned here —
chat doesn't have a cap, and #57 still applies. ``current_run_id()``
returning ``None`` is the signal.
"""

from __future__ import annotations

import logging
from typing import Optional

from routines.hooks import before_llm_call
from routines.hooks.types import LLMCallHookContext
from routines.skills._runtime.llm_call_counter import (
    count_for,
    current_run_id,
    increment_for_current_run,
)
from routines.skills._runtime.registry import get_active_skill_cap

logger = logging.getLogger(__name__)


class LLMCallsCapExceeded(RuntimeError):
    """Raised when a skill dispatch's LLM-call count would exceed its
    declared ``cost_ceiling.llm_calls`` cap.

    Carries structured fields so the dispatcher can surface a clean
    refusal to the user (skill name + cap + attempted count + run_id)
    AND so the audit row records all four. Sits in the same exception
    family as #57's ``InvocationBlocked`` — route handlers that catch
    one usually want to catch both. Distinct class so the dispatcher
    can render a per-skill cap-exceeded message different from
    "scope-wide budget paused"."""

    def __init__(
        self,
        *,
        skill_name: str,
        run_id: str,
        cap: int,
        attempted: int,
    ) -> None:
        self.skill_name = skill_name
        self.run_id = run_id
        self.cap = cap
        self.attempted = attempted
        reason = (
            f"skill {skill_name!r} exceeded llm_calls cap "
            f"({attempted} > {cap}; run_id={run_id})"
        )
        self.reason = reason
        super().__init__(reason)


@before_llm_call
def enforce_llm_calls_cap(ctx: LLMCallHookContext) -> Optional[bool]:
    """Pre-call gate.

    Logic:
      1. No ``run_id`` bound → chat lane → no-op. Return ``None`` so the
         dispatcher proceeds; #57 budget gate handles cost-safety.
      2. ``run_id`` bound, skill has no cap declared → increment for
         telemetry; return ``None`` (proceed). The counter is visible
         via ``count_for(run_id)`` for tests / future #69 rollup.
      3. ``run_id`` bound + cap declared + next_count would exceed →
         raise ``LLMCallsCapExceeded``. We raise rather than return
         ``False`` because the dispatcher's chat router only surfaces
         budget-block reasons today (#57's ``budget_block`` stamp);
         raising lets a route's exception handler render a clean
         per-skill cap message.
    """
    rid = current_run_id()
    if rid is None:
        return None  # chat lane — ungoverned by this hook

    skill_name = ctx.skill.name if ctx.skill else "(unknown)"
    cap = get_active_skill_cap(skill_name, "llm_calls")

    if cap is None:
        # No cap declared — count for visibility only.
        increment_for_current_run()
        return None

    next_count = count_for(rid) + 1
    if next_count > cap:
        logger.warning(
            "llm_calls cap exceeded: skill=%s cap=%d attempted=%d run_id=%s",
            skill_name, cap, next_count, rid,
        )
        # Stash on the context so dispatchers that surface usage
        # (sessions/router.py-style) can render the reason even if they
        # catch the exception at a higher layer.
        if isinstance(ctx.usage, dict):
            ctx.usage["llm_calls_cap_block"] = {
                "skill": skill_name,
                "cap": cap,
                "attempted": next_count,
                "run_id": rid,
            }
        raise LLMCallsCapExceeded(
            skill_name=skill_name,
            run_id=rid,
            cap=cap,
            attempted=next_count,
        )

    increment_for_current_run()
    return None


__all__ = ["LLMCallsCapExceeded", "enforce_llm_calls_cap"]
