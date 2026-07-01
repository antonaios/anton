"""The two predicates that gate the enterprise-MNPI cloud lift.

Kept in a thin module (not policy/storage) because they bridge the attestation
store with ``shared.routing`` (the plan tier + the enterprise lane mapping).
Both are fail-closed and import ``shared.routing`` lazily so the package has no
import-time dependency on routing (and routing never imports this package).
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional, Tuple

from .policy import Attestation
from .storage import find_active_attestation


def mnpi_cloud_allowed(provider: str, *, now: Optional[datetime] = None) -> bool:
    """True iff explicitly-assigned MNPI may route to ``provider``'s cloud lane:
    the plan tier is ``enterprise`` AND ``provider`` holds an active attestation
    (DPA + ZDR + no-training, unexpired, un-revoked).

    Fail-closed: bridge/consumer tier, no attestation, or any lookup trouble →
    ``False``. ``provider`` may be any spelling (lane-raw ``claude`` / ``codex``
    or brand ``anthropic`` / ``openai``) — ``find_active_attestation`` normalises
    it, so the gate and the grant key on the same canonical provider.
    """
    try:
        from ..shared.routing import plan_tier

        if plan_tier() != "enterprise":
            return False
        return find_active_attestation(provider=provider, now=now) is not None
    except Exception:  # noqa: BLE001 — fail-closed: any error denies cloud MNPI
        return False


def mnpi_cloud_lane_if_attested(
    task_type: str, *, now: Optional[datetime] = None,
) -> Optional[Tuple[str, Attestation]]:
    """The ``(lane, attestation)`` an EXPLICIT-MNPI call lifts to under the
    enterprise tier + an active per-provider attestation, or ``None`` to stay
    local.

    The lane mirrors the confidential enterprise mapping
    (``shared.routing.enterprise_cloud_lane``); the attestation is returned so the
    caller can stamp its id on the audit trail. Fail-closed (``None``) on bridge
    tier, a forced-local task type (transcript/embed/multimodal extraction), or no
    active attestation for the lane's resolved provider.

    NOTE: provenance (explicit vs unknown-coerced MNPI) is gated by the CALLER
    (``decide_route`` only invokes this for an explicit MNPI assignment); this
    helper assumes the MNPI is already known to be explicit.
    """
    try:
        from ..shared import routing as shared_routing

        if shared_routing.plan_tier() != "enterprise":
            return None
        candidate = shared_routing.enterprise_cloud_lane(task_type)  # type: ignore[arg-type]
        if candidate is None:
            return None  # task type forces local even for confidential/MNPI
        provider = shared_routing.lane_to_model(candidate)[0]
        att = find_active_attestation(provider=provider, now=now)
        if att is None:
            return None
        return candidate, att
    except Exception:  # noqa: BLE001 — fail-closed: any error stays local
        return None
