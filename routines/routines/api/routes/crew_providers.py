"""Crew cloud-promotion routes (#crew-cloud-promotion, Phase A) — loopback-only.

Three endpoints, all loopback-only (the crew subprocess + the dashboard both
live on the bridge host; same guard pattern as ``skills_providers`` /
``sensitivity_overrides`` / ``lane_status``):

  * ``POST  /api/crew/_llm``            — the GATED LLM route-through for a
    PROMOTED crew subprocess. This is the load-bearing endpoint: it is the ONLY
    way a promoted crew reaches a frontier cloud model, and it keeps the crew
    subprocess CREDENTIAL-FREE (the load-bearing containment layer). It
    authenticates the caller by its run_id against the live-run registry,
    re-derives the role's lane/model/sensitivity SERVER-SIDE (never trusting the
    request), runs the SAME central sensitivity + budget gates the chat/skill
    paths use, then dispatches through the shared cloud dispatcher. MNPI /
    confidential are fail-closed here independently of the override resolver.

  * ``GET   /api/crew/providers``       — the per-crew promotion matrix for the
    dashboard Crews section (which crews are promotable, current overrides).

  * ``PATCH /api/crew/{verb}/provider`` — write/clear a crew (or per-role)
    promotion in the operator sidecar (``_claude/crew_overrides.yaml``).

NEVER imports metagpt (the crew boundary stays ``routines.crew.proxy``). The
cloud dispatch helpers are imported lazily inside the handler so this module
stays light + avoids any import-order coupling to ``sessions.router``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from routines.crew import overrides as crew_overrides
from routines.crew import registry, run_context
from routines.hooks.central_guards import CostCapExceeded, SensitivityViolation
from routines.hooks.decorators import run_after_llm_hooks, run_before_llm_hooks
from routines.hooks.types import LLMCallHookContext, SkillRef, WorkspaceRef
from routines.shared import routing as shared_routing

log = logging.getLogger(__name__)

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "testclient"})


def _loopback_only(request: Request) -> None:
    """Refuse any non-loopback client (same pattern as skills_providers). The
    crew subprocess + the dashboard are both on the bridge host; nothing remote
    has any business calling these — least-privilege for the one endpoint that
    can reach a cloud model on a crew's behalf."""
    client_host = request.client.host if request.client else None
    if client_host not in _LOOPBACK_HOSTS:
        log.warning("crew-providers endpoint refused non-loopback client %r", client_host)
        raise HTTPException(
            status_code=403,
            detail=f"crew-providers endpoints are loopback-only; refusing {client_host!r}",
        )


router = APIRouter(
    prefix="/api/crew",
    tags=["crew"],
    dependencies=[Depends(_loopback_only)],
)


# ─────────────────────────────────────────────────────────────────────────────
# POST /api/crew/_llm — the gated route-through
# ─────────────────────────────────────────────────────────────────────────────


class CrewLLMRequest(BaseModel):
    """A promoted crew role's LLM call, posted back to the bridge.

    The subprocess supplies ONLY its run_id (self-auth), the role making the
    call, and the prompt/system text. Everything that decides WHERE the call
    goes — the lane, the model, the sensitivity tier — is re-derived
    server-side from the run context; the request cannot widen its own access."""

    model_config = ConfigDict(extra="forbid")
    run_id: str
    role: str
    prompt: str
    system: Optional[str] = None


class CrewLLMResponse(BaseModel):
    run_id: str
    role: str
    lane: str
    provider: str
    model: str
    content: str
    route_label: str
    usage: dict[str, int] = Field(default_factory=dict)


