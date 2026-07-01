"""Bootstrap seed for the Agent-SDK monthly credit (#llm-routing-postjune15 B3).

The headless Claude lane (``claude -p``) consumes the operator's MAX-tier
monthly Agent-SDK plan credit post-2026-06-15 (Pro $20 / Max 5x $100 /
Max 20x $200). We model that credit as a #57 provider-scope ``BudgetPolicy``
with ``cap_usd`` = the monthly dollar figure, so the EXISTING hard-gate +
overrun-incident + ack machinery enforces credit exhaustion (block → operator
ack). The all-models provider scope (``b="*"``) is checked on every cloud call
by ``gate._applicable_scopes`` (B3), and spend aggregation normalizes the
provider so claude-subprocess / claude-api / anthropic spend all count toward it.

The dollar figure is operator/tier-specific, so it is NOT hardcoded: set the
``AGENTIC_AGENT_SDK_CREDIT_USD`` env var to your tier's monthly credit and the
policy is seeded at bridge boot. Idempotent + non-clobbering: the seed creates
the policy ONLY when one does not already exist for the scope, so an operator's
later dashboard edit (raise / lower the cap, or an incident ack → raise_cap) is
never overwritten on the next restart. Unset / invalid env var → no-op (the
operator can still create the policy via POST /api/budgets or the dashboard).
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Optional

from routines.budgets.policy import BudgetPolicy, ScopeRef
from routines.budgets.storage import get_policy, upsert_policy

log = logging.getLogger(__name__)

# Operator sets this to their tier's monthly Agent-SDK credit in USD.
AGENT_SDK_CREDIT_USD_ENV = "AGENTIC_AGENT_SDK_CREDIT_USD"

# The credit caps ALL Claude (Anthropic) spend regardless of model. The gate's
# all-models provider scope is keyed on the NORMALIZED provider (#B3 + review
# SEV-2), so this scope uses "anthropic": gate._applicable_scopes emits
# provider:anthropic:* for the cloud Claude lane (normalize("claude")), and the
# dashboard + spend aggregation use the same canonical key. ``b="*"`` is the
# all-models wildcard; claude-subprocess / claude-api / anthropic rows all
# aggregate in.
AGENT_SDK_CREDIT_SCOPE = ScopeRef(kind="provider", a="anthropic", b="*")


def _read_credit_usd() -> Optional[float]:
    raw = os.environ.get(AGENT_SDK_CREDIT_USD_ENV)
    if raw is None or not raw.strip():
        return None
    try:
        val = float(raw.strip())
    except ValueError:
        log.warning(
            "%s=%r is not a number — skipping Agent-SDK credit seed",
            AGENT_SDK_CREDIT_USD_ENV, raw,
        )
        return None
    if val <= 0:
        log.warning(
            "%s=%s is not positive — skipping Agent-SDK credit seed",
            AGENT_SDK_CREDIT_USD_ENV, val,
        )
        return None
    return val


def seed_agent_sdk_credit() -> Optional[BudgetPolicy]:
    """Seed the Agent-SDK monthly-credit provider-scope policy from the
    ``AGENTIC_AGENT_SDK_CREDIT_USD`` env var, IFF it is set and no policy exists
    yet for the scope.

    Returns the seeded policy, or ``None`` when the env var is unset / invalid
    OR a policy already exists for the scope (non-clobbering — operator edits
    win). Best-effort: callers (the bridge lifespan) tolerate any storage error
    without failing boot."""
    cap_usd = _read_credit_usd()
    if cap_usd is None:
        return None
    existing = get_policy(AGENT_SDK_CREDIT_SCOPE)
    if existing is not None:
        log.info(
            "Agent-SDK credit policy already exists (cap_usd=%s) — not "
            "reseeding (operator edits win)", existing.cap_usd,
        )
        return None
    now = datetime.now(timezone.utc)
    seeded = upsert_policy(BudgetPolicy(
        scope=AGENT_SDK_CREDIT_SCOPE,
        cap_usd=cap_usd,
        created=now,
        last_modified=now,
    ))
    log.info(
        "seeded Agent-SDK monthly-credit policy: scope=%s cap_usd=%s",
        AGENT_SDK_CREDIT_SCOPE.id(), cap_usd,
    )
    return seeded


__all__ = [
    "AGENT_SDK_CREDIT_USD_ENV",
    "AGENT_SDK_CREDIT_SCOPE",
    "seed_agent_sdk_credit",
]
