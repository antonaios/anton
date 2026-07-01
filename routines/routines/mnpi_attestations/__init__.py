"""#llm-routing-postjune15 P5 — per-provider MNPI cloud-attestations.

The STANDING (auto-on) enterprise gate for the conditional MNPI→cloud path. An
attestation records that a cloud provider carries the contractual data
protections (DPA + ZDR + no-training); under ``AGENTIC_PLAN_TIER=enterprise`` an
active attestation lets **explicitly-assigned** MNPI route to that provider's
cloud lane — distinct from the confidential-only ``sensitivity_overrides``
window, and NEVER reachable by an override.

Default-off: an empty store reproduces the absolute pre-P5 MNPI-local floor
([no-mnpi-to-cloud](<vault>/CLAUDE.md#no-mnpi-to-cloud), §5.2).

Public API:

  grant_attestation(provider, dpa, zdr, no_training, granted_by, duration_seconds, *, now)
    → Attestation
  find_active_attestation(provider, *, now) → Attestation | None
  list_active_attestations(*, now) → list[Attestation]
  revoke_attestation(attestation_id, reason='operator', *, now) → Attestation
  mnpi_cloud_allowed(provider, *, now) → bool
  mnpi_cloud_lane_if_attested(task_type, *, now) → (lane, Attestation) | None
"""

from .policy import (
    Attestation,
    AttestationRefused,
    DEFAULT_DURATION_SECONDS,
    MAX_DURATION_SECONDS,
    MIN_DURATION_SECONDS,
    normalize_provider,
)
from .storage import (
    AttestationNotFound,
    find_active_attestation,
    grant_attestation,
    list_active_attestations,
    revoke_attestation,
)
from .gate import mnpi_cloud_allowed, mnpi_cloud_lane_if_attested

__all__ = [
    "Attestation",
    "AttestationRefused",
    "AttestationNotFound",
    "DEFAULT_DURATION_SECONDS",
    "MAX_DURATION_SECONDS",
    "MIN_DURATION_SECONDS",
    "normalize_provider",
    "grant_attestation",
    "find_active_attestation",
    "list_active_attestations",
    "revoke_attestation",
    "mnpi_cloud_allowed",
    "mnpi_cloud_lane_if_attested",
]
