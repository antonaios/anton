"""Tier 2 per-skill provider endpoints (#llm-routing-tier-2, 2026-06-03).

Two endpoints, both loopback-only (same pattern as ``sensitivity_overrides.py``
+ ``budgets.py``):

  * GET   /api/skills/providers            — the per-skill provider matrix
                                              (sensitivity, allowed, effective
                                              provider, sampling params, cost +
                                              last fire) FOR THE HARNESS Tier 2
                                              dashboard page.
  * PATCH /api/skills/<key>/provider       — write a per-skill override to the
                                              operator sidecar
                                              (``_claude/provider_overrides.yaml``).

The sidecar is vault-owned (operator commits it per CLAUDE.md §5.7) but the
bridge WRITES it so dashboard clicks never touch the version-controlled
SKILL.md files. Precedence the matrix reflects:
``sidecar > SKILL.md frontmatter > AGENTIC_CLOUD_PROVIDER env var > default``.
See ``LLM-ROUTING-2026-06-02.md`` §Tier 2.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from routines.shared import profile
from routines.skills import registry

log = logging.getLogger(__name__)

# The CLOUD providers whose per-provider sensitivity ceiling (§E parity) the
# matrix surfaces. anthropic (Claude) + openai (Codex) are the wired cloud lanes;
# both sit UNCONFIGURED today (-> the §4 matrix governs, = `internal` in bridge
# tier) until Enterprise/ZDR raises one via providers.<name>.max_sensitivity.
_CEILING_PROVIDERS = ("anthropic", "openai")

# The Tier 1 env var the dispatcher consults — kept as a literal here rather
# than imported from sessions.router to avoid an api→sessions import just for a
# string. Must stay in sync with router.AGENTIC_CLOUD_PROVIDER_ENV.
_CLOUD_PROVIDER_ENV = "AGENTIC_CLOUD_PROVIDER"

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "testclient"})


def _loopback_only(request: Request) -> None:
    client_host = request.client.host if request.client else None
    if client_host not in _LOOPBACK_HOSTS:
        log.warning(
            "skills-providers endpoint refused non-loopback connection from %r",
            client_host,
        )
        raise HTTPException(
            status_code=403,
            detail=(
                "skills-providers endpoints are loopback-only; refusing "
                f"connection from {client_host!r}"
            ),
        )


router = APIRouter(
    prefix="/api/skills",
    tags=["skills"],
    dependencies=[Depends(_loopback_only)],
)


# ────────────────────────────────────────────────────────────────────────────
# DTOs
# ────────────────────────────────────────────────────────────────────────────


class LLMParamsDTO(BaseModel):
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None


class SkillProviderRow(BaseModel):
    key: str
    sensitivity: str
    workspace_scope: str
    # Frontmatter-declared routing (the SKILL.md baseline).
    preferred_provider: Optional[str] = None
    fallback_provider: Optional[str] = None
    allowed_providers: list[str] = Field(default_factory=list)
    preferred_model: Optional[str] = None   # operator-pinned cloud model alias (Task 3)
    llm_params: LLMParamsDTO = Field(default_factory=LLMParamsDTO)
    # Effective values after the sidecar overlay + env/default fall-through.
    effective_provider: str
    effective_source: str          # sidecar | frontmatter | env | task-class | default | confidential-policy
    effective_model: Optional[str] = None   # resolved cloud model alias, None = lane default
    effective_llm_params: LLMParamsDTO = Field(default_factory=LLMParamsDTO)
    # Non-null when the resolution failed loud — TIER2 PROVIDER INVALID (typo'd
    # sidecar/frontmatter), TIER2 PROVIDER NOT ALLOWED (resolved provider outside
    # the skill's allow-list), or TIER2 SKILL NOT FOUND (sidecar → phantom skill).
    # The matrix surfaces this so the operator sees the error instead of a
    # misleading "attempted provider" (#llm-routing-tier-2-matrix-error). None on
    # a clean resolution; confidential/MNPI rows resolve cleanly to ollama-only,
    # so they carry effective_source="confidential-policy" with NO error here.
    effective_error: Optional[str] = None
    # The raw operator sidecar entry for this skill, if any (so the UI can show
    # exactly what's overridden + offer a "clear" affordance).
    override: Optional[dict[str, Any]] = None
    # Telemetry roll-up (best-effort; null/zero until the skill fires under
    # Tier 2 — pre-Tier-2 llm-call rows carry no skill identity).
    last_fire: Optional[str] = None
    last_provider: Optional[str] = None
    cost_usd: float = 0.0
    calls: int = 0


class SkillsProvidersResponse(BaseModel):
    skills: list[SkillProviderRow]
    env_provider: Optional[str] = None
    default_provider: str
    sidecar_path: str
    # Per-CLOUD-provider sensitivity ceiling (providers.<name>.max_sensitivity in
    # _claude/profile.md) -- None = UNCONFIGURED (no per-provider cap; the §4
    # matrix + the override window remain the gates). The §E parity readout:
    # anthropic + openai both unconfigured today (= `internal` in bridge tier)
    # until Enterprise/ZDR raises one.
    provider_ceilings: dict[str, Optional[str]] = Field(default_factory=dict)
    as_of: datetime


# ── Taxonomy catalog (#35 — TAXONOMY dashboard tab) ──────────────────────────
# The TAXONOMY tab catalogs every skill VERB from SKILL.md frontmatter. The
# providers matrix (above) already surfaces sensitivity / workspace_scope / cost
# + telemetry, but NOT the three catalog columns the tab needs: description
# (triggers), tile_label, and the per-skill cost CEILINGS. Rather than bolt
# catalog fields onto the provider-routing row (a different concern), this is a
# sibling read-only endpoint on the same /api/skills router — it reuses the
# loopback guard + the telemetry roll-up, and reads the SAME registry the
# providers matrix does, so the catalog can never drift from the frontmatter.


class SkillTaxonomyRow(BaseModel):
    """One catalogued skill verb, straight from validated SKILL.md frontmatter
    (+ a best-effort telemetry roll-up). The dashboard cross-references
    ``name`` against its own wired-workflow table to derive wired-vs-stub."""

    name: str                      # the verb / registry key (frontmatter `name`)
    description: str               # full frontmatter description (carries Triggers + Output)
    tile_label: str
    sensitivity: str               # public | internal | confidential | MNPI
    workspace_scope: str           # project | bd | general | any
    lane: str = "skill"            # v1: every registered verb is a single skill.
    version: str
    cost_ceiling_tokens: int
    cost_ceiling_seconds: int
    allowed_tools: list[str] = Field(default_factory=list)
    # "Output destination" — where the skill lands work. vault_write globs +
    # the #76 captures_to_vault target (the dated semantic fact). Either may be
    # empty (a read-only / proposal-only skill writes nowhere directly).
    vault_write: list[str] = Field(default_factory=list)
    captures_target: Optional[str] = None
    captures_section: Optional[str] = None
    # Telemetry roll-up (best-effort; null/zero until the skill fires). Lets the
    # tab show a "recent runs" hint per verb without a second endpoint.
    last_fire: Optional[str] = None
    last_provider: Optional[str] = None
    cost_usd: float = 0.0
    calls: int = 0


class SkillTaxonomyResponse(BaseModel):
    skills: list[SkillTaxonomyRow]
    # Forward-compat lanes (OUTSTANDING #35 §6 lane taxonomy). Composites
    # (``routines/composites/*.json``) and crews (``routines/crews/*.py``) are
    # not yet present on disk, so these are empty in v1 — the tab renders them
    # as "follow-up" sections rather than hiding the lane structure.
    composites: list[dict[str, Any]] = Field(default_factory=list)
    crews: list[dict[str, Any]] = Field(default_factory=list)
    counts: dict[str, int] = Field(default_factory=dict)
    as_of: datetime


class PatchProviderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    preferred_provider: Optional[str] = Field(
        None,
        description=(
            "anthropic | openai | ollama-only | prefer_local. Omit to leave "
            "unchanged."
        ),
    )
    preferred_model: Optional[str] = Field(
        None,
        description=(
            "Cloud model alias: opus | sonnet | haiku | opus-1m "
            "(#llm-routing-postjune15 P2). Omit to leave unchanged."
        ),
    )
    llm_params: Optional[LLMParamsDTO] = Field(
        None, description="Sampling params {temperature, max_tokens}. Omit to leave unchanged.",
    )
    clear: bool = Field(
        False, description="Remove this skill's sidecar entry entirely (revert to frontmatter).",
    )


# ────────────────────────────────────────────────────────────────────────────
# Telemetry roll-up (best-effort, tolerant)
# ────────────────────────────────────────────────────────────────────────────


def _skill_telemetry(jsonl_path: Path) -> dict[str, dict]:
    """Aggregate ``llm_calls.jsonl`` by skill → {last_fire, last_provider,
    cost_usd, calls}. Tolerant: missing file / malformed lines are skipped.

    Timestamps are the writer's ``isoformat(timespec="seconds")`` UTC strings,
    so lexicographic comparison == chronological — no parse needed to find the
    latest fire."""
    agg: dict[str, dict] = {}
    if not jsonl_path.exists():
        return agg
    try:
        with jsonl_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                skill = rec.get("skill")
                if not skill:
                    continue
                a = agg.setdefault(
                    str(skill),
                    {"last_fire": None, "last_provider": None, "cost_usd": 0.0, "calls": 0},
                )
                a["calls"] += 1
                a["cost_usd"] += float(rec.get("cost_usd") or 0.0)
                ts = rec.get("ts")
                if ts and (a["last_fire"] is None or ts > a["last_fire"]):
                    a["last_fire"] = ts
                    a["last_provider"] = rec.get("provider")
    except OSError as e:
        log.warning("skills-providers: could not read telemetry %s: %s", jsonl_path, e)
    for a in agg.values():
        a["cost_usd"] = round(a["cost_usd"], 4)
    return agg


def _row_for(
    key: str,
    meta,
    *,
    env_provider: Optional[str],
    overrides: dict[str, dict],
    telemetry: dict[str, dict],
) -> SkillProviderRow:
    """Build one matrix row by composing frontmatter + sidecar + telemetry."""
    routing = meta.routing
    resolution = registry.resolve_skill_provider(key, env_provider=env_provider)
    tel = telemetry.get(key, {})
    return SkillProviderRow(
        key=key,
        sensitivity=meta.sensitivity,
        workspace_scope=meta.workspace_scope,
        preferred_provider=routing.preferred_provider,
        fallback_provider=routing.fallback_provider,
        allowed_providers=list(routing.allowed_providers),
        preferred_model=routing.preferred_model,
        llm_params=LLMParamsDTO(
            temperature=routing.llm_params.temperature,
            max_tokens=routing.llm_params.max_tokens,
        ),
        effective_provider=resolution.provider,
        effective_source=resolution.source,
        # Only the Claude lane consumes a model pin; for openai/ollama-only/
        # confidential-local rows the pin isn't used, so don't show a misleading
        # alias there (the declared preferred_model above still records intent).
        effective_model=resolution.model if resolution.provider == "anthropic" else None,
        effective_llm_params=LLMParamsDTO(
            temperature=resolution.llm_params.get("temperature"),
            max_tokens=resolution.llm_params.get("max_tokens"),
        ),
        effective_error=resolution.error,
        override=overrides.get(key),
        last_fire=tel.get("last_fire"),
        last_provider=tel.get("last_provider"),
        cost_usd=tel.get("cost_usd", 0.0),
        calls=tel.get("calls", 0),
    )


def _taxonomy_row_for(name: str, meta, *, telemetry: dict[str, dict]) -> SkillTaxonomyRow:
    """Build one catalog row from a registry :class:`SkillMetadata` + telemetry.

    Pure read of the validated frontmatter — no resolution / sidecar overlay
    (that's the providers matrix's job). ``vault_write`` + the captures target
    are the skill's declared write surface ("output destination")."""
    caps = meta.capabilities
    captures = meta.captures_to_vault
    tel = telemetry.get(name, {})
    return SkillTaxonomyRow(
        name=name,
        description=meta.description,
        tile_label=meta.tile_label,
        sensitivity=meta.sensitivity,
        workspace_scope=meta.workspace_scope,
        version=meta.version,
        cost_ceiling_tokens=meta.cost_ceiling_tokens,
        cost_ceiling_seconds=meta.cost_ceiling_seconds,
        allowed_tools=list(meta.allowed_tools),
        vault_write=list(caps.vault_write),
        captures_target=captures.target if captures else None,
        captures_section=captures.section if captures else None,
        last_fire=tel.get("last_fire"),
        last_provider=tel.get("last_provider"),
        cost_usd=tel.get("cost_usd", 0.0),
        calls=tel.get("calls", 0),
    )


