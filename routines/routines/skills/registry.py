"""Skill registry — the ONE frontmatter-reading surface (#61 core).

Scans ``routines/skills/*/SKILL.md`` at startup, parses + validates the §14
frontmatter into :class:`SkillMetadata`, and exposes:

  * :func:`scan` — (re)build the global registry from disk. Idempotent.
  * :func:`load_skill_metadata` — fetch one registered skill (lazy-scans on
    first call so callers that never hit ``_lifespan`` — e.g. unit tests
    constructing the app without lifespan — still resolve skills).
  * :func:`get_active_skill_cap` — replaces the #67 ``_runtime`` stub. Reads
    ``cost_ceiling_<key>`` from the skill's frontmatter; ``None`` for
    unregistered skills / undeclared keys (preserves #67's no-op-gating
    contract until a real cap is declared).
  * :func:`validate_all` — return a list of §14 validation errors (empty = all
    valid). PURE: never mutates the global registry, so it's safe to point at
    an arbitrary ``skills_dir`` (synthetic-skill tests). As of #61-capabilities
    this also cross-checks the ``capabilities:`` block (network vs sensitivity,
    vault_write vs workspace_scope, malformed globs).
  * :func:`validate_or_raise` — fail-fast wrapper the bridge ``_lifespan``
    calls so a misconfigured skill refuses to BOOT rather than failing on
    first call.

This is the canonical skills-registry location. #67's
``routines/skills/_runtime/registry.py`` re-exports :func:`get_active_skill_cap`
from here — there is ONE registry surface, not two.
"""

from __future__ import annotations

import logging
import os
import platform
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Optional

import frontmatter
import yaml

from routines.shared.routing import (
    CLOUD_MODEL_ALIASES,
    TaskType,
    task_class_provider_override,
)

logger = logging.getLogger(__name__)

# Skills live one directory below this module: routines/skills/<skill>/SKILL.md.
SKILLS_DIR = Path(__file__).resolve().parent


# ─────────────────────────────────────────────────────────────────────────────
# §14 frontmatter enums + lane taxonomy (the validation contract)
# ─────────────────────────────────────────────────────────────────────────────

# Canonical sensitivity enum — matches routines.hooks.types.Sensitivity
# (uppercase MNPI, not the §14.1 prose lowercase). The code's Literal is the
# source of truth the runtime gates against.
_SENSITIVITY_TIERS = ("public", "internal", "confidential", "MNPI")
_WORKSPACE_SCOPES = ("project", "bd", "general", "any")

# Cloud lanes a confidential/MNPI skill must NOT list in allowed_tools, and the
# local lane it MUST list. Mirrors the §4 lane→sensitivity matrix enforced at
# runtime by ``central_guards.enforce_sensitivity_lane``.
_CLOUD_LANES = ("llm_cloud", "minimax")
_LOCAL_LANE = "llm_local"

# Top-level frontmatter keys every SKILL.md must declare (§14.1).
_REQUIRED_TOP_KEYS = ("name", "description", "version", "license", "allowed_tools")
# Recognised sub-keys inside the optional ``capabilities:`` block
# (#61-capabilities). A skill declares the surface it touches; the validator
# cross-checks it against sensitivity + workspace_scope at startup. Any key
# outside this set is a typo (e.g. ``netwrok``) and is rejected.
_CAPABILITY_KEYS = ("vault_read", "vault_write", "fs_roots", "network")
# Recognised sub-keys inside the optional ``captures_to_vault:`` block (#76).
# An opt-in declaration of WHAT a deliverable-producing skill captures back to
# the vault's semantic memory + WHERE. ``target`` / ``fields`` / ``headline``
# are required when the block is present; ``section`` is optional (defaults to
# "Valuation history"). Any key outside this set is a typo and is rejected.
_CAPTURES_KEYS = ("target", "fields", "headline", "section")
_CAPTURES_REQUIRED = ("target", "fields", "headline")
_DEFAULT_CAPTURE_SECTION = "Valuation history"

# Recognised sub-keys inside the optional ``requires:`` block (#74.2 readiness
# routing). A skill declares the runtime state it needs to exist BEFORE its work
# runs; the central guard ``enforce_skill_preconditions`` checks them at DISPATCH
# time and fails fast with a chat-friendly "not ready" message, instead of the
# skill crashing half-way through on a missing file. Absent block → no
# preconditions (back-compat). Any key outside this set is a typo and is rejected.
_REQUIRES_KEYS = ("vault_paths", "fs_roots")

# ── Tier 2 LLM-routing frontmatter (#llm-routing-tier-2, 2026-06-03) ──────────
# Optional top-level keys (siblings of ``capabilities:`` / ``captures_to_vault:``)
# declaring per-skill provider preference + sampling params. All absent →
# back-compat: the skill expresses no preference + the dispatcher falls through
# to the AGENTIC_CLOUD_PROVIDER env var, then the 'anthropic' default. See
# ``LLM-ROUTING-2026-06-02.md`` §Tier 2.
_ROUTING_TOP_KEYS = (
    "preferred_provider", "fallback_provider", "allowed_providers",
    "preferred_model", "llm_params", "fallback_llm_params",
)
# Operator-selectable cloud model aliases (#llm-routing-postjune15 P2 Task 3) —
# the valid ``preferred_model`` values (CLOUD_MODEL_ALIASES from shared.routing).
_PREFERRED_MODEL_VALUES = CLOUD_MODEL_ALIASES
# Provider key taxonomies. ``ollama-only`` is a *preferred* sentinel meaning
# "force the local lane — never route this skill to a cloud provider"; in the
# allow-list the local provider is plain ``ollama``. ``prefer_local`` is the
# token-saving *downgrade* sentinel (#llm-routing-postjune15 P2): it asks a
# public/internal CLOUD pick to run on the local Ollama lane instead — distinct
# from ``ollama-only`` (the confidential fail-closed "refuse cloud"). Both are
# local-safe, so both are valid at ANY sensitivity and both map to the plain
# ``ollama`` allow-list key. The cloud providers are the ones a
# confidential/MNPI skill must NOT name (parity with the §4 cloud-lane + #61
# network rules — confidential data never leaves the local box).
_CLOUD_PROVIDERS = ("anthropic", "openai")
# Preferred sentinels that resolve to the local ``ollama`` allow-list key
# (neither is a cloud provider; both never leave the local box).
_LOCAL_PREFERRED_SENTINELS = ("ollama-only", "prefer_local")
_PREFERRED_PROVIDER_VALUES = ("anthropic", "openai", "ollama-only", "prefer_local")
_FALLBACK_PROVIDER_VALUES = ("anthropic", "openai")
_ALLOWED_PROVIDER_VALUES = ("anthropic", "openai", "ollama")
# Default allow-list when ``allowed_providers:`` is omitted = every provider.
DEFAULT_ALLOWED_PROVIDERS: tuple[str, ...] = ("anthropic", "openai", "ollama")
# Recognised sub-keys inside ``llm_params:`` / ``fallback_llm_params:``.
_LLM_PARAM_KEYS = ("temperature", "max_tokens")
# The cloud dispatcher's terminal default when nothing else resolves.
DEFAULT_CLOUD_PROVIDER = "anthropic"
# Keys inside the ``metadata:`` block (§14.1).
_REQUIRED_META_KEYS = (
    "sensitivity", "workspace_scope", "tile_label",
    "cost_ceiling_tokens", "cost_ceiling_seconds",
    "guardrails", "guardrail_max_retries",
)


def _is_cloud_lane(tool: str) -> bool:
    """A tool/lane that leaves the local machine. ``claude-cli*`` is the
    enterprise cloud lane family (see §4)."""
    return tool in _CLOUD_LANES or tool.startswith("claude-cli")


# ─────────────────────────────────────────────────────────────────────────────
# SkillMetadata
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SkillCapabilities:
    """The declared capability surface for one skill (#61-capabilities).

    Each field is a tuple of declarations (frozen-friendly), defaulting to
    empty — an absent ``capabilities:`` block parses as "declares nothing"
    (back-compat: un-migrated skills don't break the bridge).

      * ``vault_read`` / ``vault_write`` — vault-relative path globs
        (``"Projects/**"``). ``vault_write`` is cross-checked against
        ``workspace_scope`` at startup.
      * ``fs_roots`` — external filesystem roots the skill may touch
        (absolute, forward-slash: ``"<workspace-root>/**"``).
      * ``network`` — allowed network hosts (``"api.tavily.com"``). MUST be
        empty for a confidential/MNPI skill — the declarative, systemic form
        of the §5.2 no-MNPI-to-cloud rule, enforced at boot."""

    vault_read: tuple[str, ...] = ()
    vault_write: tuple[str, ...] = ()
    fs_roots: tuple[str, ...] = ()
    network: tuple[str, ...] = ()


