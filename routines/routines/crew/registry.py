"""Crew registry — verb → manifest + sensitivity/lane resolution (#31).

The registry is a static dict for v1 (one smoke crew). #32/#33/#36 sessions
add their manifests here; the entry shape mirrors the ``MANIFEST`` dict at
the top of each crew-side module (``crews_src/<verb>_crew.py``) — keep the
field names identical on both sides.

Sensitivity resolution (adapted from the staged spec's matrix, hardened with
the #61-era ``_strictest`` fail-closed discipline):

  1. A manifest ``sensitivity_override`` LOCKS the crew: any request override
     that differs is refused 403 (the future ``/triage`` MNPI lock — spec
     §5.2, load-bearing). The locked tier is still combined via
     ``_strictest`` with the workspace default so a hypothetical *loosening*
     manifest (e.g. ``internal`` on a confidential workspace) can never relax
     the tier — the staged sketch let the manifest win unconditionally in
     both directions, which is fail-open for that (admittedly pathological)
     case.
  2. Else a request override must be same-or-stricter than the workspace
     default, otherwise 403.
  3. Else the workspace default applies (project/bd → confidential,
     general → internal — same map as ``central_guards._grandfather_sensitivity``).

Lane resolution: **v1 crews are local-only** — ``pick_lane`` returns
``"ollama"`` for every tier, per [[CLAUDE]] §5.2 ("crews default to local-LLM
lanes; no cloud on confidential"). The staged spec routed internal/public
crews to ``claude-cli``; promoting any crew to a cloud lane is an explicit
operator decision (morning-brief decision point), not a default.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

from routines.crew.types import Sensitivity, WorkspaceType
from routines.hooks.central_guards import _strictest
from routines.skills.registry import CapturesToVault


class CrewRegistryError(KeyError):
    """Unknown crew verb."""


class SensitivityRefused(RuntimeError):
    """A sensitivity override was refused (locked crew, or a loosening
    override). The route maps this to HTTP 403."""


class CrewManifestEntry(BaseModel):
    """Bridge-side mirror of a crew module's MANIFEST dict."""

    # CapturesToVault is a stdlib frozen dataclass reused from the skill registry
    # (single source of truth for the capture contract); hold it as an opaque
    # type so pydantic stores the instance verbatim without coercion.
    model_config = ConfigDict(arbitrary_types_allowed=True)

    verb: str
    module: str                  # python module name inside the crew dir
    description: str
    sensitivity_override: Sensitivity | None = None
    cost_cap_tokens: int
    cost_cap_seconds: int
    roles: list[str]
    models_default: dict[str, str] = Field(default_factory=dict)
    # Opt-in conclusion→vault capture (#captures-to-vault-crews). Absent → the
    # crew does not capture (back-compat: hello_world). Reuses the skill-side
    # CapturesToVault shape so ONE emitter (capture.emit_deliverable_proposal)
    # serves skills + crews. Mirrored as a plain dict in the crew-side MANIFEST.
    captures_to_vault: CapturesToVault | None = None

    @field_validator("captures_to_vault")
    @classmethod
    def _check_captures(cls, v: CapturesToVault | None) -> CapturesToVault | None:
        """Boot-time guard mirroring the skill registry's
        ``_validate_captures_to_vault``: a declared block must carry a non-empty
        ``target`` + ``headline`` + ``fields``."""
        if v is None:
            return v
        if not (isinstance(v.target, str) and v.target.strip()):
            raise ValueError("captures_to_vault.target must be a non-empty string")
        if not (isinstance(v.headline, str) and v.headline.strip()):
            raise ValueError("captures_to_vault.headline must be a non-empty string")
        if not v.fields:
            raise ValueError("captures_to_vault.fields must be non-empty")
        return v


