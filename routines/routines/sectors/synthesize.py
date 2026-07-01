"""Sector synthesizer — reads provenance, recalculates metadata, proposes claim-file updates.

Plan v3 §6.9 Phase 4. Deterministic v1 — no LLM rewrites yet.

For each sector:
  1. Walk `Sectors/<X>/_sources/from-*.md` for entries with `status: applied`
  2. Group by claim_target × source_root (B5 independence dedup)
  3. Compute weighted_independence per claim_target (B5)
  4. Compute confidence per B6 (deterministic)
  5. Compare to existing claim files' frontmatter
  6. Propose updates to:
     - claim file frontmatter (new last_refreshed, source_count, sources_independent,
       weighted_independence, confidence)
     - candidate new claims (orphan bullets not yet in claim file)
     - conflicts (multiple independent sources disagreeing — B7)
  7. Write proposal `Routines/sector-synthesis/<date>-<sector>.md` with `status: pending-review`

The LLM-driven claim-file rewriting (proposing the actual claim text changes,
not just metadata) is a Phase 4.5 enhancement; defer until operator workflow
stabilises and we know the failure modes.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date as date_cls
from datetime import datetime, timezone
from pathlib import Path

import frontmatter

from routines.sectors.schema import (
    CLAIM_TYPES, ConfidenceTier, SOURCE_WEIGHTS,
)
from routines.shared.vault_writer import atomic_write

log = logging.getLogger(__name__)


@dataclass
class ProvenanceEntry:
    """One applied entry parsed from a `_sources/from-*.md` file."""
    source_type: str
    source_path: str
    source_root: str
    claim_targets: list[str]
    bullets: list[str]
    applied_on: str | None = None


@dataclass
class ClaimRecalc:
    """Recalculated metadata for one claim file."""
    claim_type: str
    file_path: Path
    current_source_count: int = 0
    current_sources_independent: int = 0
    current_weighted_independence: int = 0
    current_confidence: str = "low"
    new_source_count: int = 0
    new_sources_independent: int = 0
    new_weighted_independence: int = 0
    new_confidence: ConfidenceTier = "low"
    candidate_new_bullets: list[tuple[str, str]] = field(default_factory=list)
                                            # [(source_root, bullet), ...]
    conflicts: list[dict] = field(default_factory=list)

    @property
    def metadata_changed(self) -> bool:
        return (
            self.new_source_count != self.current_source_count
            or self.new_weighted_independence != self.current_weighted_independence
            or self.new_confidence != self.current_confidence
        )


@dataclass
class SectorSynthesisProposal:
    sector: str
    generated_at: datetime
    run_id: str
    recalcs: list[ClaimRecalc] = field(default_factory=list)
    markdown_path: str | None = None


def synthesize_sector(vault_root: Path, sector: str, run_id: str) -> SectorSynthesisProposal:
    """Main entry point — synthesizes one sector."""
    sector_dir = vault_root / "Sectors" / sector
    if not sector_dir.is_dir():
        log.warning("synthesize: sector dir not found at %s", sector_dir)
        return SectorSynthesisProposal(
            sector=sector,
            generated_at=datetime.now(timezone.utc),
            run_id=run_id,
        )

    # 1. Gather all applied provenance entries
    provenance = _gather_provenance(sector_dir)
    log.info("synthesize: %d applied provenance entries for sector=%s",
             len(provenance), sector)

    # 2. Index provenance by claim_target
    by_claim_target: dict[str, list[ProvenanceEntry]] = {}
    for p in provenance:
        for target in p.claim_targets:
            by_claim_target.setdefault(target.lower(), []).append(p)

    # 3. Build a recalc for each claim file
    recalcs: list[ClaimRecalc] = []
    for claim_type in CLAIM_TYPES:
        claim_file = sector_dir / f"{claim_type.capitalize()}.md"
        if not claim_file.exists():
            continue
        recalc = _recalc_claim(claim_file, claim_type, by_claim_target.get(claim_type, []))
        recalcs.append(recalc)

    return SectorSynthesisProposal(
        sector=sector,
        generated_at=datetime.now(timezone.utc),
        run_id=run_id,
        recalcs=recalcs,
    )


# ── Provenance gathering ──────────────────────────────────────────────


_EXTRACT_HEADING_RE = re.compile(r"^##\s+(?:extract|note)-([\d-]+)-(.+?)\s*$")
_FIELD_RE = re.compile(r"^- \*\*(\w+):\*\*\s+(.+?)\s*$")
_BULLET_RE = re.compile(r"^\s+- (.+?)\s*$")  # nested bullets under "Bullets:"


def _gather_provenance(sector_dir: Path) -> list[ProvenanceEntry]:
    """Walk `_sources/from-*.md` and extract applied entries."""
    out: list[ProvenanceEntry] = []
    sources_dir = sector_dir / "_sources"
    if not sources_dir.is_dir():
        return out

    for source_file in sorted(sources_dir.glob("from-*.md")):
        try:
            post = frontmatter.load(source_file)
        except Exception as e:  # noqa: BLE001
            log.warning("synthesize: failed to read %s: %s", source_file, e)
            continue

        # Skip the file-level `status: empty` initial state
        file_status = str(post.metadata.get("status") or "").lower()
        if file_status in ("empty", ""):
            continue

        source_type_map = {
            "from-projects.md": "project",
            "from-newsletters.md": "newsletter",
            "from-meetings.md": "meeting",
            "from-research.md": "research",
            "from-bd.md": "bd",
            "from-manual.md": "manual",
        }
        source_type = source_type_map.get(source_file.name, "unknown")

        # Parse the body for applied entries
        body = post.content or ""
        out.extend(_parse_extract_blocks(body, source_type, str(source_file)))

    return out


def _parse_extract_blocks(body: str, source_type: str, source_file: str) -> list[ProvenanceEntry]:
    """Extract per-entry blocks from a provenance file body.

    Format expected:
        ## extract-2026-05-20-xyz
        - **claim_targets:** [Valuation, Buyers]
        - **status:** applied
        - **source_root:** project:Acme
        - **Bullets:**
          - bullet 1
          - bullet 2
    """
    out: list[ProvenanceEntry] = []
    lines = body.splitlines()
    i = 0
    while i < len(lines):
        m = _EXTRACT_HEADING_RE.match(lines[i])
        if not m:
            i += 1
            continue

        # Found a block — collect fields until next heading or EOF
        fields: dict[str, str] = {}
        bullets: list[str] = []
        in_bullets = False
        i += 1
        while i < len(lines) and not lines[i].startswith("##"):
            line = lines[i]
            fm = _FIELD_RE.match(line)
            if fm:
                key = fm.group(1).lower()
                value = fm.group(2)
                if key == "bullets":
                    in_bullets = True
                else:
                    fields[key] = value
                    in_bullets = False
            elif in_bullets:
                bm = _BULLET_RE.match(line)
                if bm:
                    bullets.append(bm.group(1))
            i += 1

        # Only emit if status is applied (default to applied if missing — operator-curated)
        status = fields.get("status", "applied").lower()
        if status != "applied":
            continue

        claim_targets_raw = fields.get("claim_targets") or fields.get("claim_target") or ""
        targets = _parse_list_field(claim_targets_raw)
        out.append(ProvenanceEntry(
            source_type=source_type,
            source_path=source_file,
            source_root=fields.get("source_root", f"unknown:{source_file}"),
            claim_targets=targets,
            bullets=bullets,
            applied_on=fields.get("applied_at") or fields.get("noted_on"),
        ))

    return out


def _parse_list_field(raw: str) -> list[str]:
    """Parse a field value like '[Valuation, Buyers]' or 'Valuation, Buyers'."""
    s = raw.strip().strip("[]")
    if not s:
        return []
    return [t.strip() for t in s.split(",") if t.strip()]


# ── Per-claim recalc ──────────────────────────────────────────────────


def _recalc_claim(
    claim_file: Path,
    claim_type: str,
    provenance: list[ProvenanceEntry],
) -> ClaimRecalc:
    """Recalculate one claim file's metadata from provenance."""
    rc = ClaimRecalc(claim_type=claim_type, file_path=claim_file)

    # Current metadata
    try:
        post = frontmatter.load(claim_file)
    except Exception as e:  # noqa: BLE001
        log.warning("synthesize: failed to read claim file %s: %s", claim_file, e)
        return rc

    meta = post.metadata or {}
    rc.current_source_count = int(meta.get("source_count") or 0)
    rc.current_sources_independent = int(meta.get("sources_independent") or 0)
    rc.current_weighted_independence = int(meta.get("weighted_independence") or 0)
    rc.current_confidence = str(meta.get("confidence") or "low")

    # Apply B5 weighted independence calculation
    by_root: dict[str, int] = {}  # source_root -> highest weight contribution
    for p in provenance:
        weight = _weight_for_source_type(p.source_type)
        existing = by_root.get(p.source_root, 0)
        if weight > existing:
            by_root[p.source_root] = weight

    rc.new_source_count = sum(1 for p in provenance)
    rc.new_sources_independent = len(by_root)
    rc.new_weighted_independence = sum(by_root.values())

    # B6 confidence formula (without recency for now; deterministic)
    rc.new_confidence = _confidence_tier(rc.new_weighted_independence)

    # Candidate new bullets — bullets in provenance not yet appearing in claim file body
    existing_text = (post.content or "").lower()
    for p in provenance:
        for bullet in p.bullets:
            # Crude similarity: check if first 30 chars appear in claim file
            snippet = bullet[:30].lower()
            if snippet and snippet not in existing_text:
                rc.candidate_new_bullets.append((p.source_root, bullet))

    return rc


