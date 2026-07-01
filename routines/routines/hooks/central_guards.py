"""Central guard handlers, registered at app startup.

These move policy enforcement OUT of every skill and INTO a thin middleware
layer the hook factory routes through.

  1. ``enforce_sensitivity_lane`` — refuses cloud for confidential/MNPI
     per [[CLAUDE]] §4. Fails closed.
  2. ``audit_log`` — emits Skill[Started|Completed|Failed] triplet to the
     per-skill JSONL via ``routines.shared.audit.write``.
  3. ``enforce_cost_cap`` — token / wall-clock ceiling per invocation.
  4. ``enforce_workspace_policy`` — refuses writes outside the paths in
     [[workspace-write-policy]] §2.
  5. ``enforce_skill_sensitivity`` (#61) — hard gate for registered-skill
     tool calls: refuses a scoped skill on the wrong workspace type, or any
     skill on MNPI inputs. Reads ``workspace_scope`` from the skill registry,
     replacing the LBO route's hand-rolled gate.
  6. ``enforce_skill_preconditions`` (#74.2) — readiness routing: a registered
     skill with a ``requires:`` block (vault paths / fs roots that must exist)
     is refused at DISPATCH with a chat-friendly "not ready" message if any are
     missing, instead of crashing mid-run. No-op for skills without the block.

**Grandfather fallback.** Skills without SKILL.md frontmatter (everything
pre-#21) get safe defaults inferred from the workspace tier:
  * sensitivity → ``confidential`` for project/bd, ``internal`` for general
  * cost cap    → 10k total tokens, 60s wall-clock
  * workspace path policy → vault paths only, no writes to ``<vault>``

Once #21 mass migration lands, the grandfather block can be deleted — every
skill will declare its own metadata."""

from __future__ import annotations

import glob as _glob
import logging
from pathlib import Path
from typing import Any

from routines.hooks.decorators import (
    after_llm_call,
    after_tool_call,
    before_llm_call,
    before_tool_call,
)
from routines.hooks.events import (
    SkillInvocationCompleted,
    SkillInvocationFailed,
    SkillInvocationRefused,
    SkillInvocationResumed,
    SkillInvocationStarted,
    SkillInvocationSuspended,
)
from routines.hooks.event_bus import bridge_event_bus
from routines.hooks.types import (
    LLMCallHookContext,
    Sensitivity,
    ToolCallHookContext,
)
from routines.shared import audit
from routines.shared import routing as shared_routing
from routines.shared.write_policy import (
    STATIC_WRITE_ROOTS as _ALLOWED_WRITE_ROOTS,  # back-compat re-export
    WorkspacePolicyViolation,
    path_is_allowed as _path_is_allowed,  # back-compat (deal_tracker F-2, tests)
)
from routines.skills.registry import load_skill_metadata

logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────────────
# Guard exceptions
# ────────────────────────────────────────────────────────────────────────────


class SensitivityViolation(RuntimeError):
    """Raised (or signalled by returning False) when the chosen lane is too
    permissive for the resolved sensitivity tier."""


class CostCapExceeded(RuntimeError):
    """Raised when the invocation exceeds its token / wall-clock cap."""


class SkillScopeRefused(RuntimeError):
    """Raised by :func:`enforce_skill_sensitivity` when a registered skill's
    tool call violates its declared ``workspace_scope`` or runs on MNPI
    inputs. Route handlers catch this and map it to HTTP 403 — the same
    observable refusal the LBO route used to hand-roll, now enforced centrally
    for every skill that declares frontmatter."""


class SkillPreconditionsNotMet(RuntimeError):
    """Raised by :func:`enforce_skill_preconditions` (#74.2 readiness routing)
    when a registered skill declares a ``requires:`` block (a vault path / fs
    root that must exist) and one or more of those preconditions isn't satisfied
    at dispatch time. The app-level exception handler maps it to HTTP 409 with a
    chat-friendly "skill not ready" message — the skill fails FAST instead of
    crashing half-way through on the missing file."""


# ────────────────────────────────────────────────────────────────────────────
# Grandfather fallback (remove once #21 mass migration completes)
# ────────────────────────────────────────────────────────────────────────────


_WORKSPACE_DEFAULT_SENSITIVITY: dict[str, Sensitivity] = {
    "project": "confidential",
    "bd":      "confidential",
    "general": "internal",
}

# Global default ceiling — only consulted when SKILL.md doesn't declare its own.
_DEFAULT_TOKEN_CAP = 10_000
_DEFAULT_WALL_CLOCK_SECONDS = 60


def _grandfather_sensitivity(workspace_type: str) -> Sensitivity:
    """Fail-closed default. Anything not explicitly mapped → confidential."""
    return _WORKSPACE_DEFAULT_SENSITIVITY.get(workspace_type, "confidential")


def _resolve_sensitivity(ctx: LLMCallHookContext | ToolCallHookContext) -> Sensitivity:
    """SKILL.md frontmatter wins if present; otherwise grandfather."""
    declared = (
        ctx.skill.metadata.get("sensitivity")
        if ctx.skill.metadata else None
    )
    if declared in ("public", "internal", "confidential", "MNPI"):
        return declared  # type: ignore[return-value]
    return _grandfather_sensitivity(ctx.workspace.type)


