"""Context-manager helper for wrapping tool-call route handlers with the
#22 hook stack.

Each skill bridge route in ``routines/api/routes/`` is a tool call from
the hook stack's perspective: receives Pydantic request → invokes
business logic → returns Pydantic response. Wrapping every such handler
with ``run_before_tool_hooks`` / ``run_after_tool_hooks`` produces a
uniform audit + policy surface.

Usage::

    @router.post("/workflows/comps", response_model=CompsResult)
    def workflow_comps(req: CompsRequest) -> CompsResult:
        with tool_call_hooks(
            tool_name="comps_pull",
            workspace_type="general",
            workspace_name="default",
            sensitivity="public",
            tool_input=req.model_dump(),
        ) as ctx:
            result = build_comps(req.symbol, ...)
            ctx.result = result.model_dump()
            return result

Semantics:
  * Before-hook returning ``False`` → raises ``HTTPException(403)`` from
    inside ``__enter__``; the route never reaches its body.
  * Before-hook raising → the exception propagates out of ``__enter__``
    (typically ``SensitivityViolation`` from central guards → caller can
    catch and 403; default behaviour is just to bubble).
  * Body raises → after-hooks still fire (with ``usage.status="error"`` +
    ``error_class`` + ``error_message``) BEFORE the exception re-raises,
    so audit trails always materialise.
  * Normal exit → ``usage.status="ok"``, ``usage.duration_ms``, after-
    hooks fire, ``__exit__`` returns ``None`` so the caller's return is
    untouched.
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from typing import Any, Iterator, Literal

from fastapi import HTTPException

from routines.hooks.decorators import run_after_tool_hooks, run_before_tool_hooks
from routines.hooks.types import (
    Sensitivity,
    SkillRef,
    ToolCallHookContext,
    WorkspaceRef,
)
from routines.telemetry.llm_writer import make_run_id

logger = logging.getLogger(__name__)


WorkspaceType = Literal["project", "bd", "general"]


@contextmanager
def tool_call_hooks(
    *,
    tool_name: str,
    workspace_type: WorkspaceType = "general",
    workspace_name: str = "default",
    sensitivity: Sensitivity = "public",
    tool_input: dict[str, Any] | None = None,
    skill_metadata: dict[str, Any] | None = None,
    run_id: str | None = None,
) -> Iterator[ToolCallHookContext]:
    """Wrap a tool-call route handler with the #22 hook stack.

    See module docstring for the full behaviour spec.

    Args:
      * ``tool_name`` — kebab-or-snake-case identifier; lands in the audit
        log filename (``runs/tool.<name>.jsonl``) so keep it stable.
      * ``workspace_type`` / ``workspace_name`` — pulled from the request
        when available, else conservative defaults. Skill routes without a
        workspace concept fall back to ``general`` / ``default``.
      * ``sensitivity`` — explicit sensitivity tier for this call. Most
        skills today are public (operator-typed tickers, public news);
        BD-side workflows should pass ``confidential``.
      * ``tool_input`` — typically ``req.model_dump()``. Surfaced to
        before-hooks so policy guards can inspect parameters.
      * ``skill_metadata`` — SKILL.md frontmatter dict for the
        grandfather guards (#21 not yet shipped, but the path is open).
      * ``run_id`` — optional correlation id (#59). When ``None`` (every direct
        caller today) a fresh id is minted, preserving current behaviour. The
        ``@anton_skill`` wrapper passes ``current_run_id()`` so the audit row +
        per-tool counters + suspend/resume share the request-boundary id.

    Yields the ``ToolCallHookContext`` so the caller can stash
    ``ctx.result`` for after-hooks.
    """
    ctx = ToolCallHookContext(
        # #59 run-id correlation: reuse a caller-supplied run_id (e.g. the
        # request-boundary X-ANTON-Run-Id, passed by @anton_skill) so a network
        # retry coalesces + suspend/resume correlates. Default None → mint, which
        # preserves the existing behaviour for every current direct caller.
        run_id=run_id or make_run_id(),
        skill=SkillRef(name=tool_name, metadata=skill_metadata or {}),
        workspace=WorkspaceRef(type=workspace_type, name=workspace_name),
        sensitivity=sensitivity,
        tool_name=tool_name,
        tool_input=tool_input or {},
    )
    t0 = time.monotonic()

    # Before-hooks: returning False blocks the call. Raising propagates.
    try:
        proceed = run_before_tool_hooks(ctx)
    except Exception:
        # Propagate guard-raised exceptions (SensitivityViolation,
        # WorkspacePolicyViolation, etc) to the caller — they're meaningful
        # refusals, not observability noise.
        raise
    if proceed is False:
        logger.warning(
            "tool_call_hooks: before-hook blocked tool=%s workspace=%s/%s",
            tool_name, workspace_type, workspace_name,
        )
        raise HTTPException(
            status_code=403,
            detail=f"tool {tool_name!r} blocked by before_tool_call hook",
        )

    # Body runs. After-hooks fire in both happy + error paths.
    try:
        yield ctx
    except Exception as e:
        ctx.usage["status"] = "error"
        ctx.usage["error_class"] = type(e).__name__
        ctx.usage["error_message"] = str(e)
        ctx.usage["duration_ms"] = int((time.monotonic() - t0) * 1000)
        try:
            run_after_tool_hooks(ctx)
        except Exception as hook_err:  # noqa: BLE001
            logger.warning(
                "tool_call_hooks: after-hooks raised on error path (suppressed): %s",
                hook_err,
            )
        raise

    # Happy path
    ctx.usage.setdefault("status", "ok")
    ctx.usage["duration_ms"] = int((time.monotonic() - t0) * 1000)
    try:
        run_after_tool_hooks(ctx)
    except Exception as e:  # noqa: BLE001 — observability never breaks the caller
        logger.warning("tool_call_hooks: after-hooks raised (suppressed): %s", e)


__all__ = ["tool_call_hooks"]
