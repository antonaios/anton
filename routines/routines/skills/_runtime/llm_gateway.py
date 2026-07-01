"""#63 L3 — the gated ``llm()`` helper for skill bodies.

Skills that call an LLM client directly (``OllamaClient().chat()`` /
``ClaudeAPIClient().chat()``) BYPASS the platform's compute-lane governance: the
#no-mnpi-to-cloud sensitivity lane (was cited as §5.4; confidential/MNPI must
never reach the cloud), the #57
budget gate, the #67 per-skill llm-call cap, and the Tier-2 per-skill provider
routing. The chat dispatcher (``sessions/router.py::route_and_respond``) runs all
of that; skill routes had no equivalent.

This module gives an ``@anton_skill`` body a single governed call::

    from routines.skills._runtime.llm_gateway import llm
    out = llm("Summarise these three filings: ...")
    text = out.text

It reuses the SAME governed path the chat dispatcher uses — there is no second
dispatch implementation to drift:

  1. ``shared_routing.pick_lane(task_type, sensitivity)`` picks the lane. THIS is
     the #no-mnpi-to-cloud guarantee (was cited as §5.4): a confidential/MNPI
     skill resolves to a LOCAL (Ollama) lane and never builds a cloud decision.
  2. ``run_before_llm_hooks`` fires the registered guards — the sensitivity lane
     guard (fail-closed: raises ``SensitivityViolation`` → we surface
     ``SkillLLMRefused``), the #57 budget gate (block → ``SkillLLMRefused``), and
     the #67 cap. A blocked call NEVER reaches a provider.
  3. ``_call_ollama`` (local) / ``_dispatch_cloud_llm`` (cloud, Tier-2 resolved
     with per-skill provider + fallback) does the actual call.
  4. ``run_after_llm_hooks`` writes the telemetry + audit row.

The wrapper (``@anton_skill``) sets a :class:`SkillLLMContext` contextvar around
the body so ``llm()`` knows which skill / run_id / sensitivity / workspace it is
running under (using the wrapper's RESOLVED sensitivity — the strictest of the
request tier and the skill's registry floor). Called outside a skill body it
raises :class:`SkillLLMUnavailable`.

**Override windows (#llm-routing-override, agent-leg Phase-2 slice 1):** a
confidential skill call is lifted to the cloud lane the enterprise tier would
grant it (``shared_routing.override_cloud_lane``) — but ONLY while an
operator-opened sensitivity-override window is active for the
(skill, workspace, provider) tuple (1–30 min, justification required,
audit-logged — ``routines/sensitivity_overrides``). With no window open the
behaviour is byte-identical to the local-only description above. MNPI is never
lifted: the ceiling enum excludes it at the policy/storage/endpoint layers and
the #no-mnpi-to-cloud backstop here (was cited as §5.4) refuses it
unconditionally. The sensitivity gate
(``central_guards.enforce_sensitivity_lane``) independently re-finds the window
on its own bypass path — two layers must agree before a confidential prompt
reaches a cloud provider, and the window id is stamped on the hook context so
the audit row records which override authorised the call.

**Per-skill system prompt (#llm-skill-system-prompt):** a skill may declare a
default system prompt in its SKILL.md frontmatter (``llm_system_prompt:`` inline,
or ``llm_system_prompt_file:`` a sibling file); the registry resolves it and the
``@anton_skill`` wrapper carries it on :class:`SkillLLMContext`. ``llm()`` applies
it (an explicit ``llm(prompt, system=…)`` overrides per-call), threading it
through the dispatch helpers' additive ``system`` override down to
``client.chat(system=…)``. When a skill declares none and passes none, the
helpers fall back to the platform persona (``_ANTON_SYSTEM_PROMPT``) — so an
un-migrated skill is byte-identical to before. The governance — the load-bearing
part — is unchanged.
"""

from __future__ import annotations

import logging
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)

# #no-mnpi-to-cloud cloud rules (was cited as §5.4) enforced by the backstop in llm():
#   * "mnpi"         → NEVER cloud — absolute, not liftable by any override.
#   * "confidential" → cloud ONLY inside an active #llm-routing-override window
#                      (operator-opened, time-boxed, audited).