def _resolve_token_cap(ctx: LLMCallHookContext) -> int:
    declared = ctx.skill.metadata.get("cost_cap_tokens") if ctx.skill.metadata else None
    if isinstance(declared, int) and declared > 0:
        return declared
    return _DEFAULT_TOKEN_CAP


# ────────────────────────────────────────────────────────────────────────────
# Lane → sensitivity matrix (the actual gate)
# ────────────────────────────────────────────────────────────────────────────


_LOCAL_LANES = {
    "ollama",
    "ollama-haiku",
    "ollama-embed",
    "ollama-multimodal",
}


def _is_local_lane(lane: str) -> bool:
    return lane in _LOCAL_LANES


def provider_for_override_lookup(lane: str) -> str:
    """Map a lane string → the provider name the sensitivity-overrides storage
    keys on (``'anthropic'`` / ``'openai'`` / ``'ollama'``). The override row is
    keyed by provider, not lane. Shared by the sensitivity gate's bypass consult
    below AND the skill ``llm()`` gateway's pre-pick consult
    (#llm-routing-override) so both layers resolve the SAME window row for a
    given decision."""
    if lane.startswith("codex"):
        return "openai"
    if lane.startswith("claude") or lane.startswith("anthropic"):
        return "anthropic"
    if _is_local_lane(lane):
        return "ollama"
    return lane


_SENSITIVITY_RANK: dict[Sensitivity, int] = {
    "MNPI": 0,           # most restrictive
    "confidential": 1,
    "internal": 2,
    "public": 3,         # least restrictive
}


def _strictest(a: Sensitivity, b: Sensitivity) -> Sensitivity:
    """Return the MORE restrictive of two tiers (lower rank = stricter).

    Fail-closed: an UNKNOWN/garbage tier is normalised to ``"MNPI"`` — the
    strictest KNOWN value — BEFORE comparison. (Ranking an unknown at 0 but
    returning the unknown string would be unsafe: ``_lane_allowed_for`` treats
    any unrecognised tier as PERMISSIVE — its final ``return True`` — so a raw
    unknown could OPEN the gate. Coercing to ``"MNPI"`` makes the downstream
    matrix correctly force local-only.) Used so the sensitivity-lane gate
    enforces the strictest of (a) the route-resolved ``ctx.sensitivity`` the
    dispatcher carries and (b) the skill-frontmatter/grandfather resolution —
    the gate must never act on a tier looser than EITHER signal."""
    if a not in _SENSITIVITY_RANK:
        a = "MNPI"
    if b not in _SENSITIVITY_RANK:
        b = "MNPI"
    return a if _SENSITIVITY_RANK[a] <= _SENSITIVITY_RANK[b] else b


def _lane_allowed_for(
    sensitivity: Sensitivity, lane: str, *, mnpi_explicit: bool = False,
) -> bool:
    """The decision matrix from [[CLAUDE]] §4.

    Rules:
      * MNPI                         → local only, UNLESS the call is EXPLICIT
                                       MNPI (``mnpi_explicit``) + enterprise tier
                                       + an active per-provider attestation, then
                                       local OR that provider's cloud lane
                                       (#llm-routing-postjune15 P5; default-off)
      * confidential, bridge tier    → local only
      * confidential, enterprise tier → local OR Claude Enterprise (claude-cli*)
      * internal / public            → any lane
    """
    if sensitivity == "MNPI":
        if _is_local_lane(lane):
            return True
        # #llm-routing-postjune15 P5 (enterprise-MNPI): a CLOUD lane is allowed
        # for MNPI ONLY when ALL THREE hold — the call is EXPLICIT MNPI
        # (operator-assigned, not an unknown→MNPI coercion), the plan tier is
        # ``enterprise``, AND the lane's provider holds an ACTIVE per-provider
        # attestation. This re-enforces the explicit-provenance + attestation
        # conditions INDEPENDENTLY of the decide_route lift, so two layers must
        # agree before MNPI reaches cloud. Default-off: empty store / bridge tier
        # / not-explicit → False (the absolute pre-P5 floor). Fail-closed on error.
        if not mnpi_explicit:
            return False
        try:
            from routines.mnpi_attestations import mnpi_cloud_allowed
            return mnpi_cloud_allowed(provider_for_override_lookup(lane))
        except Exception as e:  # noqa: BLE001 — fail-closed: refuse the cloud lane
            logger.warning(
                "MNPI attestation lookup failed for lane=%r (refusing cloud — "
                "fail-closed): %s", lane, e,
            )
            return False
    if sensitivity == "confidential":
        if _is_local_lane(lane):
            return True
        tier = shared_routing.plan_tier()
        return tier == "enterprise" and lane.startswith("claude-cli")
    if sensitivity in ("internal", "public"):
        return True
    # Unknown / unmapped tier → FAIL CLOSED, treated like MNPI (local only).
    # Defense in depth: the gate's _strictest() already normalises an unknown
    # ctx.sensitivity to MNPI before it reaches here, but a FUTURE direct caller
    # of this matrix that bypasses _strictest must not get a fail-OPEN `True`
    # for an unrecognised tier. (codex-5.5 blind-review SEV-2, 2026-06-06.)
    return _is_local_lane(lane)


