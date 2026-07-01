"""Per-skill, per-tool ``tool_calls`` sub-cap — #74.5 (THOTH small pack §5).

Sibling to #67's ``llm_call_cap``. Where #67 bounds a skill dispatch's total
LLM calls, this bounds each TOOL separately: a runaway ``vault_read`` loop is
capped independently of ``fs_write`` or LLM calls, so one chatty tool can't
exhaust a skill's budget unnoticed.

Hook into the #22 ``@before_tool_call`` chain. Each tool call belonging to a
skill dispatch increments a per-``(run_id, tool_name)`` counter; once the count
would exceed the skill's declared ``cost_ceiling_tool_<tool_name>`` cap, the hook
raises ``ToolCallsCapExceeded`` and the dispatcher aborts the skill mid-run.

Defence in depth, three layers now:
  * #57 budget gate    — runaway COST (money spent before reset).
  * #67 llm_calls cap   — runaway LLM-call LOOPS.
  * #74.5 tool sub-caps — runaway TOOL loops (per tool, e.g. ``vault_read``).

Today (pre-#21) every skill's per-tool cap reads as ``None`` (see
``registry.get_active_skill_cap`` — no ``cost_ceiling_tool_*`` frontmatter is
declared yet). The counter still increments — useful telemetry / debug surface
— but no call is blocked. Once a SKILL.md declares
``cost_ceiling_tool_vault_read: 50`` the cap goes live with zero code change
(``cost_ceiling_*`` keys are captured generically by ``_cost_ceilings``).

The chat lane (no ``run_id`` bound) is intentionally ungoverned here — chat has
no per-skill cap, and #57 still applies. ``current_run_id()`` returning ``None``
is the signal.
"""

from __future__ import annotations

import logging
from typing import Optional

from routines.hooks import before_tool_call
from routines.hooks.types import ToolCallHookContext
from routines.skills._runtime.llm_call_counter import (
    check_and_increment_tool,
    current_run_id,
)
from routines.skills._runtime.registry import get_active_skill_cap

logger = logging.getLogger(__name__)


def _tool_cap_key(tool_name: str) -> str:
    """Frontmatter cap key for a tool: ``cost_ceiling_tool_<tool_name>``.

    Reuses the generic ``cost_ceiling_<key>`` lookup, so no registry change is
    needed — ``get_active_skill_cap(name, _tool_cap_key("vault_read"))`` reads
    ``cost_ceiling_tool_vault_read``."""
    return f"tool_{tool_name}"


class ToolCallsCapExceeded(RuntimeError):
    """Raised when a skill dispatch's per-tool call count would exceed its
    declared ``cost_ceiling_tool_<tool_name>`` cap.

    Carries structured fields so the dispatcher can surface a clean refusal
    (skill name + tool + cap + attempted count + run_id) AND so the audit row
    records all five. Sibling of #67's ``LLMCallsCapExceeded`` — a route that
    catches one usually wants to catch both — but distinct so the dispatcher can
    render a per-tool message different from a per-skill llm_calls cap or a
    scope-wide budget pause."""

    def __init__(
        self,
        *,
        skill_name: str,
        run_id: str,
        tool_name: str,
        cap: int,
        attempted: int,
    ) -> None:
        self.skill_name = skill_name
        self.run_id = run_id
        self.tool_name = tool_name
        self.cap = cap
        self.attempted = attempted
        reason = (
            f"skill {skill_name!r} exceeded tool_calls cap for tool "
            f"{tool_name!r} ({attempted} > {cap}; run_id={run_id})"
        )
        self.reason = reason
        super().__init__(reason)


@before_tool_call
def enforce_tool_calls_cap(ctx: ToolCallHookContext) -> Optional[bool]:
    """Pre-call gate, mirroring #67's ``enforce_llm_calls_cap``.

    Logic:
      1. No ``run_id`` bound → chat lane → no-op. Return ``None`` so the
         dispatcher proceeds; #57 budget gate handles cost-safety.
      2. ``run_id`` bound, skill declares no cap for THIS tool (``cap is
         None``) → ``check_and_increment_tool`` increments for telemetry and
         reports allowed; return ``None`` (proceed). Visible via
         ``tool_count_for(run_id, tool_name)`` for tests / future #69 rollup.
      3. ``run_id`` bound + cap declared + the attempt would exceed → raise
         ``ToolCallsCapExceeded`` (the atomic helper did NOT increment). We
         raise rather than return ``False`` so a route's exception handler can
         render a clean per-tool cap message (``run_before_tool_hooks`` treats
         ``False`` only as a silent block).

    The test-and-increment is a SINGLE atomic step
    (``check_and_increment_tool``) so concurrent same-``(run_id, tool)`` calls
    can't both slip past the cap via a check-then-act race.
    """
    rid = current_run_id()
    if rid is None:
        return None  # chat lane — ungoverned by this hook

    skill_name = ctx.skill.name if ctx.skill else "(unknown)"
    tool_name = ctx.tool_name
    cap = get_active_skill_cap(skill_name, _tool_cap_key(tool_name))

    allowed, attempted = check_and_increment_tool(tool_name, cap)
    if not allowed:
        logger.warning(
            "tool_calls cap exceeded: skill=%s tool=%s cap=%s attempted=%d run_id=%s",
            skill_name, tool_name, cap, attempted, rid,
        )
        # Stash on the context so dispatchers that surface usage can render the
        # reason even if they catch the exception at a higher layer.
        if isinstance(ctx.usage, dict):
            ctx.usage["tool_calls_cap_block"] = {
                "skill": skill_name,
                "tool": tool_name,
                "cap": cap,
                "attempted": attempted,
                "run_id": rid,
            }
        raise ToolCallsCapExceeded(
            skill_name=skill_name,
            run_id=rid,
            tool_name=tool_name,
            cap=cap,
            attempted=attempted,
        )

    return None


__all__ = ["ToolCallsCapExceeded", "enforce_tool_calls_cap"]
