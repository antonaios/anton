"""Four hook decorators built from one factory.

Mirrors CrewAI's ``hooks/decorators.py`` (see [[CREWAI-EVALUATION]] §2.2)
with two adaptations:
  1. Filter kwarg is ``skills=[...]`` (not ``agents=[...]``) — ANTON's unit
     of invocation is the skill, not a persona.
  2. Tool decorators filter on ``tools=[...]`` (the tool name).

Contract:
  * ``@before_llm_call(skills=[...])`` / ``@before_tool_call(tools=[...])``:
    returning ``False`` blocks the call (dispatcher must respect). Returning
    ``None`` is a no-op. Mutate the context to alter the inputs.
  * ``@after_llm_call(skills=[...])`` / ``@after_tool_call(tools=[...])``:
    returning a string (LLM) or value (tool) replaces the response. Returning
    ``None`` is a no-op.
  * Bare ``@before_llm_call`` (no parens) also works — the factory disambiguates
    by checking whether the first arg is callable.

The dispatcher reads ``_hook_registry`` at LLM-call / tool-call time and runs
every registered hook whose ``skills`` / ``tools`` filter matches (``None``
means "match all")."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Literal

from routines.hooks.types import LLMCallHookContext, ToolCallHookContext

Phase = Literal["before", "after"]
Kind = Literal["llm", "tool"]


@dataclass
class HookRegistration:
    func: Callable[..., Any]
    phase: Phase
    kind: Kind
    filter_skills: tuple[str, ...] | None = None
    filter_tools: tuple[str, ...] | None = None


@dataclass
class _HookRegistry:
    """Global registry. Tests reset via ``clear()``."""

    hooks: list[HookRegistration] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def register(self, registration: HookRegistration) -> None:
        with self._lock:
            self.hooks.append(registration)

    def list(
        self,
        *,
        phase: Phase | None = None,
        kind: Kind | None = None,
    ) -> list[HookRegistration]:
        with self._lock:
            out = list(self.hooks)
        if phase is not None:
            out = [h for h in out if h.phase == phase]
        if kind is not None:
            out = [h for h in out if h.kind == kind]
        return out

    def clear(self) -> None:
        with self._lock:
            self.hooks.clear()


hook_registry = _HookRegistry()


# ────────────────────────────────────────────────────────────────────────────
# Factory
# ────────────────────────────────────────────────────────────────────────────


def _normalize_filter(values: Iterable[str] | None) -> tuple[str, ...] | None:
    if values is None:
        return None
    return tuple(values)


def _create_hook_decorator(
    *,
    phase: Phase,
    kind: Kind,
    marker_attr: str,
    filter_kwarg: str,  # "skills" for llm, "tools" for tool
) -> Callable[..., Any]:
    """Build one of the four public decorators.

    Returns a decorator factory that supports both ``@deco`` (bare) and
    ``@deco(skills=[...])`` (parenthesised) usage."""

    def decorator_factory(*args: Any, **kwargs: Any) -> Any:
        # Bare usage: @before_llm_call without parens — first positional
        # is the decorated function.
        if args and callable(args[0]) and not kwargs:
            func = args[0]
            return _attach(func, phase, kind, marker_attr, None, None)

        skills = kwargs.get("skills") if filter_kwarg == "skills" else None
        tools = kwargs.get("tools") if filter_kwarg == "tools" else None
        filter_skills = _normalize_filter(skills)
        filter_tools = _normalize_filter(tools)

        def wrap(func: Callable[..., Any]) -> Callable[..., Any]:
            return _attach(func, phase, kind, marker_attr, filter_skills, filter_tools)

        return wrap

    return decorator_factory


def _attach(
    func: Callable[..., Any],
    phase: Phase,
    kind: Kind,
    marker_attr: str,
    filter_skills: tuple[str, ...] | None,
    filter_tools: tuple[str, ...] | None,
) -> Callable[..., Any]:
    setattr(func, marker_attr, True)
    setattr(func, "_hook_phase", phase)
    setattr(func, "_hook_kind", kind)
    setattr(func, "_hook_filter_skills", filter_skills)
    setattr(func, "_hook_filter_tools", filter_tools)
    hook_registry.register(HookRegistration(
        func=func,
        phase=phase,
        kind=kind,
        filter_skills=filter_skills,
        filter_tools=filter_tools,
    ))
    return func


# ────────────────────────────────────────────────────────────────────────────
# Public decorators
# ────────────────────────────────────────────────────────────────────────────


before_llm_call = _create_hook_decorator(
    phase="before",
    kind="llm",
    marker_attr="is_before_llm_call_hook",
    filter_kwarg="skills",
)
after_llm_call = _create_hook_decorator(
    phase="after",
    kind="llm",
    marker_attr="is_after_llm_call_hook",
    filter_kwarg="skills",
)
before_tool_call = _create_hook_decorator(
    phase="before",
    kind="tool",
    marker_attr="is_before_tool_call_hook",
    filter_kwarg="tools",
)
after_tool_call = _create_hook_decorator(
    phase="after",
    kind="tool",
    marker_attr="is_after_tool_call_hook",
    filter_kwarg="tools",
)


# ────────────────────────────────────────────────────────────────────────────
# Dispatch helpers (called by the LLM dispatcher / tool runner once #22 lands)
# ────────────────────────────────────────────────────────────────────────────


def _matches_filter(filter_: tuple[str, ...] | None, value: str) -> bool:
    return filter_ is None or value in filter_


def run_before_llm_hooks(ctx: LLMCallHookContext) -> bool:
    """Run every registered ``before_llm_call`` whose ``skills`` filter matches.

    Returns ``False`` if any hook returned ``False`` — dispatcher must skip
    the call. Returns ``True`` otherwise."""
    for h in hook_registry.list(phase="before", kind="llm"):
        if not _matches_filter(h.filter_skills, ctx.skill.name):
            continue
        result = h.func(ctx)
        if result is False:
            return False
    return True


def run_after_llm_hooks(ctx: LLMCallHookContext) -> None:
    """Run every registered ``after_llm_call`` whose ``skills`` filter matches.

    A handler returning a string overwrites ``ctx.response``."""
    for h in hook_registry.list(phase="after", kind="llm"):
        if not _matches_filter(h.filter_skills, ctx.skill.name):
            continue
        result = h.func(ctx)
        if isinstance(result, str):
            ctx.response = result


def run_before_tool_hooks(ctx: ToolCallHookContext) -> bool:
    for h in hook_registry.list(phase="before", kind="tool"):
        if not _matches_filter(h.filter_tools, ctx.tool_name):
            continue
        result = h.func(ctx)
        if result is False:
            return False
    return True


def run_after_tool_hooks(ctx: ToolCallHookContext) -> None:
    for h in hook_registry.list(phase="after", kind="tool"):
        if not _matches_filter(h.filter_tools, ctx.tool_name):
            continue
        result = h.func(ctx)
        if result is not None:
            ctx.result = result


__all__ = [
    "before_llm_call",
    "after_llm_call",
    "before_tool_call",
    "after_tool_call",
    "hook_registry",
    "HookRegistration",
    "run_before_llm_hooks",
    "run_after_llm_hooks",
    "run_before_tool_hooks",
    "run_after_tool_hooks",
]
