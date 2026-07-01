"""BD-decay routine.

Walks Companies/<X>.md for any company with `bd_state` set. For each:
  - Parses `bd_last_contact:` (ISO date)
  - Looks up the state's decay threshold (per Plan v3 §6.9 Phase 5)
  - If days-since > threshold, flags it as stale

Output: list of StaleEntry dataclasses, suitable for morning brief
integration or standalone report.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date as date_cls
from pathlib import Path

import frontmatter

log = logging.getLogger(__name__)


# Decay thresholds per Plan v3 §6.9 Phase 5 (days)
DECAY_THRESHOLDS: dict[str, int] = {
    "watching": 180,
    "engaged": 60,
    "dormant": 90,
    "dead": -1,        # sticky; never decays
    "won": -1,         # sticky
    "lost": -1,        # sticky
}


@dataclass
class StaleEntry:
    """One company whose BD watch has gone stale."""
    company_path: str          # vault-relative POSIX
    company_name: str
    sector: str
    bd_state: str
    bd_last_contact: str       # ISO date string
    bd_owner: str
    days_since_contact: int
    threshold_days: int
    days_over: int             # days_since_contact - threshold_days


def scan(vault_root: Path, today: date_cls | None = None) -> list[StaleEntry]:
    """Walk Companies/ and return all stale BD watch entries.

    Companies with no bd_state, no bd_last_contact, or sticky state
    (dead/won/lost) are excluded.
    """
    if today is None:
        today = date_cls.today()

    out: list[StaleEntry] = []
    companies_dir = vault_root / "Companies"
    if not companies_dir.is_dir():
        log.info("bd-decay: no Companies/ at %s", companies_dir)
        return out

    for f in sorted(companies_dir.iterdir()):
        if not f.is_file() or not f.name.endswith(".md"):
            continue
        try:
            meta = frontmatter.load(f).metadata or {}
        except Exception as e:  # noqa: BLE001
            log.warning("bd-decay: failed to read %s: %s", f, e)
            continue

        bd_state = meta.get("bd_state")
        if not bd_state:
            continue
        bd_state = str(bd_state).strip().lower()

        threshold = DECAY_THRESHOLDS.get(bd_state, -1)
        if threshold < 0:
            continue  # sticky state, never decays

        last_contact_raw = meta.get("bd_last_contact")
        if not last_contact_raw:
            # No last_contact recorded — treat as immediately stale
            last_contact = today
            days_since = threshold + 1  # synthetic — flag it
            last_contact_str = "(unset)"
        else:
            try:
                last_contact = date_cls.fromisoformat(str(last_contact_raw))
                days_since = (today - last_contact).days
                last_contact_str = last_contact.isoformat()
            except (ValueError, TypeError):
                log.warning("bd-decay: %s has unparseable bd_last_contact=%s", f, last_contact_raw)
                continue

        if days_since <= threshold:
            continue  # not yet stale

        sector = _slug_from_sector_field(meta.get("sector"))
        bd_owner = str(meta.get("bd_owner") or "(unset)").strip()

        out.append(StaleEntry(
            company_path=str(f.relative_to(vault_root)).replace("\\", "/"),
            company_name=f.stem,
            sector=sector,
            bd_state=bd_state,
            bd_last_contact=last_contact_str,
            bd_owner=bd_owner,
            days_since_contact=days_since,
            threshold_days=threshold,
            days_over=days_since - threshold,
        ))

    log.info("bd-decay: %d stale entries", len(out))
    return out


def _slug_from_sector_field(field_value) -> str:
    """Derive sector slug from various sector frontmatter shapes."""
    if not field_value:
        return "(unset)"
    import re
    s = str(field_value).lower()
    m = re.search(r"sectors/([a-z0-9-]+)", s)
    if m:
        return m.group(1)
    return s.strip().replace(" ", "-")


def format_stale_for_morning_brief(stale: list[StaleEntry]) -> str:
    """Render stale entries as a morning-brief markdown section.

    Returns an empty string when there are no stale entries (so the brief
    routine can elide the section entirely).
    """
    if not stale:
        return ""

    # Sort by days_over desc (most-stale first)
    stale_sorted = sorted(stale, key=lambda s: s.days_over, reverse=True)

    lines = [
        "## BD watch -- stale entries",
        "",
        f"_{len(stale_sorted)} compan{'y' if len(stale_sorted) == 1 else 'ies'} past decay threshold._",
        "",
    ]
    for s in stale_sorted[:10]:
        lines.append(
            f"- [[Companies/{s.company_name}]] (sector: {s.sector}, state: "
            f"`{s.bd_state}`) -- last contact {s.bd_last_contact}, "
            f"{s.days_since_contact}d ago (threshold {s.threshold_days}d, "
            f"{s.days_over}d over). Owner: {s.bd_owner}."
        )
    if len(stale_sorted) > 10:
        lines.append(f"- ...and {len(stale_sorted) - 10} more")
    lines.append("")
    return "\n".join(lines)
