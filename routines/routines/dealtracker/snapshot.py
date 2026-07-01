"""Weekly snapshot of the canonical precedent transactions tracker.

Per Decision-3 of ``COMPS-REDESIGN-2026-06-01.md`` (WS2): the live tracker
filename is STABLE — date-stamped snapshots live under ``./Archive/`` and are
produced by a separate weekly cadence job, NOT on every append.

This module is the cadence job. It copies the live workbook to
``./Archive/Precedent_transactions_tracker_<YYYY-MM-DD>.xlsx`` and is
idempotent — safe to re-fire any number of times per day.

Two skip paths keep the archive lean:

* **Same-day re-fire** — if the archive already has today's snapshot, do
  nothing (status ``"skipped_today_exists"``).
* **Live unchanged** — if the live file's sha256 matches the most recent
  archive's sha256, do nothing (status ``"skipped_unchanged"``).
  This catches the "weekly cron fired but the operator hasn't touched the
  tracker in 10 days" case so we don't accumulate identical snapshots.

The third path is the happy one — copy the live file to the dated archive
filename (status ``"created"``).

CLI: ``python -m routines.dealtracker.cli snapshot`` (see ``cli.py``).
Cron: registered as ``precedent-tracker-snapshot`` in
``routines/scheduler/jobs.py`` (weekly, Mon 02:30 Europe/London — no
collision with the existing Mon 06:30 morning-brief / 08:00 vault-health).

Tests use ``tmp_path`` for the archive dir — NEVER writes to the real
``<workspace-root>/.../Archive/``. The production CLI default uses
the canonical path.
"""

from __future__ import annotations

import hashlib
import logging
import shutil
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Literal, Optional

from routines.dealtracker.workbook import CANONICAL_WORKBOOK_PATH

logger = logging.getLogger(__name__)


# Default archive sits next to the live workbook, in an ``Archive`` subfolder.
_DEFAULT_ARCHIVE_DIR_NAME = "Archive"

# Snapshot filename pattern. The ``YYYY-MM-DD`` date prefix sorts correctly
# in any file browser, and the stem-prefix match is what
# ``_today_snapshot_exists`` looks for.
_SNAPSHOT_STEM_PREFIX = "Precedent_transactions_tracker_"
_SNAPSHOT_SUFFIX = ".xlsx"


SnapshotStatus = Literal[
    "created",                  # new file written
    "skipped_today_exists",     # today's snapshot already exists
    "skipped_unchanged",        # live file unchanged vs most-recent archive
    "skipped_no_live",          # the live workbook doesn't exist yet
]


@dataclass(frozen=True)
class SnapshotResult:
    """The structured outcome of a snapshot call.

    Always returned (never raises on the skip paths) — the audit-row writer in
    the scheduler reads this to record the cadence.
    """

    status: SnapshotStatus
    snapshot_path: Optional[Path]
    reason: str


def _default_archive_dir(live_path: Path) -> Path:
    """``<live_path>/../Archive`` — the canonical convention."""
    return live_path.parent / _DEFAULT_ARCHIVE_DIR_NAME


def _snapshot_filename(today: date) -> str:
    """``Precedent_transactions_tracker_2026-06-01.xlsx``."""
    return f"{_SNAPSHOT_STEM_PREFIX}{today.isoformat()}{_SNAPSHOT_SUFFIX}"


def _file_sha256(path: Path) -> str:
    """Streaming sha256 of a file. Tracker workbooks are tens of KB to a few
    MB — streaming is overkill but cheap and future-proof for large files."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _most_recent_snapshot(archive_dir: Path) -> Optional[Path]:
    """Return the lexicographically-latest snapshot file in ``archive_dir``
    (which is also the date-latest, given the ISO-date filename convention)."""
    if not archive_dir.is_dir():
        return None
    candidates = sorted(
        archive_dir.glob(f"{_SNAPSHOT_STEM_PREFIX}*{_SNAPSHOT_SUFFIX}")
    )
    return candidates[-1] if candidates else None


def snapshot_tracker(
    live_path: Optional[Path] = None,
    archive_dir: Optional[Path] = None,
    today: Optional[date] = None,
) -> SnapshotResult:
    """Snapshot the canonical precedent transactions tracker.

    Args:
        live_path: Path to the live workbook. Default = ``CANONICAL_WORKBOOK_PATH``.
        archive_dir: Where dated snapshots land. Default = ``<live>/../Archive``.
        today: Override for the snapshot date — tests pass this; production
            uses today's UTC date.

    Returns:
        ``SnapshotResult`` carrying the status + (when created) the snapshot
        path + a human-readable reason. Never raises on the skip paths; only
        OS errors during the actual copy propagate.

    Idempotency contract (Decision-3 of COMPS-REDESIGN-2026-06-01):
        * Re-running on the same day with no live-file change → no-op.
        * Re-running on a different day when the live file is unchanged →
          no-op (don't accumulate identical snapshots).
        * First run of the day with a modified live file → new snapshot.
    """
    live = live_path or CANONICAL_WORKBOOK_PATH
    archive = archive_dir or _default_archive_dir(live)
    today = today or datetime.now(timezone.utc).date()

    # No live workbook → nothing to snapshot. (First-ever fire on a fresh
    # install, or the tracker has been moved/deleted.)
    if not live.is_file():
        reason = f"live workbook does not exist: {live}"
        logger.info("snapshot: %s", reason)
        return SnapshotResult(
            status="skipped_no_live", snapshot_path=None, reason=reason,
        )

    archive.mkdir(parents=True, exist_ok=True)
    target = archive / _snapshot_filename(today)

    # Same-day re-fire guard — the most common skip case.
    if target.is_file():
        reason = f"today's snapshot already exists: {target.name}"
        logger.info("snapshot: %s", reason)
        return SnapshotResult(
            status="skipped_today_exists", snapshot_path=target, reason=reason,
        )

    # Live-unchanged guard — covers "weekly cron fires but the operator
    # hasn't touched the tracker since the last snapshot".
    prior = _most_recent_snapshot(archive)
    if prior is not None:
        try:
            live_hash = _file_sha256(live)
            prior_hash = _file_sha256(prior)
        except OSError as e:
            # Hash failure → conservatively snapshot anyway. Better a
            # duplicate than a lost cadence.
            logger.warning(
                "snapshot: hash check failed (%s) — snapshotting anyway", e,
            )
        else:
            if live_hash == prior_hash:
                reason = (
                    f"live workbook unchanged vs most-recent snapshot "
                    f"{prior.name} (sha256 match)"
                )
                logger.info("snapshot: %s", reason)
                return SnapshotResult(
                    status="skipped_unchanged",
                    snapshot_path=prior,
                    reason=reason,
                )

    # Happy path — copy the live workbook to the dated archive filename.
    # copy2 preserves timestamps so the archive carries the live file's
    # mtime (operator-visible if they sort by Date Modified).
    shutil.copy2(str(live), str(target))
    reason = f"snapshot created: {target.name}"
    logger.info("snapshot: %s", reason)
    return SnapshotResult(
        status="created", snapshot_path=target, reason=reason,
    )


__all__ = [
    "SnapshotResult",
    "SnapshotStatus",
    "snapshot_tracker",
]