@router.post("/_llm", response_model=CrewLLMResponse)
def crew_llm(req: CrewLLMRequest) -> CrewLLMResponse:
    """Serve one promoted crew role's LLM call through the gated cloud dispatcher.

    Fail-closed at every step:
      * unknown / ended run_id            → 404 (not a live run)
      * role not promoted for this run    → 403 (the run context is authoritative)
      * sensitivity gate refuses the lane → 403 (MNPI/confidential never reach
        cloud here — the gate re-checks independently of the override resolver)
      * budget gate blocks                → 402
      * cloud dispatch returns an ERROR   → 502 (so the crew role fails loudly
        rather than ingesting an error string as analysis)

    The crew subprocess holds NO cloud credentials; the dispatch (subprocess /
    API / degrade-to-local) runs entirely bridge-side."""
    rc = run_context.get(req.run_id)
    if rc is None:
        # No live run with this id — ended, never existed, or a fully-local run
        # that was never registered. Don't disclose which.
        raise HTTPException(404, "no live promotable crew run for that run_id")

    rp = rc.cloud_roles.get(req.role)
    if rp is None:
        # The run is live but THIS role was not promoted — the crew should only
        # call the bridge for a promoted role. Refuse rather than route.
        raise HTTPException(
            403, f"role {req.role!r} is not promoted to a cloud lane for this run",
        )

    # Re-derive the dispatch target SERVER-SIDE from the authoritative context.
    provider, _default_model = shared_routing.lane_to_model(rp.lane)
    model = rp.model

    hook_ctx = LLMCallHookContext(
        run_id=rc.run_id,
        skill=SkillRef(
            name=f"crew.{rc.verb}",
            metadata={
                "sensitivity": rc.sensitivity,
                "cost_cap_tokens": rc.cost_cap_tokens,
            },
        ),
        workspace=WorkspaceRef(type=rc.workspace_type, name=rc.workspace_name),
        sensitivity=rc.sensitivity,         # SERVER-recorded tier — the gate input
        lane=rp.lane,
        provider=provider,
        model=model,
        prompt=req.prompt,
        system=req.system,
        # Crews carry operator-assigned MNPI via the manifest lock; only that
        # provenance is ever eligible for the P5 lift (Phases C). For Phase A
        # promotions (public/internal) this is always False.
        mnpi_explicit=rc.mnpi_explicit,
    )

    # ── The gate. enforce_sensitivity_lane RAISES SensitivityViolation on a
    #    refusal (MNPI/confidential → cloud is refused here, fail-closed, even if
    #    a forged context somehow carried a cloud lane); enforce_budget_gate
    #    returns False + stashes the block on ctx.usage. Identical chain to the
    #    chat path (sessions/router.py).
    try:
        proceed = run_before_llm_hooks(hook_ctx)
    except SensitivityViolation as e:
        log.warning(
            "crew _llm refused: run=%s verb=%s role=%s lane=%s sensitivity=%s — %s",
            rc.run_id, rc.verb, req.role, rp.lane, rc.sensitivity, e,
        )
        raise HTTPException(403, f"refused: {e}") from e
    if not proceed:
        block = hook_ctx.usage.get("budget_block") if isinstance(hook_ctx.usage, dict) else None
        reason = (block or {}).get("reason") if isinstance(block, dict) else None
        raise HTTPException(402, f"budget gate blocked the promoted crew call: {reason or 'cap reached'}")

    # Stamp the attestation that authorises an MNPI cloud send onto the audit
    # trail (#crew-cloud-promotion Phase C — mirrors the chat path's decide_route).
    # The gate above ALREADY confirmed an active attestation exists for this
    # provider (an unattested provider would have raised SensitivityViolation), so
    # this only records WHICH attestation authorised the send + feeds the dispatch
    # fallback re-stamp. Best-effort, audit-only: a lookup miss never breaks the
    # (already gate-approved) call.
    if (
        hook_ctx.sensitivity == "MNPI"
        and getattr(hook_ctx, "mnpi_explicit", False)
        and not rp.lane.startswith("ollama")
    ):
        try:
            from routines.mnpi_attestations import find_active_attestation
            att = find_active_attestation(provider=provider)
            if att is not None:
                hook_ctx.mnpi_attestation_id = att.id
        except Exception:  # noqa: BLE001 — audit-only; never break the call
            log.warning(
                "crew _llm: MNPI attestation-id lookup failed (audit only) "
                "run=%s role=%s", rc.run_id, req.role, exc_info=True,
            )

    # ── Dispatch through the shared cloud dispatcher (lazy import avoids any
    #    import-order coupling). The sensitivity + budget gates already ran, so
    #    these helpers — like the chat path — assume the lane is cloud-approved.
    from routines.sessions.router import (  # noqa: PLC0415 — lazy by design
        RouteDecision,
        _dispatch_cloud_claude,
        _dispatch_cloud_codex,
    )

    decision = RouteDecision(
        sensitivity=hook_ctx.sensitivity,
        lane=rp.lane,
        provider=provider,
        model=hook_ctx.model,
        mnpi_explicit=hook_ctx.mnpi_explicit,
        mnpi_attestation_id=hook_ctx.mnpi_attestation_id,
    )

    # Dispatch by the resolved PROVIDER (anthropic→Claude, openai→Codex), not by
    # exact lane string — robust if lane aliases ever change while the provider
    # mapping stays stable (codex review). rp.provider is set by the resolver
    # (_PROVIDER_LANE), so it is always "anthropic" or "openai" here.
    if rp.provider == "anthropic":
        body, route_label, usage = _dispatch_cloud_claude(
            decision, req.prompt, hook_ctx=hook_ctx, system=req.system,
        )
    elif rp.provider == "openai":
        body, route_label, usage = _dispatch_cloud_codex(
            decision, req.prompt, hook_ctx=hook_ctx, system=req.system,
        )
    else:
        # The server resolved the provider, so this is unreachable — but never
        # dispatch a provider we don't recognise.
        raise HTTPException(500, f"promoted provider {rp.provider!r} has no cloud dispatch path")

    # Merge token usage onto the ctx so the after-hooks (audit + cost cap) see it,
    # then run them — same telemetry trail as any cloud call. Best-effort: an
    # audit/cost-cap raise must not turn a completed call into a 500.
    if isinstance(hook_ctx.usage, dict):
        hook_ctx.usage.update(usage)
    hook_ctx.response = body
    try:
        run_after_llm_hooks(hook_ctx)
    except CostCapExceeded as e:
        log.warning(
            "crew _llm cost cap (post-call; call already made) run=%s role=%s lane=%s: %s",
            rc.run_id, req.role, rp.lane, e,
        )
    except Exception as e:  # noqa: BLE001 — telemetry must never break the call
        log.warning(
            "crew _llm after-hooks failed (telemetry loss) run=%s role=%s lane=%s: %s",
            rc.run_id, req.role, rp.lane, e,
        )

    # A cloud ERROR route (provider unwired/unavailable, no fallback) → 502 so the
    # crew role fails loudly instead of ingesting the error string as analysis. A
    # credit-exhaustion DEGRADE returns a real (local) answer + a non-ERROR label,
    # which passes through as a 200 — sensitivity-safe (local only tightens).
    if route_label.startswith("ERROR ·"):
        raise HTTPException(502, f"cloud dispatch failed for promoted crew role: {route_label}")

    return CrewLLMResponse(
        run_id=rc.run_id, role=req.role, lane=rp.lane, provider=provider,
        model=hook_ctx.model, content=body, route_label=route_label, usage=usage,
    )