@dataclass(frozen=True)
class SkillPreconditions:
    """The runtime readiness preconditions for one skill (#74.2 readiness
    routing).

    A skill declares the state it needs to exist BEFORE its work runs, via an
    optional top-level ``requires:`` block. The central guard
    ``enforce_skill_preconditions`` checks these at DISPATCH time and fails fast
    with a chat-friendly "not ready" message — instead of the skill crashing
    half-way through on a missing file (the Thoth "readiness routing" pattern).

    Both fields default to empty — an absent ``requires:`` block parses as "no
    preconditions" (back-compat: un-migrated skills are unaffected; the guard is
    a pure no-op for them — no filesystem access unless a skill opts in).

      * ``vault_paths`` — vault-relative paths or globs that must resolve to at
        least one existing path under the vault root (``"Registers/Lessons.md"``,
        ``"Sectors/**"``). Forward-slash, vault-RELATIVE — an absolute form (a
        leading ``/`` or a drive root ``X:/…``) or a ``..`` segment is rejected at
        boot (it would escape the vault root).
      * ``fs_roots`` — absolute filesystem paths or globs that must exist
        (``"<workspace-root>/4. Research & data/Precedent transactions tracker"``).
    """

    vault_paths: tuple[str, ...] = ()
    fs_roots: tuple[str, ...] = ()


@dataclass(frozen=True)
class CapturesToVault:
    """The declared conclusion-capture surface for one skill (#76).

    A deliverable-producing skill (LBO, and later DCF/comps) opts in to the
    deliverable→vault capture loop via a top-level ``captures_to_vault:`` block.
    It names WHAT headline metrics to capture from the skill's structured result
    and WHERE to land them as a dated, sourced semantic fact (on operator Route).

      * ``target`` — vault-relative note path, templated by run context
        (``"Companies/{deal_name}.md"``). The captured fact lands here.
      * ``fields`` — the result keys to record (``irr_central_pct`` etc.); kept
        in the proposal frontmatter so the conclusion is queryable.
      * ``headline`` — a template string (``{field}`` placeholders) rendered
        into the one-line semantic fact appended to the target note.
      * ``section`` — the ``## <section>`` heading the dated bullet appends
        under (append-only, never overwrites — §3 rule 9). Defaults to
        ``"Valuation history"``.

    Absent block → ``None`` on :class:`SkillMetadata` (the skill does not
    capture — e.g. sector-news, which writes its own newsletter)."""

    target: str
    headline: str
    fields: tuple[str, ...] = ()
    section: str = _DEFAULT_CAPTURE_SECTION


@dataclass(frozen=True)
class LLMParams:
    """Per-skill sampling parameters (#llm-routing-tier-2).

    Composes WITH provider routing: ``SkillRouting`` picks WHICH provider to
    call; ``LLMParams`` declares HOW the chosen provider samples. Both fields
    default to ``None`` ("unset → provider default"); an absent ``llm_params:``
    block parses to an all-None instance so the dispatcher passes nothing extra
    (back-compat — pre-Tier-2 every cloud ``chat()`` call left these unset).

      * ``temperature`` — 0.0 (strict deterministic) … 1.0 (full creative).
        Validated in [0.0, 1.0] at boot; outside the range hard-fails.
      * ``max_tokens`` — positive int cap, or ``None`` for the provider default.
    """

    temperature: Optional[float] = None
    max_tokens: Optional[int] = None

    def as_dict(self) -> dict[str, Any]:
        """Only the keys that are actually set — so the dispatcher can splat
        them into ``chat(**params)`` without forcing ``temperature=None`` to
        override a caller default."""
        out: dict[str, Any] = {}
        if self.temperature is not None:
            out["temperature"] = self.temperature
        if self.max_tokens is not None:
            out["max_tokens"] = self.max_tokens
        return out


@dataclass(frozen=True)
class SkillRouting:
    """The declared LLM-routing surface for one skill (#llm-routing-tier-2).

    Absent block(s) → all-default: no preference, all providers allowed, no
    sampling overrides. The dispatcher in ``routines.sessions.router`` reads
    this (overlaid by the operator sidecar) to pick a cloud provider + sampling
    params per skill, replacing the env-var-only Tier 1 behaviour.

      * ``preferred_provider`` — ``anthropic`` | ``openai`` | ``ollama-only`` |
        ``prefer_local``. ``ollama-only`` forces the local lane (the skill must
        never reach a cloud provider — the confidential fail-closed sentinel).
        ``prefer_local`` *downgrades* a public/internal cloud pick to the local
        Ollama lane to save tokens (#llm-routing-postjune15 P2); it can never
        breach sensitivity. ``None`` → defer to env var / default.
      * ``fallback_provider`` — cloud provider tried when the preferred fails
        (``anthropic`` | ``openai``). Must be within ``allowed_providers`` if
        both are explicitly set.
      * ``allowed_providers`` — explicit allow-list; defaults to all three.
        A confidential/MNPI skill MUST NOT list a cloud provider here.
      * ``preferred_model`` — operator-selected CLOUD model alias
        (``opus`` | ``sonnet`` | ``haiku`` | ``opus-1m``,
        #llm-routing-postjune15 P2 Task 3). Overrides the lane's default model
        on the Claude lane; a ``-1m`` variant also sizes the context window to
        1M. ``None`` → the lane default. Local/codex lanes ignore it.
      * ``llm_params`` / ``fallback_llm_params`` — sampling params for the
        primary / fallback path."""

    preferred_provider: Optional[str] = None
    fallback_provider: Optional[str] = None
    allowed_providers: tuple[str, ...] = DEFAULT_ALLOWED_PROVIDERS
    preferred_model: Optional[str] = None
    llm_params: LLMParams = field(default_factory=LLMParams)
    fallback_llm_params: LLMParams = field(default_factory=LLMParams)


@dataclass(frozen=True)
class SkillMetadata:
    """Parsed, validated §14 frontmatter for one skill.

    ``allowed_tools`` / ``guardrails`` are tuples (frozen-friendly).
    ``cost_ceilings`` carries every ``cost_ceiling_<key>`` declared (tokens,
    seconds, and any future ``llm_calls`` cap) so :func:`get_active_skill_cap`
    can resolve arbitrary keys without a field-per-cap explosion."""

    name: str
    description: str
    version: str
    license: str
    allowed_tools: tuple[str, ...]
    sensitivity: str            # public | internal | confidential | MNPI
    workspace_scope: str        # project | bd | general | any
    tile_label: str
    cost_ceiling_tokens: int
    cost_ceiling_seconds: int
    guardrails: tuple[str, ...]
    guardrail_max_retries: int
    cost_ceilings: dict[str, int] = field(default_factory=dict)
    capabilities: SkillCapabilities = field(default_factory=SkillCapabilities)
    captures_to_vault: Optional[CapturesToVault] = None
    routing: SkillRouting = field(default_factory=SkillRouting)
    preconditions: SkillPreconditions = field(default_factory=SkillPreconditions)
    # Per-skill default system prompt for the gated ``llm()`` helper
    # (#llm-skill-system-prompt). Resolved from the SKILL.md ``llm_system_prompt:``
    # inline value OR the contents of the ``llm_system_prompt_file:`` sibling
    # file. ``None`` → the skill declares none → ``llm()`` uses the platform
    # persona. The ``@anton_skill`` wrapper carries this onto SkillLLMContext.
    llm_system_prompt: Optional[str] = None
    source_path: Path = field(default_factory=Path)

    def to_hook_metadata(self) -> dict[str, Any]:
        """The governance subset the #22 hook stack reads off
        ``ctx.skill.metadata`` (sensitivity / workspace_scope / cost ceilings).

        Centralised here so the ``@anton_skill`` wrapper (and any route) build the
        hook-context metadata from ONE place — as the consumed surface grows the
        callers can't drift (codex arch-review SEV-3). Faithful to what the
        routes pass today; extend here (not per-caller) when a guard starts
        reading a new field."""
        return {
            "sensitivity": self.sensitivity,
            "workspace_scope": self.workspace_scope,
            "cost_ceiling_tokens": self.cost_ceiling_tokens,
            "cost_ceiling_seconds": self.cost_ceiling_seconds,
        }


# Module-level registry. Rebuilt by ``scan()``; lazily populated by
# ``load_skill_metadata`` on first access.
_REGISTRY: dict[str, SkillMetadata] = {}
_scanned = False


# ─────────────────────────────────────────────────────────────────────────────
# Parsing + validation (pure helpers — no global mutation)
# ─────────────────────────────────────────────────────────────────────────────


