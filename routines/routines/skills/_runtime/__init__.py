"""Runtime substrate for the #21 SKILL.md migration.

Built ahead of #21 so #67's ``llm_calls`` cap (CREWAI §2.4 pattern) can
land standalone. Three modules:

  * ``llm_call_counter`` — ContextVar-scoped ``run_id`` + thread-safe
    counter dict. Survives async boundaries via ContextVar's task-copy
    semantics.
  * ``registry`` — ``get_active_skill_cap()`` stub that returns ``None``
    until #21 wires real SKILL.md frontmatter reading. Until then no
    skill is enforced; counter just increments for telemetry visibility.
  * ``llm_call_cap`` — ``@before_llm_call`` hook + ``LLMCallsCapExceeded``
    exception. Registered at app startup alongside #57 budget gate.

Public re-exports below for callers (skill dispatchers in the future,
tests today)."""

from __future__ import annotations

from routines.skills._runtime.guardrails import (
    TIER_RETRY_BUDGETS,
    GuardrailRetriesExhausted,
    GuardrailVerdict,
    effective_retry_budget,
    evaluate_guardrails,
    known_guardrail_names,
    llm_with_guardrails,
    record_output_guardrails,
    tier_retry_budget,
    validate_guardrail_names,
)
from routines.skills._runtime.llm_call_cap import (
    LLMCallsCapExceeded,
    enforce_llm_calls_cap,
)
from routines.skills._runtime.llm_call_counter import (
    count_for,
    current_run_id,
    increment_for_current_run,
    increment_tool_for_current_run,
    reset,
    set_run_id,
    skill_run,
    tool_count_for,
)
from routines.skills._runtime.registry import get_active_skill_cap
from routines.skills._runtime.tool_call_cap import (
    ToolCallsCapExceeded,
    enforce_tool_calls_cap,
)

__all__ = [
    "GuardrailRetriesExhausted",
    "GuardrailVerdict",
    "LLMCallsCapExceeded",
    "TIER_RETRY_BUDGETS",
    "ToolCallsCapExceeded",
    "count_for",
    "current_run_id",
    "effective_retry_budget",
    "enforce_llm_calls_cap",
    "enforce_tool_calls_cap",
    "evaluate_guardrails",
    "get_active_skill_cap",
    "increment_for_current_run",
    "increment_tool_for_current_run",
    "known_guardrail_names",
    "llm_with_guardrails",
    "record_output_guardrails",
    "reset",
    "set_run_id",
    "skill_run",
    "tier_retry_budget",
    "tool_count_for",
    "validate_guardrail_names",
]
