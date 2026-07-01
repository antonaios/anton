"""Per-crew (per-role) cloud-lane promotion — the operator override store
(#crew-cloud-promotion, Phase A).

Crews are LOCAL-ONLY by default (``crew/registry.py::pick_lane`` returns
``"ollama"`` for every tier — [[CLAUDE]] §5.2). This module is the operator's
sanctioned, sensitivity-gated escape hatch: it promotes a crew's LLM calls —
ideally only its GENERATION roles — from the local Ollama lane to a frontier
cloud model (Claude / Codex) when output quality justifies it.

It is a DELIBERATE mirror of the skill provider-override store
(``skills/registry.py::resolve_skill_provider`` + ``provider_overrides.yaml`` +
``api/routes/skills_providers.py``): a vault-owned, bridge-writable YAML sidecar
read on a mtime cache, atomically written by the PATCH route, env-redirectable
for tests.

The hard rule it enforces: a crew/role promotes to cloud only as far as its
EFFECTIVE sensitivity permits — public/internal always; **confidential** only on
the Claude lane under ``AGENTIC_PLAN_TIER=enterprise`` (Phase B); **MNPI** only
when the run is EXPLICIT MNPI under enterprise + an active per-provider P5
attestation (Phase C — Claude or Codex). Anything below that bar is FORCED local
here. This is fail-closed in TWO independent places:

  1. HERE — ``resolve_crew_promotion`` emits a cloud lane for a role ONLY when
     ``_cloud_lane_allowed`` admits its (tier, provider), and
     ``save_crew_override`` REFUSES a cloud promotion the lift doesn't permit
     (e.g. an MNPI-locked crew without enterprise + an active attestation).
  2. The central sensitivity gate (``hooks/central_guards.enforce_sensitivity_lane``)
     re-checks every promoted call at the loopback ``/api/crew/_llm`` endpoint —
     so even a hand-edited sidecar or a forged subprocess request cannot push a
     crew past its sensitivity ceiling (the gate raises, → 403).

Sidecar shape (``_claude/crew_overrides.yaml``)::

    explore:                          # crew verb
      preferred_provider: anthropic   # crew-level default for every role
      preferred_model: opus           # optional cloud model alias
      roles:                          # optional per-role refinement
        Coordinator: { preferred_provider: local }   # keep THIS role local

    triage:                           # MNPI-locked → PATCH refuses any cloud entry

A role with no crew-level default and no role entry stays LOCAL (promotion is
opt-in). ``preferred_provider: local`` is the explicit keep-local sentinel.
"""

from __future__ import annotations

import logging
import os
import platform
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

from routines.crew.registry import CrewManifestEntry, get_manifest
from routines.shared import routing as shared_routing

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Sidecar location (mirrors skills.registry.sidecar_path)
# ─────────────────────────────────────────────────────────────────────────────

CREW_OVERRIDES_ENV = "AGENTIC_CREW_OVERRIDES"      # full-path redirect (tests/operator)
AGENTIC_VAULT_ENV = "AGENTIC_VAULT"
_SIDECAR_RELATIVE = ("_claude", "crew_overrides.yaml")

_overrides_lock = threading.Lock()
# str(path) → (mtime_ns, parsed) so a hot dispatch path doesn't re-parse every
# call, but a PATCH / operator hot-edit is picked up without a bridge restart.
_overrides_cache: dict[str, tuple[int, dict[str, dict]]] = {}


def _default_vault_root() -> Path:
    """Mirror ``skills.registry._default_vault_root`` WITHOUT importing the
    skills layer (kept identical so both sidecars resolve to the same vault)."""
    env = os.environ.get(AGENTIC_VAULT_ENV)
    if env:
        return Path(env)
    if platform.system() == "Windows":
        return Path("<vault>")
    return Path("/mnt/x/OS AI Vault")


def crew_sidecar_path() -> Path:
    """Resolve the crew-overrides sidecar path.

    ``AGENTIC_CREW_OVERRIDES`` (a full path) wins — tests + operators redirect
    here; otherwise ``<vault>/_claude/crew_overrides.yaml``."""
    explicit = os.environ.get(CREW_OVERRIDES_ENV)
    if explicit:
        return Path(explicit)
    return _default_vault_root().joinpath(*_SIDECAR_RELATIVE)