def _iter_skill_md(skills_dir: Path) -> Iterator[Path]:
    """Yield every ``<skill>/SKILL.md`` directly under ``skills_dir``.

    The glob matches only directories that actually contain a SKILL.md, so
    ``_runtime/`` (no SKILL.md) and ``__pycache__`` are skipped for free."""
    yield from sorted(skills_dir.glob("*/SKILL.md"))


def _cost_ceilings(meta: dict) -> dict[str, int]:
    """Extract ``cost_ceiling_<key>`` → int from a metadata block."""
    out: dict[str, int] = {}
    for k, v in meta.items():
        if k.startswith("cost_ceiling_") and isinstance(v, int) and not isinstance(v, bool):
            out[k.removeprefix("cost_ceiling_")] = v
    return out


def _parse_capabilities(post_meta: dict) -> SkillCapabilities:
    """Build :class:`SkillCapabilities` from the top-level ``capabilities:``
    block. Absent → all-empty (back-compat). Assumes the block already passed
    :func:`_validate_capabilities` (each declared value is a list of str)."""
    block = post_meta.get("capabilities") or {}

    def _tuple(key: str) -> tuple[str, ...]:
        vals = block.get(key) or ()
        return tuple(str(v) for v in vals)

    return SkillCapabilities(
        vault_read=_tuple("vault_read"),
        vault_write=_tuple("vault_write"),
        fs_roots=_tuple("fs_roots"),
        network=_tuple("network"),
    )


def _parse_requires(post_meta: dict) -> SkillPreconditions:
    """Build :class:`SkillPreconditions` from the top-level ``requires:`` block
    (#74.2). Absent → all-empty (back-compat). Assumes the block already passed
    :func:`_validate_requires` (each declared value is a list of str)."""
    block = post_meta.get("requires") or {}

    def _tuple(key: str) -> tuple[str, ...]:
        vals = block.get(key) or ()
        return tuple(str(v) for v in vals)

    return SkillPreconditions(
        vault_paths=_tuple("vault_paths"),
        fs_roots=_tuple("fs_roots"),
    )


def _parse_captures_to_vault(post_meta: dict) -> Optional[CapturesToVault]:
    """Build :class:`CapturesToVault` from the top-level ``captures_to_vault:``
    block. Absent → ``None`` (the skill doesn't capture). Assumes the block
    already passed :func:`_validate_captures_to_vault`."""
    block = post_meta.get("captures_to_vault")
    if not block:
        return None
    return CapturesToVault(
        target=str(block["target"]),
        headline=str(block["headline"]),
        fields=tuple(str(f) for f in (block.get("fields") or ())),
        section=str(block.get("section") or _DEFAULT_CAPTURE_SECTION),
    )


def _coerce_llm_params(block: Any) -> LLMParams:
    """Build :class:`LLMParams` from an ``llm_params:`` mapping (or ``None``).

    Lenient on TYPE here (validation already ran); a missing/None block → an
    all-None instance. ``max_tokens: null`` round-trips to ``None`` (provider
    default)."""
    if not isinstance(block, dict):
        return LLMParams()
    temp = block.get("temperature")
    max_tok = block.get("max_tokens")
    return LLMParams(
        temperature=float(temp) if isinstance(temp, (int, float)) and not isinstance(temp, bool) else None,
        max_tokens=int(max_tok) if isinstance(max_tok, int) and not isinstance(max_tok, bool) else None,
    )


def _parse_routing(post_meta: dict) -> SkillRouting:
    """Build :class:`SkillRouting` from the top-level routing keys
    (#llm-routing-tier-2). Assumes :func:`_validate_routing` passed. Absent →
    all-default (no preference, all providers allowed, no sampling overrides)."""
    preferred = post_meta.get("preferred_provider")
    fallback = post_meta.get("fallback_provider")
    allowed = post_meta.get("allowed_providers")
    preferred_model = post_meta.get("preferred_model")
    allowed_tuple = (
        tuple(str(p) for p in allowed)
        if isinstance(allowed, list) and allowed
        else DEFAULT_ALLOWED_PROVIDERS
    )
    return SkillRouting(
        preferred_provider=str(preferred) if preferred else None,
        fallback_provider=str(fallback) if fallback else None,
        allowed_providers=allowed_tuple,
        preferred_model=str(preferred_model) if preferred_model else None,
        llm_params=_coerce_llm_params(post_meta.get("llm_params")),
        fallback_llm_params=_coerce_llm_params(post_meta.get("fallback_llm_params")),
    )


def _validate_captures_to_vault(post_meta: dict, who: str) -> list[str]:
    """Return validation errors for the optional ``captures_to_vault:`` block (#76).

    Absent → no error (opt-in; most skills don't capture). When present it must
    be a mapping declaring ``target`` (str) + ``headline`` (str) + ``fields``
    (list of str); ``section`` is an optional str. Unknown keys are typos and
    are rejected so a mistyped ``targett:`` can't silently disable capture."""
    if "captures_to_vault" not in post_meta:
        return []

    block = post_meta.get("captures_to_vault")
    if not isinstance(block, dict):
        return [
            f"{who}: captures_to_vault must be a mapping, got "
            f"{type(block).__name__}"
        ]

    errors: list[str] = []

    for key in block:
        if key not in _CAPTURES_KEYS:
            errors.append(
                f"{who}: unknown captures_to_vault key {key!r} "
                f"(expected one of {_CAPTURES_KEYS})"
            )

    for key in _CAPTURES_REQUIRED:
        if key not in block:
            errors.append(f"{who}: captures_to_vault missing required key {key!r}")

    for key in ("target", "headline", "section"):
        if key in block and not (isinstance(block[key], str) and block[key].strip()):
            errors.append(
                f"{who}: captures_to_vault.{key} must be a non-empty string"
            )

    fields = block.get("fields")
    if "fields" in block:
        if not isinstance(fields, list) or not fields:
            errors.append(
                f"{who}: captures_to_vault.fields must be a non-empty list"
            )
        else:
            for entry in fields:
                if not isinstance(entry, str) or not entry.strip():
                    errors.append(
                        f"{who}: captures_to_vault.fields entry {entry!r} is not "
                        f"a non-empty string"
                    )

    return errors


def _validate_llm_params(block: Any, who: str, label: str) -> list[str]:
    """Validate one ``llm_params:`` / ``fallback_llm_params:`` mapping.

    Rules (hard-fail at boot):
      * must be a mapping; unknown keys are typos and are rejected;
      * ``temperature`` (if present) is a number in [0.0, 1.0] — the spec's
        sampling range. 1.5 / -0.1 hard-fail;
      * ``max_tokens`` (if present) is a positive int OR ``null`` (provider
        default)."""
    if block is None:
        return []
    if not isinstance(block, dict):
        return [f"{who}: {label} must be a mapping, got {type(block).__name__}"]

    errors: list[str] = []
    for key in block:
        if key not in _LLM_PARAM_KEYS:
            errors.append(
                f"{who}: unknown {label} key {key!r} (expected one of {_LLM_PARAM_KEYS})"
            )

    if "temperature" in block:
        temp = block["temperature"]
        if isinstance(temp, bool) or not isinstance(temp, (int, float)):
            errors.append(
                f"{who}: {label}.temperature must be a number in [0.0, 1.0], "
                f"got {temp!r}"
            )
        elif not (0.0 <= float(temp) <= 1.0):
            errors.append(
                f"{who}: {label}.temperature {temp!r} outside [0.0, 1.0]"
            )

    if "max_tokens" in block:
        mt = block["max_tokens"]
        if mt is not None and (isinstance(mt, bool) or not isinstance(mt, int) or mt <= 0):
            errors.append(
                f"{who}: {label}.max_tokens must be a positive int or null, got {mt!r}"
            )

    return errors


