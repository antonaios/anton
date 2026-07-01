"""Step 1 — calendar check: which watched public companies report *today*.

Scans ``Companies/*.md`` for notes whose frontmatter declares
``type: public-company`` and whose ``next-reporting-date`` equals the run date.
Pure filesystem + frontmatter; no network, no LLM.

A "watched public company" is a vault note the operator has opted into the
earnings tracker by setting ``type: public-company`` (distinct from the generic
``type: company``) plus the calendar field. The ``earnings-tracker add-watch``
CLI scaffolds exactly this shape.

Frontmatter fields consumed (all optional except type + the date):

  * ``type: public-company``     — the opt-in marker (required to be scanned)
  * ``next-reporting-date``      — ISO date; the trigger (required to be *due*)
  * ``name`` / ``ticker``        — identity; ``name`` falls back to the filestem
  * ``sector``                   — wikilink or slug; drives the sector-page write
  * ``consensus-source``         — provenance string (e.g. "Bloomberg consensus")
  * ``consensus``                — optional operator-curated consensus figures
  * ``reporting-cadence``        — quarterly | semi-annual | annual (roll-forward)
  * ``earnings-source-url`` / ``ir-url`` / ``rns-url`` — where to fetch results
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

import frontmatter

log = logging.getLogger(__name__)

PUBLIC_COMPANY_TYPE = "public-company"

# Frontmatter keys we accept for the announcement source URL, in priority order.
_SOURCE_URL_KEYS = ("earnings-source-url", "ir-url", "rns-url", "results-url")

_WIKILINK_RE = re.compile(r"\[\[([^\]|#]+?)(?:\|[^\]]+)?(?:#[^\]]+)?\]\]")


@dataclass
class CompanyEntry:
    """One watched public company resolved from its ``Companies/<name>.md``."""

    path: Path
    name: str
    ticker: str = ""
    next_reporting_date: Optional[date] = None
    sector: str = ""                 # slug derived from the frontmatter sector
    consensus_source: str = ""
    consensus: dict[str, Any] = field(default_factory=dict)
    cadence: str = "quarterly"
    source_url: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def stem(self) -> str:
        return self.path.stem


def _coerce_date(value: Any) -> Optional[date]:
    """Coerce a frontmatter value to a ``date``. PyYAML parses a bare
    ``2026-05-07`` to a ``date`` already; an ISO string also works."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return date.fromisoformat(value.strip()[:10])
        except ValueError:
            return None
    return None


def sector_slug(value: Any) -> str:
    """Derive a sector slug from a frontmatter ``sector`` value.

    Accepts a wikilink (``"[[Sectors/telecoms/_Index]]"`` → ``telecoms``), a
    bare slug (``"hospitality"``), or a path-ish string. Returns ``""`` when
    nothing usable is present."""
    if not value:
        return ""
    s = str(value).strip()
    m = _WIKILINK_RE.search(s)
    if m:
        s = m.group(1).strip()
    # Strip a Sectors/ prefix and any trailing _Index / sub-path → first segment.
    s = s.replace("\\", "/")
    if s.lower().startswith("sectors/"):
        s = s[len("sectors/"):]
    first = s.split("/", 1)[0].strip()
    return first.lower().replace(" ", "-")


def _source_url_from(meta: dict[str, Any]) -> str:
    for key in _SOURCE_URL_KEYS:
        v = meta.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def load_company(path: Path) -> Optional[CompanyEntry]:
    """Parse one ``Companies/<name>.md``. Returns ``None`` when the note is not
    a watched public company (wrong/absent ``type``) or can't be parsed."""
    try:
        post = frontmatter.load(path)
    except Exception as e:  # noqa: BLE001 — a malformed note shouldn't kill the scan
        log.warning("earnings calendar: failed to parse %s (%s) — skipping", path, e)
        return None

    meta = dict(post.metadata) if isinstance(post.metadata, dict) else {}
    if str(meta.get("type") or "").strip().lower() != PUBLIC_COMPANY_TYPE:
        return None

    cadence = str(meta.get("reporting-cadence") or "quarterly").strip().lower() or "quarterly"
    consensus = meta.get("consensus")
    if not isinstance(consensus, dict):
        consensus = {}

    return CompanyEntry(
        path=path,
        name=str(meta.get("name") or path.stem).strip() or path.stem,
        ticker=str(meta.get("ticker") or "").strip(),
        next_reporting_date=_coerce_date(meta.get("next-reporting-date")),
        sector=sector_slug(meta.get("sector")),
        consensus_source=str(meta.get("consensus-source") or "").strip(),
        consensus=consensus,
        cadence=cadence,
        source_url=_source_url_from(meta),
        metadata=meta,
    )


def scan_watched(companies_dir: Path) -> list[CompanyEntry]:
    """All ``type: public-company`` notes under ``companies_dir`` (any date)."""
    if not companies_dir.is_dir():
        log.info("earnings calendar: %s not a directory — nothing to scan", companies_dir)
        return []
    out: list[CompanyEntry] = []
    for path in sorted(companies_dir.glob("*.md")):
        if path.name.startswith("_"):   # _template.md and similar are not companies
            continue
        entry = load_company(path)
        if entry is not None:
            out.append(entry)
    return out


def scan_due(companies_dir: Path, today: date, *, include_overdue: bool = True) -> list[CompanyEntry]:
    """Watched companies whose ``next-reporting-date`` is due on/before ``today``.

    ``include_overdue=True`` (the default) returns companies whose date is in the
    past too — that's the catch-up behaviour: a missed fire (bridge down, or the
    announcement hadn't dropped yet) is re-picked-up on a later run because the
    frontmatter date is only rolled forward once a capture *succeeds*. With
    ``include_overdue=False`` only an exact ``== today`` match is returned."""
    out: list[CompanyEntry] = []
    for entry in scan_watched(companies_dir):
        d = entry.next_reporting_date
        if d is None:
            continue
        if d == today or (include_overdue and d < today):
            out.append(entry)
    return out


__all__ = [
    "PUBLIC_COMPANY_TYPE",
    "CompanyEntry",
    "load_company",
    "scan_watched",
    "scan_due",
    "sector_slug",
]
