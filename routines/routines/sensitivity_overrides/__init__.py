"""#llm-routing-override (2026-06-03) — operator sensitivity override windows.

Per-window, time-limited, audit-logged operator escape hatch from the
central sensitivity guard. Mirrors the #57 budget-ack pattern: the
guard is the default; the operator can explicitly open a time-limited
override window with required justification + full audit trail.

Hard rules (load-bearing):

  * **Override CANNOT raise the ceiling to ``MNPI``** — the no-MNPI-to-cloud
    rule (CLAUDE.md §5.2, anchor #no-mnpi-to-cloud — was also cited as §5.4)
    is absolute. Requests with ``ceiling="MNPI"`` are refused at the endpoint
    (HTTP 422).
  * **Time-limited by default, OR until-closed.** Default 300s (5 min); max
    3600s (1 hour, #llm-routing-postjune15 P2 — was 30 min); refused if
    duration_seconds is neither the until-closed sentinel nor in [60, 3600]. A
    timed window expires + stops being honoured even if not explicitly closed.
    ``duration_seconds == UNTIL_CLOSED_DURATION`` (0) opens an UNTIL-CLOSED
    window: no auto-expiry (``expires_at`` NULL), drops only on explicit
    close / supersede; the dashboard shows a persistent banner + re-confirm.
  * **Required justification** — non-empty string; recorded as the operator's
    intent + reviewed in the audit trail.
  * **Single active override per (skill, workspace, provider) tuple.**
    Opening a second supersedes (closes) the first.
  * **Composes with #57 budget gate + #61 sensitivity guard** — the override
    bypasses the SENSITIVITY refusal only; budget caps + per-workspace policy
    still fire normally on top.

Public API:

  open_override(skill, workspace, provider, ceiling, duration_seconds, justification, *, now)
    → Override
  list_active_overrides(*, now) → list[Override]
  find_active_override(skill, workspace, provider, ceiling, *, now) → Override | None
  close_override(override_id, reason='operator', *, now) → Override

See ``LLM-ROUTING-2026-06-02.md`` §5 for the design spec.
"""

from .policy import (
    Override,
    OverrideRefused,
    MAX_DURATION_SECONDS,
    DEFAULT_DURATION_SECONDS,
    UNTIL_CLOSED_DURATION,
)
from .storage import (
    open_override,
    list_active_overrides,
    find_active_override,
    close_override,
    OverrideNotFound,
)

__all__ = [
    "Override",
    "OverrideRefused",
    "OverrideNotFound",
    "MAX_DURATION_SECONDS",
    "DEFAULT_DURATION_SECONDS",
    "UNTIL_CLOSED_DURATION",
    "open_override",
    "list_active_overrides",
    "find_active_override",
    "close_override",
]
