"""Freshness sweep for sector claim files.

Plan v3 §6.9 Phase 6 + B16. Walks `Sectors/<X>/*.md` claim files and flags
those past their freshness thresholds:
  - **90 days**: warning chip (operator may want to refresh)
  - **180 days**: auto-bump confidence down one tier

Default thresholds configurable via `_claude/profile.md` body (future);
constants below are the defaults.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date as date_cls
from pathlib import Path

import frontmatter

log = logging.getLogger(__name__)


WARNING_DAYS = 90
AUTO_BUMP_DAYS = 180

_CONFIDENCE_TIERS = ["high", "medium", "low"]


@dataclass
class StaleClaim:
    """One claim file past its freshness threshold."""
    path: str                       # vault-relative POSIX
    sector: str
    claim_type: str
    current_confidence: str
    suggested_confidence: str       # same as current if only warning; bumped if auto-bump
    last_refreshed: str             # ISO date
    days_since_refresh: int
    severity: str                   # "warning" | "auto-bump"


def scan(vault_root: Path, today: date_cls | None = None) -> list[StaleClaim]:
    """Walk Sectors/<X>/*.md and return all stale claim files."""
    if today is None:
        today = date_cls.today()

    out: list[StaleClaim] = []
    sectors_dir = vault_root / "Sectors"
    if not sectors_dir.is_dir():
        return out

    for sector_dir in sorted(sectors_dir.iterdir()):
        if not sector_dir.is_dir() or sector_dir.name.startswith("_"):
            continue
        for f in sorted(sector_dir.iterdir()):
            if not f.is_file() or not f.name.endswith(".md"):
                continue
            # Only check claim files (skip views like _Index, BD, People)
            if f.name in ("_Index.md", "BD.md", "People.md"):
                continue
            try:
                meta = frontmatter.load(f).metadata or {}
            except Exception as e:  # noqa: BLE001
                log.warning("freshness: failed to read %s: %s", f, e)
                continue

            if str(meta.get("type") or "") != "sector-claim":
                continue

            last_refreshed_raw = meta.get("last_refreshed")
            if not last_refreshed_raw:
                continue
            try:
                last_refreshed = date_cls.fromisoformat(str(last_refreshed_raw))
            except (ValueError, TypeError):
                log.warning("freshness: %s has unparseable last_refreshed=%s",
                           f, last_refreshed_raw)
                continue

            days = (today - last_refreshed).days
            if days < WARNING_DAYS:
                continue

            current = str(meta.get("confidence") or "low").lower()
            severity = "auto-bump" if days >= AUTO_BUMP_DAYS else "warning"
            suggested = _bump_down(current) if severity == "auto-bump" else current

            out.append(StaleClaim(
                path=str(f.relative_to(vault_root)).replace("\\", "/"),
                sector=sector_dir.name,
                claim_type=str(meta.get("claim_type") or f.stem.lower()),
                current_confidence=current,
                suggested_confidence=suggested,
                last_refreshed=last_refreshed.isoformat(),
                days_since_refresh=days,
                severity=severity,
            ))

    log.info("freshness: %d stale claim files", len(out))
    return out


def _bump_down(tier: str) -> str:
    """high -> medium, medium -> low, low -> low (saturates)."""
    if tier == "high":
        return "medium"
    if tier == "medium":
        return "low"
    return "low"


def render_report(stale: list[StaleClaim], today: date_cls | None = None) -> str:
    """Render a markdown report suitable for operator review."""
    if today is None:
        today = date_cls.today()
    if not stale:
        return f"# Vault freshness sweep -- {today.isoformat()}\n\n_All claim files within freshness thresholds._\n"

    auto_bump = [s for s in stale if s.severity == "auto-bump"]
    warnings = [s for s in stale if s.severity == "warning"]

    out = [
        "---",
        "type: vault-health-report",
        "report_kind: freshness",
        "sensitivity: internal",
        f"date: {today.isoformat()}",
        f"stale_count: {len(stale)}",
        f"auto_bump_count: {len(auto_bump)}",
        f"warning_count: {len(warnings)}",
        "status: pending-review",
        "tags: [vault-health, freshness, routines]",
        "---",
        "",
        f"# Vault freshness sweep -- {today.isoformat()}",
        "",
        f"Walked all `Sectors/<X>/*.md` claim files and found "
        f"**{len(stale)} stale**: {len(auto_bump)} past auto-bump threshold "
        f"({AUTO_BUMP_DAYS}d), {len(warnings)} past warning threshold "
        f"({WARNING_DAYS}d).",
        "",
    ]

    if auto_bump:
        out += [
            f"## Auto-bump suggested ({len(auto_bump)})",
            "",
            "These files are past the 180d threshold. Suggested action: "
            "edit `confidence:` frontmatter to the bumped-down value, OR "
            "refresh the file content + advance `last_refreshed:`.",
            "",
            "| File | Sector | Claim type | Current | Suggested | Last refreshed | Days |",
            "|---|---|---|---|---|---|---|",
        ]
        for s in sorted(auto_bump, key=lambda x: x.days_since_refresh, reverse=True):
            out.append(
                f"| [[{s.path}]] | {s.sector} | {s.claim_type} | "
                f"`{s.current_confidence}` | `{s.suggested_confidence}` | "
                f"{s.last_refreshed} | {s.days_since_refresh} |"
            )
        out.append("")

    if warnings:
        out += [
            f"## Warning -- approaching auto-bump ({len(warnings)})",
            "",
            "These files are past 90d but not yet 180d. Refresh proactively "
            "if you have current sources; otherwise they'll auto-bump on "
            f"day {AUTO_BUMP_DAYS}.",
            "",
            "| File | Sector | Claim type | Confidence | Last refreshed | Days |",
            "|---|---|---|---|---|---|",
        ]
        for s in sorted(warnings, key=lambda x: x.days_since_refresh, reverse=True):
            out.append(
                f"| [[{s.path}]] | {s.sector} | {s.claim_type} | "
                f"`{s.current_confidence}` | {s.last_refreshed} | "
                f"{s.days_since_refresh} |"
            )
        out.append("")

    out += [
        "",
        "## Operator action",
        "",
        "1. For each auto-bump file: either refresh the content or accept "
        "the confidence demotion by editing the frontmatter.",
        "2. For warnings: prioritise the files closest to 180d for proactive refresh.",
        "3. Update this proposal `status:` to `applied` or `rejected` once handled.",
    ]
    return "\n".join(out)