def _provider_ceiling_allows(sensitivity: Sensitivity, lane: str) -> bool:
    """Per-provider sensitivity ceiling (#llm-routing-postjune15 P2 Task 5, folds
    #llm-routing-per-provider-tiers). A CLOUD provider with an EXPLICITLY
    configured ``providers.<name>.max_sensitivity`` (in _claude/profile.md) may
    handle a call only up to that ceiling. This is PURELY ADDITIVE: an
    UNCONFIGURED provider has no per-provider restriction (the §4 matrix + the
    override window remain the gates), so the default bridge/enterprise behaviour
    is unchanged until the operator caps a provider (e.g. 'openai sees public
    only') or raises one ('anthropic → confidential' once ZDR lands). Local lanes
    have no cloud ceiling. Composes with the override window — an active override
    bypasses this too (the sanctioned way to exceed a ceiling, LLM-ROUTING §E).
    Fail-closed only on a MISCONFIGURED ceiling VALUE (read but unrecognised); a
    config-read FAILURE defers to the §4 matrix (no added restriction).

    #llm-routing-postjune15 P5 interaction: this ceiling is ANDed with the §4
    matrix, so an explicitly-configured sub-MNPI ``max_sensitivity`` on a provider
    DENIES an MNPI call even if that provider holds an active MNPI attestation
    (deny-wins — an explicit operator cap is authoritative; the attestation does
    NOT override it). Unconfigured providers (the norm) add no restriction, so an
    attestation alone suffices there."""
    if _is_local_lane(lane):
        return True
    provider = provider_for_override_lookup(lane)
    try:
        from routines.shared import profile as _profile_mod
        ceiling = _profile_mod.provider_max_sensitivity(provider)
    except Exception as e:  # noqa: BLE001 — a config read must never crash the gate
        logger.warning(
            "provider-ceiling lookup failed for %r (no restriction applied — the "
            "§4 matrix + override remain the gates): %s", provider, e,
        )
        return True
    if ceiling is None:
        return True   # unconfigured provider → no per-provider restriction
    s_rank = _SENSITIVITY_RANK.get(sensitivity)
    c_rank = _SENSITIVITY_RANK.get(ceiling)
    if s_rank is None or c_rank is None:
        return False   # misconfigured ceiling value (or unknown call tier) → fail closed
    # Higher rank = looser; the call is allowed iff it is no STRICTER than the
    # ceiling (e.g. ceiling=internal permits internal + public, refuses
    # confidential + MNPI).
    return s_rank >= c_rank


# ────────────────────────────────────────────────────────────────────────────
# 1. Sensitivity-lane guard
# ────────────────────────────────────────────────────────────────────────────