_REGISTRY: dict[str, CrewManifestEntry] = {
    "hello_world": CrewManifestEntry(
        verb="hello_world",
        module="hello_world_crew",
        description="3-role smoke crew for boundary verification. Not user-facing.",
        # None = inherit from workspace tier. NOTE: the staged spec is
        # internally inconsistent here (§5.2's table says "internal always",
        # the crew template's MANIFEST says inherit). Inherit is the
        # fail-closed reading — a smoke run from a project workspace gets
        # confidential, not a loosened internal — so inherit wins.
        sensitivity_override=None,
        cost_cap_tokens=10_000,
        # 300, not the spec's 60 — measured 99s healthy wall time on the
        # 2026-06-10 real-boundary smoke (cold qwen3:14b load dominates);
        # the route clamps the subprocess wall clock to this value, so 60s
        # would time out every cold run. Keep in sync with the crew-side
        # MANIFEST in crews_src/hello_world_crew.py.
        cost_cap_seconds=300,
        roles=["Analyst", "Reviewer", "Synthesist"],
        models_default={
            "Analyst": "qwen3:14b",
            "Reviewer": "qwen3:8b",
            "Synthesist": "qwen3:14b",
        },
    ),
    # #33 — DeepDive: "synthesize what we know about X + what's interesting".
    # 5 roles: three parallel analysts (Vault/Financial/Industry) fan into a
    # synthetic Coordinator (fakes a JOIN — MetaGPT has no first-class fan-in)
    # that publishes DeepDiveReady, which the Synthesist watches. Per
    # [[autonomous-crews]] §2.
    "explore": CrewManifestEntry(
        verb="explore",
        module="explore_crew",
        description=(
            "DeepDive — synthesize what the vault + market tools know about a "
            "company/topic, plus what's interesting. 5 roles, fan-in synthesis."
        ),
        # None = inherit from workspace tier (same fail-closed reading as
        # hello_world). [[autonomous-crews]] §2 says "general workspace +
        # public target → Claude", but v1 pick_lane is local-only on EVERY
        # tier (operator-gated cloud promotion, #31 morning-brief decision),
        # so explore runs all-Ollama today exactly like hello_world.
        sensitivity_override=None,
        # [[autonomous-crews]] §1 cost-cap row: 80k tokens / 4 min (design).
        cost_cap_tokens=80_000,
        # SMOKE TUNING 2026-06-14 (uncommitted): bumped 240->480. The first
        # live run reached the Synthesist at ~182s and was killed mid-synthesis
        # at the 240s design cap; measured need is ~280-300s on qwen3:14b
        # thinking-mode (~12.7 tok/s). 480 gives headroom (< the 600s global
        # ceiling). Re-tune to the measured value + sync the crew-side MANIFEST
        # at commit; thinking-off / per-role qwen3:8b tiering are the efficiency path.
        cost_cap_seconds=480,
        roles=[
            "VaultArchaeologist",
            "FinancialAnalyst",
            "IndustryAnalyst",
            "Coordinator",
            "Synthesist",
        ],
        # All analytic roles on qwen3:14b per §2 ("all roles get OllamaLLM
        # (qwen3:14b)"). The Coordinator is a pure fan-in sentinel — it makes
        # NO LLM call — so its model is declared (registry symmetry) but never
        # instantiated for inference; qwen3:8b documents "cheap, unused".
        models_default={
            "VaultArchaeologist": "qwen3:14b",
            "FinancialAnalyst": "qwen3:14b",
            "IndustryAnalyst": "qwen3:14b",
            "Coordinator": "qwen3:8b",
            "Synthesist": "qwen3:14b",
        },
        # #captures-to-vault-crews: opt-in conclusion capture. The crew resolves
        # the target note at runtime (Companies/<target>.md for a company,
        # Topics/<target>.md for a sector) into outcome["target_note"], so the
        # registry target is just that crew-supplied path. Section/fields/headline
        # mirror crews_src/explore_crew.py MANIFEST.
        captures_to_vault=CapturesToVault(
            target="{target_note}",
            section="Deep-dive history",
            fields=("headline",),
            headline="Deep-dive — {target}: {headline}",
        ),
    ),
    # #32 — CIMTriage: read a CIM/teaser, flag what matters. Per
    # [[autonomous-crews]] §3.
    "triage": CrewManifestEntry(
        verb="triage",
        module="triage_crew",
        description="Read a CIM/teaser and flag red flags, opportunities, key "
                    "metrics + buyer DD questions. Always local Ollama (MNPI).",
        # LOCKED MNPI (load-bearing — autonomous-crews §3): CIM inputs default
        # MNPI per the vault constitution §4 / [[no-mnpi-to-cloud]]. The lock
        # means resolve_crew_sensitivity refuses 403 on any differing request
        # override, and (with pick_lane local-only) /triage can never reach a
        # cloud lane. Keep in sync with crews_src/triage_crew.py MANIFEST.
        sensitivity_override="MNPI",
        # SMOKE TUNING 2026-06-15 (uncommitted): bumped 50k->100k. A real 4MB CIM
        # through 6 roles hit 60k WITH thinking on; thinking-off (ollama_config)
        # cuts that sharply, but a large CIM is genuinely token-heavy — give
        # headroom. Re-tune to measured + sync the crew MANIFEST at commit.
        cost_cap_tokens=100_000,
        # 600 (the global wall-clock ceiling), not the spec's 180s: a SIX-role
        # crew on a cold qwen3:14b load measured well past 180s for the 3-role
        # hello_world (99s). The 50k TOKEN cap is the real cost guarantee; this
        # only bounds a hang. Flagged for operator tuning after a live timing.
        cost_cap_seconds=600,
        roles=["Ingestor", "RedFlags", "Opportunities", "KeyMetrics",
               "QuestionsForMgmt", "Summariser"],
        # Per-role tiering (#crew-efficiency-followups, 2026-06-15): the three
        # EXTRACTION analyst roles run the schema-constrained structured call
        # where the grammar carries the structure, so qwen3:8b (~2x faster) holds
        # the quality bar (live smoke `fe81231a` confirmed it). QuestionsForMgmt
        # is back on qwen3:14b: that same smoke showed 8b produced 0 usable DD
        # questions — generation, not extraction, is its weak spot. Ingestor
        # (mechanical, no LLM call) + Summariser (memo narrative) stay 14b.
        # Still 100% local Ollama -> MNPI containment unchanged.
        models_default={
            "Ingestor": "qwen3:14b",
            "RedFlags": "qwen3:8b",
            "Opportunities": "qwen3:8b",
            "KeyMetrics": "qwen3:8b",
            "QuestionsForMgmt": "qwen3:14b",
            "Summariser": "qwen3:14b",
        },
        # #captures-to-vault-crews: opt-in conclusion capture. MNPI-safe — the
        # proposal + the Route-append are LOCAL vault writes only (no egress).
        # {entity} is the crew's inferred deal identifier. Mirrors
        # crews_src/triage_crew.py MANIFEST.
        captures_to_vault=CapturesToVault(
            target="Companies/{entity}.md",
            section="Triage history",
            fields=("high", "med", "low", "opportunities", "questions"),
            headline=(
                "{entity} triage: {high} high / {med} med red flags, "
                "{opportunities} opportunities, {questions} DD questions"
            ),
        ),
    ),
    # #36 — /debate <thesis>: Bull/Bear stress-test over N rounds. Inherits
    # the workspace tier (no MNPI lock — a debate follows the workspace it runs
    # in; [[autonomous-crews]] §4). v1 lanes are local-only, so every role runs
    # on the single qwen3:14b model; the matrix below is honest about that.
    # Keep these values in sync with the crew-side MANIFEST in
    # crews_src/debate_crew.py.
    "debate": CrewManifestEntry(
        verb="debate",
        module="debate_crew",
        description=(
            "Stress-test a thesis with explicit Bull/Bear voices over N rounds "
            "(default 3, --rounds=N up to 5). A ContextLoader builds a balanced "
            "brief, a Moderator summarises each round, and a Synthesist writes "
            "the final consensus / disagreement / recommended-action note. "
            "Chat-only by default; promotable to Topics/Theses/<thesis>.md."
        ),
        sensitivity_override=None,
        cost_cap_tokens=60_000,
        # SMOKE TUNING 2026-06-14 (uncommitted): bumped 300->600 for a rounds=1
        # smoke. rounds=3 is ~11 SEQUENTIAL Bull/Bear/Moderator calls (~660s on
        # qwen3:14b thinking-mode) — over the 600s global ceiling; rounds=1 is 5
        # calls (~350-470s) and fits 600. Re-tune + sync the crew MANIFEST at
        # commit; thinking-off is the real fix for rounds>=2.
        cost_cap_seconds=600,
        roles=["ContextLoader", "Bull", "Bear", "Moderator", "Synthesist"],
        models_default={
            "ContextLoader": "qwen3:14b",
            "Bull": "qwen3:14b",
            "Bear": "qwen3:14b",
            "Moderator": "qwen3:14b",
            "Synthesist": "qwen3:14b",
        },
        # #captures-to-vault-crews: opt-in conclusion capture. The crew resolves
        # the target at runtime — Companies/<deal>.md when an explicit --deal arg
        # is given, else Topics/Theses/<thesis-slug>.md — into
        # outcome["target_note"]. Mirrors crews_src/debate_crew.py MANIFEST.
        captures_to_vault=CapturesToVault(
            target="{target_note}",
            section="Debate history",
            fields=("verdict", "recommended_action", "rounds"),
            headline="Debate — {thesis}: {verdict}; next: {recommended_action}",
        ),
    ),
    # #ingest-digest (stages 1-4: doc-scanner + parallel analyzers + cross-doc
    # synthesizer + completeness review). Mirrors crews_src/digest_crew.py::
    # MANIFEST. sensitivity_override stays None (inherit): operator decision 2 is
    # SPLIT routing, so the per-doc public/private classifier — NOT the manifest
    # — is the cloud-eligibility gate. v1 lanes are local-only regardless
    # (pick_lane below), and the classifier pins effective_lane="local", so
    # inherit is fail-closed (project/bd → confidential → local).
    "digest": CrewManifestEntry(
        verb="digest",
        module="digest_crew",
        description=(
            "Ingest a drop dir of deal docs (stages 1-4): scan + fail-closed "
            "public/private routing + per-doc atomic-fact extraction + cross-doc "
            "synthesis + completeness review. Local-only."
        ),
        sensitivity_override=None,
        cost_cap_tokens=60_000,
        cost_cap_seconds=540,
        roles=["DocScanner", "DocAnalyzer", "Synthesizer", "Reviewer"],
        models_default={
            # Per-role models keyed by EXACT role name so the generic models map
            # (#33) resolves each via ollama_config.model_for_role; the legacy
            # "Analyst" stopgap was dropped at integration once the models dict
            # landed. Tiering (2026-06-15): synthesis/generation → 14b; the
            # Reviewer gate is deterministic (0 tokens) but labelled 8b.
            "DocAnalyzer": "qwen3:14b",
            "Synthesizer": "qwen3:14b",
            "Reviewer": "qwen3:8b",
        },
        # #captures-to-vault-crews: opt-in conclusion capture. {project} is the
        # deal identifier (the crew skips the capture when it is empty). Mirrors
        # crews_src/digest_crew.py MANIFEST.
        captures_to_vault=CapturesToVault(
            target="Companies/{project}.md",
            section="Digest history",
            fields=("docs", "facts", "entities", "contradictions", "gate", "uncited"),
            headline=(
                "{project} digest: {docs} docs, {facts} facts, {entities} entities, "
                "{contradictions} contradictions; review {gate}"
            ),
        ),
    ),
}