# ─────────────────────────────────────────────────────────────────────────────
# Promotion vocabulary
# ─────────────────────────────────────────────────────────────────────────────

# Cloud providers an operator may name + the lane / default model each maps to.
# The lane string is what the central sensitivity gate keys on
# (``provider_for_override_lookup`` maps ``claude-*`` → anthropic, ``codex*`` →
# openai); the model is threaded onto ``RouteDecision.model`` at dispatch.
_PROVIDER_LANE: dict[str, str] = {
    "anthropic": "claude-cli",
    "openai": "codex-cli",
}
_PROVIDER_DEFAULT_MODEL: dict[str, str] = {
    "anthropic": "opus",     # Opus 4.8 (client _model_alias pins the concrete id)
    "openai": "gpt-5",       # default Codex model alias
}
# The explicit keep-local sentinel (a role/crew the operator pins to Ollama).
_LOCAL_SENTINEL = "local"

# Cloud model aliases the operator may pin (anthropic only — Claude family).
# Mirrors shared_routing.CLOUD_MODEL_ALIASES; openai is single-model in v1.
_ANTHROPIC_MODEL_ALIASES = frozenset(shared_routing.CLOUD_MODEL_ALIASES)

# These effective tiers may promote to cloud UNCONDITIONALLY (any cloud
# provider). confidential (Phase B) + MNPI (Phase C) are lifted CONDITIONALLY by
# ``_cloud_lane_allowed`` below — enterprise tier, and (MNPI) an explicit
# assignment + an active per-provider attestation.
_CLOUD_ELIGIBLE_TIERS = frozenset({"public", "internal"})


@dataclass(frozen=True)
class RolePromotion:
    """One crew role's resolved cloud routing."""

    lane: str       # cloud lane, e.g. "claude-cli" | "codex-cli"
    provider: str   # "anthropic" | "openai" (for audit / matrix)
    model: str      # cloud model alias, e.g. "opus" | "haiku" | "gpt-5"


@dataclass(frozen=True)
class CrewPromotion:
    """The resolved promotion decision for one crew run.

    ``cloud_roles`` holds ONLY the roles routed to a cloud lane; every other
    role stays on its manifest-default local Ollama model. ``eligible`` records
    whether the run's effective sensitivity permits cloud at all (False forces
    every role local regardless of the sidecar). ``source`` / ``error`` are for
    the providers matrix + the dashboard, mirroring ``ProviderResolution``."""

    verb: str
    effective_sensitivity: str
    cloud_roles: dict[str, RolePromotion] = field(default_factory=dict)
    eligible: bool = False
    source: str = "none"           # "sidecar" | "none" | "forced-local"
    error: Optional[str] = None

    @property
    def any_promoted(self) -> bool:
        return bool(self.cloud_roles)


class CrewOverrideRefused(ValueError):
    """A sidecar write request was invalid (unknown provider/model, or a cloud
    promotion on an MNPI-locked crew). The PATCH endpoint maps this to 422."""


# ─────────────────────────────────────────────────────────────────────────────
# Sidecar read (tolerant, mtime-cached) — mirrors skills.registry
# ─────────────────────────────────────────────────────────────────────────────