@before_llm_call
def enforce_sensitivity_lane(ctx: LLMCallHookContext) -> bool | None:
    """Block the call if the dispatcher's chosen lane is too permissive.

    Returns ``False`` to block. The dispatcher should treat the block as a
    refusal — downgrade lanes, surface the SensitivityViolation reason to
    the user, do NOT silently retry on a cheaper lane.

    #llm-routing-override (Codex-review SEV-2 fix, 2026-06-03): when the
    standard refusal would fire, consult the sensitivity-overrides
    storage; an active override window for this (skill, workspace,
    provider, ceiling) tuple bypasses the refusal. **MNPI is never
    bypassable** — the override ceiling enum excludes MNPI at both the
    Pydantic and storage layers, but we also short-circuit here as
    defence in depth. Audit: the override's id is stamped on the ctx
    so post-hooks can record which override authorised the bypass.
    """
    # Defense-in-depth (#no-mnpi-to-cloud gate independence — was cited as
    # §5.4): take the STRICTEST of the
    # route-resolved tier the dispatcher carried (``ctx.sensitivity``) and the
    # skill-frontmatter/grandfather resolution. The gate must not trust a skill's
    # declared frontmatter sensitivity if the dispatcher already classified the
    # call more strictly (e.g. a future cloud-routing skill that declares
    # ``internal`` but runs on a confidential workspace). For chat both signals
    # are equal (route stuffs ``decision.sensitivity`` into both), so this is a
    # no-op there; it only ever ADDS restriction. Fail-closed.
    resolved = _strictest(ctx.sensitivity, _resolve_sensitivity(ctx))
    # Stamp the resolved value back on the context so downstream hooks /
    # audit see the same tier the gate decided on.
    ctx.sensitivity = resolved
    # Two independent allow conditions, both required: the §4 sensitivity↔lane
    # matrix AND the per-provider ceiling (#llm-routing-postjune15 P2 Task 5).
    # The override consult below bypasses EITHER refusal (LLM-ROUTING §E). The
    # ceiling is purely additive — an UNCONFIGURED provider adds no restriction —
    # so this is behaviour-identical until an operator caps/raises a provider (it
    # is NOT a "default internal" ceiling, which would regress enterprise).
    matrix_ok = _lane_allowed_for(
        resolved, ctx.lane, mnpi_explicit=getattr(ctx, "mnpi_explicit", False),
    )
    ceiling_ok = _provider_ceiling_allows(resolved, ctx.lane)
    if matrix_ok and ceiling_ok:
        return None

    # MNPI is absolute (CLAUDE.md §5.2, anchor #no-mnpi-to-cloud — was also
    # cited as §5.4) — never bypassed by an override, regardless of what's in
    # the DB.
    if resolved != "MNPI":
        try:
            from routines.sensitivity_overrides import find_active_override
            workspace_id = (
                f"{ctx.workspace.type}:{ctx.workspace.name}"
                if ctx.workspace else "unknown"
            )
            # Map lane → provider for the override lookup (shared with the
            # skill llm() gateway — see provider_for_override_lookup).
            provider = provider_for_override_lookup(ctx.lane)

            override = find_active_override(
                skill=ctx.skill.name,
                workspace=workspace_id,
                provider=provider,
                ceiling=resolved,
            )
            if override is not None:
                logger.info(
                    "sensitivity-lane refusal bypassed by override id=%s "
                    "(skill=%s workspace=%s provider=%s ceiling=%s) — "
                    "justification: %s",
                    override.id, ctx.skill.name, workspace_id, provider,
                    resolved, override.justification[:80],
                )
                # Audit-stamp so post-hooks can record which override
                # authorised the bypass.
                ctx.sensitivity_override_id = override.id
                return None
        except Exception as e:  # noqa: BLE001 — override lookup must never
            # crash the gate. A failure here defaults to the refusal path
            # below, which is the safe behaviour.
            logger.warning(
                "sensitivity-override lookup failed (proceeding with "
                "refusal): %s", e,
            )

    # Name the actual refusal cause: the §4 matrix, or the per-provider ceiling
    # when the matrix allowed the lane but the provider is capped below the call.
    if not matrix_ok:
        reason = f"sensitivity={resolved!r} forbids lane={ctx.lane!r}"
    else:
        reason = (
            f"provider ceiling for lane={ctx.lane!r} forbids "
            f"sensitivity={resolved!r}"
        )
    logger.warning(
        "sensitivity-lane gate refused: skill=%s %s", ctx.skill.name, reason,
    )
    raise SensitivityViolation(f"refused: {reason}")


# ────────────────────────────────────────────────────────────────────────────
# 2. Audit log
# ────────────────────────────────────────────────────────────────────────────


def _audit_dir() -> Path:
    """Audit JSONL lives at ``<routines-repo>/runs/``."""
    return Path(__file__).resolve().parents[2] / "runs"


@after_llm_call
def audit_log(ctx: LLMCallHookContext) -> None:
    """Append a post-call audit row. Independent of bus events — survives
    even when no Skill[Completed] is emitted (e.g. raw LLM call outside a
    skill context)."""
    try:
        audit.write_structured(
            actor={"type": "agent", "id": ctx.skill.name or "unknown"},
            entity_type="skill_run",
            entity_id=ctx.skill.name or "unknown",
            action="llm_call",
            routine=f"hook.llm.{ctx.skill.name or 'unknown'}",
            run_id=ctx.run_id,
            status="ok",
            audit_dir=_audit_dir(),
            inputs={
                "skill": ctx.skill.name,
                "sensitivity": ctx.sensitivity,
                "lane": ctx.lane,
                "provider": ctx.provider,
                "model": ctx.model,
                "workspace_type": ctx.workspace.type,
                "workspace_name": ctx.workspace.name,
            },
            outputs={"usage": ctx.usage} if ctx.usage else None,
            duration_ms=int(ctx.usage.get("duration_ms", 0)) if ctx.usage else None,
        )
    except Exception as e:  # noqa: BLE001 — audit must never break the call
        logger.warning("audit_log handler failed: %s", e)


