"""Stage 4 — review gate for the digest crew (#ingest-digest).

A DETERMINISTIC completeness check over the stage-3 ``SynthesisResult`` (no LLM,
no maths). Verifies three things and returns a ``ReviewResult``:

  * PROVENANCE — every extracted fact carries provenance (the source doc +
    locator the analyzer stamps). An uncited fact is a BLOCKING issue
    (``passed=False``) so the deferred emit stage (5) can refuse to write it to
    the vault — "fail the run on uncited claims" (#ingest-digest design §6).
  * ENTITY RESOLUTION — every fused entity resolves to an existing vault note or
    is flagged NEW. Resolution is against a caller-supplied set of NORMALISED
    known-note keys (the crew builds it from a one-time, filenames-only vault
    scan); an empty/None set => everything is "new" — a valid degraded mode with
    no vault. INFORMATIONAL, not blocking (a new deal legitimately has new
    entities).
  * ORPHAN SUBJECTS — fact subjects not among the fused entities (may include
    events, e.g. "Phase I bids", not only entities). INFORMATIONAL.

stdlib + pydantic only (the package docstring) so the bridge suite loads it by
file path. Name matching reuses ``synthesize.norm_key`` for parity with how
fusion + contradiction grouping normalise names. Unlike ``synthesize_facts``
(which carries an optional ``narrate_fn`` LLM seam), ``review_digest`` takes NO
model seam at all — its determinism is structural, not incidental.
"""

from __future__ import annotations

from _shared.digest.models import ReviewResult, SynthesisResult
from _shared.digest.synthesize import norm_key


def review_digest(
    synthesis: SynthesisResult,
    *,
    known_entities: set[str] | None = None,
) -> ReviewResult:
    """Run the deterministic completeness gate over a ``SynthesisResult``.

    ``known_entities`` is a set of names that resolve to an existing vault note;
    they are ``norm_key``-normalised INSIDE this function (so a caller may pass
    raw note stems — resolution never depends on the caller normalising first).
    None/empty => every fused entity is flagged new (the degraded, vault-free
    mode). Order-stable (follows synthesis order)."""
    known = {norm_key(k) for k in (known_entities or set())}

    # 1. Provenance — an uncited fact is the BLOCKING gate failure. (synthesis.
    #    facts already excludes error docs, so these are the emit candidates.)
    uncited: list[str] = [
        f"{f.subject} | {f.field} = {f.value}"
        for f in synthesis.facts
        if not (f.provenance or "").strip()
    ]

    # 2. Entity resolution — known vault note vs new (informational).
    new_entities: list[str] = [
        e.name for e in synthesis.entities if norm_key(e.name) not in known
    ]

    # 3. Orphan subjects — fact subjects not among the fused entities
    #    (informational; may include events). De-duped, first-seen order.
    entity_keys = {norm_key(e.name) for e in synthesis.entities}
    orphan_subjects: list[str] = []
    seen: set[str] = set()
    for f in synthesis.facts:
        k = norm_key(f.subject)
        # f.subject is non-empty by construction (the analyzer drops any fact
        # whose subject/field/value aren't all present), so k is normally truthy;
        # the `k and` guard just avoids emitting an empty-string orphan in a
        # degenerate case, never hides a real subject.
        if k and k not in entity_keys and k not in seen:
            seen.add(k)
            orphan_subjects.append(f.subject)

    return ReviewResult(
        passed=not uncited,
        uncited=uncited,
        new_entities=new_entities,
        orphan_subjects=orphan_subjects,
    )


__all__ = ["review_digest"]