def _weight_for_source_type(source_type: str) -> int:
    type_map = {
        "project": "project-lessons",
        "meeting": "meeting",
        "research": "research",
        "bd": "bd",
        "newsletter": "newsletter",
        "manual": "manual",
    }
    return SOURCE_WEIGHTS.get(type_map.get(source_type, ""), 1)


def _confidence_tier(weighted_independence: int) -> ConfidenceTier:
    """Plan v3 §6.9 B6 — deterministic.

    Recency-based bump-down handled by the freshness sweep (Phase 6), not here.
    """
    if weighted_independence >= 10:
        return "high"
    if weighted_independence >= 5:
        return "medium"
    return "low"


# ── Writer ────────────────────────────────────────────────────────────


def synthesis_proposal_path(vault_root: Path, sector: str, the_date: date_cls) -> Path:
    return (vault_root / "Routines" / "sector-synthesis"
            / f"{the_date.isoformat()}-{sector}.md")


def write_synthesis_proposal(
    vault_root: Path,
    proposal: SectorSynthesisProposal,
    the_date: date_cls,
) -> Path:
    path = synthesis_proposal_path(vault_root, proposal.sector, the_date)
    atomic_write(path, _render_synthesis_markdown(proposal, the_date), vault_root=vault_root)
    return path