def _validate_routing(post_meta: dict, who: str) -> list[str]:
    """Return validation errors for the optional Tier 2 routing keys
    (#llm-routing-tier-2). Absent keys → no error (back-compat).

    The load-bearing cross-check (the reason this is a HARD boot gate, parity
    with the #61 confidential-⇒-no-network rule and the §4 confidential-⇒-no-
    cloud-lane rule): a confidential/MNPI skill MUST NOT name a CLOUD provider
    in ``preferred_provider`` or ``allowed_providers`` — confidential material
    never leaves the local box, so its only valid providers are local
    (``ollama`` / ``ollama-only`` / ``prefer_local``). Plus: provider enums are
    checked, sampling params are range-checked, and a declared fallback must sit
    inside a declared allow-list."""
    if not any(k in post_meta for k in _ROUTING_TOP_KEYS):
        return []

    errors: list[str] = []
    sensitivity = (post_meta.get("metadata") or {}).get("sensitivity")
    is_confidential = sensitivity in ("confidential", "MNPI")

    # ── preferred_provider ────────────────────────────────────────────────
    preferred = post_meta.get("preferred_provider")
    if preferred is not None:
        if preferred not in _PREFERRED_PROVIDER_VALUES:
            errors.append(
                f"{who}: unknown preferred_provider {preferred!r} "
                f"(expected one of {_PREFERRED_PROVIDER_VALUES})"
            )
        elif is_confidential and preferred in _CLOUD_PROVIDERS:
            errors.append(
                f"{who}: sensitivity={sensitivity!r} forbids cloud "
                f"preferred_provider {preferred!r} — confidential data never "
                f"leaves the local box (CLAUDE.md §5.2). Use 'ollama-only'."
            )

    # ── fallback_provider ─────────────────────────────────────────────────
    fallback = post_meta.get("fallback_provider")
    if fallback is not None:
        if fallback not in _FALLBACK_PROVIDER_VALUES:
            errors.append(
                f"{who}: unknown fallback_provider {fallback!r} "
                f"(expected one of {_FALLBACK_PROVIDER_VALUES})"
            )
        elif is_confidential:
            # Every fallback value is a cloud provider, so any fallback on a
            # confidential skill is forbidden.
            errors.append(
                f"{who}: sensitivity={sensitivity!r} forbids cloud "
                f"fallback_provider {fallback!r} (CLAUDE.md §5.2)"
            )

    # ── allowed_providers ─────────────────────────────────────────────────
    allowed = post_meta.get("allowed_providers")
    allowed_list: list[str] = []
    if allowed is not None:
        if not isinstance(allowed, list) or not allowed:
            errors.append(
                f"{who}: allowed_providers must be a non-empty list, got {allowed!r}"
            )
        else:
            for p in allowed:
                if p not in _ALLOWED_PROVIDER_VALUES:
                    errors.append(
                        f"{who}: unknown allowed_providers entry {p!r} "
                        f"(expected one of {_ALLOWED_PROVIDER_VALUES})"
                    )
                else:
                    allowed_list.append(p)
            if is_confidential:
                cloud = [p for p in allowed_list if p in _CLOUD_PROVIDERS]
                if cloud:
                    errors.append(
                        f"{who}: sensitivity={sensitivity!r} forbids cloud "
                        f"allowed_providers {cloud} — confidential data never "
                        f"leaves the local box (CLAUDE.md §5.2)"
                    )

    # ── preferred ∈ allowed (only when both explicitly set) ───────────────
    # The local sentinels (ollama-only / prefer_local) map to the 'ollama'
    # allow-list key.
    if preferred in _PREFERRED_PROVIDER_VALUES and allowed_list:
        prov_key = "ollama" if preferred in _LOCAL_PREFERRED_SENTINELS else preferred
        if prov_key not in allowed_list:
            errors.append(
                f"{who}: preferred_provider {preferred!r} not in allowed_providers "
                f"{allowed_list}"
            )

    # ── fallback ∈ allowed (only when BOTH explicitly set) ────────────────
    if fallback in _FALLBACK_PROVIDER_VALUES and allowed_list and fallback not in allowed_list:
        errors.append(
            f"{who}: fallback_provider {fallback!r} not in allowed_providers "
            f"{allowed_list}"
        )

    # ── preferred_model (#llm-routing-postjune15 P2 Task 3) ───────────────
    # An operator-selected CLOUD model alias. Validated against the known set;
    # NOT cross-checked against sensitivity (it selects a model, never a
    # provider — it can't breach the §5.2 floor: a confidential skill is still
    # forced local regardless of the model named).
    preferred_model = post_meta.get("preferred_model")
    if preferred_model is not None and preferred_model not in _PREFERRED_MODEL_VALUES:
        errors.append(
            f"{who}: unknown preferred_model {preferred_model!r} "
            f"(expected one of {_PREFERRED_MODEL_VALUES})"
        )

    # ── sampling params ───────────────────────────────────────────────────
    errors.extend(_validate_llm_params(post_meta.get("llm_params"), who, "llm_params"))
    errors.extend(
        _validate_llm_params(
            post_meta.get("fallback_llm_params"), who, "fallback_llm_params"
        )
    )

    return errors


def _is_malformed_glob(g: object) -> bool:
    """A path-glob entry is malformed if it's not a non-empty string or it
    uses a backslash. Vault/fs globs are forward-slash, vault-relative-or-
    absolute (matching the [[workspace-write-policy]] normalisation, which
    lowercases + forward-slashes before comparison); a ``\\`` means the author
    used a Windows separator and the glob would never match at runtime."""
    if not isinstance(g, str) or not g.strip():
        return True
    return "\\" in g


def _validate_capabilities(post_meta: dict, who: str) -> list[str]:
    """Return validation errors for the optional ``capabilities:`` block.

    The load-bearing rule (the reason this exists): a confidential/MNPI skill
    MUST declare ``network: []`` — it can't be authored to reach an external
    endpoint, the systemic form of the §5.2 MNPI-never-to-cloud rule. Plus:
    project-scoped skills can't declare ``vault_write`` outside ``Projects/``;
    malformed globs and unknown capability keys are rejected.

    ``fs_roots`` is shape-checked (malformed globs) but NOT cross-checked
    against the §2 workspace-write-policy roots: the canonical write-policy
    roots list ``<workspace-root>/`` while the LBO skill's own Output
    Contract legitimately targets ``<workspace-root>/`` (a known path-migration
    discrepancy, see workspace-write-policy §12). Hard-checking fs_roots here
    would false-reject; vault_write is the scope-checked surface."""
    if "capabilities" not in post_meta:
        return []  # absent → empty caps, no error (back-compat)

    block = post_meta.get("capabilities")
    if not isinstance(block, dict):
        return [f"{who}: capabilities must be a mapping, got {type(block).__name__}"]

    errors: list[str] = []

    # Unknown keys are typos (e.g. 'netwrok') — reject so they can't silently
    # declare nothing.
    for key in block:
        if key not in _CAPABILITY_KEYS:
            errors.append(
                f"{who}: unknown capabilities key {key!r} "
                f"(expected one of {_CAPABILITY_KEYS})"
            )

    # Each declared value must be a list.
    for key in _CAPABILITY_KEYS:
        if key not in block:
            continue
        val = block[key]
        if not isinstance(val, list):
            errors.append(
                f"{who}: capabilities.{key} must be a list, got "
                f"{type(val).__name__}"
            )
            continue
        # Glob shape (network entries are hosts, not path globs, but the same
        # non-empty-string check applies).
        for entry in val:
            if key == "network":
                if not isinstance(entry, str) or not entry.strip():
                    errors.append(
                        f"{who}: capabilities.network host {entry!r} is not a "
                        f"non-empty string"
                    )
            elif _is_malformed_glob(entry):
                errors.append(
                    f"{who}: capabilities.{key} has malformed glob {entry!r} "
                    f"(must be a non-empty forward-slash path)"
                )

    # ── The load-bearing cross-check: confidential/MNPI ⇒ no network ──────────
    network = block.get("network")
    sensitivity = (post_meta.get("metadata") or {}).get("sensitivity")
    if (
        isinstance(network, list) and network
        and sensitivity in ("confidential", "MNPI")
    ):
        errors.append(
            f"{who}: sensitivity={sensitivity!r} forbids network endpoints "
            f"{network} — exfiltration risk (CLAUDE.md §5.2)"
        )

    # ── vault_write must stay inside the workspace_scope ──────────────────────
    # A project-scoped skill writes only under the deal tree (Projects/**);
    # declaring vault_write to Companies/**, Registers/**, etc. escapes its
    # scope and is rejected.
    vault_write = block.get("vault_write")
    scope = (post_meta.get("metadata") or {}).get("workspace_scope")
    if scope == "project" and isinstance(vault_write, list):
        for g in vault_write:
            if isinstance(g, str) and not g.strip().lstrip("/").startswith("Projects/"):
                errors.append(
                    f"{who}: workspace_scope='project' forbids vault_write "
                    f"glob {g!r} outside 'Projects/**'"
                )

    return errors


def _has_dotdot_segment(p: str) -> bool:
    """True if any path segment is exactly ``..`` (traversal). Splits on both
    separators so a Windows-style entry is caught too (those are already
    rejected as malformed globs, but this is belt-and-suspenders)."""
    return ".." in p.replace("\\", "/").split("/")


def _is_absolute_fs_path(p: str) -> bool:
    """A forward-slash ABSOLUTE path: a drive root (``X:/…``) or a posix/UNC
    root (``/…`` / ``//…``). Deterministic across host OS — we do NOT call
    ``os.path.isabs`` (whose answer depends on the test runner's platform), so
    an ``fs_roots`` declaration validates the same on Windows and WSL."""
    if p.startswith("/"):
        return True
    return len(p) >= 3 and p[1] == ":" and p[0].isalpha() and p[2] == "/"