# Trust model (codex-5.5 SEV-2): skill bodies are OPERATOR-AUTHORED — they are
# inside the trusted computing base of this single-operator bridge. The
# ``set_skill_llm_context`` / ``reset_skill_llm_context`` setters are
# WRAPPER-INTERNAL (the ``@anton_skill`` jacket owns them) — they are NOT part of
# the public API (kept out of ``__all__``) and a skill must not call them. The
# #no-mnpi-to-cloud backstop (was cited as §5.4) + fail-closed before-hooks are
# defense-in-depth ON TOP of that
# trust; a fully untrusted-skill model (where a body could forge a looser
# sensitivity) would need the sensitivity derived from a server-owned run
# registry rather than a contextvar — out of scope while skills are TCB.


# ─────────────────────────────────────────────────────────────────────────────
# Skill-LLM context (set by the @anton_skill wrapper around the body)
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SkillLLMContext:
    """What ``llm()`` needs about the skill currently executing. Set by the
    wrapper from its governed context (the resolved, strictest sensitivity)."""

    skill: str
    run_id: Optional[str]
    sensitivity: str
    workspace_type: str
    workspace_name: str
    cost_cap_tokens: Optional[int] = None
    # The skill's DECLARED default system prompt (#llm-skill-system-prompt),
    # resolved by the registry from the SKILL.md ``llm_system_prompt`` /
    # ``llm_system_prompt_file`` frontmatter and carried here by the wrapper.
    # ``None`` → the skill declared none → llm() falls back to the platform
    # persona. An explicit ``llm(prompt, system=…)`` overrides this per-call.
    system_prompt: Optional[str] = None


_skill_llm_var: ContextVar[Optional[SkillLLMContext]] = ContextVar(
    "anton_skill_llm_ctx", default=None
)


def set_skill_llm_context(ctx: SkillLLMContext) -> Any:
    """Bind the skill-LLM context for the duration of a body call. Returns a
    token the caller passes to :func:`reset_skill_llm_context` (a ContextVar so
    it survives the threadpool boundary, like ``current_run_id``)."""
    return _skill_llm_var.set(ctx)


def reset_skill_llm_context(token: Any) -> None:
    _skill_llm_var.reset(token)


def current_skill_llm_context() -> Optional[SkillLLMContext]:
    """The skill-LLM context for the body running now, or ``None`` outside a
    governed skill body."""
    return _skill_llm_var.get()


# ─────────────────────────────────────────────────────────────────────────────
# Result + errors
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class LLMResult:
    """The outcome of a governed skill LLM call."""

    text: str
    route: str                 # e.g. "ROUTED · LOCAL OLLAMA → qwen3:14b"
    lane: str
    provider: str
    model: str
    sensitivity: str
    usage: dict = field(default_factory=dict)  # prompt/completion/total tokens

    @property
    def is_error(self) -> bool:
        return self.route.startswith("ERROR ·")


class SkillLLMUnavailable(RuntimeError):
    """``llm()`` was called outside an ``@anton_skill`` body (no context set)."""


class SkillLLMRefused(RuntimeError):
    """A pre-call guard blocked the LLM call — the #no-mnpi-to-cloud sensitivity
    lane guard (was cited as §5.4), the #57 budget gate, or the #67 cap. The
    call never reached a provider."""


# ─────────────────────────────────────────────────────────────────────────────
# The gated call
# ─────────────────────────────────────────────────────────────────────────────


