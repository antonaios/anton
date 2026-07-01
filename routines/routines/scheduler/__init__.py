"""Bridge-embedded scheduler.

See [[scheduler]] (this directory's scheduler.py) for the BridgeScheduler
class, and [[../api/routes/scheduler]] for the read-only GET endpoint.

#23 STATUS: framework only. Concrete job registration (morning_brief cron,
maintenance jobs) lands in a follow-on session — this module ships the
scheduler instance + lifecycle + read API, nothing more."""

from __future__ import annotations

from routines.scheduler.scheduler import (
    BridgeScheduler,
    get_scheduler,
    reset_scheduler_for_tests,
)

__all__ = ["BridgeScheduler", "get_scheduler", "reset_scheduler_for_tests"]