def _validate_requires(post_meta: dict, who: str) -> list[str]:
    """Return validation errors for the optional ``requires:`` block (#74.2).

    Shape-only (the existence of the declared paths is a RUNTIME check done by
    ``central_guards.enforce_skill_preconditions`` at dispatch, not at boot — a
    required file may legitimately be absent at boot and created before the skill
    is first invoked). The block must be a mapping of known keys to lists of
    non-empty forward-slash strings; ``vault_paths`` are vault-relative (no
    ``..`` traversal); ``fs_roots`` must be absolute. Absent → no errors
    (back-compat)."""
    if "requires" not in post_meta:
        return []  # absent → no preconditions, no error (back-compat)

    block = post_meta.get("requires")
    if not isinstance(block, dict):
        return [f"{who}: requires must be a mapping, got {type(block).__name__}"]

    errors: list[str] = []

    # Unknown keys are typos (e.g. 'vualt_paths') — reject so they can't
    # silently declare nothing.
    for key in block:
        if key not in _REQUIRES_KEYS:
            errors.append(
                f"{who}: unknown requires key {key!r} "
                f"(expected one of {_REQUIRES_KEYS})"
            )

    for key in _REQUIRES_KEYS:
        if key not in block:
            continue
        val = block[key]
        if not isinstance(val, list):
            errors.append(
                f"{who}: requires.{key} must be a list, got {type(val).__name__}"
            )
            continue
        for entry in val:
            if _is_malformed_glob(entry):
                errors.append(
                    f"{who}: requires.{key} has malformed path {entry!r} "
                    f"(must be a non-empty forward-slash path)"
                )
                continue
            # No traversal — the runtime contract resolves vault_paths UNDER the
            # vault root, so a '..' segment would escape it (codex-5.5 SEV-2).
            if _has_dotdot_segment(entry):
                errors.append(
                    f"{who}: requires.{key} path {entry!r} must not contain a "
                    f"'..' segment (no traversal)"
                )
            if key == "vault_paths":
                # vault_paths are vault-RELATIVE. An absolute form — leading '/'
                # OR a drive root ('X:/…') — escapes the vault: on Windows
                # ``vault_root / 'X:/foo'`` DISCARDS vault_root, so an unrelated
                # file outside the vault would satisfy the precondition
                # (codex-5.5 R2 SEV-2). Reject it at boot.
                if _is_absolute_fs_path(entry):
                    errors.append(
                        f"{who}: requires.vault_paths path {entry!r} must be "
                        f"vault-relative, not absolute"
                    )
            else:  # fs_roots
                # fs_roots are absolute by contract — a RELATIVE entry would
                # resolve against the bridge cwd at runtime (wrong +
                # non-deterministic). Reject it at boot (codex-5.5 SEV-2).
                if not _is_absolute_fs_path(entry):
                    errors.append(
                        f"{who}: requires.fs_roots path {entry!r} must be absolute "
                        f"(a drive root 'X:/…' or a posix root '/…'), not relative"
                    )

    return errors


def _validate_llm_system_prompt(post_meta: dict, source: Path, who: str) -> list[str]:
    """Validate the optional per-skill system-prompt declaration
    (#llm-skill-system-prompt). A skill may declare the default system prompt for
    its gated ``llm()`` calls EITHER inline (``llm_system_prompt:`` — a string /
    YAML block scalar) OR via a sibling file (``llm_system_prompt_file:``) — never
    both. Absent → no errors (the skill's ``llm()`` calls use the platform
    persona).

    Hard-fail-at-boot rules (mirrors the rest of §14):
      * declaring BOTH keys is ambiguous → error;
      * ``llm_system_prompt`` must be a non-empty string;
      * ``llm_system_prompt_file`` must be a non-empty string naming a file that
        resolves INSIDE the skill directory (no ``..`` escape — a skill must not
        point its system prompt at an arbitrary file on disk), exists, is a
        regular file, and is non-empty.
    """
    # Key PRESENCE, not value truthiness (codex-5.5 SEV-2): a present-but-null
    # declaration (``llm_system_prompt:`` with no value → YAML ``None``) is a
    # malformed declaration that must hard-fail, not be silently treated as
    # "absent". ``.get()`` can't tell the two apart, so test membership.
    has_inline = "llm_system_prompt" in post_meta
    has_file = "llm_system_prompt_file" in post_meta
    if not has_inline and not has_file:
        return []

    inline = post_meta.get("llm_system_prompt")
    file_ref = post_meta.get("llm_system_prompt_file")

    errors: list[str] = []
    if has_inline and has_file:
        errors.append(
            f"{who}: declare ONE of llm_system_prompt / llm_system_prompt_file, "
            "not both"
        )

    if has_inline and (not isinstance(inline, str) or not inline.strip()):
        errors.append(f"{who}: llm_system_prompt must be a non-empty string")

    if has_file:
        if not isinstance(file_ref, str) or not file_ref.strip():
            errors.append(
                f"{who}: llm_system_prompt_file must be a non-empty string (a path "
                "relative to the skill directory)"
            )
        else:
            skill_dir = source.parent.resolve()
            target = (skill_dir / file_ref).resolve()
            if skill_dir != target and skill_dir not in target.parents:
                errors.append(
                    f"{who}: llm_system_prompt_file {file_ref!r} escapes the skill "
                    "directory (no '..' traversal)"
                )
            elif not target.is_file():
                errors.append(
                    f"{who}: llm_system_prompt_file {file_ref!r} not found at {target}"
                )
            else:
                try:
                    if not target.read_text(encoding="utf-8").strip():
                        errors.append(
                            f"{who}: llm_system_prompt_file {file_ref!r} is empty"
                        )
                # UnicodeError covers a non-UTF-8 file (codex-5.5 SEV-3) — a
                # controlled boot error, not an uncaught UnicodeDecodeError.
                except (OSError, UnicodeError) as e:
                    errors.append(
                        f"{who}: llm_system_prompt_file {file_ref!r} unreadable: {e}"
                    )
    return errors


def _resolve_llm_system_prompt(post_meta: dict, source: Path) -> Optional[str]:
    """The skill's declared ``llm()`` default system prompt (stripped), or
    ``None``. At most one of the two keys is set — :func:`_validate_llm_system_prompt`
    guarantees it — so order is moot. Assumes validation passed (the sibling file
    exists + is readable + non-empty)."""
    inline = post_meta.get("llm_system_prompt")
    if isinstance(inline, str) and inline.strip():
        return inline.strip()
    file_ref = post_meta.get("llm_system_prompt_file")
    if isinstance(file_ref, str) and file_ref.strip():
        target = (source.parent / file_ref).resolve()
        return target.read_text(encoding="utf-8").strip()
    return None