def llm(prompt: str, *, system: Optional[str] = None, task_type: str = "synthesis") -> LLMResult:
    """Run a governed LLM call from inside an ``@anton_skill`` body.

    ``task_type`` (``"synthesis"`` | ``"extraction"`` | …) feeds
    ``pick_lane`` alongside the skill's sensitivity to choose the lane.

    ``system`` is the system prompt for THIS call (#llm-skill-system-prompt).
    Precedence: an explicit ``system=`` argument (a per-call override — e.g. a
    skill with two distinct LLM steps) wins; otherwise the skill's DECLARED
    default (``SkillLLMContext.system_prompt``, resolved by the registry from the
    SKILL.md ``llm_system_prompt`` / ``llm_system_prompt_file`` frontmatter) is
    used; when neither is present the dispatch helpers fall back to the platform
    persona (``_ANTON_SYSTEM_PROMPT``) — so an un-migrated skill is unchanged.

    Raises :class:`SkillLLMUnavailable` outside a skill body and
    :class:`SkillLLMRefused` when a guard blocks the call (sensitivity / budget /
    cap)."""
    sctx = current_skill_llm_context()
    if sctx is None:
        raise SkillLLMUnavailable(
            "llm() must be called inside an @anton_skill body (no skill-LLM "
            "context is set) — direct skill LLM calls must go through this helper "
            "so they don't bypass the §5.4 lane / #57 budget / #67 cap"
        )

    # Lazy imports: keep this module's import graph light AND avoid any circular
    # import with sessions.router at load time.
    import os

    from routines.hooks.central_guards import (
        SensitivityViolation,
        provider_for_override_lookup,
    )
    from routines.hooks.decorators import run_after_llm_hooks, run_before_llm_hooks
    from routines.sensitivity_overrides import find_active_override
    from routines.hooks.types import LLMCallHookContext, SkillRef, WorkspaceRef
    from routines.sessions import router as R
    from routines.shared import routing as shared_routing
    from routines.skills._runtime.llm_call_counter import bind_run_id
    from routines.skills import registry as skill_registry

    # 1. Lane decision from the skill's RESOLVED sensitivity. pick_lane is the
    #    #no-mnpi-to-cloud guarantee (was cited as §5.4) — confidential/MNPI →
    #    a local lane (never a cloud one).
    lane = shared_routing.pick_lane(task_type, sctx.sensitivity)
    provider, model = shared_routing.lane_to_model(lane)
    decision = R.RouteDecision(
        sensitivity=sctx.sensitivity, lane=lane, provider=provider, model=model,
    )

    def _tier2(dec: "R.RouteDecision") -> tuple["R.RouteDecision", Any]:
        """Tier-2 per-skill provider resolution for a CLOUD decision (mirrors
        route_and_respond): resolve BEFORE the gates so they key off the REAL
        provider (#llm-routing-tier-2 caller contract)."""
        env_provider = os.environ.get(R.AGENTIC_CLOUD_PROVIDER_ENV)
        env_provider = env_provider.lower() if env_provider else None
        res = skill_registry.resolve_skill_provider(
            sctx.skill, env_provider=env_provider, task_type=task_type,
        )
        return R._decision_for_tier2(dec, res), res

    # Local routes never touch a cloud provider.
    resolution = None
    if not decision.is_local:
        decision, resolution = _tier2(decision)

    # #llm-routing-override: a confidential call may be LIFTED to the cloud lane
    # the enterprise tier would grant it — ONLY inside an operator-opened
    # override window for this (skill, workspace, provider) tuple (1–30 min,
    # justification required, audited). No window open → this block is a no-op
    # and the call stays local, byte-identical to before. MNPI is never lifted
    # (gated here on "confidential" AND refused absolutely in the backstop
    # below AND excluded from the ceiling enum at the policy/storage/endpoint
    # layers). Any trouble during the consult → stay local (fail-closed).
    override = None
    sens_lower = str(sctx.sensitivity).lower()
    if decision.is_local and sens_lower == "confidential":
        candidate_lane = shared_routing.override_cloud_lane(task_type)
        if candidate_lane is not None:
            try:
                c_provider, c_model = shared_routing.lane_to_model(candidate_lane)
                cand = R.RouteDecision(
                    sensitivity=sctx.sensitivity, lane=candidate_lane,
                    provider=c_provider, model=c_model,
                )
                cand, cand_resolution = _tier2(cand)
                if cand.is_local:
                    # #llm-routing-postjune15 P2: the skill resolved to
                    # ``prefer_local``, so ``_decision_for_tier2`` just downgraded
                    # the cloud candidate back to the local Ollama lane. The skill
                    # prefers local, so the confidential→cloud override window does
                    # NOT apply — ``prefer_local`` outranks the window and the call
                    # stays local (the safe direction; never widens access). Made
                    # explicit so it doesn't depend on a local lane accidentally
                    # missing the window lookup.
                    override = None
                else:
                    # Key the window lookup off the tuple the sensitivity gate will
                    # itself use on its bypass path — same workspace id format, same
                    # lane→provider mapping — so both layers resolve the SAME row.
                    override = find_active_override(
                        skill=sctx.skill,
                        workspace=f"{sctx.workspace_type}:{sctx.workspace_name}",
                        provider=provider_for_override_lookup(cand.lane),
                        ceiling="confidential",
                    )
                    if override is not None:
                        logger.warning(
                            "skill llm() confidential call lifted to cloud by "
                            "override id=%s (skill=%s lane=%s provider=%s, expires "
                            "%s) — justification: %s",
                            override.id, sctx.skill, cand.lane, cand.provider,
                            # until-closed windows carry no expiry (#llm-routing-
                            # postjune15 P2) — expires_at is None.
                            override.expires_at.isoformat() if override.expires_at
                            else "until-closed",
                            override.justification[:80],
                        )
                        decision, resolution = cand, cand_resolution
            except Exception as e:  # noqa: BLE001 — consult must never widen access
                override = None
                logger.warning(
                    "sensitivity-override consult failed — staying on the "
                    "local lane (fail-closed): %s", e,
                )

    # Per-skill system prompt (#llm-skill-system-prompt). Precedence: an explicit
    # per-call ``system=`` override, else the skill's declared default, else
    # ``None`` (the dispatch helpers' ``_effective_system`` then applies the
    # platform persona — an un-migrated skill is byte-identical to before).
    effective_system = system or sctx.system_prompt

    # #no-mnpi-to-cloud BACKSTOP (was cited as §5.4; defense in depth,
    # codex-5.5 SEV-1): even if pick_lane, the
    # Tier-2 resolution or the override consult were ever wrong:
    #   * MNPI must NEVER reach a cloud lane — absolute, NOT liftable by any
    #     override (the ceiling enum already excludes MNPI at the policy,
    #     storage and endpoint layers; this is one more independent layer).
    #   * confidential may reach a cloud lane ONLY via the active override
    #     window found above (#llm-routing-override).
    # Fail-closed BEFORE the hook stack / any dispatch.
    if not decision.is_local:
        if sens_lower == "mnpi":
            raise SkillLLMRefused(
                f"§5.4: an {sctx.sensitivity!r} skill LLM call may not use a "
                f"cloud lane (provider={decision.provider!r}) — refusing "
                f"(never overridable)"
            )
        if sens_lower == "confidential" and override is None:
            raise SkillLLMRefused(
                f"§5.4: a {sctx.sensitivity!r} skill LLM call may not use a "
                f"cloud lane (provider={decision.provider!r}) without an active "
                f"sensitivity-override window — refusing"
            )

    hook_ctx = LLMCallHookContext(
        run_id=sctx.run_id,  # the wrapper always sets a concrete run_id
        skill=SkillRef(
            name=sctx.skill,
            metadata={
                "sensitivity": sctx.sensitivity,
                "cost_cap_tokens": sctx.cost_cap_tokens,
            },
        ),
        workspace=WorkspaceRef(type=sctx.workspace_type, name=sctx.workspace_name),
        sensitivity=sctx.sensitivity,
        lane=decision.lane,
        provider=decision.provider,
        model=decision.model,
        prompt=prompt,
        system=effective_system,
        usage={"lane": "skill", "skill": sctx.skill},
    )
    if override is not None:
        # Pre-stamp the authorising window for telemetry/audit. The sensitivity
        # gate independently re-finds the window on its own bypass path and
        # stamps the same id (defense in depth — if the window expired between
        # the consult above and the gate, the gate refuses and the call never
        # dispatches).
        hook_ctx.sensitivity_override_id = override.id

    # Per-skill sampling params (#llm-routing-tier-2) for the local lane too —
    # the cloud path stamps them inside _dispatch_cloud_llm.
    if decision.is_local:
        hook_ctx.llm_params = skill_registry.resolve_skill_provider(sctx.skill).llm_params

    # Bind the skill's run_id so the #67 per-skill llm-call cap (keyed on the
    # current_run_id contextvar, not hook_ctx.run_id) reliably counts THIS call
    # (codex-5.5 SEV-2).
    with bind_run_id(sctx.run_id):
        # 2. BEFORE hooks — FAIL-CLOSED. These include POLICY gates (the
        #    #no-mnpi-to-cloud sensitivity lane, was cited as §5.4, + #57
        #    budget), not just observability. A raised
        #    policy refusal OR any error in the gate chain REFUSES — we never
        #    fall through to a provider, because a blocked call is always safe
        #    and a proceeded one might leak (codex-5.5 SEV-1).
        try:
            proceed = run_before_llm_hooks(hook_ctx)
        except SensitivityViolation as e:
            raise SkillLLMRefused(f"sensitivity lane guard refused the call: {e}") from e
        except Exception as e:  # noqa: BLE001 — fail-closed: a guard-chain error blocks
            raise SkillLLMRefused(
                f"pre-call governance check errored — refusing (fail-closed): {e}"
            ) from e
        if proceed is False:
            reason = ""
            if isinstance(hook_ctx.usage, dict):
                block = hook_ctx.usage.get("budget_block") or {}
                reason = (
                    hook_ctx.usage.get("block_reason")
                    or (block.get("reason") if isinstance(block, dict) else "")
                    or ""
                )
            raise SkillLLMRefused(
                f"a pre-call guard blocked the LLM call{f': {reason}' if reason else ''}"
            )

        # TOCTOU recheck (codex slice-1 SEV-2): a confidential call on a cloud
        # lane must hold a STILL-ACTIVE override window AT DISPATCH TIME — an
        # operator close / expiry landing between the pre-pick consult (or the
        # gate's bypass) and this point must refuse, not dispatch. Re-stamp
        # with the currently-active row (a superseding window changes the id).
        # Any lookup trouble → refuse (fail-closed). MNPI cannot reach here —
        # the backstop above already refused any MNPI cloud decision.
        if not decision.is_local and sens_lower == "confidential":
            still_active = None
            try:
                still_active = find_active_override(
                    skill=sctx.skill,
                    workspace=f"{sctx.workspace_type}:{sctx.workspace_name}",
                    provider=provider_for_override_lookup(decision.lane),
                    ceiling="confidential",
                )
            except Exception as e:  # noqa: BLE001 — fail-closed
                logger.warning(
                    "dispatch-time override recheck errored — refusing "
                    "(fail-closed): %s", e,
                )
            if still_active is None:
                raise SkillLLMRefused(
                    "§5.4: the sensitivity-override window for this "
                    "confidential call is no longer active at dispatch time — "
                    "refusing"
                )
            hook_ctx.sensitivity_override_id = still_active.id

        # 3. Dispatch through the SAME helpers the chat path uses. after-hooks
        #    ALWAYS run (telemetry for a failed call too — codex-5.5 SEV-3), then
        #    any unexpected dispatch exception propagates to the skill.
        body = ""
        route_label = ""
        usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        dispatch_error: Optional[BaseException] = None
        try:
            if decision.is_local:
                body, route_label, usage = R._call_ollama(
                    decision, prompt, hook_ctx=hook_ctx, system=effective_system,
                )
            else:
                body, route_label, usage = R._dispatch_cloud_llm(
                    decision, prompt, hook_ctx=hook_ctx, resolution=resolution,
                    system=effective_system,
                )
        except BaseException as e:  # noqa: BLE001 — record telemetry, then re-raise
            dispatch_error = e
            route_label = f"ERROR · {type(e).__name__}"

        # 4. AFTER hooks — telemetry + audit (best-effort; never break the skill).
        status = "error" if (dispatch_error is not None or route_label.startswith("ERROR ·")) else "ok"
        hook_ctx.response = body
        if isinstance(hook_ctx.usage, dict):
            hook_ctx.usage.update({
                **usage, "route": route_label, "status": status,
                "error_class": type(dispatch_error).__name__ if dispatch_error else None,
            })
        try:
            run_after_llm_hooks(hook_ctx)
        except Exception as e:  # noqa: BLE001 — observability must not break the skill
            logger.warning("skill llm() after-hooks raised (suppressed): %s", e)

        if dispatch_error is not None:
            raise dispatch_error

    return LLMResult(
        text=body, route=route_label, lane=decision.lane, provider=decision.provider,
        model=decision.model, sensitivity=sctx.sensitivity, usage=usage,
    )


# Public API: skill bodies use ``llm()`` + handle ``SkillLLMRefused`` /
# ``SkillLLMUnavailable``. ``set_skill_llm_context`` / ``reset_skill_llm_context``
# / ``SkillLLMContext`` are WRAPPER-INTERNAL (importable, but not advertised) so a
# skill can't trivially forge its own context (codex-5.5 SEV-2).
__all__ = [
    "llm",
    "LLMResult",
    "SkillLLMUnavailable",
    "SkillLLMRefused",
    "current_skill_llm_context",
]