def get_manifest(verb: str) -> CrewManifestEntry:
    m = _REGISTRY.get(verb)
    if m is None:
        raise CrewRegistryError(verb)
    return m


def list_manifests() -> list[CrewManifestEntry]:
    return list(_REGISTRY.values())


# ────────────────────────────────────────────────────────────────────────────
# Sensitivity + lane resolution
# ────────────────────────────────────────────────────────────────────────────


_WORKSPACE_DEFAULT_SENSITIVITY: dict[str, Sensitivity] = {
    "project": "confidential",
    "bd": "confidential",
    "general": "internal",
}

_KNOWN_TIERS = ("public", "internal", "confidential", "MNPI")


def workspace_default_sensitivity(
    workspace_type: WorkspaceType | str,
    declared_tier: str | None = None,
) -> Sensitivity:
    """Workspace-tier default, combined fail-closed with any tier the caller
    already resolved (e.g. the session's workspace record)."""
    base = _WORKSPACE_DEFAULT_SENSITIVITY.get(str(workspace_type), "confidential")
    if declared_tier is None:
        return base
    if declared_tier not in _KNOWN_TIERS:
        # Fail-closed (#sec-crew-workspace-tier-validation, Shannon run #2): the
        # old ``if declared_tier in _KNOWN_TIERS`` form SILENTLY DROPPED an
        # unrecognised tier (fell through to ``return base``), contradicting the
        # ``_strictest`` fail-closed posture used everywhere else. An unknown
        # caller-supplied tier now coerces to the strictest tier, so a typo'd or
        # hostile value can only TIGHTEN the lane, never be ignored.
        return _strictest(base, "MNPI")  # type: ignore[arg-type]
    return _strictest(base, declared_tier)  # type: ignore[arg-type]


