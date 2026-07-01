"""Public/private routing classifier — the SAFETY-CRITICAL core of the digest
crew (#ingest-digest, operator decision 2: SPLIT routing).

> **A misclassified MNPI/confidential doc must NEVER be eligible for a cloud
> lane** (CLAUDE.md §4 / vault constitution [no-mnpi-to-cloud]). When in any
> doubt, the answer is LOCAL.

The classifier decides, per doc, whether it is *verified-public* (a published
filing / published transcript — the only class operator decision 2 lets reach a
cloud lane later) versus everything-else (always local). It is a PURE,
deterministic function of (the doc's inventory record, a bounded text sample) —
no LLM, no network — so it is fully reproducible and unit-testable in the bridge
venv.

Three fail-closed asymmetries make the default LOCAL:

  1. **Private signals are believed from anywhere; public claims require
     content proof.** A confidentiality marker in the *filename OR the text*
     forces ``verified_public=False``. A published-document marker only counts
     when found in the extracted *content* — filenames lie (a CIM can be saved
     as ``ACME_10-K.pdf``), so a filename alone can never OPEN the cloud gate,
     only close it.
  2. **Too little to read ⇒ cannot verify ⇒ private.** A scanned/encrypted/
     empty doc (no extractable text) can't be confirmed public, so it fails
     closed regardless of its name.
  3. **Project-context tier vetoes.** Even a textbook-public filing sitting in
     a ``confidential``/``MNPI`` deal pile is NOT cloud-eligible — the deal
     context dominates the doc's intrinsic class (a leaked draft 10-K in an
     MNPI data room is still MNPI).

And one slice-level backstop on top of all that: **cloud routing is NOT wired
in this slice.** ``effective_lane`` is ALWAYS ``"local"``; ``cloud_eligible`` is
computed (so the seam + its tests are real) but never acted on. Flipping that
switch is the operator-gated cloud-routing follow-up — and the reason this
module wants careful review before that happens.

Signal lists are OUR OWN fixed phrases (never the untrusted doc content), so the
matched markers are safe to surface in ``RoutingDecision.reason`` / ``signals``
for review + audit — same deal-name-hygiene stance as the injection guard
(``routines/guards/injection/scan.py`` records the matched ANTON keyword, never
the text).
"""

from __future__ import annotations

from _shared.digest.models import DocCandidate, RoutingDecision

# SLICE GUARD (#ingest-digest stage 1-2): cloud routing is not wired. The
# classifier still COMPUTES cloud_eligible so the seam + its tests exist, but
# effective_lane is pinned local until the operator-gated cloud-routing
# follow-up. Do NOT flip this to enable cloud without that review.
SLICE_FORCES_LOCAL = True

# Minimum extracted-text length before a "published document" claim is even
# considered. Below this we have effectively nothing to verify against, so we
# fail closed (asymmetry #2). Tuned generously low — a real filing/transcript
# yields tens of KB; this only catches the empty/scanned/encrypted case.
MIN_VERIFY_CHARS = 400

# Confidentiality / restricted-distribution markers. ANY hit (filename OR text)
# forces verified_public=False. Lowercased; substring match. Conservative by
# design — a false "private" only costs a cloud-lane opportunity (the doc still
# gets digested locally), whereas a false "public" is the failure this whole
# module exists to prevent.
PRIVATE_MARKERS: tuple[str, ...] = (
    "confidential",
    "strictly private",
    "private and confidential",
    "private & confidential",
    "information memorandum",
    "confidential information memorandum",
    "cim",
    "teaser",
    "data room",
    "dataroom",
    "non-disclosure",
    "nondisclosure",
    "nda",
    "mnpi",
    "material non-public",
    "material nonpublic",
    "inside information",
    "not for distribution",
    "do not distribute",
    "not for circulation",
    "internal use only",
    "for internal use",
    "proprietary and confidential",
    "draft - confidential",
    "project ",          # deal codenames ("Project Falcon") — presumptively private
    "under embargo",
    "embargoed",
    "restricted",
)

