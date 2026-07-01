"""BudgetPolicy + ScopeRef + InvocationBlock data models.

Three scope kinds, ordered by specificity (least → most specific):
  * ``global`` — overall monthly cap.
  * ``provider`` — per ``(provider, model)`` pair (e.g. anthropic/opus-4.7).
  * ``workspace`` — per ``(workspace_type, workspace_name)`` ring-fence.

The gate evaluates ALL applicable scopes and returns the FIRST block.
Scope evaluation order is the same as the specificity list above — least
to most specific — so a global block surfaces before a per-provider block
when both would fire (matters for the audit message: operators see the
broadest blocker first, then drill).

Period model is monthly UTC: the spend window resets at exactly
``00:00:00`` UTC on the 1st of each month. Not local timezone. Not
rolling 30d. The reset behaviour is load-bearing for reproducible audit
reports — see ``EVAL-CROSS-REF-AUDIT`` §"#57".
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator


ScopeKind = Literal["global", "provider", "workspace", "workspace_provider"]


class ScopeRef(BaseModel):
    """Identifies a budget scope.

    Construct one of these shapes:
      * ``ScopeRef(kind="global")``
      * ``ScopeRef(kind="provider", a="anthropic", b="claude-opus-4-7")``
        — a ``b="*"`` wildcard means "all models of this provider" (the
        per-LLM token cap the dashboard sets; see gate._record_matches_scope).
      * ``ScopeRef(kind="workspace", a="project", b="DemoTarget")``
      * ``ScopeRef(kind="workspace_provider", a="project:DemoTarget",
        b="claude")`` — per-project-per-LLM token cap. ``a`` is the
        ``<workspace_type>:<workspace_name>`` compound key, ``b`` the provider.

    ``a`` / ``b`` are the two scope keys; their meaning depends on ``kind``.
    The ``a+b required`` validation below covers every non-global kind.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: ScopeKind
    a: Optional[str] = None
    b: Optional[str] = None

    @model_validator(mode="after")
    def _validate_shape(self) -> "ScopeRef":
        if self.kind == "global":
            if self.a is not None or self.b is not None:
                raise ValueError("global scope takes no a/b keys")
        else:
            if not self.a or not self.b:
                raise ValueError(
                    f"scope kind={self.kind!r} requires both a + b keys"
                )
        return self

    def id(self) -> str:
        return scope_id(self)


def scope_id(scope: ScopeRef) -> str:
    """Stable string id for a scope — used as the SQLite primary key."""
    if scope.kind == "global":
        return "global"
    return f"{scope.kind}:{scope.a}:{scope.b}"


# ────────────────────────────────────────────────────────────────────────────
# BudgetPolicy
# ────────────────────────────────────────────────────────────────────────────


class BudgetPolicy(BaseModel):
    """One policy row.

    Persisted to SQLite ``policies`` table keyed by ``scope_id(scope)``. The
    last_modified field is bumped on every upsert so the dashboard can show
    when an operator last touched a cap (and so audit can pin a raise_cap
    ack to a specific edit).
    """

    model_config = ConfigDict(extra="forbid")

    scope: ScopeRef
    period: Literal["monthly_utc"] = "monthly_utc"
    warn_pct: float = Field(80.0, ge=0.0, le=100.0)
    hard_pct: float = Field(100.0, ge=0.0, le=100.0)
    cap_usd: float = Field(..., ge=0.0)
    cap_tokens: Optional[int] = Field(None, ge=0)   # track+warn only; gate ignores it
    created: datetime
    last_modified: datetime

    @model_validator(mode="after")
    def _warn_le_hard(self) -> "BudgetPolicy":
        if self.warn_pct > self.hard_pct:
            raise ValueError(
                f"warn_pct ({self.warn_pct}) must be ≤ hard_pct ({self.hard_pct})"
            )
        return self


# ────────────────────────────────────────────────────────────────────────────
# InvocationBlock
# ────────────────────────────────────────────────────────────────────────────


class Contributor(BaseModel):
    """One sub-scope that drove spend in the blocked scope.

    Surfaced on InvocationBlock so the dashboard banner / chat refusal
    can tell the operator not just THAT a cap tripped, but WHAT inside
    that scope caused it. Example: a global block at 105% might show
    "anthropic/claude-opus-4-7 = $60 (57%), project/DemoTelco = $45 (43%)".

    ``kind`` says which axis this contributor is on:
      * ``provider`` — ``a/b`` are ``(provider, model)``
      * ``workspace`` — ``a/b`` are ``(workspace_type, workspace_name)``
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["provider", "workspace"]
    a: str
    b: str
    spend_usd: float
    pct_of_scope: float  # contributor spend / scope total spend × 100


class InvocationBlock(BaseModel):
    """Returned by ``get_invocation_block`` when a scope is over cap.

    Fields are designed for direct rendering in the dashboard's red
    BLOCKED banner — every field maps to one cell of the banner.

    ``contributors`` carries the top sub-scopes by spend so the banner
    can show what tripped the cap, not just that something did.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    scope: ScopeRef
    reason: str                    # human-readable: "scope at 103% of $40 cap"
    current_pct: float             # spend / cap * 100
    hard_pct: float                # cap threshold (usually 100)
    cap_usd: float
    current_spend_usd: float
    incident_id: Optional[str] = None     # set if/when an incident is opened
    contributors: list[Contributor] = []   # top sub-scopes by spend


class BudgetWarn(BaseModel):
    """Returned by ``get_invocation_warn`` when a scope is in the WARN band.

    The NON-FATAL sibling of ``InvocationBlock``: spend has crossed
    ``warn_pct`` of cap but is still below ``hard_pct``, so the call
    PROCEEDS. The gate stashes a compact form on
    ``ctx.usage['budget_warn']`` and the after-hook stamps the telemetry
    row, so the dashboard can surface an approaching-cap chip without
    refusing the call.

    Deliberately carries NO ``contributors`` / ``incident_id`` (unlike the
    block): a warn can fire on EVERY call once spend crosses the soft
    threshold, so it must stay cheap — no extra telemetry scan, no incident
    side-effect. It is pure information.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    scope: ScopeRef
    reason: str                    # "scope at 85% of $40 cap (warn 80%, hard 100%)"
    current_pct: float             # spend / cap * 100
    warn_pct: float                # soft threshold (usually 80)
    hard_pct: float                # hard/block threshold (usually 100)
    cap_usd: float
    current_spend_usd: float


# ────────────────────────────────────────────────────────────────────────────
# Monthly-UTC window helper
# ────────────────────────────────────────────────────────────────────────────


def monthly_utc_window(now: Optional[datetime] = None) -> tuple[datetime, datetime]:
    """Return ``(month_start, now)`` for the current monthly UTC window.

    Window semantics: ``ts >= month_start AND ts <= now``. The start is
    exactly ``00:00:00`` UTC on the 1st of the current month. Not local
    TZ, not a rolling 30d window — this is the contract per #57 spec and
    is asserted by ``test_monthly_utc_reset``.
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return month_start, now


__all__ = [
    "ScopeKind",
    "ScopeRef",
    "BudgetPolicy",
    "Contributor",
    "InvocationBlock",
    "BudgetWarn",
    "scope_id",
    "monthly_utc_window",
]