def _read_sidecar(path: Path) -> dict[str, dict]:
    """Parse the sidecar YAML → ``{verb: entry_dict}``. Tolerant: a
    missing/empty/malformed file or non-mapping entries are dropped with a
    warning (a bad sidecar must NEVER crash a crew launch)."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as e:
        logger.warning("crew_overrides sidecar unreadable at %s: %s", path, e)
        return {}
    if not isinstance(raw, dict):
        logger.warning(
            "crew_overrides sidecar at %s is not a mapping (got %s); ignoring",
            path, type(raw).__name__,
        )
        return {}
    out: dict[str, dict] = {}
    for verb, entry in raw.items():
        if isinstance(entry, dict):
            out[str(verb)] = entry
        else:
            logger.warning(
                "crew_overrides sidecar entry %r is not a mapping; ignoring", verb,
            )
    return out


def load_crew_overrides(*, force: bool = False) -> dict[str, dict]:
    """Return the parsed operator sidecar, keyed by crew verb.

    Missing file → ``{}`` (the common case). Cached on (path, mtime_ns);
    ``force=True`` bypasses the cache. Each crew launch calls this — the mtime
    check keeps it cheap while still picking up a PATCH / hot-edit."""
    path = crew_sidecar_path()
    key = str(path)
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        with _overrides_lock:
            _overrides_cache.pop(key, None)
        return {}

    if not force:
        cached = _overrides_cache.get(key)
        if cached is not None and cached[0] == mtime:
            return cached[1]

    parsed = _read_sidecar(path)
    with _overrides_lock:
        _overrides_cache[key] = (mtime, parsed)
    return parsed


def _clear_overrides_cache() -> None:
    """Test-only: drop the mtime cache so a redirected sidecar is re-read."""
    with _overrides_lock:
        _overrides_cache.clear()


# ─────────────────────────────────────────────────────────────────────────────
# Resolution
# ─────────────────────────────────────────────────────────────────────────────


def _role_request(entry: dict, role: str) -> Optional[dict]:
    """The promotion request for ``role``: per-role entry wins over the
    crew-level default. Returns a normalised ``{preferred_provider,
    preferred_model}`` dict, or ``None`` when the role is not promoted.

    A per-role value may be a bare provider string (``RedFlags: anthropic``) or
    a mapping (``RedFlags: {preferred_provider: anthropic, preferred_model:
    opus}``). The explicit ``local`` sentinel (at either level) keeps the role
    local."""
    roles = entry.get("roles")
    role_val = roles.get(role) if isinstance(roles, dict) else None

    # Per-role entry (string shorthand or mapping).
    if isinstance(role_val, str):
        role_req: Optional[dict] = {"preferred_provider": role_val}
    elif isinstance(role_val, dict):
        role_req = role_val
    else:
        role_req = None

    if role_req is not None:
        prov = role_req.get("preferred_provider")
        if prov == _LOCAL_SENTINEL:
            return None  # explicit keep-local
        if isinstance(prov, str) and prov in _PROVIDER_LANE:
            return {
                "preferred_provider": prov,
                "preferred_model": role_req.get("preferred_model")
                or entry.get("preferred_model"),
            }
        # A per-role entry EXISTS but names an unknown/invalid provider (e.g. a
        # hand-edited `gemini`): FAIL-CLOSED to local for THIS role. The operator
        # set something role-specific, so do NOT silently inherit the crew-level
        # cloud default (codex review) — an operator disabling a role must win.
        return None

    # No per-role entry → inherit the crew-level default.
    crew_prov = entry.get("preferred_provider")
    if crew_prov == _LOCAL_SENTINEL or not isinstance(crew_prov, str):
        return None
    if crew_prov not in _PROVIDER_LANE:
        return None
    return {
        "preferred_provider": crew_prov,
        "preferred_model": entry.get("preferred_model"),
    }


def _resolve_model(provider: str, requested: Optional[str]) -> str:
    """The cloud model alias for ``provider``: an operator pin if valid, else
    the provider default. An invalid anthropic pin falls back to the default
    (the dispatcher would otherwise reject an unknown id)."""
    if provider == "anthropic":
        if isinstance(requested, str) and requested in _ANTHROPIC_MODEL_ALIASES:
            return requested
        return _PROVIDER_DEFAULT_MODEL["anthropic"]
    # openai is single-model in v1; ignore any pin.
    return _PROVIDER_DEFAULT_MODEL["openai"]


def _cloud_lane_allowed(
    effective_sensitivity: str, provider: str, *, mnpi_explicit: bool,
) -> bool:
    """Whether a role at ``effective_sensitivity`` may promote to ``provider``'s
    cloud lane — the RESOLVER-side mirror of the central gate
    (``central_guards._lane_allowed_for`` + ``mnpi_attestations.mnpi_cloud_allowed``),
    at per-(tier, provider) granularity. Keeping the two in lock-step means the
    resolver never EMITS a role the gate would 403 (which would burn the local
    analysts then refuse at the Synthesist). The gate stays the independent
    backstop — this is the convenience/efficiency layer, never the only check.

      * public / internal           → any cloud provider
      * confidential, enterprise    → ``anthropic`` ONLY (Claude Enterprise; §4
                                      never routes confidential to Codex)
      * MNPI, enterprise, EXPLICIT  → a provider holding an ACTIVE per-provider
                                      attestation (Claude OR Codex)
      * anything else               → local (fail-closed)

    Fail-closed: bridge tier, non-explicit MNPI, an unattested provider, an
    unknown tier, or any attestation-lookup error → ``False``.
    """
    if effective_sensitivity in _CLOUD_ELIGIBLE_TIERS:
        return True
    if effective_sensitivity == "confidential":
        return provider == "anthropic" and shared_routing.plan_tier() == "enterprise"
    if effective_sensitivity == "MNPI":
        if not mnpi_explicit or shared_routing.plan_tier() != "enterprise":
            return False
        try:
            # Lazy import (mirrors the gate) — the attestation package must not be
            # an import-time dependency of the resolver.
            from routines.mnpi_attestations import mnpi_cloud_allowed
            return mnpi_cloud_allowed(provider)
        except Exception:  # noqa: BLE001 — fail-closed: refuse the cloud lane
            logger.warning(
                "crew MNPI attestation lookup failed for provider=%r (refusing "
                "cloud — fail-closed)", provider, exc_info=True,
            )
            return False
    return False


def resolve_crew_promotion(
    manifest: CrewManifestEntry,
    effective_sensitivity: str,
    *,
    mnpi_explicit: bool = False,
    overrides: Optional[dict[str, dict]] = None,
) -> CrewPromotion:
    """Resolve which of ``manifest``'s roles promote to a cloud lane for a run
    at ``effective_sensitivity``.

    ``mnpi_explicit`` carries the run's MNPI provenance (operator-assigned vs
    unknown-coerced) for the Phase-C attestation lift — pass the value
    ``registry.mnpi_explicit_for_run`` computed for the run; it is ignored for
    non-MNPI tiers. Fail-closed in two layers: a tier that can't reach cloud at
    all yields NO cloud roles, and each emitted role must additionally pass
    ``_cloud_lane_allowed`` for its (tier, provider) — so confidential→Codex and
    MNPI→unattested-provider are dropped to local here, and the central gate
    re-checks every surviving promotion at dispatch."""
    verb = manifest.verb
    if overrides is None:
        overrides = load_crew_overrides()
    entry = overrides.get(verb) if isinstance(overrides, dict) else None

    # Tier-level gate: can ANY cloud promotion happen at this effective tier?
    # (Per-PROVIDER admissibility — confidential→Claude-only, MNPI→attested
    # provider — is enforced per role below via ``_cloud_lane_allowed``.)
    if effective_sensitivity in _CLOUD_ELIGIBLE_TIERS:
        eligible = True
    elif effective_sensitivity == "confidential":
        eligible = shared_routing.plan_tier() == "enterprise"
    elif effective_sensitivity == "MNPI":
        eligible = mnpi_explicit and shared_routing.plan_tier() == "enterprise"
    else:
        eligible = False

    if not eligible:
        # Forced local: confidential/MNPI without the enterprise (+ explicit, for
        # MNPI) lift, or any unknown tier. The central gate is the independent
        # backstop; this layer just avoids emitting a doomed promotion.
        return CrewPromotion(
            verb=verb, effective_sensitivity=effective_sensitivity,
            cloud_roles={}, eligible=False,
            source="forced-local" if entry else "none",
        )

    if not isinstance(entry, dict):
        return CrewPromotion(
            verb=verb, effective_sensitivity=effective_sensitivity,
            cloud_roles={}, eligible=True, source="none",
        )

    cloud_roles: dict[str, RolePromotion] = {}
    for role in manifest.roles:
        req = _role_request(entry, role)
        if req is None:
            continue
        provider = req["preferred_provider"]
        # Per-provider sensitivity admissibility (mirrors the central gate). A
        # confidential run drops a Codex-requested role to local; an MNPI run
        # drops any role whose provider lacks an active attestation. Eligible
        # public/internal runs admit every provider.
        if not _cloud_lane_allowed(
            effective_sensitivity, provider, mnpi_explicit=mnpi_explicit,
        ):
            continue
        lane = _PROVIDER_LANE[provider]
        model = _resolve_model(provider, req.get("preferred_model"))
        cloud_roles[role] = RolePromotion(lane=lane, provider=provider, model=model)

    return CrewPromotion(
        verb=verb, effective_sensitivity=effective_sensitivity,
        cloud_roles=cloud_roles, eligible=True,
        source="sidecar" if cloud_roles else "none",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Sidecar write (atomic) — mirrors skills.registry.save_skill_override
# ─────────────────────────────────────────────────────────────────────────────


def _write_sidecar_atomic(path: Path, data: dict) -> None:
    """Write ``data`` as YAML via tmp + ``os.replace`` so a concurrent reader
    never sees a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=str(path.parent), prefix=".crew_overrides_", suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(
                "# crew_overrides.yaml — per-crew cloud-lane promotion "
                "(#crew-cloud-promotion).\n"
                "# Written by PATCH /api/crew/<verb>/provider; operator commits "
                "per CLAUDE.md §5.7.\n"
                "# A crew/role promotes to cloud ONLY when its effective "
                "sensitivity is public/internal\n"
                "# (Phase A). confidential/MNPI are gated + force-local.\n"
            )
            yaml.safe_dump(data, f, sort_keys=True, default_flow_style=False)
        os.replace(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _validate_promotion_value(
    provider: Optional[str], model: Optional[str], *, who: str,
) -> list[str]:
    """Shape-validate a (provider, model) promotion pair. ``local`` / ``None``
    provider is always valid (keep-local). Returns a list of human-readable
    errors (empty = valid)."""
    errors: list[str] = []
    if provider is None or provider == _LOCAL_SENTINEL:
        return errors  # keep-local: model is irrelevant
    if provider not in _PROVIDER_LANE:
        errors.append(
            f"{who}: preferred_provider {provider!r} is not one of "
            f"{sorted(_PROVIDER_LANE)} or {_LOCAL_SENTINEL!r}"
        )
        return errors
    if model is not None:
        if provider == "anthropic" and model not in _ANTHROPIC_MODEL_ALIASES:
            errors.append(
                f"{who}: preferred_model {model!r} is not a known Claude alias "
                f"{sorted(_ANTHROPIC_MODEL_ALIASES)}"
            )
        # openai ignores the model pin (single-model v1) — not an error.
    return errors


def save_crew_override(
    verb: str,
    *,
    preferred_provider: Optional[str] = None,
    preferred_model: Optional[str] = None,
    role: Optional[str] = None,
    clear: bool = False,
) -> Optional[dict]:
    """Write (or clear) ``verb``'s entry in the operator sidecar.

    With ``role=None`` the change applies at the crew level (default for every
    role); with ``role`` set, it writes/clears just that role's entry under
    ``roles:``. Provided fields MERGE over any existing entry.

    Refuses (``CrewOverrideRefused`` → 422) a cloud promotion onto a
    sensitivity-LOCKED crew that the lift doesn't permit: an MNPI lock needs
    ``AGENTIC_PLAN_TIER=enterprise`` + an active per-provider attestation for the
    named provider (Phase C); a confidential lock needs enterprise + the Claude
    (anthropic) lane (Phase B). Outside that — bridge tier, no attestation, or a
    keep-local/model-only write naming no liftable provider — a locked-crew write
    is refused; the sidecar can't record what the gate would 403. Raises
    ``KeyError`` (→ 404) when ``verb`` is not a registered crew. Atomic write.
    Returns the entry written (``None`` when cleared)."""
    manifest = get_manifest(verb)   # CrewRegistryError(KeyError) → route 404

    lock = manifest.sensitivity_override
    if role is not None and role not in manifest.roles:
        raise CrewOverrideRefused(
            f"crew {verb!r} has no role {role!r} (roles: {list(manifest.roles)})"
        )

    if not clear:
        # Shape-validate FIRST so an unknown/invalid provider is rejected here
        # (precise message naming the valid set), and the locked-crew lift gate
        # below only ever sees a CANONICAL provider — local / anthropic / openai.
        # This ordering closes the codex-review-flagged gap where an unknown
        # provider could skip the lift gate if shape validation were ever loosened
        # to accept more providers than _PROVIDER_LANE (dual-review hardening).
        who = f"crew override for {verb!r}" + (f" role {role!r}" if role else "")
        errors = _validate_promotion_value(preferred_provider, preferred_model, who=who)
        if errors:
            raise CrewOverrideRefused("; ".join(errors))

        # A sensitivity-locked crew (the /triage MNPI lock) may carry a cloud
        # promotion ONLY under the enterprise lift for its locked tier —
        # confidential → the Claude (anthropic) lane under enterprise (Phase B);
        # MNPI → a provider with an ACTIVE per-provider attestation under
        # enterprise (Phase C). The manifest lock IS the operator's explicit
        # assignment, so mnpi_explicit=True here. Anything the lift doesn't permit
        # — bridge tier, no attestation, or a keep-local/model-only write naming
        # no liftable provider — is refused: the sidecar must not record a
        # promotion the constitution forbids. (Clearing is always allowed — it can
        # only remove.) The central gate re-checks at dispatch regardless, so this
        # is the convenience guard, not the only one.
        if lock in ("confidential", "MNPI"):
            if preferred_provider is None or preferred_provider == _LOCAL_SENTINEL:
                raise CrewOverrideRefused(
                    f"crew {verb!r} is sensitivity-locked to {lock!r}; only a cloud "
                    f"promotion permitted by the enterprise/P5 lift may be written "
                    f"(a keep-local or model-only write is refused — use clear=true "
                    f"to remove instead)"
                )
            # preferred_provider is a canonical cloud provider here (shape-validated
            # above), so the lift gate covers EVERY non-local locked-crew write.
            if not _cloud_lane_allowed(lock, preferred_provider, mnpi_explicit=True):
                raise CrewOverrideRefused(
                    f"crew {verb!r} is sensitivity-locked to {lock!r}; promotion to "
                    f"{preferred_provider!r} is refused — it requires "
                    f"AGENTIC_PLAN_TIER=enterprise"
                    + (
                        " and an active P5 attestation for this provider"
                        if lock == "MNPI" else " on the Claude (anthropic) lane"
                    )
                    + " (CLAUDE.md §5.2)"
                )

    path = crew_sidecar_path()
    with _overrides_lock:
        current = _read_sidecar(path) if path.exists() else {}
        entry: dict = dict(current.get(verb) or {})

        if role is None:
            # Crew-level mutation.
            if clear:
                # Clearing the crew level drops the crew-level keys but keeps any
                # per-role entries (clear the whole crew via role=None + no roles).
                entry.pop("preferred_provider", None)
                entry.pop("preferred_model", None)
            else:
                if preferred_provider is not None:
                    entry["preferred_provider"] = preferred_provider
                if preferred_model is not None:
                    entry["preferred_model"] = preferred_model
        else:
            # Per-role mutation under ``roles:``.
            roles = dict(entry.get("roles") or {})
            if clear:
                roles.pop(role, None)
            else:
                role_entry: dict = dict(roles.get(role) or {}) if isinstance(roles.get(role), dict) else {}
                if preferred_provider is not None:
                    role_entry["preferred_provider"] = preferred_provider
                if preferred_model is not None:
                    role_entry["preferred_model"] = preferred_model
                roles[role] = role_entry
            if roles:
                entry["roles"] = roles
            else:
                entry.pop("roles", None)

        # Drop an emptied crew entry entirely so the sidecar stays clean.
        if entry:
            current[verb] = entry
        else:
            current.pop(verb, None)

        _write_sidecar_atomic(path, current)
        _overrides_cache.pop(str(path), None)
        return current.get(verb)


__all__ = [
    "CREW_OVERRIDES_ENV",
    "crew_sidecar_path",
    "RolePromotion",
    "CrewPromotion",
    "CrewOverrideRefused",
    "load_crew_overrides",
    "resolve_crew_promotion",
    "save_crew_override",
]
