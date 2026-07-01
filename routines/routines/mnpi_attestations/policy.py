"""Policy + dataclasses for per-provider MNPI cloud-attestation records.

An *attestation* records that a cloud provider carries the contractual data
protections — **DPA + ZDR + no-training, all three** — that, under
``AGENTIC_PLAN_TIER=enterprise``, make it eligible to receive MNPI. This is the
*standing* (auto-on) gate for the conditional enterprise-MNPI path
(#llm-routing-postjune15 P5): once an attestation is active for a provider,
**explicitly-assigned** MNPI may route to that provider's cloud lane with no
per-call override window.

Hard rules (load-bearing — trace against CLAUDE.md §5.2 #no-mnpi-to-cloud):

  * **All three protections required.** A grant missing any of dpa / zdr /
    no_training is refused; ``is_active`` re-checks all three (defence in depth).
  * **Every attestation MUST expire.** There is no "until-closed" form — a
    compliance artefact has a validity period. Fail-closed once expired.
  * **Distinct from ``sensitivity_overrides``.** That module lifts
    *confidential* per-window; MNPI is NEVER liftable by an override window —
    only by an active attestation under the enterprise tier. The two never mix.
  * **The empty store is the default.** No attestation → behaviour is
    byte-identical to the absolute pre-P5 floor (MNPI local-only).
  * **Lift applies to EXPLICIT MNPI only.** The enforcement layers gate the
    lift on operator/workspace-assigned MNPI, never on an unknown sensitivity
    coerced to MNPI (which keeps its non-liftable local floor).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

# Grant-duration bounds. A DPA/ZDR term is annual-ish; force periodic renewal
# rather than a multi-year set-and-forget (compliance hygiene). Operator may
# choose anywhere in [MIN, MAX]; default one year.
DEFAULT_DURATION_SECONDS = 365 * 24 * 3600    # 1 year
MAX_DURATION_SECONDS = 400 * 24 * 3600        # ~13 months ceiling (annual + slack)
MIN_DURATION_SECONDS = 24 * 3600              # 1 day (any shorter = mistake)

# Canonical provider keys. A lane resolves to a raw provider via
# ``shared.routing.lane_to_model`` ("claude" / "codex" / "minimax"); the
# operator-facing API + telemetry use the brand-normalised form
# ("anthropic" / "openai"). We normalise on BOTH the grant side and the
# gate-lookup side (deliberately avoiding the #57/B3 budget-gate asymmetry where
# only one side normalised) so an attestation can never be "granted but never
# found".
_PROVIDER_ALIASES = {
    "claude": "anthropic",
    "anthropic": "anthropic",
    "claude-cli": "anthropic",
    "codex": "openai",
    "openai": "openai",
    "codex-cli": "openai",
    "minimax": "minimax",
}


def normalize_provider(provider: str) -> str:
    """Map any provider/lane spelling to its canonical attestation key. Unknown
    values pass through lower-cased (so a typo can never silently alias onto a
    real, attested provider)."""
    key = (provider or "").strip().lower()
    return _PROVIDER_ALIASES.get(key, key)


# The ONLY providers attestable for MNPI cloud routing: Claude (anthropic) +
# Codex (openai) — the cloud lanes the operator expects to carry an enterprise
# DPA. MiniMax is deliberately EXCLUDED (no-confidential-to-minimax, §5 rule 3 —
# MNPI is stricter still). An unknown / typo'd provider is rejected at grant so a
# misspelled attestation can't sit dormant while the operator believes a real
# provider is attested.
ATTESTABLE_PROVIDERS = frozenset({"anthropic", "openai"})


class AttestationRefused(ValueError):
    """Grant request invalid (a missing protection, bad duration, empty field,
    or a non-attestable provider). Route maps to HTTP 422."""


@dataclass(frozen=True)
class Attestation:
    """A single active or historical per-provider MNPI cloud-attestation.

    Identity:
      * id        — short hex slug
      * provider  — canonical provider key (``normalize_provider``)

    Protections (ALL three required to be active):
      * dpa         — a signed Data Processing Agreement is in force
      * zdr         — zero-data-retention is contractually guaranteed
      * no_training — the payload is contractually excluded from training

    Lifecycle:
      * granted_at  — when the operator recorded the attestation
      * expires_at  — REQUIRED; attestation stops being honoured at/after this
      * revoked_at  — explicit early revoke OR supersede by a new grant on the
                      same provider; None while live

    Audit:
      * granted_by     — operator identity (who recorded it)
      * revoked_reason — 'operator' | 'superseded' | None
    """

    id: str
    provider: str
    dpa: bool
    zdr: bool
    no_training: bool
    granted_by: str
    granted_at: datetime
    expires_at: datetime
    revoked_at: datetime | None = None
    revoked_reason: str | None = None

    def is_active(self, *, now: datetime) -> bool:
        """True iff this attestation currently authorises MNPI→cloud for its
        provider. Fail-closed: requires all three protections present, not
        revoked, and not yet expired."""
        if self.revoked_at is not None:
            return False
        if not (self.dpa and self.zdr and self.no_training):
            return False
        return now < self.expires_at