# ────────────────────────────────────────────────────────────────────────────
# Endpoints
# ────────────────────────────────────────────────────────────────────────────


@router.get("/taxonomy", response_model=SkillTaxonomyResponse)
def get_skill_taxonomy() -> SkillTaxonomyResponse:
    """The verb catalog for the TAXONOMY tab (#35). One row per registered skill,
    sorted by name, sourced verbatim from validated SKILL.md frontmatter (the
    registry the providers matrix also reads — so the catalog can't rot). Cost +
    last-fire are a best-effort telemetry roll-up. Composites / crews are empty
    until those lanes land on disk."""
    from routines.telemetry import llm_writer as _writer_mod
    telemetry = _skill_telemetry(_writer_mod.LLM_CALLS_JSONL)

    skills = registry.registered_skills()
    rows = [
        _taxonomy_row_for(name, meta, telemetry=telemetry)
        for name, meta in sorted(skills.items())
    ]

    by_sensitivity: dict[str, int] = {}
    for row in rows:
        by_sensitivity[row.sensitivity] = by_sensitivity.get(row.sensitivity, 0) + 1

    return SkillTaxonomyResponse(
        skills=rows,
        composites=[],
        crews=[],
        counts={
            "skills": len(rows),
            "composites": 0,
            "crews": 0,
            **{f"sensitivity_{k}": v for k, v in by_sensitivity.items()},
        },
        as_of=datetime.now(timezone.utc),
    )


