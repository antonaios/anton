"""Stage 2 — parallel per-doc analyzers for the digest crew (#ingest-digest).

Per doc: deterministic text extraction (``extract``) → public/private routing
(``classifier``) → LOCAL-Ollama enrichment into ``subject``/``field``/``value``
atomic facts. Docs run concurrently under an ``asyncio.Semaphore`` (operator
decision: "bounded concurrency") — ``concurrency`` caps how many enrichment LLM
calls are in flight at once. Async, because the live crew's MetaGPT roles call
``self.llm.aask`` natively async; the blocking text extraction is pushed to a
thread so it parallelises too without stalling the loop.

The LLM only ever TRANSCRIBES — entities/claims/dates, and numbers copied
verbatim with their currency + period. It does NOT calculate ([no-llm-maths],
vault constitution §5.1); the deterministic layer does no maths either, and the
prompt forbids arithmetic.

LANE SAFETY (defense in depth): enrichment refuses to run on anything but the
``"local"`` lane. In this slice ``RoutingDecision.effective_lane`` is always
``"local"`` (cloud is not wired — ``classifier.SLICE_FORCES_LOCAL``), so
``chat_fn`` is the local-Ollama adapter, full stop. The assertion is the seam:
when cloud routing lands, the caller selects ``chat_fn`` by ``effective_lane``
and this guard ensures a confidential/MNPI doc can never be handed a cloud
client even by a caller bug.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Awaitable, Callable

from _shared.digest.classifier import classify_doc
from _shared.digest.extract import DigestExtractError, extract_text
from _shared.digest.models import AtomicFact, DocAnalysis, DocCandidate

logger = logging.getLogger(__name__)

# chat_fn(system, prompt) -> raw model text (expected JSON), async. The
# semaphore is what bounds how many of these run concurrently.
ChatFn = Callable[[str, str], Awaitable[str]]
# extract_fn / classify_fn are injectable so tests drive analysis without real
# files or the pypdfium/docx deps.
ExtractFn = Callable[..., str]
ClassifyFn = Callable[[DocCandidate, str], Any]

# Default parallel analyzers. Local Ollama serialises inference on one GPU, so a
# small bound keeps the queue shallow without flooding; operator-tunable.
DEFAULT_CONCURRENCY = 3

_VALID_KINDS = {"entity", "claim", "date", "number"}

_SYSTEM_PROMPT = """\
You are a deal-document analyst extracting ATOMIC FACTS from a single document.

Return ONLY a JSON object with this shape:
{
  "entities": ["Legal or trading names of companies/people/funds mentioned"],
  "facts": [
    {"kind": "claim",  "subject": "<entity the claim is about>", "field": "<attribute>", "value": "<asserted value>", "unit": "", "period": "", "locator": "p.<n> or section"},
    {"kind": "number", "subject": "<entity>", "field": "revenue", "value": "142", "unit": "GBP m", "period": "FY25", "locator": "p.<n>"},
    {"kind": "date",   "subject": "<event>", "field": "due_date", "value": "2026-06-30", "locator": "p.<n>"}
  ]
}

Rules:
- Output VALID JSON only — no markdown fences, no prose.
- Every fact is a (subject, field, value) triple. subject = the entity the fact
  is ABOUT; field = the attribute; value = the asserted value.
- Numbers: copy the figure, currency and period EXACTLY as written. Do NOT
  convert currencies, compute ratios, sum, or otherwise calculate anything.