def _on_skill_started(evt: SkillInvocationStarted) -> None:
    """Bus-side companion to ``audit_log`` — captures invocations that go
    through the bus without an LLM call (e.g. tool-only composites)."""
    try:
        audit.write_structured(
            actor={"type": "agent", "id": evt.skill},
            entity_type="skill_run",
            entity_id=evt.skill,
            action="start",
            routine=f"skill.{evt.skill}",
            run_id=evt.run_id,
            status="started",
            audit_dir=_audit_dir(),
            inputs={
                "sensitivity": evt.sensitivity,
                "workspace_type": evt.workspace_type,
                "workspace_name": evt.workspace_name,
                "inputs_hash": evt.inputs_hash,
            },
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("audit started handler failed: %s", e)


def _on_skill_completed(evt: SkillInvocationCompleted) -> None:
    try:
        audit.write_structured(
            actor={"type": "agent", "id": evt.skill},
            entity_type="skill_run",
            entity_id=evt.skill,
            action="complete",
            routine=f"skill.{evt.skill}",
            run_id=evt.run_id,
            status="ok",
            audit_dir=_audit_dir(),
            inputs={
                "sensitivity": evt.sensitivity,
                "lane": evt.lane,
                "workspace_type": evt.workspace_type,
                "workspace_name": evt.workspace_name,
            },
            outputs={"tokens": evt.tokens} if evt.tokens else None,
            duration_ms=evt.duration_ms or None,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("audit completed handler failed: %s", e)


def _on_skill_failed(evt: SkillInvocationFailed) -> None:
    try:
        audit.write_structured(
            actor={"type": "agent", "id": evt.skill},
            entity_type="skill_run",
            entity_id=evt.skill,
            action="fail",
            routine=f"skill.{evt.skill}",
            run_id=evt.run_id,
            status="error",
            audit_dir=_audit_dir(),
            inputs={
                "workspace_type": evt.workspace_type,
                "workspace_name": evt.workspace_name,
            },
            error=f"{evt.error_class}: {evt.error}" if evt.error_class else evt.error,
            duration_ms=evt.duration_ms or None,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("audit failed handler failed: %s", e)


def _on_skill_refused(evt: SkillInvocationRefused) -> None:
    """#anton-skill-refusal-audit — a #no-mnpi-to-cloud scope/MNPI refusal (was
    cited as §5.4) caught at the
    ``@anton_skill`` perimeter (before admission) leaves NO after-hook audit row,
    so this bus subscriber is the only trail for the platform's most
    safety-critical gate. Records workspace + the requested/resolved tiers + the
    refusal reason; NEVER the request payload (which may carry the very MNPI
    content the gate refused). ``status='refused'`` distinguishes a clean
    perimeter refusal from an errored run in the audit feed."""
    try:
        audit.write_structured(
            actor={"type": "agent", "id": evt.skill},
            entity_type="skill_run",
            entity_id=evt.skill,
            action="refuse",
            routine=f"skill.{evt.skill}",
            run_id=evt.run_id,
            status="refused",
            audit_dir=_audit_dir(),
            inputs={
                "requested_sensitivity": evt.requested_sensitivity,
                "sensitivity": evt.sensitivity,
                "workspace_type": evt.workspace_type,
                "workspace_name": evt.workspace_name,
            },
            error=evt.reason,
            duration_ms=evt.duration_ms or None,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("audit refused handler failed: %s", e)


def _on_skill_suspended(evt: SkillInvocationSuspended) -> None:
    """#63 phase 2b — a suspended run is a clean cooperative pause (the 4th
    terminal exit), audited as ``status='suspended'`` so the feed shows the run
    is waiting on the operator, not errored."""
    try:
        audit.write_structured(
            actor={"type": "agent", "id": evt.skill},
            entity_type="skill_run",
            entity_id=evt.skill,
            action="suspend",
            routine=f"skill.{evt.skill}",
            run_id=evt.run_id,
            status="suspended",
            audit_dir=_audit_dir(),
            inputs={
                "sensitivity": evt.sensitivity,
                "workspace_type": evt.workspace_type,
                "workspace_name": evt.workspace_name,
                # prompt only — never the persisted state (which is sanitized
                # but still an internal continuation checkpoint, not audit).
                "prompt": evt.prompt,
                "expires_at": evt.expires_at,
            },
            duration_ms=evt.duration_ms or None,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("audit suspended handler failed: %s", e)


def _on_skill_resumed(evt: SkillInvocationResumed) -> None:
    """#63 phase 2b — the operator answered; the body re-ran. The resumed
    segment then ends in its own Completed/Failed/Suspended row."""
    try:
        audit.write_structured(
            actor={"type": "agent", "id": evt.skill},
            entity_type="skill_run",
            entity_id=evt.skill,
            action="resume",
            routine=f"skill.{evt.skill}",
            run_id=evt.run_id,
            status="resumed",
            audit_dir=_audit_dir(),
            inputs={
                "sensitivity": evt.sensitivity,
                "workspace_type": evt.workspace_type,
                "workspace_name": evt.workspace_name,
            },
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("audit resumed handler failed: %s", e)


# ────────────────────────────────────────────────────────────────────────────
# 2b. Audit log — tool side
# ────────────────────────────────────────────────────────────────────────────


def _summarise_result(result: Any) -> Any:
    """Trim a result blob for the audit log.

    Full skill outputs can be megabytes (e.g. equity-research with 6 peers
    + 12 news items). The audit log shouldn't carry that. Reduce to a
    compact summary:
      * dict → keep top-level keys; for list values, record length
      * list → length only
      * primitive → passthrough
      * else → ``str(...)`` capped at 200 chars
    """
    if isinstance(result, dict):
        out: dict[str, Any] = {}
        for k, v in list(result.items())[:32]:
            if isinstance(v, list):
                out[k] = {"_list_len": len(v)}
            elif isinstance(v, dict):
                out[k] = {"_dict_keys": list(v.keys())[:16]}
            elif isinstance(v, (str, int, float, bool)) or v is None:
                out[k] = v
            else:
                out[k] = str(type(v).__name__)
        return out
    if isinstance(result, list):
        return {"_list_len": len(result)}
    if isinstance(result, (str, int, float, bool)) or result is None:
        return result
    s = str(result)
    return s[:200] + ("..." if len(s) > 200 else "")


@after_tool_call
def audit_tool_call(ctx: ToolCallHookContext) -> None:
    """Append a per-call audit row to ``runs/tool.<tool_name>.jsonl``.

    Mirrors the LLM-side ``audit_log`` pattern. Independent of bus events
    — survives even when no Skill[Completed] is emitted (most skill
    invocations today don't push to the bus)."""
    try:
        audit.write_structured(
            actor={"type": "agent", "id": f"tool:{ctx.tool_name or 'unknown'}"},
            entity_type="skill_run",
            entity_id=ctx.tool_name or "unknown",
            action="tool_call",
            routine=f"tool.{ctx.tool_name or 'unknown'}",
            run_id=ctx.run_id,
            status=str(ctx.usage.get("status", "ok")),
            audit_dir=_audit_dir(),
            inputs={
                "tool": ctx.tool_name,
                "tool_input": ctx.tool_input,
                "sensitivity": ctx.sensitivity,
                "workspace_type": ctx.workspace.type if ctx.workspace else None,
                "workspace_name": ctx.workspace.name if ctx.workspace else None,
            },
            outputs={"result_summary": _summarise_result(ctx.result)} if ctx.result is not None else None,
            duration_ms=int(ctx.usage.get("duration_ms")) if ctx.usage.get("duration_ms") is not None else None,
            error=(
                f"{ctx.usage.get('error_class')}: {ctx.usage.get('error_message')}"
                if ctx.usage.get("status") == "error" else None
            ),
        )
    except Exception as e:  # noqa: BLE001 — audit must never break the call
        logger.warning("audit_tool_call handler failed: %s", e)


# ────────────────────────────────────────────────────────────────────────────
# 3. Cost cap
# ────────────────────────────────────────────────────────────────────────────


@after_llm_call
def enforce_cost_cap(ctx: LLMCallHookContext) -> None:
    """Refuse usage that exceeds the per-invocation token cap.

    Cap source: SKILL.md ``cost_cap_tokens`` if declared, else the global
    grandfather default (10k tokens). Wall-clock isn't enforced here — the
    dispatcher times out at the provider level."""
    if not ctx.usage:
        return
    total = int(ctx.usage.get("total_tokens", 0))
    if not total:
        return
    cap = _resolve_token_cap(ctx)
    if total > cap:
        logger.warning(
            "cost cap exceeded: skill=%s total=%d cap=%d",
            ctx.skill.name, total, cap,
        )
        raise CostCapExceeded(
            f"skill={ctx.skill.name!r} consumed {total} tokens; cap was {cap}"
        )


# ────────────────────────────────────────────────────────────────────────────
# 4. Workspace policy
# ────────────────────────────────────────────────────────────────────────────


# The allowlist + matcher moved to ``routines.shared.write_policy`` with F-4
# (#sec-workspace-policy-chokepoint) so the LLM tool-call guard below, the
# deal-tracker workbook sandbox (F-2) and the ``atomic_write``/``atomic_move``
# primitives all enforce the SAME policy. ``_ALLOWED_WRITE_ROOTS`` (the static
# absolute roots) and ``_path_is_allowed`` are re-exported above for
# back-compat; the policy now ALSO accepts the vault-anchored sandbox
# prefixes and refuses the constitution deny-set — see the write_policy
# module docstring.

_WRITE_TOOL_NAMES = {"vault_write", "fs_write", "file_write", "write"}


@before_tool_call
def enforce_workspace_policy(ctx: ToolCallHookContext) -> bool | None:
    """Refuse writes outside allowed roots.

    Only fires for tools in ``_WRITE_TOOL_NAMES`` (no filter kwarg on the
    decorator because tool names are still in flux pre-#21 — the gate
    itself does the matching)."""
    if ctx.tool_name not in _WRITE_TOOL_NAMES:
        return None
    path = ctx.tool_input.get("path") or ctx.tool_input.get("file_path")
    if not path:
        # Write tool with no explicit path → reject; we can't validate.
        logger.warning(
            "workspace policy refused: tool=%s no path in input", ctx.tool_name,
        )
        raise WorkspacePolicyViolation(
            f"tool={ctx.tool_name!r} called without an explicit path argument"
        )
    if not _path_is_allowed(str(path)):
        logger.warning(
            "workspace policy refused: tool=%s path=%s",
            ctx.tool_name, path,
        )
        raise WorkspacePolicyViolation(
            f"tool={ctx.tool_name!r} refused write to {path!r} — outside allowed roots"
        )
    return None


# ────────────────────────────────────────────────────────────────────────────
# 5. Skill sensitivity / workspace-scope guard (#61)
# ────────────────────────────────────────────────────────────────────────────


@before_tool_call
def enforce_skill_sensitivity(ctx: ToolCallHookContext) -> bool | None:
    """Hard gate for skill tool calls — the central guard that replaces the
    LBO route's hand-rolled workspace/MNPI check (#61).

    Fires for every ``@before_tool_call`` dispatch, but only acts when the
    tool name resolves to a registered skill (``load_skill_metadata`` raises
    ``KeyError`` for non-skill tools like ``vault_write`` / ``comps_pull`` —
    those pass straight through). For a registered skill it reads the skill's
    declared ``workspace_scope`` from the registry and the call's resolved
    ``sensitivity`` from the context, then refuses:

      * a scoped skill (``workspace_scope`` ≠ ``any``) invoked on a workspace
        whose type doesn't match its scope — e.g. the project-only LBO skill
        on a general/bd workspace;
      * any skill invoked on MNPI inputs (CLAUDE.md §5.2 — confidential skills
        never run on MNPI data; wait for embargo lift).

    Raises :class:`SkillScopeRefused` (route → 403). Every migrated skill
    inherits this gate by declaring frontmatter — no per-route code."""
    try:
        meta = load_skill_metadata(ctx.tool_name)
    except KeyError:
        return None  # not a registered skill — nothing to gate

    ws_type = ctx.workspace.type if ctx.workspace else None
    if meta.workspace_scope != "any" and ws_type != meta.workspace_scope:
        logger.warning(
            "skill scope gate refused: skill=%s scope=%s workspace=%s",
            ctx.tool_name, meta.workspace_scope, ws_type,
        )
        raise SkillScopeRefused(
            f"workspace is {ws_type}; {ctx.tool_name} requires a "
            f"{meta.workspace_scope} workspace"
        )

    if ctx.sensitivity == "MNPI":
        logger.warning(
            "skill sensitivity gate refused: skill=%s sensitivity=MNPI",
            ctx.tool_name,
        )
        raise SkillScopeRefused(
            f"{ctx.tool_name} does not run on MNPI inputs — wait for embargo "
            f"lift (CLAUDE.md §5.2)"
        )

    return None


# ────────────────────────────────────────────────────────────────────────────
# 6. Skill readiness preconditions (#74.2 readiness routing)
# ────────────────────────────────────────────────────────────────────────────


def _vault_path_satisfied(vault_root: Path, entry: str) -> bool:
    """True if a vault-relative ``requires.vault_paths`` entry resolves to at
    least one existing path under ``vault_root``. A glob (``Sectors/**``) is
    satisfied by ANY match; a literal (``Registers/Lessons.md``) by existence.

    Fail-closed backstop (validation rejects these for registered skills; this
    guards stub/unvalidated callers): an ABSOLUTE entry — leading ``/`` or a
    drive root ``X:/…`` — or a ``..`` traversal segment can't be UNDER the vault
    root. Critically, ``vault_root / 'X:/foo'`` DISCARDS ``vault_root`` on
    Windows (a drive-absolute right operand), so it would escape; we refuse it
    rather than join (codex-5.5 R2)."""
    rel = entry.strip()
    if not rel:
        return False
    norm = rel.replace("\\", "/")
    # Absolute (leading-slash or drive-root) → not vault-relative → fail closed.
    if norm.startswith("/") or (len(norm) >= 2 and norm[1] == ":" and norm[0].isalpha()):
        return False
    if ".." in norm.split("/"):
        return False
    if _glob.has_magic(norm):
        return any(vault_root.glob(norm))
    return (vault_root / norm).exists()


def _resolve_vault_root() -> Path | None:
    """The live vault root as an ABSOLUTE path, or ``None`` if it can't be
    resolved / isn't absolute / is blank.

    Returning ``None`` makes every vault precondition fail-closed (→ "not ready"
    / HTTP 409) rather than (a) escaping as an unhandled 500 or (b) resolving a
    blank/relative root against the process cwd — which would WRONGLY satisfy a
    precondition from wherever the bridge happens to be running (codex-5.5 R2).
    Absoluteness uses a deterministic string check, NOT ``Path.is_absolute()``
    (whose answer for a WSL ``/mnt/x`` root differs on a Windows host)."""
    try:
        from routines.api import deps  # lazy: avoid the import at module load
        raw = deps.VAULT
    except Exception:  # noqa: BLE001
        return None
    if not raw:
        return None
    s = str(raw).replace("\\", "/")
    is_abs = s.startswith("/") or (
        len(s) >= 3 and s[1] == ":" and s[0].isalpha() and s[2] == "/"
    )
    if not is_abs:
        return None
    try:
        return Path(raw)
    except Exception:  # noqa: BLE001
        return None


def _fs_root_satisfied(entry: str) -> bool:
    """True if an absolute ``requires.fs_roots`` entry exists. A glob is
    satisfied by any match (``recursive=True`` so ``**`` matches the vault-side
    ``Path.glob`` recursive semantics); a literal by existence.

    Fail-closed backstop (parity with ``_vault_path_satisfied``; validation
    rejects these for registered skills): a ``..`` traversal or a NON-absolute
    entry returns False — a relative ``fs_roots`` entry would otherwise resolve
    against the process cwd (codex-5.5 R3 SEV-3)."""
    e = entry.strip()
    if not e:
        return False
    norm = e.replace("\\", "/")
    if ".." in norm.split("/"):
        return False
    is_abs = norm.startswith("/") or (
        len(norm) >= 3 and norm[1] == ":" and norm[0].isalpha() and norm[2] == "/"
    )
    if not is_abs:
        return False
    if _glob.has_magic(e):
        return bool(_glob.glob(e, recursive=True))
    return Path(e).exists()


def _unmet_preconditions(pre: Any) -> list[str]:
    """Return chat-friendly descriptions of each unmet precondition (empty list
    = all satisfied). Vault paths resolve against the LIVE vault root (read from
    ``routines.api.deps`` at call time so the check follows the running bridge's
    configured vault); fs roots resolve against the filesystem. EVERY resolution
    is fail-closed: a path-resolution error — INCLUDING an unresolvable vault
    root (``deps.VAULT`` unset/invalid) — is treated as UNMET so a broken
    declaration surfaces as "not ready" (HTTP 409) rather than silently passing
    OR escaping as an unhandled 500."""
    missing: list[str] = []

    vault_paths = tuple(getattr(pre, "vault_paths", ()))
    if vault_paths:
        vault_root = _resolve_vault_root()  # absolute Path, or None (→ all unmet)
        for entry in vault_paths:
            ok = False
            if vault_root is not None:
                try:
                    ok = _vault_path_satisfied(vault_root, entry)
                except Exception:  # noqa: BLE001 — broken declaration is "not ready"
                    ok = False
            if not ok:
                missing.append(f"vault path {entry!r} not found")

    for entry in getattr(pre, "fs_roots", ()):
        try:
            ok = _fs_root_satisfied(entry)
        except Exception:  # noqa: BLE001
            ok = False
        if not ok:
            missing.append(f"filesystem path {entry!r} not found")
    return missing


@before_tool_call
def enforce_skill_preconditions(ctx: ToolCallHookContext) -> bool | None:
    """Fail fast at dispatch when a registered skill's declared readiness
    preconditions (#74.2 ``requires:`` block) aren't met — the Thoth "readiness
    routing" pattern: validate at dispatch + fail with a chat-friendly message,
    instead of letting the skill crash half-way through on a missing file.

    Fires for every ``@before_tool_call`` dispatch, but only ACTS when the tool
    name resolves to a registered skill that DECLARES a ``requires:`` block.
    Non-skill tools (``load_skill_metadata`` → ``KeyError``) and skills without
    preconditions pass straight through — a pure no-op with no filesystem
    access. Today no SKILL.md declares ``requires:``, so this is
    mechanism-ahead-of-consumers (like #74.5); it goes live the moment a skill
    opts in.

    Raises :class:`SkillPreconditionsNotMet` (app handler → HTTP 409) listing
    exactly which declared path(s) are missing."""
    try:
        meta = load_skill_metadata(ctx.tool_name)
    except KeyError:
        return None  # not a registered skill — nothing to gate

    pre = getattr(meta, "preconditions", None)
    if pre is None or (not pre.vault_paths and not pre.fs_roots):
        return None  # no declared preconditions — no-op

    missing = _unmet_preconditions(pre)
    if missing:
        logger.warning(
            "skill preconditions unmet: skill=%s missing=%s",
            ctx.tool_name, missing,
        )
        raise SkillPreconditionsNotMet(
            f"{ctx.tool_name} isn't ready — {len(missing)} precondition(s) "
            f"unmet: " + "; ".join(missing) + ". Create the missing path(s), "
            "then re-run."
        )
    return None


# ────────────────────────────────────────────────────────────────────────────
# Public wiring helper
# ────────────────────────────────────────────────────────────────────────────


def register_central_guards() -> None:
    """Wire bus-side audit handlers. The four decorator-based guards register
    themselves at import time (via the ``@before_*`` / ``@after_*`` decorators);
    this function adds the bus subscribers for Skill[Started|Completed|Failed].

    Idempotent — safe to call multiple times under FastAPI reload:
    ``BridgeEventBus.on()`` identity-checks before append, so re-running this
    on lifespan re-entry leaves each subscriber registered exactly once
    instead of stacking copies (which duplicated 'start'/'complete'/'fail'
    audit rows per skill lifecycle event after any reload).
    ``tests/hooks/test_central_guards_idempotent.py`` pins the contract."""
    bridge_event_bus.on(SkillInvocationStarted)(_on_skill_started)
    bridge_event_bus.on(SkillInvocationCompleted)(_on_skill_completed)
    bridge_event_bus.on(SkillInvocationFailed)(_on_skill_failed)
    bridge_event_bus.on(SkillInvocationSuspended)(_on_skill_suspended)
    bridge_event_bus.on(SkillInvocationResumed)(_on_skill_resumed)
    bridge_event_bus.on(SkillInvocationRefused)(_on_skill_refused)


__all__ = [
    "SensitivityViolation",
    "CostCapExceeded",
    "WorkspacePolicyViolation",
    "SkillScopeRefused",
    "SkillPreconditionsNotMet",
    "enforce_sensitivity_lane",
    "audit_log",
    "audit_tool_call",
    "enforce_cost_cap",
    "enforce_workspace_policy",
    "enforce_skill_sensitivity",
    "enforce_skill_preconditions",
    "register_central_guards",
]