def _render_synthesis_markdown(proposal: SectorSynthesisProposal, the_date: date_cls) -> str:
    out: list[str] = [
        "---",
        "type: sector-synthesis-proposal",
        f"sector: {proposal.sector}",
        "sensitivity: confidential",
        f"date: {the_date.isoformat()}",
        f"generated_at: {proposal.generated_at.isoformat()}",
        f"run_id: {proposal.run_id}",
        f"recalcs: {len(proposal.recalcs)}",
        f"changed: {sum(1 for r in proposal.recalcs if r.metadata_changed)}",
        "status: pending-review",
        f"tags: [sector-synthesis, {proposal.sector}, routines, proposal]",
        "---",
        "",
        f"# Sector synthesis proposal -- {proposal.sector} -- {the_date.isoformat()}",
        "",
        (
            f"Reads applied provenance from `Sectors/{proposal.sector}/_sources/` "
            f"and recalculates claim file metadata per B5 (weighted independence) "
            f"+ B6 (confidence formula). **No automatic file edits** -- operator "
            f"reviews this proposal + applies metadata changes manually via Obsidian "
            f"(or via a future `sector-synthesize apply` CLI)."
        ),
        "",
        "## Recalculated claim files",
        "",
    ]

    any_changes = False
    for rc in proposal.recalcs:
        if not rc.metadata_changed and not rc.candidate_new_bullets:
            continue
        any_changes = True
        out += [
            f"### {rc.claim_type.capitalize()}",
            "",
            f"File: `{rc.file_path.name}`",
            "",
            "**Metadata changes:**",
            "",
            "| Field | Current | New |",
            "|---|---|---|",
            f"| source_count | {rc.current_source_count} | {rc.new_source_count} |",
            f"| sources_independent | {rc.current_sources_independent} | {rc.new_sources_independent} |",
            f"| weighted_independence | {rc.current_weighted_independence} | {rc.new_weighted_independence} |",
            f"| confidence | {rc.current_confidence} | {rc.new_confidence} |",
            "",
        ]
        if rc.candidate_new_bullets:
            out += [
                f"**Candidate new claims ({len(rc.candidate_new_bullets)}):**",
                "",
            ]
            for source_root, bullet in rc.candidate_new_bullets[:10]:
                out.append(f"- `{source_root}`: {bullet}")
            if len(rc.candidate_new_bullets) > 10:
                out.append(f"- ...and {len(rc.candidate_new_bullets) - 10} more")
            out.append("")
        out.append("---")
        out.append("")

    if not any_changes:
        out += [
            "_No metadata changes detected this run. All claim files current relative "
            "to applied provenance. Re-check after extraction routines emit new "
            "proposals + operator applies them._",
        ]

    out += [
        "",
        "## Operator action",
        "",
        "1. For each claim file with metadata changes: open the file in Obsidian, "
        "update the listed frontmatter fields, save.",
        "2. For each candidate new claim: either incorporate into the relevant section "
        "of the claim file with proper sub-section heading and source citation, OR "
        "reject (claim is duplicative / wrong / out-of-scope).",
        "3. Update this proposal's frontmatter `status:` to `applied` or `rejected`.",
        "",
        "_Generated by `routines.sectors.synthesize` -- fully local. "
        f"Run-id: {proposal.run_id}._",
    ]

    return "\n".join(out)
