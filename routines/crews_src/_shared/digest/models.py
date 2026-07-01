"""Data shapes for the digest crew's stage 1-2 slice (#ingest-digest).

Pydantic models so the crew can serialise per-doc structures across the
JSON-over-stdio boundary (and the bridge / CLI can re-validate them). stdlib +
pydantic ONLY — no metagpt, no pypdfium2 — see the package docstring.

The fact triple (``subject`` / ``field`` / ``value``) is deliberately the same
shape ``routines/recall/retrieve.py::apply_contradiction_penalty`` consumes
(#54-contradiction, see ``routines/recall/CONTRADICTION-NOTES.md``): a digested
claim that lands in the vault later (stage 5, deferred) is then directly
comparable for the contradiction sweep — the digest crew becomes the structured
*producer* the narrow detector has been waiting for.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# ── enums ────────────────────────────────────────────────────────────────────

DocType = Literal["pdf", "docx", "md", "unknown"]

# Per-doc sensitivity tier, same vocab as the platform's CLAUDE.md §4 tiers.
# The scanner pre-classifies from PROJECT CONTEXT (the deal's workspace tier);
# it is NOT a content judgement — that is the classifier's job.
Sensitivity = Literal["public", "internal", "confidential", "MNPI"]

# What an analyzer extracts. ``claim`` / ``date`` / ``number`` are all carried
# as ``subject``/``field``/``value`` triples; ``entity`` names also live in
# ``DocAnalysis.entities`` for convenience.
FactKind = Literal["entity", "claim", "date", "number"]

# The lane stage-2 enrichment ACTUALLY runs on. v1 slice: always ``"local"``
# (cloud routing is not wired — see ``classifier`` + the deferred seam). The
# ``"cloud"`` literal exists so attaching cloud routing later is a value change,
# not a contract rev.
EffectiveLane = Literal["local", "cloud"]


# ── stage 1: scan + classify ──────────────────────────────────────────────────


class RoutingDecision(BaseModel):
    """The public/private classifier's verdict for one doc — the
    SAFETY-CRITICAL output of stage 1 (operator decision 2, SPLIT routing).

    ``cloud_eligible`` records whether the doc COULD use a cloud lane in the
    future; ``effective_lane`` records what it runs on TODAY. In this slice
    ``effective_lane`` is ALWAYS ``"local"`` — cloud routing is not wired — so a
    ``cloud_eligible=True`` doc still runs local. That gap is the seam the
    operator-gated cloud-routing follow-up attaches to (and the reason this
    classifier needs careful review before that switch is flipped)."""

    verified_public: bool          # strong published-doc signal AND no private signal
    cloud_eligible: bool           # verified_public AND context tier permits cloud
    effective_lane: EffectiveLane  # what enrichment runs on — "local" in this slice, always
    doc_sensitivity: Sensitivity   # the tier this decision was taken under
    reason: str                    # human-readable rationale (audited; deal-name-free)
    signals: list[str] = Field(default_factory=list)  # which cues fired (for review)


class DocCandidate(BaseModel):
    """One inventoried file from the drop dir (stage 1)."""

    path: str
    filename: str
    doc_type: DocType
    size_bytes: int
    content_sha256: str = ""            # "" when the file could not be hashed
    sensitivity_hint: Sensitivity       # pre-classification from project context
    is_duplicate: bool = False
    duplicate_of: str | None = None     # path of the first file with this hash
    supported: bool = True              # False for unknown/again-unsupported types
    routing: RoutingDecision | None = None  # filled by the classifier (stage 1b)


class ScanResult(BaseModel):
    """Stage-1 inventory of a drop dir."""

    drop_dir: str
    project: str
    project_sensitivity: Sensitivity
    candidates: list[DocCandidate] = Field(default_factory=list)
    total_files: int = 0
    unique_docs: int = 0
    duplicates: int = 0
    unsupported: int = 0


# ── stage 2: per-doc atomic-fact extraction ───────────────────────────────────


class AtomicFact(BaseModel):
    """One atomic fact pulled from a doc, carried as a ``subject``/``field``/
    ``value`` triple (#54-contradiction shape).

    Examples by ``kind``:
      * claim  — subject="Project Falcon", field="audit_opinion", value="qualified"
      * number — subject="Project Falcon", field="revenue", value="142",
                  unit="GBP m", period="FY25"
      * date   — subject="Phase I bids", field="due_date", value="2026-06-30"
      * entity — subject="Acme Holdings Ltd", field="entity_type", value="company"
    """

    kind: FactKind
    subject: str
    field: str
    value: str
    unit: str = ""           # currency / unit for numbers (e.g. "GBP m", "%")
    period: str = ""         # period for numbers (e.g. "FY25", "LTM Mar 26")
    provenance: str = ""     # source doc + locator (page/§) — stamped per claim


class DocAnalysis(BaseModel):
    """Stage-2 result for a single doc."""

    path: str
    doc_type: DocType
    status: Literal["ok", "error", "skipped"] = "ok"
    routing: RoutingDecision | None = None
    chars_extracted: int = 0
    enriched: bool = False               # True if the LLM enrichment ran
    entities: list[str] = Field(default_factory=list)
    facts: list[AtomicFact] = Field(default_factory=list)
    error: str = ""


# ── stage 3: cross-doc synthesis ───────────────────────────────────────────────


class FusedEntity(BaseModel):
    """One entity after cross-doc fusion (stage 3). ``name`` is the canonical
    display name (first-seen original casing); ``mentions`` counts the DISTINCT
    docs that named it; ``doc_paths`` lists them; ``wikilink`` is the vault-link
    form the emit stage drops into notes."""

    name: str
    wikilink: str
    mentions: int = 1
    doc_paths: list[str] = Field(default_factory=list)


class ContradictionEntry(BaseModel):
    """One side of a contradiction — an asserted value and where it came from."""

    value: str
    unit: str = ""
    period: str = ""
    provenance: str = ""


class Contradiction(BaseModel):
    """≥2 docs (or one doc) asserting DIFFERENT values for the SAME comparable
    attribute of the same subject (stage 3). Detection is DETERMINISTIC over the
    #54 triples: group by ``(subject, field)``; a ``number`` fact only contradicts
    another at the SAME ``unit`` + ``period`` (FY24 vs FY25, or GBP vs USD, are
    distinct facts — not a contradiction). The reviewer (stage 4) and operator
    see every divergent value + its provenance. Authoring-time and date-free —
    unlike recall's query-time ``apply_contradiction_penalty`` (see
    ``recall/CONTRADICTION-NOTES.md``); the emitted notes carry the
    subject/field/value frontmatter that detector consumes."""

    subject: str
    field: str
    unit: str = ""
    period: str = ""
    entries: list[ContradictionEntry] = Field(default_factory=list)


class SynthesisResult(BaseModel):
    """Stage-3 output: entity-keyed fusion + contradiction surfacing + the
    wikilink web over the per-doc ``DocAnalysis`` list. The deterministic core
    (``entities`` / ``facts`` / ``contradictions``) is reproducible; ``narrative``
    is an OPTIONAL local-Ollama cross-doc summary (``""`` when not generated)."""

    project: str = ""
    entities: list[FusedEntity] = Field(default_factory=list)
    facts: list[AtomicFact] = Field(default_factory=list)
    contradictions: list[Contradiction] = Field(default_factory=list)
    narrative: str = ""


# ── stage 4: review gate ───────────────────────────────────────────────────────


class ReviewResult(BaseModel):
    """Stage-4 completeness-gate verdict over the stage-3 synthesis. ``passed``
    is False iff there are UNCITED facts (the blocking issue — the deferred emit
    refuses uncited claims). ``new_entities`` (no matching vault note) and
    ``orphan_subjects`` (fact subjects not in the fused entity set — these may
    include events, not just entities) are INFORMATIONAL, not blocking."""

    passed: bool = True
    uncited: list[str] = Field(default_factory=list)
    new_entities: list[str] = Field(default_factory=list)
    orphan_subjects: list[str] = Field(default_factory=list)


# ── whole-slice result ─────────────────────────────────────────────────────────


class DigestSliceResult(BaseModel):
    """What the digest crew produces. Stages 1-2 (scan + per-doc analysis) plus,
    as of #ingest-digest stages-345, stage 3 (cross-doc ``synthesis``) run inside
    the crew, which sets ``deferred_stages`` to the stages still NOT built
    (``["review", "emit"]``). The field DEFAULT stays the fully-deferred
    ``["synthesise", "review", "emit"]`` so parsing a pre-stage-3 payload that
    omits the field still reports synthesise as deferred (``synthesis`` is then
    ``None``)."""

    project: str
    scan: ScanResult
    analyses: list[DocAnalysis] = Field(default_factory=list)
    synthesis: SynthesisResult | None = None
    review: ReviewResult | None = None
    deferred_stages: list[str] = Field(
        default_factory=lambda: ["synthesise", "review", "emit"]
    )


__all__ = [
    "DocType",
    "Sensitivity",
    "FactKind",
    "EffectiveLane",
    "RoutingDecision",
    "DocCandidate",
    "ScanResult",
    "AtomicFact",
    "DocAnalysis",
    "FusedEntity",
    "ContradictionEntry",
    "Contradiction",
    "SynthesisResult",
    "ReviewResult",
    "DigestSliceResult",
]