# ─────────────────────────────────────────────────────────────────────────────
# GET /api/crew/providers — the promotion matrix
# ─────────────────────────────────────────────────────────────────────────────


class CrewProviderRow(BaseModel):
    verb: str
    description: str
    roles: list[str]
    # The manifest sensitivity lock (e.g. triage = MNPI), or None for inherit.
    sensitivity_lock: Optional[str] = None
    # Whether the dashboard should offer a cloud option for this crew. An
    # unlocked crew is always promotable (the per-run workspace tier still gates
    # at launch); a confidential-locked crew only under enterprise (Phase B); an
    # MNPI-locked crew only under enterprise + an active attestation (Phase C).
    promotable: bool
    # The live plan tier permits the enterprise lift (confidential→cloud, and a
    # prerequisite for MNPI). Surfaced so the modal can explain WHY a locked crew
    # is / isn't promotable right now.
    enterprise: bool = False
    # MNPI-locked AND currently liftable (enterprise + ≥1 attestable provider has
    # an active attestation). The modal still checks per-PROVIDER attestation
    # before enabling Promote; this is the coarse "show the affordance" flag.
    mnpi_promotable: bool = False
    # Local per-role models (the manifest default — what a non-promoted role runs).
    models_default: dict[str, str] = Field(default_factory=dict)
    # The raw operator sidecar entry, if any (so the UI can show + clear it).
    override: Optional[dict[str, Any]] = None
    # Per-role cloud lane as the operator's promotion INTENT resolves under the
    # crew's real ceiling + the live tier/attestations (see _crew_row).
    promoted_roles: dict[str, str] = Field(default_factory=dict)


class CrewProvidersResponse(BaseModel):
    crews: list[CrewProviderRow]
    sidecar_path: str
    plan_tier: str
    as_of: datetime


def _any_active_attestation() -> bool:
    """True if ANY attestable provider currently holds an active attestation —
    the coarse "MNPI is liftable somewhere" signal for the matrix. The modal
    fetches the per-provider detail from ``GET /api/mnpi/attestations``."""
    try:
        from routines.mnpi_attestations import list_active_attestations
        return bool(list_active_attestations())
    except Exception:  # noqa: BLE001 — a lookup miss must never blank the matrix
        log.warning("crew providers: attestation lookup failed", exc_info=True)
        return False