@router.get("/providers", response_model=SkillsProvidersResponse)
def get_skill_providers() -> SkillsProvidersResponse:
    """The per-skill provider matrix. One row per registered skill, sorted by
    key. Effective provider/params reflect the full precedence chain; cost +
    last-fire are a best-effort telemetry roll-up."""
    env_provider = os.environ.get(_CLOUD_PROVIDER_ENV)
    env_provider = env_provider.lower() if env_provider else None

    # Resolve the telemetry path lazily so tests can monkeypatch it.
    from routines.telemetry import llm_writer as _writer_mod
    telemetry = _skill_telemetry(_writer_mod.LLM_CALLS_JSONL)

    overrides = registry.load_skill_overrides()
    skills = registry.registered_skills()

    rows = [
        _row_for(
            key, meta,
            env_provider=env_provider, overrides=overrides, telemetry=telemetry,
        )
        for key, meta in sorted(skills.items())
    ]

    return SkillsProvidersResponse(
        skills=rows,
        env_provider=env_provider,
        default_provider=registry.DEFAULT_CLOUD_PROVIDER,
        sidecar_path=str(registry.sidecar_path()),
        provider_ceilings={p: profile.provider_max_sensitivity(p) for p in _CEILING_PROVIDERS},
        as_of=datetime.now(timezone.utc),
    )


