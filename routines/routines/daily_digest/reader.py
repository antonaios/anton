"""Read a stored daily digest back into a DailyDigest object."""

from __future__ import annotations

import json
import logging
from datetime import date as date_cls
from datetime import datetime, timezone
from pathlib import Path

import frontmatter

from routines.daily_digest.schema import DailyDigest, DigestRow
from routines.daily_digest.writer import digest_path

log = logging.getLogger(__name__)


def load_today(vault_root: Path) -> DailyDigest | None:
    return load_for_date(vault_root, datetime.now(timezone.utc).date())


def load_for_date(vault_root: Path, the_date: date_cls) -> DailyDigest | None:
    path = digest_path(vault_root, the_date)
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        log.warning("daily-digest: read %s failed: %s", path, e)
        return None
    try:
        post = frontmatter.loads(text)
    except Exception as e:  # noqa: BLE001
        log.warning("daily-digest: parse %s failed: %s", path, e)
        return None

    raw = post.metadata.get("data")
    if not raw:
        return None
    try:
        payload = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError as e:
        log.warning("daily-digest: payload JSON parse failed for %s: %s", path, e)
        return None
    if not isinstance(payload, dict):
        return None

    return DailyDigest(
        date=str(payload.get("date", "")),
        source=str(payload.get("source", "")),
        activity=[DigestRow(**r) for r in payload.get("activity", []) if isinstance(r, dict)],
        vaultChanges=[DigestRow(**r) for r in payload.get("vaultChanges", []) if isinstance(r, dict)],
        antonCloses=str(payload.get("antonCloses", "")),
    )