def resolve_workspace_anchor(
    session_workspace_type: WorkspaceType | str | None,
) -> Sensitivity:
    """Server-side authoritative sensitivity FLOOR for a crew run
    (#sec-crew-workspace-tier-validation 6b-2, Shannon run #2).

    The crew launch request carries a CALLER-SUPPLIED ``workspace.type`` the
    bridge cannot verify. A local caller can mislabel a confidential
    (project/bd) workspace as ``general`` (→ ``internal``) — harmless while
    crews were local-only, but now that they can be cloud-promoted
    (#crew-cloud-promotion) a downgraded tier can open a cloud lane. This
    returns a floor derived from a SERVER-SIDE source of truth instead of the
    request:

      * SESSION-backed run (``session_workspace_type`` resolved from the
        session store — stored at creation, not rewritable through this
        request) → that type's default sensitivity, exactly the chat path's
        anchor (``sessions/router.default_sensitivity_for``);
      * SESSION-LESS run (``None`` — no ``session_id``, an id that didn't
        resolve, or a store-read error) → ``"confidential"``, the strictest
        base tier. An unverifiable ``general`` claim then cannot open a cloud
        lane; cloud-promoting a general/public crew run requires launching it
        from a session (operator decision, 2026-06-18).

    The floor never exceeds ``confidential`` (no workspace type defaults to
    MNPI), so it can only TIGHTEN a run and can never manufacture MNPI
    provenance for the P5 attestation lift. Mirrors ``_WORKSPACE_DEFAULT_
    SENSITIVITY`` / the grandfather map — keep the three in lock-step."""
    if session_workspace_type is None:
        return "confidential"
    return _WORKSPACE_DEFAULT_SENSITIVITY.get(str(session_workspace_type), "confidential")