# Published-document markers. Counted ONLY in extracted content (not filename).
# Presence of one of these — with NO private marker anywhere AND enough text —
# is what lets a doc be verified-public.
#
# HIGH-CONFIDENCE ONLY (codex-5.5 xhigh SEV-1, 2026-06-13, ×2): every entry must
# be a phrase very unlikely to appear in a confidential deal document but
# characteristic of a published filing / transcript. Weak standalone phrases were
# REMOVED — "published by" (appears anywhere), "investor relations" (CIMs
# describe the target's IR function), "edgar" (a common given name), "prepared
# remarks" (board decks use it), and "for immediate release" (the standard press-
# release header, present even on unpublished/internal DRAFTS) — because any one
# of them could flip an unrelated doc to cloud-eligible, breaking fail-closed.
# What remains are SEC-filing boilerplate + explicit published-transcript phrases.
PUBLIC_MARKERS: tuple[str, ...] = (
    "form 10-k",
    "form 10-q",
    "form 8-k",
    "annual report on form",
    "quarterly report on form",
    "securities and exchange commission",
    "u.s. securities and exchange commission",
    "pursuant to section 13 or 15(d)",
    "earnings call transcript",
    "conference call transcript",
    "this transcript is provided",
)


def _matched(markers: tuple[str, ...], haystack: str) -> list[str]:
    """The subset of ``markers`` present in ``haystack`` (lowercased,
    deduped, stable order). Returns OUR phrases — safe to record."""
    return [m for m in markers if m in haystack]


def classify_doc(candidate: DocCandidate, text_sample: str) -> RoutingDecision:
    """Decide the routing for one doc. FAIL CLOSED to local on any doubt.

    Args:
        candidate: the inventory record (type, sensitivity hint from project
            context, support flag, filename).
        text_sample: a bounded sample of the doc's extracted text (the analyzer
            extracts this once and passes it in). May be ``""`` when the doc had
            no extractable text — which is itself a fail-closed signal.

    Returns a :class:`RoutingDecision`. ``effective_lane`` is ALWAYS ``"local"``
    in this slice (see ``SLICE_FORCES_LOCAL``)."""
    sens = candidate.sensitivity_hint
    filename_l = candidate.filename.lower()
    text_l = (text_sample or "").lower()

    signals: list[str] = []

    # Asymmetry #1 (close side): private markers from filename OR content.
    private_hits = _matched(PRIVATE_MARKERS, filename_l + "\n" + text_l)
    # Asymmetry #1 (open side): public markers from CONTENT ONLY.
    public_hits = _matched(PUBLIC_MARKERS, text_l)
    enough_text = len((text_sample or "").strip()) >= MIN_VERIFY_CHARS

    # Build the determination, narrowest-true-last so ``reason`` names the
    # binding constraint. Every path that isn't an explicit, content-proven,
    # private-marker-free, well-read public doc resolves to NOT public.
    if not candidate.supported or candidate.doc_type == "unknown":
        verified_public = False
        reason = f"unsupported/unknown doc type ({candidate.doc_type!r}) — fail closed to local"
    elif private_hits:
        verified_public = False
        signals = [f"private:{m.strip()}" for m in private_hits]
        reason = "confidentiality marker(s) present — fail closed to local"
    elif not enough_text:
        verified_public = False
        reason = (
            f"insufficient extractable text ({len((text_sample or '').strip())} "
            f"< {MIN_VERIFY_CHARS} chars) — cannot verify public, fail closed to local"
        )
    elif not public_hits:
        verified_public = False
        reason = "no published-document signal in content — uncertain, fail closed to local"
    else:
        verified_public = True
        signals = [f"public:{m}" for m in public_hits]
        reason = "verified-public signal in content with no confidentiality markers"

    # Asymmetry #3: project-context tier vetoes cloud eligibility. Only the two
    # lowest tiers may EVER be cloud-eligible; confidential/MNPI never are, even
    # for a textbook-public doc. (And cloud_eligible always implies
    # verified_public.)
    context_permits_cloud = sens in ("public", "internal")
    cloud_eligible = verified_public and context_permits_cloud
    if verified_public and not context_permits_cloud:
        reason += f"; but context tier {sens!r} vetoes cloud eligibility → local"

    # Slice backstop: cloud is not wired — everything runs local regardless of
    # cloud_eligible. This is the seam the cloud-routing follow-up replaces.
    effective_lane = "local"
    if SLICE_FORCES_LOCAL and cloud_eligible:
        reason += "; cloud routing not wired in this slice → effective lane local"

    return RoutingDecision(
        verified_public=verified_public,
        cloud_eligible=cloud_eligible,
        effective_lane=effective_lane,
        doc_sensitivity=sens,
        reason=reason,
        signals=signals,
    )


__all__ = [
    "SLICE_FORCES_LOCAL",
    "MIN_VERIFY_CHARS",
    "PRIVATE_MARKERS",
    "PUBLIC_MARKERS",
    "classify_doc",
]