def _crew_row(manifest: registry.CrewManifestEntry, overrides: dict[str, dict]) -> CrewProviderRow:
    lock = manifest.sensitivity_override
    enterprise = shared_routing.plan_tier() == "enterprise"

    # Resolve the operator's promotion INTENT against the crew's REAL ceiling so
    # the matrix reflects what WOULD promote under the live tier/attestations: a
    # locked crew at its lock (an MNPI lock IS explicit provenance), else
    # "internal" — a representative eligible tier for an unlocked crew (the
    # per-run workspace tier still gates at launch).
    if lock == "MNPI":
        resolve_tier, resolve_explicit = "MNPI", True
    elif lock == "confidential":
        resolve_tier, resolve_explicit = "confidential", False
    else:
        resolve_tier, resolve_explicit = "internal", False
    promo = crew_overrides.resolve_crew_promotion(
        manifest, resolve_tier, mnpi_explicit=resolve_explicit, overrides=overrides,
    )

    # Promotability for the dashboard trigger (the gate is still the final word
    # at dispatch; this only decides whether to OFFER the affordance).
    if lock == "MNPI":
        # `enterprise and …` short-circuits the attestation DB read in bridge tier.
        mnpi_promotable = enterprise and _any_active_attestation()
        promotable = mnpi_promotable
    elif lock == "confidential":
        mnpi_promotable = False
        promotable = enterprise
    else:
        mnpi_promotable = False
        promotable = True

    return CrewProviderRow(
        verb=manifest.verb,
        description=manifest.description,
        roles=list(manifest.roles),
        sensitivity_lock=lock,
        promotable=promotable,
        enterprise=enterprise,
        mnpi_promotable=mnpi_promotable,
        models_default=dict(manifest.models_default),
        override=overrides.get(manifest.verb),
        promoted_roles={role: rp.lane for role, rp in promo.cloud_roles.items()},
    )


@router.get("/providers", response_model=CrewProvidersResponse)
def get_crew_providers() -> CrewProvidersResponse:
    """The per-crew promotion matrix for the dashboard Crews section. One row per
    registered crew; ``promotable`` reflects the LIVE lift (unlocked always;
    confidential-locked under enterprise; MNPI-locked under enterprise + an active
    attestation); ``promoted_roles`` shows the operator's current per-role cloud
    intent as it resolves under the crew's real ceiling + tier/attestations."""
    overrides = crew_overrides.load_crew_overrides()
    rows = [_crew_row(m, overrides) for m in registry.list_manifests()]
    return CrewProvidersResponse(
        crews=rows,
        sidecar_path=str(crew_overrides.crew_sidecar_path()),
        plan_tier=shared_routing.plan_tier(),
        as_of=datetime.now(timezone.utc),
    )


# ─────────────────────────────────────────────────────────────────────────────
# PATCH /api/crew/{verb}/provider — write/clear a promotion
# ─────────────────────────────────────────────────────────────────────────────


class PatchCrewProviderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    preferred_provider: Optional[str] = Field(
        None,
        description="anthropic | openai | local. Omit to leave unchanged.",
    )
    preferred_model: Optional[str] = Field(
        None,
        description="Cloud model alias: opus | sonnet | haiku | opus-1m (anthropic). Omit to leave unchanged.",
    )
    role: Optional[str] = Field(
        None,
        description="Apply at this role only; omit for the crew-level default.",
    )
    clear: bool = Field(
        False, description="Remove the (crew- or role-level) entry, reverting to local.",
    )


@router.patch("/{verb}/provider", response_model=CrewProviderRow)
def patch_crew_provider(verb: str, payload: PatchCrewProviderRequest) -> CrewProviderRow:
    """Write (or clear) ``verb``'s promotion in the operator sidecar.

    422 for: nothing to update, unknown provider/model, or a cloud promotion a
    sensitivity-locked crew's lift doesn't permit (bridge tier / no attestation /
    a non-liftable provider — see ``save_crew_override``). 404 when ``verb`` is
    not a registered crew."""
    if (
        not payload.clear
        and payload.preferred_provider is None
        and payload.preferred_model is None
    ):
        raise HTTPException(
            422, "nothing to update — supply preferred_provider, preferred_model, or clear=true",
        )

    try:
        crew_overrides.save_crew_override(
            verb,
            preferred_provider=payload.preferred_provider,
            preferred_model=payload.preferred_model,
            role=payload.role,
            clear=payload.clear,
        )
    except KeyError as e:
        raise HTTPException(404, f"crew {verb!r} is not registered") from e
    except crew_overrides.CrewOverrideRefused as e:
        raise HTTPException(422, str(e)) from e

    log.info(
        "crew provider override %s: verb=%s role=%s provider=%s model=%s",
        "cleared" if payload.clear else "written",
        verb, payload.role, payload.preferred_provider, payload.preferred_model,
    )
    overrides = crew_overrides.load_crew_overrides(force=True)
    return _crew_row(registry.get_manifest(verb), overrides)


__all__ = ["router"]