- Dates: ISO 8601 (YYYY-MM-DD) when the doc gives a full date; else copy as written.
- If the document states nothing extractable, return {"entities": [], "facts": []}.
- Do NOT invent facts, sources, or values not present in the text.
"""


def _loads_lenient(content: str) -> dict | None:
    """Parse a JSON object from model output, tolerating a fence / prose
    preamble (a crew-side trim of ``ollama_client.parse_json_response``, which
    can't be imported across the venv boundary).

    Returns the parsed dict (possibly empty ``{}`` for a model that legitimately
    found nothing) on success, or ``None`` on a PARSE FAILURE. The caller must
    distinguish the two (codex-5.5 SEV-2): a parse failure is an enrichment
    failure, not a successful zero-fact analysis."""
    s = (content or "").strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else s
        if s.endswith("```"):
            s = s.rsplit("```", 1)[0]
        s = s.strip()
    if not s.startswith("{"):
        i, j = s.find("{"), s.rfind("}")
        if i == -1 or j == -1 or j < i:
            return None
        s = s[i : j + 1]
    try:
        out = json.loads(s)
    except json.JSONDecodeError:
        return None
    return out if isinstance(out, dict) else None


def _facts_from_payload(payload: dict, *, source_path: str) -> tuple[list[str], list[AtomicFact]]:
    """Validate the model payload into entities + AtomicFacts. Malformed facts
    are dropped (never crash the doc). Provenance is stamped DETERMINISTICALLY
    with the source path; the model's locator is appended when present — the
    source doc is never taken on the model's word."""
    entities: list[str] = []
    for e in payload.get("entities") or []:
        if isinstance(e, str) and e.strip():
            entities.append(e.strip())

    facts: list[AtomicFact] = []
    for raw in payload.get("facts") or []:
        if not isinstance(raw, dict):
            continue
        subject = str(raw.get("subject") or "").strip()
        field = str(raw.get("field") or "").strip()
        value = str(raw.get("value") or "").strip()
        if not (subject and field and value):
            continue  # an incomplete triple is not a fact
        kind = raw.get("kind")
        kind = kind if kind in _VALID_KINDS else "claim"
        locator = str(raw.get("locator") or "").strip()
        provenance = source_path + (f" {locator}" if locator else "")
        facts.append(AtomicFact(
            kind=kind,  # type: ignore[arg-type]
            subject=subject,
            field=field,
            value=value,
            unit=str(raw.get("unit") or "").strip(),
            period=str(raw.get("period") or "").strip(),
            provenance=provenance,
        ))
    return entities, facts


def _user_prompt(candidate: DocCandidate, text: str) -> str:
    return (
        f"Document: {candidate.filename} (type {candidate.doc_type}).\n\n"
        f"--- begin extracted text ---\n{text}\n--- end extracted text ---\n\n"
        "Extract entities + atomic facts per the schema. JSON only."
    )


async def analyze_one(
    candidate: DocCandidate,
    *,
    chat_fn: ChatFn,
    extract_fn: ExtractFn = extract_text,
    classify_fn: ClassifyFn = classify_doc,
    max_chars: int = 12_000,
) -> DocAnalysis:
    """Analyze a single doc: extract → classify → (local) enrich.

    ``max_chars`` bounds the text handed to the model (the extractor already
    bounds the read far higher; this is the per-call prompt budget)."""
    try:
        text = await asyncio.to_thread(extract_fn, candidate.path, doc_type=candidate.doc_type)
    except DigestExtractError as e:
        # Couldn't read it at all — still classify (fail-closed on empty text)
        # so the routing decision exists, but mark the doc errored.
        routing = classify_fn(candidate, "")
        return DocAnalysis(
            path=candidate.path, doc_type=candidate.doc_type, status="error",
            routing=routing, chars_extracted=0, enriched=False, error=str(e),
        )

    routing = classify_fn(candidate, text)
    analysis = DocAnalysis(
        path=candidate.path, doc_type=candidate.doc_type,
        routing=routing, chars_extracted=len(text),
    )

    if not text.strip():
        analysis.status = "skipped"  # nothing to enrich
        return analysis

    # LANE SAFETY — defense in depth. The slice only ever produces "local";
    # refuse anything else rather than hand doc text to an unknown lane.
    lane = getattr(routing, "effective_lane", "local")
    if lane != "local":
        analysis.status = "error"
        analysis.error = (
            f"refusing enrichment: effective_lane={lane!r} is not 'local' and "
            "cloud routing is not wired in this slice"
        )
        return analysis

    # A doc-id for logs/errors that is NOT the filename (filenames can encode a
    # deal name — codex data-handling MED): the content hash prefix, else "?".
    doc_id = (candidate.content_sha256 or "")[:12] or "?"
    try:
        raw = await chat_fn(_SYSTEM_PROMPT, _user_prompt(candidate, text[:max_chars]))
        payload = _loads_lenient(raw)
        if payload is None:
            # Unparseable model output is an enrichment FAILURE, not a
            # successful empty extraction (codex-5.5 SEV-2). Keep the doc's
            # routing; mark it un-enriched with a content-free message.
            analysis.enriched = False
            analysis.error = "enrichment returned unparseable output"
            return analysis
        entities, facts = _facts_from_payload(payload, source_path=candidate.path)
        analysis.entities = entities
        analysis.facts = facts
        analysis.enriched = True
    except Exception as e:  # noqa: BLE001 — one bad LLM response must not lose
        # the doc's inventory + routing; record it and move on (partial).
        # Log + store the exception TYPE only — never the filename or str(e),
        # which can carry a deal name / raw content (codex data-handling MED).
        logger.warning("digest analyze: enrichment failed for doc %s: %s",
                       doc_id, type(e).__name__)
        analysis.enriched = False
        analysis.error = f"enrichment failed: {type(e).__name__}"
    return analysis


async def analyze_docs(
    candidates: list[DocCandidate],
    *,
    chat_fn: ChatFn,
    concurrency: int = DEFAULT_CONCURRENCY,
    extract_fn: ExtractFn = extract_text,
    classify_fn: ClassifyFn = classify_doc,
    max_chars: int = 12_000,
) -> list[DocAnalysis]:
    """Run the per-doc analyzers under a bounded semaphore, preserving input
    order in the result. ``concurrency`` is the max number of analyzers (and
    thus enrichment LLM calls) in flight at once."""
    if not candidates:
        return []
    sem = asyncio.Semaphore(max(1, concurrency))

    async def _bounded(c: DocCandidate) -> DocAnalysis:
        async with sem:
            return await analyze_one(
                c, chat_fn=chat_fn, extract_fn=extract_fn,
                classify_fn=classify_fn, max_chars=max_chars,
            )

    return list(await asyncio.gather(*(_bounded(c) for c in candidates)))


__all__ = [
    "ChatFn",
    "DEFAULT_CONCURRENCY",
    "analyze_one",
    "analyze_docs",
]