@router.patch("/{key}/provider", response_model=SkillProviderRow)
def patch_skill_provider(key: str, payload: PatchProviderRequest) -> SkillProviderRow:
    """Write (or clear) ``key``'s entry in the operator sidecar.

    422 for: nothing to update, unknown provider, out-of-range temperature, or
    a cloud override on a confidential/MNPI skill (the sidecar can't do what the
    boot validator forbids). 404 when ``key`` is not a registered skill."""
    if (
        not payload.clear
        and payload.preferred_provider is None
        and payload.preferred_model is None
        and payload.llm_params is None
    ):
        raise HTTPException(
            status_code=422,
            detail=(
                "nothing to update — supply preferred_provider, preferred_model, "
                "llm_params, or clear=true"
            ),
        )

    llm_params = (
        payload.llm_params.model_dump(exclude_none=True)
        if payload.llm_params is not None else None
    )

    try:
        registry.save_skill_override(
            key,
            preferred_provider=payload.preferred_provider,
            preferred_model=payload.preferred_model,
            llm_params=llm_params,
            clear=payload.clear,
        )
    except KeyError as e:
        raise HTTPException(
            status_code=404,
            detail=f"skill {key!r} is not registered",
        ) from e
    except registry.ProviderOverrideRefused as e:
        raise HTTPException(status_code=422, detail=str(e)) from e

    log.info(
        "skill provider override %s: key=%s preferred=%s model=%s llm_params=%s",
        "cleared" if payload.clear else "written",
        key, payload.preferred_provider, payload.preferred_model, llm_params,
    )

    # Return the fresh effective row for this skill.
    env_provider = os.environ.get(_CLOUD_PROVIDER_ENV)
    env_provider = env_provider.lower() if env_provider else None
    from routines.telemetry import llm_writer as _writer_mod
    telemetry = _skill_telemetry(_writer_mod.LLM_CALLS_JSONL)
    overrides = registry.load_skill_overrides(force=True)
    meta = registry.load_skill_metadata(key)
    return _row_for(
        key, meta,
        env_provider=env_provider, overrides=overrides, telemetry=telemetry,
    )