def _validate_frontmatter(post_meta: dict, source: Path) -> list[str]:
    """Return §14 validation errors for one SKILL.md's parsed frontmatter.

    Every error is prefixed with the offending skill (name if present, else
    the directory) so a startup failure names what to fix."""
    meta_block = post_meta.get("metadata") or {}
    name = post_meta.get("name") or source.parent.name
    who = f"skill {name!r} ({source.parent.name}/SKILL.md)"
    errors: list[str] = []

    # Required top-level keys.
    for key in _REQUIRED_TOP_KEYS:
        if key not in post_meta:
            errors.append(f"{who}: missing required frontmatter key {key!r}")
    if "metadata" not in post_meta:
        errors.append(f"{who}: missing required frontmatter key 'metadata'")
        # Without the metadata block the per-key checks below are moot.
        return errors

    # Required metadata keys.
    for key in _REQUIRED_META_KEYS:
        if key not in meta_block:
            errors.append(f"{who}: missing required metadata key {key!r}")

    sensitivity = meta_block.get("sensitivity")
    if sensitivity is not None and sensitivity not in _SENSITIVITY_TIERS:
        errors.append(
            f"{who}: sensitivity {sensitivity!r} not in {_SENSITIVITY_TIERS}"
        )

    scope = meta_block.get("workspace_scope")
    if scope is not None and scope not in _WORKSPACE_SCOPES:
        errors.append(
            f"{who}: workspace_scope {scope!r} not in {_WORKSPACE_SCOPES}"
        )

    # Cost ceilings must be positive ints.
    for key in ("cost_ceiling_tokens", "cost_ceiling_seconds"):
        val = meta_block.get(key)
        if val is not None and (not isinstance(val, int) or isinstance(val, bool) or val <= 0):
            errors.append(f"{who}: {key} must be a positive int, got {val!r}")

    # Guardrails (#24 runtime half): every declared name must resolve to a
    # registered runtime guardrail (an output checker or a documented
    # declarative entry in ``routines.skills._runtime.guardrails``) — an
    # unknown/typo'd name is a STARTUP error, never a silent no-op at runtime.
    # Lazy import: the ``_runtime`` package imports back into this module (via
    # ``_runtime.registry``), which is safe at call time but would be a cycle
    # at module-import time.
    guardrails_decl = meta_block.get("guardrails")
    if guardrails_decl is not None:
        if not isinstance(guardrails_decl, list):
            errors.append(
                f"{who}: guardrails must be a list of names, got "
                f"{type(guardrails_decl).__name__}"
            )
        else:
            from routines.skills._runtime.guardrails import validate_guardrail_names

            errors.extend(validate_guardrail_names(guardrails_decl, who))

    # guardrail_max_retries (#24): the per-skill retry UPPER BOUND the runtime
    # min()s with the sensitivity-tier budget. Must be a non-negative int.
    gmr = meta_block.get("guardrail_max_retries")
    if gmr is not None and (not isinstance(gmr, int) or isinstance(gmr, bool) or gmr < 0):
        errors.append(
            f"{who}: guardrail_max_retries must be a non-negative int, got {gmr!r}"
        )

    # Sensitivity ↔ lane consistency (§4). Only when sensitivity is a known
    # confidential/MNPI tier and allowed_tools is a list.
    allowed = post_meta.get("allowed_tools")
    if sensitivity in ("confidential", "MNPI") and isinstance(allowed, list):
        cloud = [t for t in allowed if isinstance(t, str) and _is_cloud_lane(t)]
        if cloud:
            errors.append(
                f"{who}: sensitivity={sensitivity!r} forbids cloud lane(s) "
                f"{cloud} in allowed_tools (§4)"
            )
        if _LOCAL_LANE not in allowed:
            errors.append(
                f"{who}: sensitivity={sensitivity!r} must allow {_LOCAL_LANE!r} "
                f"in allowed_tools (§4)"
            )

    # Capability manifest (#61-capabilities). Absent block → no errors.
    errors.extend(_validate_capabilities(post_meta, who))

    # Conclusion-capture declaration (#76). Absent block → no errors.
    errors.extend(_validate_captures_to_vault(post_meta, who))

    # Tier 2 provider routing + sampling params (#llm-routing-tier-2). Absent
    # keys → no errors.
    errors.extend(_validate_routing(post_meta, who))

    # Readiness preconditions (#74.2). Absent block → no errors.
    errors.extend(_validate_requires(post_meta, who))

    # Per-skill system prompt (#llm-skill-system-prompt). Absent → no errors.
    errors.extend(_validate_llm_system_prompt(post_meta, source, who))

    return errors


def _build(post_meta: dict, source: Path) -> SkillMetadata:
    """Construct a SkillMetadata from validated frontmatter. Assumes
    :func:`_validate_frontmatter` returned no errors for this skill."""
    meta_block = post_meta["metadata"]
    return SkillMetadata(
        name=post_meta["name"],
        description=str(post_meta["description"]),
        version=str(post_meta["version"]),
        license=str(post_meta["license"]),
        allowed_tools=tuple(post_meta["allowed_tools"]),
        sensitivity=meta_block["sensitivity"],
        workspace_scope=meta_block["workspace_scope"],
        tile_label=str(meta_block["tile_label"]),
        cost_ceiling_tokens=int(meta_block["cost_ceiling_tokens"]),
        cost_ceiling_seconds=int(meta_block["cost_ceiling_seconds"]),
        guardrails=tuple(meta_block.get("guardrails") or ()),
        guardrail_max_retries=int(meta_block["guardrail_max_retries"]),
        cost_ceilings=_cost_ceilings(meta_block),
        capabilities=_parse_capabilities(post_meta),
        captures_to_vault=_parse_captures_to_vault(post_meta),
        routing=_parse_routing(post_meta),
        preconditions=_parse_requires(post_meta),
        llm_system_prompt=_resolve_llm_system_prompt(post_meta, source),
        source_path=source,
    )


def _scan_dir(skills_dir: Path) -> tuple[dict[str, SkillMetadata], list[str]]:
    """Parse + validate every SKILL.md under ``skills_dir``.

    Returns ``(valid_registry, errors)``. Pure — no global mutation. Invalid
    skills are omitted from ``valid_registry`` and reported in ``errors``."""
    registry: dict[str, SkillMetadata] = {}
    errors: list[str] = []
    for path in _iter_skill_md(skills_dir):
        try:
            post = frontmatter.load(str(path))
        except Exception as e:  # noqa: BLE001 — malformed YAML is a validation error
            errors.append(f"skill {path.parent.name!r}: SKILL.md frontmatter unreadable: {e}")
            continue
        errs = _validate_frontmatter(post.metadata, path)
        if errs:
            errors.extend(errs)
            continue
        meta = _build(post.metadata, path)
        registry[meta.name] = meta
    return registry, errors


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────


def scan(skills_dir: Path | None = None) -> dict[str, SkillMetadata]:
    """(Re)build the global registry from ``skills_dir`` (default SKILLS_DIR).

    Idempotent. Only VALID skills are registered; invalid ones are skipped
    (use :func:`validate_all` / :func:`validate_or_raise` to surface why).
    Returns a copy of the registry."""
    global _scanned
    registry, _errors = _scan_dir(skills_dir or SKILLS_DIR)
    _REGISTRY.clear()
    _REGISTRY.update(registry)
    _scanned = True
    return dict(_REGISTRY)


def _ensure_scanned() -> None:
    if not _scanned:
        scan()


def load_skill_metadata(name: str) -> SkillMetadata:
    """Return the registered :class:`SkillMetadata` for ``name``.

    Lazily scans the default skills dir on first access. Raises ``KeyError``
    with a helpful message if the skill isn't registered — callers that gate
    on "is this a skill?" (e.g. the central tool guard) catch KeyError as the
    not-a-registered-skill signal."""
    _ensure_scanned()
    try:
        return _REGISTRY[name]
    except KeyError:
        raise KeyError(
            f"skill {name!r} not registered "
            f"(known skills: {sorted(_REGISTRY)})"
        ) from None


def registered_skills() -> dict[str, SkillMetadata]:
    """Return a copy of all registered skills (lazy-scans on first access).

    Unlike :func:`scan` this does NOT re-read disk on every call — it serves
    the cached registry — so endpoints that list every skill (the Tier 2
    providers matrix) stay cheap."""
    _ensure_scanned()
    return dict(_REGISTRY)


def get_active_skill_cap(name: str, key: str) -> Optional[int]:
    """Active per-skill cap for ``(name, key)``, or ``None``.

    Replaces the #67 ``_runtime`` stub. Reads ``cost_ceiling_<key>`` from the
    skill's frontmatter (e.g. ``key="tokens"`` → ``cost_ceiling_tokens``).
    Returns ``None`` for unregistered skills or undeclared keys — so a skill
    that declares no ``cost_ceiling_llm_calls`` keeps #67's counter-only
    (no-op-gating) behaviour."""
    try:
        meta = load_skill_metadata(name)
    except KeyError:
        return None
    val = meta.cost_ceilings.get(key)
    return val if isinstance(val, int) and val > 0 else None


def validate_all(skills_dir: Path | None = None) -> list[str]:
    """Return §14 validation errors across all SKILL.md (empty = all valid).

    PURE — does not mutate the global registry, so it's safe to point at an
    arbitrary ``skills_dir`` (synthetic-skill tests)."""
    _registry, errors = _scan_dir(skills_dir or SKILLS_DIR)
    return errors


def validate_or_raise(skills_dir: Path | None = None) -> None:
    """Raise ``RuntimeError`` if any skill is misconfigured. Called at bridge
    startup so a confidential skill listing a cloud lane refuses to BOOT
    rather than failing on first call (#61 fail-fast contract)."""
    errors = validate_all(skills_dir)
    if errors:
        raise RuntimeError(
            "Skill registry validation failed at startup:\n  - "
            + "\n  - ".join(errors)
        )


# ─────────────────────────────────────────────────────────────────────────────
# Tier 2 — operator sidecar + provider resolution (#llm-routing-tier-2)
# ─────────────────────────────────────────────────────────────────────────────
#
# The sidecar lives in the vault at ``_claude/provider_overrides.yaml``. It is
# vault-owned (the operator commits it; CLAUDE.md §5.7) but bridge-WRITABLE: the
# ``PATCH /api/skills/<key>/provider`` endpoint mutates it so dashboard clicks
# never touch the version-controlled SKILL.md files. Shape:
#
#     <skill_key>:
#       preferred_provider: anthropic | openai | ollama-only | prefer_local
#       preferred_model: opus | sonnet | haiku | opus-1m
#       llm_params: { temperature: 0.2, max_tokens: 2000 }
#
# Precedence (highest first): sidecar > SKILL.md frontmatter > env var > default.

