"""Hook decorators + event bus + central guards.

Pattern stolen from CrewAI (see [[CREWAI-EVALUATION]] §2.2-2.3) pared down
to ANTON's single-user, sync-only surface area.

Public API:
  * ``@before_llm_call`` / ``@after_llm_call`` / ``@before_tool_call`` /
    ``@after_tool_call`` — decorators for hook handlers.
  * ``bridge_event_bus`` — singleton pub/sub for Skill[Started|Completed|
    Failed] and LLM/Tool triplets.
  * ``LLMCallHookContext`` / ``ToolCallHookContext`` — argument types.
  * ``register_central_guards()`` — call once at app startup to wire the
    four core guards (sensitivity, audit, cost-cap, workspace).

#22 STATUS: scaffold only. Decorators + bus + central guards are in place;
the dispatcher integration that ACTUALLY invokes ``run_before_llm_hooks`` on
every LLM call is **not yet wired** — that's the second half of #22 and
lands in a follow-on session."""

from __future__ import annotations

from routines.hooks.decorators import (
    after_llm_call,
    after_tool_call,
    before_llm_call,
    before_tool_call,
    hook_registry,
    run_after_llm_hooks,
    run_after_tool_hooks,
    run_before_llm_hooks,
    run_before_tool_hooks,
)
from routines.hooks.event_bus import BridgeEventBus, bridge_event_bus
from routines.hooks.events import (
    ActivityLogged,
    Event,
    LLMCallCompleted,
    LLMCallFailed,
    LLMCallStarted,
    SkillInvocationCompleted,
    SkillInvocationFailed,
    SkillInvocationStarted,
    ToolCallCompleted,
    ToolCallFailed,
    ToolCallStarted,
)
from routines.hooks.tool_dispatch import tool_call_hooks
from routines.hooks.types import (
    LLMCallHookContext,
    SkillRef,
    ToolCallHookContext,
    WorkspaceRef,
)

__all__ = [
    # decorators
    "before_llm_call",
    "after_llm_call",
    "before_tool_call",
    "after_tool_call",
    "hook_registry",
    "run_before_llm_hooks",
    "run_after_llm_hooks",
    "run_before_tool_hooks",
    "run_after_tool_hooks",
    # event bus
    "bridge_event_bus",
    "BridgeEventBus",
    # events
    "Event",
    "SkillInvocationStarted",
    "SkillInvocationCompleted",
    "SkillInvocationFailed",
    "LLMCallStarted",
    "LLMCallCompleted",
    "LLMCallFailed",
    "ToolCallStarted",
    "ToolCallCompleted",
    "ToolCallFailed",
    "ActivityLogged",
    # context types
    "LLMCallHookContext",
    "ToolCallHookContext",
    "SkillRef",
    "WorkspaceRef",
    # dispatch helper
    "tool_call_hooks",
]
