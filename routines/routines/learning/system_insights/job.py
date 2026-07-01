"""APScheduler job entry — wraps the analyse → write pipeline.

The scheduler subprocesses the CLI (consistent with the other registered
jobs in ``routines/scheduler/jobs.py``), so this module also exposes an
in-process ``run()`` helper for tests + direct invocation.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)


def run(
    *,
    vault_root: Optional[Path] = None,
    window_days: int = 7,
    now: Optional[datetime] = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Read telemetry, analyse, write proposals. Returns a summary dict.

    Side effect: writes one ``.md`` per surviving proposal under
    ``<vault>/Routines/system-insights/``. ``dry_run=True`` skips writes
    and returns the proposal list for inspection only.
    """
    from routines.api.deps import VAULT
    from routines.learning.system_insights.analyse import analyse_window
    from routines.learning.system_insights.readers import read_all_sources
    from routines.learning.system_insights.writer import write_proposal

    now = now or datetime.now(timezone.utc)
    vault = vault_root or VAULT
    window = timedelta(days=window_days)

    by_source = read_all_sources(window=window, now=now)
    proposals = analyse_window(by_source, now=now, window_days=window_days)

    written: list[str] = []
    skipped: list[str] = []
    if not dry_run:
        for prop in proposals:
            path = write_proposal(vault, prop, now=now)
            if path is None:
                skipped.append(prop.topic_slug)
            else:
                written.append(str(path))

    summary = {
        "proposals_total": len(proposals),
        "written": len(written),
        "skipped": len(skipped),
        "dry_run": dry_run,
        "window_days": window_days,
        "sources": {k: len(v) for k, v in by_source.items()},
        "written_paths": written,
        "skipped_slugs": skipped,
    }
    log.info(
        "system_insights: analysed window=%dd, proposals=%d (written=%d, "
        "skipped=%d, dry_run=%s)",
        window_days, len(proposals), len(written), len(skipped), dry_run,
    )
    return summary


__all__ = ["run"]