PROVIDER_OVERRIDES_ENV = "AGENTIC_PROVIDER_OVERRIDES"   # full-path redirect (tests/operator)
AGENTIC_VAULT_ENV = "AGENTIC_VAULT"
_SIDECAR_RELATIVE = ("_claude", "provider_overrides.yaml")

_overrides_lock = threading.Lock()
# str(path) → (mtime_ns, parsed) so a hot dispatch path doesn't re-parse every
# call, but a PATCH / operator hot-edit is picked up without a bridge restart.
_overrides_cache: dict[str, tuple[int, dict[str, dict]]] = {}


def _default_vault_root() -> Path:
    """Mirror ``routines.api.deps._default_vault`` WITHOUT importing the api
    layer (``routines.skills`` must not depend upward on ``routines.api``)."""
    env = os.environ.get(AGENTIC_VAULT_ENV)
    if env:
        return Path(env)
    if platform.system() == "Windows":
        return Path("<vault>")
    return Path("/mnt/x/OS AI Vault")


def sidecar_path() -> Path:
    """Resolve the provider-overrides sidecar path.

    ``AGENTIC_PROVIDER_OVERRIDES`` (a full path) wins — tests + operators
    redirect here; otherwise ``<vault>/_claude/provider_overrides.yaml``."""
    explicit = os.environ.get(PROVIDER_OVERRIDES_ENV)
    if explicit:
        return Path(explicit)
    return _default_vault_root().joinpath(*_SIDECAR_RELATIVE)


