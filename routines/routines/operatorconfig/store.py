"""Per-section read/write for the operator-config surface.

READ (``read_config``) is lenient: it returns what the files actually
contain, plus per-row ``issues`` strings computed with the same rules the
live consumers apply — a malformed row is SHOWN (so the operator can fix
it from the tab), never silently dropped the way the banner fallback
does.

WRITE (``write_section``) is strict and surgical:

* the payload has already passed the pydantic section model;
* the caller carries the ``mtime`` token from its last GET — a mismatch
  (mid-flight Obsidian edit) raises ``ConflictError`` → 409, no clobber;
* only the relevant YAML payload changes (fenced block / frontmatter
  line surgery); every other byte is preserved;
* the new text is VERIFIED by re-parsing before it touches disk;
* the write is atomic (temp + replace) and audited to
  ``runs/operator-config.jsonl``.

mtime tokens travel as STRINGS: ``st_mtime_ns`` (~1.8e18) exceeds
JavaScript's safe-integer range, so a numeric JSON field would silently
lose precision in the dashboard and produce false 409s.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import frontmatter

from routines.operatorconfig import blocks, profile_edit
from routines.operatorconfig.models import (
    BannersData,
    CoverageData,
    ProfileData,
    SectorsData,
    WatchlistData,
)
from routines.shared import audit
from routines.shared.md_config import extract_section
from routines.shared.ticker_config import (  # noqa: PLC2701 — single rule source
    _SYNTHETIC_SYMBOLS,
    _TICKER_PATTERN,
)
from routines.shared.vault_writer import atomic_write

log = logging.getLogger(__name__)

ROUTINE = "operator-config"

SECTION_FILES = {
    "banners": "_claude/tickers.md",
    "watchlist": "_claude/earnings-watchlist.md",
    "coverage": "_claude/news-coverage.md",
    "sectors": "_claude/profile.md",
    "profile": "_claude/profile.md",
}


class ConflictError(Exception):
    """File changed since the caller's GET (mid-flight Obsidian edit)."""

    def __init__(self, message: str, current: "FileInfo") -> None:
        super().__init__(message)
        self.current = current


@dataclass
class FileInfo:
    path: str               # vault-relative, forward slashes
    exists: bool
    mtime: Optional[str]    # str(st_mtime_ns) — see module docstring
    mtime_iso: Optional[str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "exists": self.exists,
            "mtime": self.mtime,
            "mtime_iso": self.mtime_iso,
        }


def _file_info(vault_root: Path, rel: str) -> FileInfo:
    p = vault_root / rel
    if not p.exists():
        return FileInfo(path=rel, exists=False, mtime=None, mtime_iso=None)
    st = p.stat()
    return FileInfo(
        path=rel,
        exists=True,
        mtime=str(st.st_mtime_ns),
        mtime_iso=datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
    )


def _read_text(vault_root: Path, rel: str) -> Optional[str]:
    p = vault_root / rel
    if not p.exists():
        return None
    try:
        return p.read_text(encoding="utf-8")
    except OSError as e:
        log.warning("operator-config: read %s failed: %s", p, e)
        return None


# ── Lenient READ side ────────────────────────────────────────────────────


def _row_issues_ticker(row: Any, idx: int, *, macro: bool) -> list[str]:
    issues: list[str] = []
    if not isinstance(row, dict):
        return [f"row {idx + 1}: not a mapping"]
    sym = str(row.get("symbol", "")).strip().upper()
    kind = str(row.get("kind", "")).strip().lower()
    if not sym:
        issues.append(f"row {idx + 1}: missing symbol")
    elif macro and kind in ("rate", "indicator"):
        if sym not in _SYNTHETIC_SYMBOLS:
            issues.append(
                f"row {idx + 1}: {sym} (kind={kind}) not in the synthetic "
                f"allowlist — the bar will skip it"
            )
    elif not _TICKER_PATTERN.fullmatch(sym):
        issues.append(
            f"row {idx + 1}: {sym!r} fails the public-ticker rule — "
            f"the bar will skip it"
        )
    if macro and kind not in ("equity", "index", "commodity", "rate", "indicator"):
        issues.append(f"row {idx + 1}: unknown kind {kind!r}")
    return issues