def apply_workspace_anchor(
    resolved: Sensitivity,
    session_workspace_type: WorkspaceType | str | None,
) -> tuple[Sensitivity, bool]:
    """Floor an already-resolved crew sensitivity with the server-side
    workspace anchor (:func:`resolve_workspace_anchor`), fail-closed
    (#sec-crew-workspace-tier-validation 6b-2).

    Returns ``(final_sensitivity, anchor_tightened)`` where ``final_sensitivity
    = _strictest(resolved, floor)`` — so a caller-supplied workspace TYPE looser
    than the server's source of truth can only TIGHTEN the run, never loosen it.
    ``anchor_tightened`` is True when the anchor actually raised the tier (the
    caller claimed a looser workspace than the server knows) — the route logs
    that as a refused downgrade. The manifest lock + per-run override
    (``resolve_crew_sensitivity``) already ran; this is an additional,
    independent tightening layer, upstream of the central lane gate."""
    floor = resolve_workspace_anchor(session_workspace_type)
    final = _strictest(resolved, floor)
    return final, (final != resolved)


def resolve_crew_sensitivity(
    manifest: CrewManifestEntry,
    workspace_type: WorkspaceType | str,
    declared_tier: str | None,
    override: Sensitivity | None,
) -> Sensitivity:
    """Apply the crew sensitivity matrix (module docstring). Raises
    :class:`SensitivityRefused` instead of silently coercing — a refused
    override must be OBSERVABLE (403), per the staged spec's /triage rule."""
    ws_default = workspace_default_sensitivity(workspace_type, declared_tier)

    if manifest.sensitivity_override is not None:
        if override is not None and override != manifest.sensitivity_override:
            raise SensitivityRefused(
                f"crew {manifest.verb!r} locks sensitivity to "
                f"{manifest.sensitivity_override!r}; override {override!r} refused"
            )
        # Fail-closed: the lock can tighten but never loosen the workspace tier.
        return _strictest(manifest.sensitivity_override, ws_default)

    if override is None:
        return ws_default

    if _strictest(override, ws_default) != override:
        # override is LOOSER than the workspace default → refuse.
        raise SensitivityRefused(
            f"sensitivity_override {override!r} is less strict than "
            f"workspace default {ws_default!r} — refused"
        )
    return override


