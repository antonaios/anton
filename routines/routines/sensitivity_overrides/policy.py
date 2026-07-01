"""Policy + dataclasses for sensitivity override windows."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

# Hard duration bounds — operator can choose anywhere in [min, max], OR the
# UNTIL_CLOSED_DURATION sentinel for an open-ended window (no auto-expiry).
DEFAULT_DURATION_SECONDS = 300    # 5 minutes
MAX_DURATION_SECONDS = 3600       # 1 hour (#llm-routing-postjune15 P2; was 30 min)
MIN_DURATION_SECONDS = 60         # 1 minute (any shorter = mistake; raise to opt out)

# Sentinel duration meaning "stays open until the operator explicitly closes it"
# — no auto-expiry (#llm-routing-postjune15 P2 §D-a). Stored as expires_at=NULL;
# the dashboard renders a persistent banner (no countdown) + periodic re-confirm.
# Still revocable + audited like any window; MNPI remains excluded (the ceiling
# enum below is unchanged — an until-closed window can never raise to MNPI).
UNTIL_CLOSED_DURATION = 0

# Defense-in-depth hard cap for an until-closed window (#llm-routing-postjune15
# P2, operator-approved 2026-06-15): even with no auto-expiry, the window drops
# from "active" once it is older than this — logically, like an expired window
# (physically closed lazily on the next open on the same tuple) — so a
# confidential→cloud override can never stay open INDEFINITELY if the dashboard
# re-confirm is missed (a UI/timer reconfirm can silently fail; see the
# background-timer-no-wake lesson). Lazy: enforced on every active-check AND in
# the storage WHERE clauses; NO background timer.
UNTIL_CLOSED_HARD_CAP_SECONDS = 24 * 3600   # 24 hours

# Ceiling tiers allowed for override. MNPI deliberately excluded — that's the
# absolute rule (CLAUDE.md §5.2, anchor #no-mnpi-to-cloud — was also cited as
# §5.4) and never bypassable.
OverrideCeiling = Literal["public", "internal", "confidential"]


class OverrideRefused(ValueError):
    """Override request invalid (bad ceiling, bad duration, empty
    justification, etc.). Route maps to HTTP 422."""


@dataclass(frozen=True)
class Override:
    """A single active or historical sensitivity-override window.

    Identity:
      * id — short hex slug (deterministic on opened_at + skill)
      * skill, workspace, provider — the tuple this override applies to
      * ceiling — the highest sensitivity tier the override grants (never MNPI)

    Lifecycle:
      * opened_at  — when the operator opened the window
      * expires_at — opened_at + duration_seconds (computed at open time), OR
                     None for an until-closed window (no auto-expiry — drops
                     only on explicit close / supersede)
      * closed_at  — when actually closed (operator-close OR auto-expire OR
                     superseded by a new override on the same tuple);
                     None while active

    Audit:
      * justification — the operator's intent string (REQUIRED, non-empty)
      * closed_reason — 'operator' | 'expired' | 'superseded' | None
    """

    id: str
    skill: str
    workspace: str
    provider: str
    ceiling: OverrideCeiling
    opened_at: datetime
    expires_at: datetime | None      # None = until-closed (no auto-expiry)
    justification: str
    closed_at: datetime | None = None
    closed_reason: str | None = None

    def is_active(self, *, now: datetime) -> bool:
        """True iff the override is currently in-window. Excludes closed
        (operator/superseded) AND expired. An until-closed window
        (``expires_at is None``) stays active until explicitly closed OR until
        the ``UNTIL_CLOSED_HARD_CAP_SECONDS`` defense-in-depth cap measured from
        ``opened_at`` (whichever comes first)."""
        if self.closed_at is not None:
            return False
        if self.expires_at is None:
            return now < self.opened_at + timedelta(seconds=UNTIL_CLOSED_HARD_CAP_SECONDS)
        return now < self.expires_at