def _read_sidecar(path: Path) -> dict[str, dict]:
    """Parse the sidecar YAML → ``{skill_key: entry_dict}``. Tolerant: a
    missing/empty/malformed file or non-mapping entries are dropped with a
    warning (a bad sidecar must NEVER crash the dispatcher)."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as e:
        logger.warning("provider_overrides sidecar unreadable at %s: %s", path, e)
        return {}
    if not isinstance(raw, dict):
        logger.warning(
            "provider_overrides sidecar at %s is not a mapping (got %s); ignoring",
            path, type(raw).__name__,
        )
        return {}
    out: dict[str, dict] = {}
    for skill_key, entry in raw.items():
        if isinstance(entry, dict):
            out[str(skill_key)] = entry
        else:
            logger.warning(
                "provider_overrides sidecar entry %r is not a mapping; ignoring",
                skill_key,
            )
    return out


def load_skill_overrides(*, force: bool = False) -> dict[str, dict]:
    """Return the parsed operator sidecar, keyed by skill name.

    Missing file → ``{}`` (the common case — the operator hasn't created one).
    Cached on (path, mtime_ns); ``force=True`` bypasses the cache. The
    dispatcher calls this on every cloud dispatch — the mtime check keeps that
    cheap while still picking up a PATCH / hot-edit."""
    path = sidecar_path()
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


def _sidecar_str(entry: Optional[dict], key: str) -> Optional[str]:
    if not isinstance(entry, dict):
        return None
    v = entry.get(key)
    return str(v) if isinstance(v, str) and v.strip() else None


@dataclass(frozen=True)
class ProviderResolution:
    """The dispatcher's resolved Tier 2 decision for one skill.

      * ``provider`` — ``anthropic`` | ``openai`` | ``ollama-only`` |
        ``prefer_local``. The cloud dispatcher routes anthropic→claude,
        openai→codex, and treats ollama-only as "refuse the cloud call" (a
        local-only skill must never cloud-route). ``prefer_local`` is the
        token-saving downgrade: ``_decision_for_tier2`` rewrites a public/
        internal cloud decision to the local Ollama lane upstream of dispatch
        (#llm-routing-postjune15 P2), so it never reaches the cloud dispatcher.
      * ``llm_params`` — effective sampling params (sidecar overlaid on
        frontmatter), trimmed to set keys so it splats into ``chat(**params)``.
      * ``model`` — operator-selected CLOUD model alias (``preferred_model``,
        sidecar over frontmatter; #llm-routing-postjune15 P2 Task 3) or ``None``
        for the lane default. ``_decision_for_tier2`` threads it onto the Claude
        lane's ``decision.model``; a ``-1m`` variant also sizes the context
        window to 1M.
      * ``source`` — which layer supplied the provider: ``sidecar`` |
        ``frontmatter`` | ``env`` | ``task-class`` | ``default`` (for the
        dashboard + audit).
      * ``fallback_provider`` / ``fallback_llm_params`` — the skill's declared
        fallback (frontmatter), consulted by the dispatcher when the primary
        provider is unavailable or its call fails. ``None`` / ``{}`` when none
        declared.
      * ``error`` — set when resolution is a hard misconfiguration the dispatcher
        should surface as an ``ERROR ·`` route (composes with the round-4 status
        flip). Values: ``"TIER2 SKILL NOT FOUND"`` (sidecar references a skill the
        registry doesn't know), ``"TIER2 PROVIDER INVALID"`` (resolved provider
        is not a known key — e.g. a hand-edited sidecar typo), ``"TIER2 PROVIDER
        NOT ALLOWED"`` (resolved provider is outside the skill's
        ``allowed_providers``). Fail loud rather than silently route somewhere."""

    provider: str
    llm_params: dict[str, Any]
    source: str
    error: Optional[str] = None
    fallback_provider: Optional[str] = None
    fallback_llm_params: dict[str, Any] = field(default_factory=dict)
    # #llm-routing-postjune15 P2 Task 3 — operator-pinned cloud model alias, or
    # None for the lane default. Added last so positional construction is unaffected.
    model: Optional[str] = None


def resolve_skill_provider(
    skill_name: str,
    *,
    env_provider: Optional[str] = None,
    default: str = DEFAULT_CLOUD_PROVIDER,
    task_type: "TaskType | None" = None,
) -> ProviderResolution:
    """Resolve the cloud provider + sampling params for ``skill_name``.

    Precedence (highest first): operator sidecar > SKILL.md frontmatter >
    ``env_provider`` (the AGENTIC_CLOUD_PROVIDER value) > the per-task-class
    default (``task_type`` → provider, #llm-routing-postjune15 P2 §B) >
    ``default``.

    Unregistered skill with NO sidecar entry → graceful fall-through to
    env/task-class/default with empty params (this is the chat path —
    ``chat.session`` is not a registered skill). Unregistered skill WITH a
    sidecar entry → ``error="TIER2 SKILL NOT FOUND"`` (the sidecar points at a
    phantom skill)."""
    overrides = load_skill_overrides()
    sidecar = overrides.get(skill_name) if isinstance(overrides, dict) else None

    try:
        meta: Optional[SkillMetadata] = load_skill_metadata(skill_name)
    except KeyError:
        meta = None

    if meta is None and sidecar is not None:
        return ProviderResolution(
            provider=str(env_provider or default or DEFAULT_CLOUD_PROVIDER).lower(),
            llm_params={},
            source="env" if env_provider else "default",
            error="TIER2 SKILL NOT FOUND",
        )

    fm = meta.routing if meta is not None else SkillRouting()

    sidecar_pref = _sidecar_str(sidecar, "preferred_provider")
    if sidecar_pref:
        provider, source = sidecar_pref, "sidecar"
    elif fm.preferred_provider:
        provider, source = fm.preferred_provider, "frontmatter"
    elif env_provider:
        provider, source = env_provider, "env"
    else:
        # #llm-routing-postjune15 P2 §B: below an explicit pref (no sidecar /
        # frontmatter / env), bias the PROVIDER by task class when the class has
        # a specific bias (e.g. cross-check → openai). A task class with NO bias
        # (or no task_type) falls through to the caller's ``default`` — its
        # contract is honoured. Subordinate to an explicit allow-list: a biased
        # provider outside allowed_providers also falls back to ``default`` so
        # this layer never newly breaks a restricted skill. ``pick_lane`` owns
        # the lane; this aligns the Tier-2 provider with it (without it, the
        # blind anthropic default contradicted pick_lane's cross-check→codex).
        tc_provider = task_class_provider_override(task_type)
        if (
            tc_provider is not None
            and tc_provider != default
            and tc_provider in fm.allowed_providers
        ):
            provider, source = tc_provider, "task-class"
        else:
            provider, source = default, "default"
    provider = str(provider).lower()

    # preferred_model (#llm-routing-postjune15 P2 Task 3): operator-pinned cloud
    # model alias — sidecar over frontmatter, else None (the lane default).
    model = _sidecar_str(sidecar, "preferred_model") or fm.preferred_model or None

    # llm_params: frontmatter base, sidecar overlays per-key.
    params = dict(fm.llm_params.as_dict())
    sidecar_params = sidecar.get("llm_params") if isinstance(sidecar, dict) else None
    if isinstance(sidecar_params, dict):
        for k in _LLM_PARAM_KEYS:
            if k in sidecar_params and sidecar_params[k] is not None:
                v = sidecar_params[k]
                # Codex round-2 #3 — a HAND-EDITED sidecar bypasses the PATCH
                # validator, so re-validate each param at resolution and DROP an
                # invalid one (with a warning) so e.g. `temperature: 9` never
                # reaches chat(). The frontmatter value (already boot-validated)
                # stands in its place.
                if _validate_llm_params({k: v}, "sidecar", "llm_params"):
                    logger.warning(
                        "provider_overrides sidecar for %r has invalid %s=%r; "
                        "ignoring (keeping the frontmatter value)", skill_name, k, v,
                    )
                    continue
                params[k] = v

    # Per-skill fallback (frontmatter only — the sidecar carries primary
    # preference + params, not fallback). Consulted by the dispatcher when the
    # primary provider is unavailable / its call fails (#llm-routing-tier-2 #4).
    fb_provider = fm.fallback_provider
    fb_params = dict(fm.fallback_llm_params.as_dict())

    def _result(error: Optional[str]) -> ProviderResolution:
        return ProviderResolution(
            provider=provider, llm_params=params, source=source, error=error,
            fallback_provider=fb_provider, fallback_llm_params=fb_params,
            model=model,
        )

    # #5: a resolved provider that isn't a known key (e.g. a hand-edited sidecar
    # typo like 'gemini') must NOT silently fall through to Anthropic — fail loud.
    if provider not in _PREFERRED_PROVIDER_VALUES:
        return _result("TIER2 PROVIDER INVALID")

    # #llm-routing-postjune15 P2 Task 3 — a resolved preferred_model that isn't a
    # known alias (e.g. a hand-edited sidecar typo) must fail loud, not silently
    # pass an unknown model to chat(). (Boot validates the frontmatter; this
    # covers the sidecar, which bypasses the boot validator.)
    if model is not None and model not in _PREFERRED_MODEL_VALUES:
        return _result("TIER2 MODEL INVALID")

    # Codex round-2 #2 — a registered confidential/MNPI skill must NEVER resolve
    # to a CLOUD provider via IMPLICIT env/default selection. The boot/PATCH
    # validators only reject EXPLICIT cloud naming (preferred/allowed); without
    # this, a confidential skill with no Tier 2 keys inherits the env var /
    # 'anthropic' default and the cloud dispatcher would call it (R1's "ANY
    # path"). Force the local sentinel so the matrix reports the TRUTH (local-
    # only) instead of a misleading 'anthropic', and the dispatcher fails closed
    # via the ollama-only refusal. (A valid confidential skill can only declare
    # ollama-only or nothing — cloud preferred/allowed already hard-fail boot.)
    if (
        meta is not None
        and meta.sensitivity in ("confidential", "MNPI")
        and provider in _CLOUD_PROVIDERS
    ):
        provider, source = "ollama-only", "confidential-policy"

    # #3: enforce the skill's allow-list. allowed_providers defaults to all, so
    # only an explicitly-restricted skill can trip this — e.g. allowed:[anthropic]
    # but env/sidecar selected openai. (The local sentinels ollama-only /
    # prefer_local map to the ollama allow key.)
    if meta is not None:
        prov_for_allowed = (
            "ollama" if provider in _LOCAL_PREFERRED_SENTINELS else provider
        )
        if prov_for_allowed not in fm.allowed_providers:
            return _result("TIER2 PROVIDER NOT ALLOWED")

    return _result(None)


class ProviderOverrideRefused(ValueError):
    """A sidecar write request was invalid (unknown provider, out-of-range
    temperature, cloud-on-confidential). The PATCH endpoint maps this to 422."""


def _write_sidecar_atomic(path: Path, data: dict) -> None:
    """Write ``data`` as YAML via tmp + ``os.replace`` so a concurrent reader
    never sees a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=str(path.parent), prefix=".provider_overrides_", suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(
                "# provider_overrides.yaml — Tier 2 operator overrides "
                "(#llm-routing-tier-2).\n"
                "# Written by PATCH /api/skills/<key>/provider; operator commits "
                "per CLAUDE.md §5.7.\n"
                "# Precedence: this sidecar > SKILL.md frontmatter > "
                "AGENTIC_CLOUD_PROVIDER > default.\n"
            )
            yaml.safe_dump(data, f, sort_keys=True, default_flow_style=False)
        os.replace(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def save_skill_override(
    skill_name: str,
    *,
    preferred_provider: Optional[str] = None,
    preferred_model: Optional[str] = None,
    llm_params: Optional[dict[str, Any]] = None,
    clear: bool = False,
) -> Optional[dict]:
    """Write (or clear) one skill's entry in the operator sidecar.

    Validates the values with the SAME boot validators (unknown provider /
    unknown model / temperature range) PLUS the confidential-⇒-no-cloud rule
    against the skill's REGISTERED sensitivity — a sidecar must not be able to
    do what the frontmatter validator forbids. Atomic write.

    PATCH semantics — provided fields MERGE over any existing entry (a
    temperature-only update keeps a previously-set ``preferred_provider``). The
    MERGED entry is validated, so a partial update can't compose into an
    invalid override.

    Returns the entry written (``None`` when cleared). Raises
    ``ProviderOverrideRefused`` on invalid input; ``KeyError`` when the skill
    is not registered (route → 404)."""
    meta = load_skill_metadata(skill_name)   # KeyError → route 404

    path = sidecar_path()
    with _overrides_lock:
        current = _read_sidecar(path) if path.exists() else {}

        if clear:
            current.pop(skill_name, None)
            _write_sidecar_atomic(path, current)
            _overrides_cache.pop(str(path), None)
            return None

        # Merge provided fields over the existing entry.
        entry: dict[str, Any] = dict(current.get(skill_name) or {})
        if preferred_provider is not None:
            entry["preferred_provider"] = preferred_provider
        if preferred_model is not None:
            entry["preferred_model"] = preferred_model
        if llm_params is not None:
            entry["llm_params"] = {k: v for k, v in llm_params.items() if k in _LLM_PARAM_KEYS}

        # Validate the MERGED entry with the boot validators + the registered
        # sensitivity (a sidecar can't do what the frontmatter validator forbids
        # — chiefly cloud-on-confidential). NB: do NOT inject allowed_providers
        # into the candidate — for a skill that didn't declare one it defaults to
        # all-three, which would false-trigger the confidential-cloud-allowed
        # rule on an innocent temperature-only PATCH. The preferred-in-allowed
        # check is done explicitly below against the skill's REGISTERED list.
        who = f"sidecar override for {skill_name!r}"
        candidate: dict[str, Any] = {"metadata": {"sensitivity": meta.sensitivity}}
        if "preferred_provider" in entry:
            candidate["preferred_provider"] = entry["preferred_provider"]
        if "preferred_model" in entry:
            candidate["preferred_model"] = entry["preferred_model"]
        if "llm_params" in entry:
            candidate["llm_params"] = entry["llm_params"]
        errors = _validate_routing(candidate, who=who)
        # A sidecar preferred must also sit inside the skill's frontmatter
        # allow-list (a sidecar can't widen what the SKILL.md declared).
        pref = entry.get("preferred_provider")
        if pref in _PREFERRED_PROVIDER_VALUES:
            prov_key = "ollama" if pref in _LOCAL_PREFERRED_SENTINELS else pref
            if prov_key not in meta.routing.allowed_providers:
                errors.append(
                    f"{who}: preferred_provider {pref!r} not in the skill's "
                    f"allowed_providers {list(meta.routing.allowed_providers)}"
                )
        if errors:
            raise ProviderOverrideRefused("; ".join(errors))

        current[skill_name] = entry
        _write_sidecar_atomic(path, current)
        _overrides_cache.pop(str(path), None)
        return entry


__all__ = [
    "SkillMetadata",
    "SkillCapabilities",
    "SkillPreconditions",
    "CapturesToVault",
    "LLMParams",
    "SkillRouting",
    "ProviderResolution",
    "ProviderOverrideRefused",
    "DEFAULT_ALLOWED_PROVIDERS",
    "DEFAULT_CLOUD_PROVIDER",
    "SKILLS_DIR",
    "scan",
    "load_skill_metadata",
    "registered_skills",
    "get_active_skill_cap",
    "validate_all",
    "validate_or_raise",
    "sidecar_path",
    "load_skill_overrides",
    "resolve_skill_provider",
    "save_skill_override",
]