def mnpi_explicit_for_run(
    manifest: CrewManifestEntry,
    *,
    request_override: "Sensitivity | str | None",
    workspace_tier: str | None,
    resolved_sensitivity: str,
) -> bool:
    """Whether a crew run's MNPI tier is EXPLICIT operator-assigned provenance —
    the crew-side analogue of the chat path's ``sessions/router.py::decide_route``
    ``mnpi_explicit`` (#llm-routing-postjune15 P5 §3a safeguard). Only an explicit
    run is eligible for the Phase-C crew attestation lift.

    Returns True ONLY when the run is actually MNPI (``resolved_sensitivity``, the
    POST-guard tier) AND that MNPI came from one of three operator-assigned
    sources, each compared to the literal ``"MNPI"``:

      * the crew's manifest ``sensitivity_override`` lock (e.g. /triage — the
        operator DECLARED the crew handles MNPI; its standing identity);
      * a per-run ``sensitivity_override`` (the operator escalated THIS run);
      * the workspace's declared ``sensitivity_tier`` (the operator classified the
        workspace this run targets).

    Why this is fail-closed against the §3a coercion (the only unknown→MNPI path is
    ``_strictest`` coercing an UNRECOGNISED tier to MNPI): (a) the HTTP DTOs type
    both override + workspace tier as the ``Sensitivity`` Literal, so an unknown
    value is 422-rejected at ingress; (b) a direct caller passing an unknown
    override to a LOCKED crew is 403-refused by ``resolve_crew_sensitivity``
    (override != lock), and to an UNLOCKED crew it coerces to the workspace BASE
    tier (confidential/internal), never MNPI; and (c) for a locked crew the lock is
    already the strictest tier, so any unknown input resolves to a LOWER tier that
    ``_strictest`` discards — the MNPI always comes from the operator's lock, never
    from the coerced input. So a run that is MNPI *only* because of coercion
    matches none of the three literals → False → stays local even under enterprise
    + an active attestation (verified by ``test_mnpi_explicit_*`` in the crew suite).

    NOTE — deliberate divergence from chat: ``decide_route`` treats ONLY a
    per-MESSAGE override as explicit and a workspace DEFAULT as non-explicit
    (a chat workspace default is a PERSISTED setting that must not auto-lift every
    message). A crew's ``workspace_tier`` is different — it is supplied PER-RUN in
    the launch request (a deliberate, Literal-validated operator choice for that
    run), so it counts as explicit here. Locked in per #crew-cloud-promotion
    Phase C."""
    if resolved_sensitivity != "MNPI":
        return False
    return (
        manifest.sensitivity_override == "MNPI"
        or request_override == "MNPI"
        or workspace_tier == "MNPI"
    )


def pick_lane(sensitivity: Sensitivity) -> str:  # noqa: ARG001 — tier kept for the v2 signature
    """v1: every crew runs on the local Ollama lane regardless of tier
    ([[CLAUDE]] §5.2 — crews default to local-LLM lanes). The tier argument
    stays in the signature so the v2 cloud-promotion decision is a one-line
    change here, not a contract rev."""
    return "ollama"


