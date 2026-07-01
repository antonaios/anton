"""Hook context types.

Mirrors CrewAI's ``LLMCallHookContext`` / ``ToolCallHookContext`` shapes (see
[[CREWAI-EVALUATION]] §2.2) but pared down to ANTON's surface area.

The context dataclasses are **mutable** by design — ``before_*`` hooks edit
``prompt`` / ``system`` / ``tool_input`` in place, and ``after_*`` hooks can
inspect / replace ``response`` / ``result``. The hook factory in
[[decorators]] enforces the return-value contract on top of the mutation
contract: returning ``False`` from a ``before_*`` blocks the call.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Sensitivity = Literal["public", "internal", "confidential", "MNPI"]


@dataclass
class SkillRef:
    """Lightweight skill descriptor passed into hook contexts.

    ``metadata`` is the SKILL.md frontmatter dict once #21 lands. Pre-#21
    skills carry an empty dict; the grandfather guards in [[central_guards]]
    fill in safe defaults so the hook stack can run today."""

    name: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class WorkspaceRef:
    """The (workspace_type, workspace_name) pair that owns this invocation."""

    type: Literal["project", "bd", "general"]
    name: str


@dataclass
class LLMCallHookContext:
    """Context handed to ``@before_llm_call`` / ``@after_llm_call`` handlers.

    Fields:
      * ``run_id``  — 8-char run id, shared across the before/after pair.
      * ``skill``   — the invoking skill (name + frontmatter metadata).
      * ``workspace`` — the session/workspace this LLM call belongs to.
      * ``sensitivity`` — resolved sensitivity tier for the call.
      * ``lane``    — the routing lane the dispatcher chose (e.g. ``"ollama"``).
      * ``provider`` / ``model`` — concrete backend selection.
      * ``prompt`` / ``system`` — mutable; ``before_*`` may rewrite both.
      * ``response`` — only set on ``after_*``; ``after_*`` may overwrite.
      * ``usage`` — populated by the dispatcher post-call ({prompt_tokens,
        completion_tokens, total_tokens, duration_ms}).
    """

    run_id: str
    skill: SkillRef
    workspace: WorkspaceRef
    sensitivity: Sensitivity
    lane: str
    provider: str
    model: str
    prompt: str
    system: str | None = None
    response: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    # #75: dispatcher-side override consulted by gate + telemetry before
    # provider_for(model). Subprocess path sets "claude-subprocess" so
    # plan-credit calls segregate from future API calls in burn queries.
    provider_override: str | None = None
    # #llm-routing-override (codex-review-round-4 SEV-2 fix, 2026-06-03):
    # when ``enforce_sensitivity_lane`` bypasses a confidential refusal
    # because of an active override window, it stamps the override's id
    # here. The after-call telemetry writer picks it up + persists it on
    # the llm-call row so the burn audit + the override-history report
    # can join "which override authorised which call". Pre-fix the field
    # was set dynamically via setattr — load-bearing for security audit
    # but didn't survive the field-name → record mapping.
    sensitivity_override_id: str | None = None
    # #llm-routing-postjune15 P5 (enterprise-MNPI). ``mnpi_explicit`` carries the
    # provenance the chat route resolved (operator-assigned MNPI, eligible for the
    # attestation lift — NOT an unknown→MNPI coercion). The central sensitivity
    # gate REQUIRES it before permitting an MNPI cloud lane, so the explicit
    # condition is enforced INDEPENDENTLY of the lift (two layers agree).
    # ``mnpi_attestation_id`` is the attestation that authorised an MNPI→cloud
    # lift, stamped for the compliance audit trail (mirrors
    # ``sensitivity_override_id``). Both default off; set only on the (rare)
    # enterprise-MNPI cloud path.
    mnpi_explicit: bool = False
    mnpi_attestation_id: str | None = None
    # #llm-routing-tier-2 (2026-06-03): the cloud dispatcher resolves the
    # per-skill provider preference (sidecar > SKILL.md frontmatter > env var >
    # default) and stamps the chosen provider here so after-hooks + the
    # dashboard can see WHICH layer won. Distinct from ``provider_override``
    # (the concrete client tag: "claude-subprocess" / "codex-subprocess" /
    # "claude-api") — ``cloud_provider`` is the abstract Tier 2 selection
    # ("anthropic" / "openai" / "ollama-only" / "prefer_local"). ``prefer_local``
    # is normally rewritten to the local lane upstream of the cloud dispatcher,
    # so on the normal flow it is not stamped here; it appears only on the
    # fail-closed path where that rewrite was bypassed and the value reached
    # ``_dispatch_cloud_llm`` (which refuses the cloud call).
    cloud_provider: str | None = None
    # #llm-routing-tier-2: per-skill sampling params resolved alongside the
    # provider (``{temperature, max_tokens}``, only the keys actually set).
    # The ``_call_*`` cloud helpers splat these into the client's ``chat()``.
    # Empty/None → nothing extra passed (back-compat: pre-Tier-2 these were
    # always unset).
    llm_params: dict[str, Any] | None = None


@dataclass
class ToolCallHookContext:
    """Context handed to ``@before_tool_call`` / ``@after_tool_call`` handlers.

    Fields:
      * ``run_id``    — 8-char run id, shared across the before/after pair.
      * ``skill``     — the invoking skill.
      * ``workspace`` — the session/workspace this tool call belongs to.
      * ``sensitivity`` — resolved sensitivity tier.
      * ``tool_name`` — e.g. ``"vault_write"``, ``"fs_write"``, ``"web_fetch"``.
      * ``tool_input`` — mutable kwargs dict; ``before_*`` may rewrite paths
        or strip fields.
      * ``result`` — only set on ``after_*``; ``after_*`` may overwrite.
      * ``usage`` — populated by the dispatcher post-call. Mirrors the LLM
        context's ``usage`` shape: ``{status, duration_ms, error_class,
        error_message, ...}``. ``audit_tool_call`` reads from here.
    """

    run_id: str
    skill: SkillRef
    workspace: WorkspaceRef
    sensitivity: Sensitivity
    tool_name: str
    tool_input: dict[str, Any]
    result: Any = None
    usage: dict[str, Any] = field(default_factory=dict)
