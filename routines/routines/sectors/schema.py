"""Schemas for the sector expertise layer routines.

Plan v3 §6.9 decisions B1-B17 are the source of truth; see
`Topics/Architecture/sector-expertise.md` in the vault for the full
specification of frontmatter fields, weights, and confidence formula.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date as date_cls
from datetime import datetime
from typing import Literal


# Source-type weight table per Plan v3 §6.9 B5 (revised — project-lessons = 3).
# Used by the synthesizer (Phase 4) to compute weighted_independence.
SOURCE_WEIGHTS: dict[str, int] = {
    "cim": 3,
    "dd-report": 3,
    "vdr-doc": 3,
    "project-lessons": 3,         # revised B5: promoted from 2 to 3
    "meeting": 2,
    "research": 2,
    "manual": 2,
    "bd": 2,
    "newsletter": 1,
}

# Eight canonical claim types per B2.
CLAIM_TYPES: tuple[str, ...] = (
    "dynamics", "metrics", "valuation", "buyers",
    "issues", "comps", "competitive", "regulatory",
)

# Confidence tier names per B6.
ConfidenceTier = Literal["high", "medium", "low"]

# Provenance source types per file naming convention.
SourceType = Literal[
    "project", "newsletter", "meeting", "research", "bd", "manual",
]


@dataclass
class SectorExtract:
    """One claim extracted from a source toward a sector's claim files.

    The unit of extraction. Multiple extracts roll up into one provenance
    `_sources/from-<source-type>.md` entry per source root.
    """
    sector: str                                 # slug, e.g. "telecoms"
    source_type: SourceType
    source_path: str                            # relative to vault root
    source_root: str                            # canonical root for independence dedup
                                                # (deal_code, publisher_id, person_name, ...)
    claim_targets: list[str]                    # which claim files this informs
                                                # e.g. ["Valuation", "Buyers"]
    subsectors: list[str] = field(default_factory=list)
                                                # per claim — may be [_all]
    bullets: list[str] = field(default_factory=list)
                                                # the extracted claim text
                                                # (paraphrased, never verbatim)
    sensitivity: str = "confidential"           # B8: defaults to confidential
    extracted_on: date_cls | None = None
    extracted_by: str = ""                      # e.g. "sector-extract from-projects"
    extracted_commit: str | None = None         # vault HEAD at extraction time
    confidence_hint: ConfidenceTier | None = None
                                                # operator-provided; synthesizer recalibrates

    def weight(self) -> int:
        """Source-type weight per Plan v3 §6.9 B5."""
        type_map = {
            "project": "project-lessons",
            "meeting": "meeting",
            "research": "research",
            "bd": "bd",
            "newsletter": "newsletter",
            "manual": "manual",
        }
        return SOURCE_WEIGHTS.get(type_map.get(self.source_type, ""), 1)


@dataclass
class SectorProposal:
    """One run's output — proposals for operator review.

    Written to `Routines/sector-extraction/<date>-<sector>.md` with
    `status: pending-review`. Operator applies via REVIEW chip; the
    extracts then flow into `_sources/from-*.md` provenance files.
    """
    sector: str
    generated_at: datetime
    source_types_run: list[SourceType] = field(default_factory=list)
    extracts: list[SectorExtract] = field(default_factory=list)
    inputs_scanned: int = 0                     # files walked
    inputs_matched: int = 0                     # files that produced extracts
    skipped_reasons: dict[str, int] = field(default_factory=dict)
                                                # e.g. {"no-sector-tag": 5}
    markdown_path: str | None = None
    run_id: str = ""

    @property
    def extract_count(self) -> int:
        return len(self.extracts)


# --- Helpers ------------------------------------------------------------

def slugify_sector(name: str) -> str:
    """Title-case profile entry → lowercase-hyphenated folder slug.

    Per Plan v3 §6.9 B4: profile uses title-case for readability; folder
    slug + frontmatter `sector:` field are the lowercased entry.
    Examples:
        "Travel" → "travel"
        "Real Estate" → "real-estate"
        "Hospitality" → "hospitality"
    """
    return name.strip().lower().replace(" ", "-").replace("_", "-")


def slugify_subsector(descriptor: str) -> list[str]:
    """Reduce a sector_sub_lens descriptor to one or more sub-sector slugs.

    Lossy heuristic — operator may want to override per claim file.
    Examples:
        "Hotels (full-service / limited-service / lifestyle / boutique)"
        → ["hotels-full-service", "hotels-limited-service",
           "hotels-lifestyle", "hotels-boutique"]

        "Telecoms: Mobile operators (MNOs), MVNOs, and adjacencies"
        → ["mobile-operators", "mvnos", "adjacencies"]

    Returns one or more lowercased-hyphenated slugs derived from the
    descriptor. Caller deduplicates against any existing taxonomy.
    """
    # Heuristic — implementation deferred to Phase 4 synthesizer
    # since slug taxonomy is operator-curated, not routine-generated.
    # Routines just consume `subsectors:` frontmatter lists as-is.
    return []
