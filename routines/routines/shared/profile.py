"""Read operator config from `_claude/profile.md`.

Used by routines that need operator context — e.g. crossrefs.stub_people
filters the operator's own name out of the People-stub list (the operator
shouldn't get auto-stubbed in their own People folder when they appear in
their own meetings).

profile.md is the single source of truth for operator identity. If a
routine wants to know "who is operating this Agentic OS", it reads here.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import frontmatter

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OperatorProfile:
    """Subset of profile.md frontmatter useful to routines."""

    operator: str = ""
    operator_slug: str = ""
    qualifications: list[str] = field(default_factory=list)
    active_sectors: list[str] = field(default_factory=list)
    career_sectors: list[str] = field(default_factory=list)
    working_language: str = "en-GB"
    plan_tier: str = "bridge"
    # External (non-vault) workspace roots — added 2026-05-23 per workspace-write-policy.
    # Each is a directory whose immediate subdirectories are individual workspaces.
    external_project_paths: list[str] = field(default_factory=list)
    external_bd_path: str = ""
    external_general_path: str = ""
    sessions_path: str = ""
    # Per-provider sensitivity ceilings (#llm-routing-postjune15 P2 Task 5 /
    # folds #llm-routing-per-provider-tiers): {provider: max_sensitivity}, parsed
    # from the profile.md ``providers:`` block. An absent provider → no entry →
    # the caller applies NO per-provider restriction (the §4 matrix + override
    # window remain the gates); the ceiling is purely additive (opt-in per
    # provider). It is NOT a "default internal" — that would regress the
    # enterprise confidential→claude path.
    provider_ceilings: dict[str, str] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    def operator_name_variants(self) -> set[str]:
        """Returns the set of name variants that should match the operator
        in transcripts and cross-references.

        Used by routines that want to exclude the operator from being
        auto-stubbed in their own People folder.

        Variants:
            - the full operator name as-is
            - the first-name token
            - common diminutives are NOT included (too risky — could
              match a different person with the same nickname). Operator
              can edit the watcher manually if their nickname appears.
        """
        if not self.operator:
            return set()
        name = self.operator.strip()
        variants = {name}
        # First-name token
        first = name.split()[0] if name.split() else ""
        if first:
            variants.add(first)
        return variants


# ── profile.md mtime-cache (#eff-hotpath-batch) ──────────────────────────────
# BEFORE: ``load()`` read + frontmatter-parsed profile.md on EVERY call, across
# 16 call sites (morning_brief, daily_digest, hinotes, operatorconfig,
# sectornews, …). AFTER: the parsed OperatorProfile is cached per resolved
# profile path, keyed on the file's (mtime_ns, size). The operator tab edits
# profile.md via PUT, which changes the mtime → the next load reparses, so a
# stale cache can NEVER mask a config change. The "file absent" outcome is
# cached on a sentinel too. Process-local; the lock keeps the cache dict
# consistent if two routines load concurrently.
_profile_cache: dict[str, tuple[tuple[int, int] | None, "OperatorProfile"]] = {}
_profile_cache_lock = threading.Lock()


def _profile_stat_key(path: Path) -> tuple[int, int] | None:
    """Return (mtime_ns, size) for ``path``, or ``None`` if absent."""
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def load(vault_root: Path) -> OperatorProfile:
    """Read `<vault_root>/_claude/profile.md` and return an OperatorProfile.

    If the file is missing or unparseable, returns an empty profile (silent
    degradation — routines should still run, just without operator context).

    mtime-cached: the parsed profile is reused until the file changes
    (operator PUT bumps the mtime → next call reparses). A stale cache can
    never mask a config change.
    """
    profile_path = vault_root / "_claude" / "profile.md"
    key = str(profile_path)
    stat_key = _profile_stat_key(profile_path)

    cached = _profile_cache.get(key)
    if cached is not None and cached[0] == stat_key:
        return cached[1]

    profile = _parse_profile(profile_path, stat_key)
    with _profile_cache_lock:
        _profile_cache[key] = (stat_key, profile)
    return profile


def _parse_profile(
    profile_path: Path, stat_key: tuple[int, int] | None,
) -> OperatorProfile:
    """Read + frontmatter-parse profile.md. Empty profile on absent/unparseable."""
    if stat_key is None:
        logger.warning("profile.md not found at %s — using empty operator profile", profile_path)
        return OperatorProfile()

    try:
        post = frontmatter.loads(profile_path.read_text(encoding="utf-8"))
        meta = dict(post.metadata)
    except Exception as e:  # noqa: BLE001
        logger.warning("could not parse %s: %s — using empty operator profile", profile_path, e)
        return OperatorProfile()

    return OperatorProfile(
        operator=str(meta.get("operator", "")).strip(),
        operator_slug=str(meta.get("operator_slug", "")).strip(),
        qualifications=_safe_list(meta.get("qualifications")),
        active_sectors=_safe_list(meta.get("active_sectors")),
        career_sectors=_safe_list(meta.get("career_sectors")),
        working_language=str(meta.get("working_language", "en-GB")).strip(),
        plan_tier=str(meta.get("plan_tier", "bridge")).strip(),
        external_project_paths=_safe_list(meta.get("external_project_paths")),
        external_bd_path=str(meta.get("external_bd_path", "")).strip(),
        external_general_path=str(meta.get("external_general_path", "")).strip(),
        sessions_path=str(meta.get("sessions_path", "")).strip(),
        provider_ceilings=_parse_provider_ceilings(meta.get("providers")),
        raw=meta,
    )


def _safe_list(v: Any) -> list[str]:
    if not isinstance(v, list):
        return []
    return [str(x).strip() for x in v if x]


def _parse_provider_ceilings(v: Any) -> dict[str, str]:
    """Parse the profile.md ``providers:`` block into {provider: max_sensitivity}.

    Shape (#llm-routing-postjune15 P2 Task 5)::

        providers:
          anthropic: {max_sensitivity: internal}
          openai:    {max_sensitivity: internal}

    A non-mapping block, a non-mapping entry, or an entry with NO
    ``max_sensitivity`` key is skipped (that provider stays unconfigured → the
    caller applies no restriction). An entry WITH the key but a malformed value
    is recorded (stringified), so the gate fails CLOSED on it rather than
    silently dropping the operator's intended cap."""
    if not isinstance(v, dict):
        return {}
    out: dict[str, str] = {}
    for name, cfg in v.items():
        if not isinstance(cfg, dict) or "max_sensitivity" not in cfg:
            # Entry isn't a mapping, or declares no ceiling key → the provider is
            # UNCONFIGURED (the caller applies no restriction).
            continue
        # The key IS present: record it (stringified) even if malformed, so the
        # gate fails CLOSED on an unrecognised value rather than silently dropping
        # the operator's intended cap. Only a genuinely absent key leaves the
        # provider unconfigured.
        out[str(name).strip()] = str(cfg.get("max_sensitivity")).strip()
    return out


def _resolve_vault_root() -> Path:
    """Resolve the vault root from ``AGENTIC_VAULT`` (mirrors api.deps), with a
    platform default. For callers (e.g. the central sensitivity gate) that don't
    carry a ``VaultPaths`` — kept here so low-level modules needn't import the
    api layer."""
    import os
    import platform

    env = os.environ.get("AGENTIC_VAULT")
    if env:
        return Path(env)
    if platform.system() == "Windows":
        return Path("<vault>")
    return Path("/mnt/x/OS AI Vault")


def provider_max_sensitivity(provider: str) -> "str | None":
    """The configured ``max_sensitivity`` ceiling for a CLOUD ``provider``
    (#llm-routing-postjune15 P2 Task 5 / folds #llm-routing-per-provider-tiers),
    from the profile.md ``providers:`` block — or ``None`` when the provider is
    UNCONFIGURED. The caller then applies NO per-provider restriction (the §4
    sensitivity matrix + the override window remain the gates), so the ceiling is
    purely additive: it only ever bites on a provider the operator EXPLICITLY
    capped (e.g. 'openai sees public only') or raised (e.g. 'anthropic → confidential'
    once ZDR lands). mtime-cached via :func:`load`; tolerant (absent/unparseable
    profile → ``None``)."""
    try:
        prof = load(_resolve_vault_root())
    except Exception as e:  # noqa: BLE001 — never crash the caller on a config read
        logger.warning("provider_max_sensitivity: profile load failed: %s", e)
        return None
    return prof.provider_ceilings.get(provider)