def build_llm_config(
    lane: str,
    manifest: CrewManifestEntry,
    *,
    role_lanes: dict[str, str] | None = None,
    bridge_url: str | None = None,
    run_id: str | None = None,
) -> dict[str, object]:
    """Build the ``llm_config`` block of ``CrewInput``.

    Local lane → Ollama with the manifest's per-role model defaults.
    NOTE: base_url is 127.0.0.1, NOT localhost — Ollama lives in WSL bound to
    IPv4; Windows resolves ``localhost`` to ::1 and times out (the staged
    spec's ``localhost`` would have broken every crew on this machine).

    ``role_lanes`` (#crew-cloud-promotion, Phase A) maps a PROMOTED role → its
    cloud lane: those roles route their LLM calls BACK to the bridge's gated
    loopback ``/api/crew/_llm`` (``bridge_url``) authenticated by ``run_id``,
    while every other role keeps the direct-Ollama path. The promotion decision
    (which roles, sensitivity gating) is the ROUTE's job — this builder is given
    the already-resolved per-role lanes so the registry stays decoupled from the
    override store. Empty/None → a fully-local crew, byte-identical to v1."""
    if lane.startswith("ollama"):
        models = manifest.models_default
        promoted = dict(role_lanes) if role_lanes else {}
        return {
            "provider": "ollama",
            "base_url": "http://127.0.0.1:11434",
            "model_analyst": models.get("Analyst", "qwen3:14b"),
            "model_reviewer": models.get("Reviewer", "qwen3:8b"),
            "model_synthesist": models.get("Synthesist", "qwen3:14b"),
            # Generic role→model map (#33): carries the FULL manifest mapping so
            # crews with non-legacy role names resolve correctly. The three
            # ``model_*`` fields above stay for the hello_world legacy path.
            "models": dict(models),
            # Per-role cloud promotion: only populated when ≥1 role is promoted.
            # The crew-side factory routes a role present in ``role_lanes``
            # through the bridge; absent roles stay local. Model selection for a
            # promoted role is SERVER-authoritative (the _llm endpoint re-derives
            # it from the run context), so the crew never carries a cloud id.
            "role_lanes": promoted,
            "bridge_url": bridge_url if promoted else None,
            "run_id": run_id if promoted else None,
        }
    # Cloud lane — unreachable in v1 (pick_lane is local-only); shape kept so
    # the operator's future cloud-promotion decision doesn't rev the contract.
    # MODEL-ID CURRENCY: analyst/synthesist track the codebase-CURRENT Opus
    # (claude-opus-4-8); reviewer stays claude-haiku-4-5. Both are canonical
    # rows in telemetry/cost_table.py, so the earlier "codebase has no 4-8 id /
    # cost table tops out at 4-7" caveat is dead — these ids price correctly if
    # the branch ever goes live. Full ids are hardcoded on purpose: the lane
    # contract is a full model id, which shared/claude_*_client.py's short-name
    # _model_alias maps pass through unchanged. This stays DEAD code in v1
    # (every lane is Ollama); promoting a crew to a cloud lane is an operator
    # decision.
    return {
        "provider": "claude-cli",
        "base_url": None,
        "model_analyst": "claude-opus-4-8",
        "model_reviewer": "claude-haiku-4-5",
        "model_synthesist": "claude-opus-4-8",
        # Deliberately NO "models" map on the cloud branch: model_for_role
        # consults ``models`` FIRST, so carrying the manifest's Ollama-id matrix
        # here would make every role resolve to qwen3:* even on a cloud lane
        # (codex correctness LOW/latent). The flat Claude model_<role> fields
        # above are the cloud path; when a cloud lane is actually wired, populate
        # ``models`` with provider-appropriate per-role ids if needed.
    }


def model_for_lane(lane: str, manifest: CrewManifestEntry) -> str:
    """Representative model name for the audit/guard context (the per-role
    picks live in ``build_llm_config``)."""
    if lane.startswith("ollama"):
        return manifest.models_default.get("Analyst", "qwen3:14b")
    return "claude-opus-4-8"


__all__ = [
    "CrewManifestEntry",
    "CrewRegistryError",
    "SensitivityRefused",
    "get_manifest",
    "list_manifests",
    "workspace_default_sensitivity",
    "resolve_workspace_anchor",
    "apply_workspace_anchor",
    "resolve_crew_sensitivity",
    "mnpi_explicit_for_run",
    "pick_lane",
    "build_llm_config",
    "model_for_lane",
]
