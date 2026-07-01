"""Event types for the bridge event bus.

Lifecycle triplets — every event family ships ``Started`` / ``Completed`` /
``Failed`` with a shared base class so subscribers can match on the base
type when they don't care about the phase.

Borrowed in shape from CrewAI's ``events/types/*`` (see [[CREWAI-EVALUATION]]
§2.3) — pared down to the families ANTON needs today:
  * SkillInvocation* — used by [[central_guards]]'s audit handler
  * LLMCall*         — emitted by the LLM dispatcher
  * ToolCall*        — emitted by the tool runner

Each event carries ``event_id`` + ``created`` + ``source`` so the dashboard
SSE listener (#13 telemetry) can render an ordered, deduped stream.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal


def _new_event_id() -> str:
    return uuid.uuid4().hex[:12]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


@dataclass
class Event:
    """Base — all bus events extend this."""

    event_id: str = field(default_factory=_new_event_id)
    created: str = field(default_factory=_now_iso)
    source: str = "bridge"


# ────────────────────────────────────────────────────────────────────────────
# SkillInvocation triplet
# ────────────────────────────────────────────────────────────────────────────


@dataclass
class SkillInvocationEvent(Event):
    """Shared base — match on this to subscribe to all phases."""

    run_id: str = ""
    skill: str = ""
    workspace_type: Literal["project", "bd", "general"] = "general"
    workspace_name: str = ""


@dataclass
class SkillInvocationStarted(SkillInvocationEvent):
    sensitivity: str = "internal"
    inputs_hash: str = ""


@dataclass
class SkillInvocationCompleted(SkillInvocationEvent):
    sensitivity: str = "internal"
    lane: str = ""
    duration_ms: int = 0
    tokens: dict[str, int] = field(default_factory=dict)


@dataclass
class SkillInvocationFailed(SkillInvocationEvent):
    error: str = ""
    error_class: str = ""
    duration_ms: int = 0


@dataclass
class SkillInvocationSuspended(SkillInvocationEvent):
    """#63 phase 2b — a skill cooperatively PAUSED (raised ``SkillSuspended``)
    to await operator input. The 4th terminal exit alongside Completed/Failed
    (a suspended segment ends here, not in Completed/Failed). ``prompt`` is the
    operator-facing question; ``expires_at`` is the TTL after which the
    suspension can no longer resume."""

    sensitivity: str = "internal"
    prompt: str = ""
    expires_at: str = ""
    duration_ms: int = 0


@dataclass
class SkillInvocationResumed(SkillInvocationEvent):
    """#63 phase 2b — a previously-suspended skill RESUMED (a later
    ``POST /api/skills/{run_id}/resume`` re-invoked the body with the operator's
    answer). Correlates to the prior Suspended via the shared ``run_id``; the
    resumed segment then ends in Completed/Failed/Suspended-again."""

    sensitivity: str = "internal"


@dataclass
class SkillInvocationRefused(SkillInvocationEvent):
    """#anton-skill-refusal-audit — a #no-mnpi-to-cloud scope/MNPI refusal (was
    cited as §5.4; ``SkillScopeRefused``
    → HTTP 403) caught at the ``@anton_skill`` PERIMETER, BEFORE the run was
    admitted into ``tool_call_hooks``. Because the refusal short-circuits before
    ``__enter__`` succeeds, the after-hook audit AND the Started/Failed lifecycle
    never fire — so this event is the ONLY audit trail for a perimeter refusal
    (otherwise only the HTTP access log sees it). ``requested_sensitivity`` is the
    call's tier that tripped the gate; ``sensitivity`` is the strictest tier
    resolved (may be ``"?"`` if the refusal preceded resolution); ``reason`` is
    the refusal message — governance text, NEVER the request payload the gate
    refused."""

    sensitivity: str = "internal"
    requested_sensitivity: str = ""
    reason: str = ""
    duration_ms: int = 0


# ────────────────────────────────────────────────────────────────────────────
# LLMCall triplet
# ────────────────────────────────────────────────────────────────────────────


@dataclass
class LLMCallEvent(Event):
    run_id: str = ""
    skill: str = ""
    lane: str = ""
    provider: str = ""
    model: str = ""


@dataclass
class LLMCallStarted(LLMCallEvent):
    sensitivity: str = "internal"
    prompt_hash: str = ""


@dataclass
class LLMCallCompleted(LLMCallEvent):
    duration_ms: int = 0
    tokens: dict[str, int] = field(default_factory=dict)


@dataclass
class LLMCallFailed(LLMCallEvent):
    error: str = ""
    error_class: str = ""
    duration_ms: int = 0


# ────────────────────────────────────────────────────────────────────────────
# ToolCall triplet
# ────────────────────────────────────────────────────────────────────────────


@dataclass
class ToolCallEvent(Event):
    run_id: str = ""
    skill: str = ""
    tool_name: str = ""


@dataclass
class ToolCallStarted(ToolCallEvent):
    inputs_hash: str = ""


@dataclass
class ToolCallCompleted(ToolCallEvent):
    duration_ms: int = 0
    result_summary: str = ""


@dataclass
class ToolCallFailed(ToolCallEvent):
    error: str = ""
    error_class: str = ""
    duration_ms: int = 0


# ────────────────────────────────────────────────────────────────────────────
# ActivityLogged (#60)
# ────────────────────────────────────────────────────────────────────────────


@dataclass
class ActivityLogged(Event):
    """Fired by ``routines.shared.audit.write_structured()`` on every write.

    Lets the dashboard subscribe via SSE to live audit events instead of
    polling ``/api/activity``. Carries the same structured record that was
    persisted to ``routines/state/audit_index.db`` AND
    ``routines/runs/activity.jsonl`` — already sanitized + redacted.

    Best-effort emit per #68 — failures are logged + swallowed (see
    ``routines.shared.audit._emit_activity_logged``); audit is observability,
    never load-bearing.
    """

    run_id: str = ""
    actor_type: str = "system"
    actor_id: str = "unknown"
    action: str = ""
    entity_type: str = "session"
    entity_id: str = "unknown"
    # ``details`` is the sanitized+redacted payload; may be None for
    # very small writes.
    details: dict[str, Any] | None = None
    # ``ts`` mirrors the persisted ISO-8601 UTC string (already on the
    # SQLite row + JSONL line); included on the event so subscribers don't
    # need to re-query.
    ts: str = ""


__all__ = [
    "Event",
    "SkillInvocationEvent",
    "SkillInvocationStarted",
    "SkillInvocationCompleted",
    "SkillInvocationFailed",
    "SkillInvocationSuspended",
    "SkillInvocationResumed",
    "SkillInvocationRefused",
    "LLMCallEvent",
    "LLMCallStarted",
    "LLMCallCompleted",
    "LLMCallFailed",
    "ToolCallEvent",
    "ToolCallStarted",
    "ToolCallCompleted",
    "ToolCallFailed",
    "ActivityLogged",
]