def _normalise_rows(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    return [r if isinstance(r, dict) else {"value": r} for r in raw]


def _read_banners(vault_root: Path) -> dict[str, Any]:
    text = _read_text(vault_root, SECTION_FILES["banners"])
    issues: list[str] = []
    ticker_rows: list[dict[str, Any]] = []
    macro_rows: list[dict[str, Any]] = []
    if text is None:
        issues.append(
            "tickers.md missing — the live bars are running on the "
            "hardcoded fallback; saving here will create it"
        )
    else:
        raw_t = extract_section(text, "ticker_bar")
        raw_m = extract_section(text, "macro_bar")
        ticker_rows = _normalise_rows(raw_t)
        macro_rows = _normalise_rows(raw_m)
        for i, r in enumerate(ticker_rows):
            issues.extend(_row_issues_ticker(r, i, macro=False))
        for i, r in enumerate(macro_rows):
            issues.extend(_row_issues_ticker(r, i, macro=True))
    return {"ticker_bar": ticker_rows, "macro_bar": macro_rows, "issues": issues}


def _read_watchlist(vault_root: Path) -> dict[str, Any]:
    text = _read_text(vault_root, SECTION_FILES["watchlist"])
    issues: list[str] = []
    rows: list[dict[str, Any]] = []
    if text is None:
        issues.append("earnings-watchlist.md missing")
    else:
        rows = _normalise_rows(extract_section(text, "earnings_watchlist"))
        for i, r in enumerate(rows):
            sym = str(r.get("symbol", "")).strip().upper()
            if not sym:
                issues.append(f"row {i + 1}: missing symbol")
            elif not _TICKER_PATTERN.fullmatch(sym):
                issues.append(f"row {i + 1}: {sym!r} fails the public-ticker rule")
        if len(rows) > 30:
            issues.append("more than 30 rows — the tracker truncates at 30")
    return {"earnings_watchlist": rows, "issues": issues}


def _read_coverage(vault_root: Path) -> dict[str, Any]:
    # Lazy import — sectornews pulls search-provider deps at package level.
    from routines.sectornews.coverage import load_coverage

    entries, source = load_coverage(vault_root)
    return {
        "coverage": [
            {
                "name": e.name,
                "sector": e.sector,
                "sources": list(e.sources),
                "query": e.query,
                "enabled": e.enabled,
            }
            for e in entries
        ],
        "synthesised": source == "synthesised",
        "issues": [],
    }


def _slug(sector: str) -> str:
    # F-21 (#consistency-sector-slug): delegate to the CANONICAL slugifier the
    # sectors package writes folders with. This reader used to compute
    # ``lower().replace(" ","-")`` (no strip, no ``_``→``-``), so a profile
    # sector like "Oil_Gas" resolved to ``oil_gas`` while the writer created
    # ``oil-gas`` → the configured tree false-negatived. One slugger now.
    from routines.sectors.schema import slugify_sector
    return slugify_sector(sector)


def _read_sectors(vault_root: Path) -> dict[str, Any]:
    from routines.shared.profile import load as load_profile

    prof = load_profile(vault_root)
    sectors_dir = vault_root / "Sectors"
    template_dir = sectors_dir / "_template"
    template_count = (
        len(list(template_dir.rglob("*.md"))) if template_dir.is_dir() else 0
    )

    trees: list[dict[str, Any]] = []
    active_slugs: set[str] = set()
    for sector in prof.active_sectors:
        slug = _slug(sector)
        active_slugs.add(slug)
        tree_dir = sectors_dir / slug
        if not tree_dir.is_dir():
            status = "missing"
        else:
            have = len(list(tree_dir.rglob("*.md")))
            status = "full" if template_count and have >= template_count else "partial"
        trees.append({
            "sector": sector,
            "slug": slug,
            "tree": status,
            "note_exists": (sectors_dir / f"{sector}.md").exists(),
        })

    orphans: list[str] = []
    if sectors_dir.is_dir():
        for child in sectors_dir.iterdir():
            if not child.is_dir() or child.name.startswith("_"):
                continue
            if child.name not in active_slugs:
                orphans.append(child.name)

    return {
        "active_sectors": list(prof.active_sectors),
        "trees": trees,
        "orphan_trees": sorted(orphans),
        "scaffold_hint": (
            'Copy the template to scaffold a tree: '
            'cp -r "Sectors/_template" "Sectors/<slug>"'
        ),
    }


def _read_profile(vault_root: Path) -> dict[str, Any]:
    text = _read_text(vault_root, SECTION_FILES["profile"])
    if text is None:
        return {"issues": ["profile.md missing"]}
    try:
        meta = dict(frontmatter.loads(text).metadata)
    except Exception as e:  # noqa: BLE001
        return {"issues": [f"profile.md frontmatter unparseable: {e}"]}
    role = meta.get("current_role") or {}
    if not isinstance(role, dict):
        role = {}
    return {
        "operator": str(meta.get("operator", "")).strip(),
        "operator_slug": str(meta.get("operator_slug", "")).strip(),
        "qualifications": [
            str(q).strip() for q in (meta.get("qualifications") or [])
            if str(q).strip()
        ],
        "role_title": str(role.get("title", "")).strip(),
        "role_firm": str(role.get("firm", "")).strip(),
        "issues": [],
    }


def read_config(vault_root: Path) -> dict[str, Any]:
    """The GET payload: every section's current state + file stats.

    Credentials/provider status is composed in the route (it touches the
    credential store and network probes — not vault files).
    """
    return {
        "sections": {
            "banners": _read_banners(vault_root),
            "watchlist": _read_watchlist(vault_root),
            "coverage": _read_coverage(vault_root),
            "sectors": _read_sectors(vault_root),
            "profile": _read_profile(vault_root),
        },
        "files": {
            "tickers": _file_info(vault_root, SECTION_FILES["banners"]).as_dict(),
            "earnings_watchlist": _file_info(
                vault_root, SECTION_FILES["watchlist"]
            ).as_dict(),
            "news_coverage": _file_info(
                vault_root, SECTION_FILES["coverage"]
            ).as_dict(),
            "profile": _file_info(vault_root, SECTION_FILES["profile"]).as_dict(),
        },
    }


# ── Strict WRITE side ────────────────────────────────────────────────────


_NEWS_COVERAGE_TEMPLATE = """---
type: dashboard-config
memory_kind: procedural
sensitivity: internal
version: 1
tags: [config, news, coverage, procedural-memory]
---

# News coverage

What the daily newsletter routine covers. Each row is one morning run.
Rows with a `sector:` link feed that sector's expertise waterfall; rows
without one are standalone topics (e.g. UK macro) that just produce a
newsletter in `Resources/Newsletters/`.

Edit the YAML code block below — in Obsidian or from the dashboard's
OPERATOR tab (the tab edits this same file in place). The list is read
fresh on each run. **No restart needed.**

Fields per row:

- `name` — display name; also the newsletter filename stem. Required.
- `sector` — optional link to an expertise sector (`active_sectors`).
- `sources` — explicit URLs/feeds to scrape; empty = search fallback.
- `query` — optional custom search query (defaults to a sector-derived
  M&A query).
- `enabled` — optional; `false` pauses the row without deleting it.

## coverage

```yaml
{block}
```
"""


def _verify_block_roundtrip(
    new_text: str, section_name: str, intended: list[dict[str, Any]],
) -> None:
    parsed = extract_section(new_text, section_name)
    if parsed != intended:
        raise RuntimeError(
            f"operator-config: post-edit verification failed for "
            f"{section_name!r} — refusing to write"
        )


def _banners_text(text: str, data: BannersData) -> str:
    new = blocks.replace_yaml_block(
        text, "ticker_bar",
        blocks.dump_flow_rows([r.model_dump() for r in data.ticker_bar]),
    )
    new = blocks.replace_yaml_block(
        new, "macro_bar",
        blocks.dump_flow_rows([r.model_dump() for r in data.macro_bar]),
    )
    _verify_block_roundtrip(new, "ticker_bar", [r.model_dump() for r in data.ticker_bar])
    _verify_block_roundtrip(new, "macro_bar", [r.model_dump() for r in data.macro_bar])
    return new


def _watchlist_text(text: str, data: WatchlistData) -> str:
    rows = [
        {"symbol": r.symbol, **({"name": r.name} if r.name else {})}
        for r in data.earnings_watchlist
    ]
    new = blocks.replace_yaml_block(
        text, "earnings_watchlist", blocks.dump_flow_rows(rows),
    )
    _verify_block_roundtrip(new, "earnings_watchlist", rows)
    return new


def _coverage_rows(data: CoverageData) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for r in data.coverage:
        row: dict[str, Any] = {"name": r.name}
        if r.sector:
            row["sector"] = r.sector
        row["sources"] = list(r.sources)
        if r.query:
            row["query"] = r.query
        if not r.enabled:
            row["enabled"] = False
        rows.append(row)
    return rows


def _coverage_text(text: Optional[str], data: CoverageData) -> str:
    rows = _coverage_rows(data)
    block = blocks.dump_block(rows) if rows else "[]"
    if text is None:
        new = _NEWS_COVERAGE_TEMPLATE.format(block=block)
    else:
        new = blocks.replace_yaml_block(text, "coverage", block)
    _verify_block_roundtrip(new, "coverage", rows if rows else [])
    return new


def _sectors_text(text: str, data: SectorsData) -> str:
    new = profile_edit.set_string_list_block(
        text, "active_sectors", data.active_sectors,
    )
    meta = dict(frontmatter.loads(new).metadata)
    got = [str(s) for s in (meta.get("active_sectors") or [])]
    if got != data.active_sectors:
        raise RuntimeError(
            "operator-config: post-edit verification failed for "
            "active_sectors — refusing to write"
        )
    return new


def _profile_text(text: str, data: ProfileData) -> str:
    new = profile_edit.set_scalar(text, "operator", data.operator)
    new = profile_edit.set_scalar(new, "operator_slug", data.operator_slug)
    new = profile_edit.set_scalar(new, "qualifications", data.qualifications)
    new = profile_edit.set_scalar(new, "title", data.role_title, parent="current_role")
    new = profile_edit.set_scalar(new, "firm", data.role_firm, parent="current_role")

    meta = dict(frontmatter.loads(new).metadata)
    role = meta.get("current_role") or {}
    checks = [
        (str(meta.get("operator", "")), data.operator),
        (str(meta.get("operator_slug", "")), data.operator_slug),
        ([str(q) for q in (meta.get("qualifications") or [])], data.qualifications),
        (str(role.get("title", "")), data.role_title),
        (str(role.get("firm", "")), data.role_firm),
    ]
    for got, want in checks:
        if got != want:
            raise RuntimeError(
                "operator-config: post-edit verification failed for "
                f"profile (got {got!r}, want {want!r}) — refusing to write"
            )
    return new


def write_section(
    vault_root: Path,
    section: str,
    data: Any,
    *,
    expected_mtime: Optional[str],
    audit_dir: Path,
) -> FileInfo:
    """Apply one section's validated payload to its vault file.

    ``expected_mtime`` is the string token from the caller's last GET
    (None when the file didn't exist). Raises ``ConflictError`` on any
    mismatch; ``KeyError`` on an unknown section.
    """
    rel = SECTION_FILES[section]
    path = vault_root / rel
    run_id = audit.new_run_id()
    t0 = time.monotonic()

    current = _file_info(vault_root, rel)
    if current.exists != (expected_mtime is not None) or (
        current.exists and current.mtime != expected_mtime
    ):
        raise ConflictError(
            f"{rel} changed since your last load (or its existence changed) "
            "— re-fetch and re-apply your edit",
            current,
        )

    text = _read_text(vault_root, rel)
    if text is None and section != "coverage":
        raise ConflictError(f"{rel} is missing or unreadable", current)

    if section == "banners":
        new_text = _banners_text(text, data)            # type: ignore[arg-type]
    elif section == "watchlist":
        new_text = _watchlist_text(text, data)          # type: ignore[arg-type]
    elif section == "coverage":
        new_text = _coverage_text(text, data)
    elif section == "sectors":
        new_text = _sectors_text(text, data)            # type: ignore[arg-type]
    elif section == "profile":
        new_text = _profile_text(text, data)            # type: ignore[arg-type]
    else:
        raise KeyError(f"unknown section {section!r}")

    # Shrink the TOCTOU window (codex SEV-1): re-stat immediately before
    # the replace — a mid-transform Obsidian save lands here as a 409
    # instead of being clobbered. A true cross-process lock isn't
    # available here; the residual window is the atomic_write call
    # itself (microseconds) rather than the full parse+transform span.
    recheck = _file_info(vault_root, rel)
    if recheck.exists != current.exists or recheck.mtime != current.mtime:
        raise ConflictError(
            f"{rel} changed while your edit was being applied — re-fetch "
            "and re-apply",
            recheck,
        )

    atomic_write(path, new_text, vault_root=vault_root)
    info = _file_info(vault_root, rel)

    audit.write_structured(
        actor={"type": "user", "id": "operator"},
        entity_type="vault_note",
        entity_id=rel,
        action="update" if current.exists else "create",
        routine=ROUTINE,
        run_id=run_id,
        status="ok",
        audit_dir=audit_dir,
        inputs={"section": section},
        outputs={"path": rel, "mtime": info.mtime},
        duration_ms=int((time.monotonic() - t0) * 1000),
    )
    return info
