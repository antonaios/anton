"""Budget gating — pre-call invocation block + incident-requires-ack.

#57 transforms ``/api/telemetry/llm-burn`` from a DISPLAY surface into a
GATE surface. Every LLM call passes through ``get_invocation_block(scope)``
before it fires; if any applicable scope (global → provider+model →
workspace) is at or beyond ``hard_pct`` of cap, the call is refused and
an Incident row is written. Operator must explicitly ack (raise cap OR
leave paused) before the scope unblocks.

Public API:
  * ``BudgetPolicy`` / ``ScopeRef`` / ``InvocationBlock`` — data models.
  * ``get_invocation_block(scope)`` — pre-call gate primitive.
  * ``record_overrun`` / ``acknowledge`` / ``list_open_incidents`` —
    incident lifecycle.
  * ``enforce_budget_gate`` — the ``@before_llm_call`` hook handler.
  * Storage helpers (``upsert_policy`` / ``list_policies`` / ...).

See ``OUTSTANDING.md`` #57 + ``audits/EVAL-CROSS-REF-AUDIT-2026-05-27.md``
§"#57" + ``evaluations/PAPERCLIP-PATTERNS.md`` §"#13 + #14" for the spec.
"""

from __future__ import annotations

from routines.budgets.gate import (
    InvocationBlocked,
    enforce_budget_gate,
    get_invocation_block,
    get_invocation_warn,
)
from routines.budgets.incidents import (
    Incident,
    acknowledge,
    force_clear,
    list_open_incidents,
    record_overrun,
)
from routines.budgets.policy import (
    BudgetPolicy,
    BudgetWarn,
    InvocationBlock,
    ScopeRef,
    scope_id,
)
from routines.budgets.storage import (
    BUDGETS_DB_PATH,
    delete_policy,
    get_policy,
    list_policies,
    upsert_policy,
)

__all__ = [
    # policy
    "BudgetPolicy",
    "ScopeRef",
    "InvocationBlock",
    "BudgetWarn",
    "scope_id",
    # gate
    "get_invocation_block",
    "get_invocation_warn",
    "enforce_budget_gate",
    "InvocationBlocked",
    # incidents
    "Incident",
    "record_overrun",
    "acknowledge",
    "force_clear",
    "list_open_incidents",
    # storage
    "BUDGETS_DB_PATH",
    "get_policy",
    "list_policies",
    "upsert_policy",
    "delete_policy",
]
