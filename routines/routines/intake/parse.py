"""LLM-driven structured extraction from an inbound PDF.

Two paths, picked automatically:

  * **Text path** — pypdfium2 extracts machine-readable text per page;
    qwen3:14b reads it as a single text block. Wins on financial-fact
    extraction when the PDF has clean text (most teasers, results decks,
    CIMs, IMs).

  * **Image path** — gemma4:e4b reads rendered page PNGs. Used when text
    extraction yields too little (scanned PDFs, image-only slide decks).
    Uniquely captures visual notes (charts, diagrams) the text path
    can't see.

The orchestrator ``ingest_pdf()`` picks the path based on a chars-per-page
threshold and batches large docs into chunks (default 6 pages per call) —
both models otherwise return empty payloads when given the full doc at
once, even when individual chunks parse cleanly.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from routines.intake.pdf_render import (
    DEFAULT_MAX_PAGES, ExtractedText, RenderedPage,
    extract_text_per_page, render_pdf_to_images, total_page_count,
)
from routines.intake.schema import (
    FinancialHighlight, ImageNote, ParsedDocument,
)
from routines.shared.ollama_client import (
    OllamaClient, OllamaError, parse_json_response,
)
from routines.shared.routing import lane_to_model, pick_lane
from routines.guards.injection import scan_ingested_text

log = logging.getLogger(__name__)


# Default models — exposed for tests / direct callers that bypass routing.
DEFAULT_TEXT_MODEL = "qwen3:14b"
DEFAULT_MULTIMODAL_MODEL = "gemma4:e4b"

# Path-selection threshold (avg chars per page). Below this on average,
# pypdfium2's text extraction is too sparse to trust — fall back to images.
DEFAULT_TEXT_THRESHOLD = 150

# Batch size for the parse call. 6 pages per batch is empirically the
# upper bound where both qwen3 and gemma4 reliably return non-empty
# structured JSON; 20 pages in a single call comes back empty.
DEFAULT_BATCH_SIZE = 6


# ── shared system-prompt template ────────────────────────────────────────


_PARSE_SCHEMA_DOC = """\
Return ONLY a JSON object matching this schema:

{
  "doc_kind": "teaser" | "cim" | "im" | "expert-deck" | "research-note" | "other",
  "target_descriptor": "anonymised name as written, e.g. 'Project Falcon'",
  "target_revealed_name": "real company name if explicitly named, else empty",
  "industry": "high-level industry",
  "sector": "sector within the industry",
  "subsector": "subsector / sub-vertical if distinguishable",
  "geography": "e.g. 'UK + Ireland', 'EMEA', 'North America'",
  "financials": [
    {"metric": "Revenue", "value": "GBP 142m", "period": "FY25"},
    ...
  ],
  "investment_highlights": ["bullet 1", "bullet 2", ...],
  "process_notes": "any timing / phase / bid-date language",
  "advisor": "advisor named on the doc, e.g. 'Rothschild & Co'",
  "confidentiality": "verbatim confidentiality clause if printed",
  "image_notes": [
    {"page": 1, "kind": "chart", "summary": "revenue bar chart FY21-FY25"},
    ...
  ],
  "summary": "2-3 sentence operator-facing precis of what this document is"
}

Rules:
- Output VALID JSON only. No markdown fences, no preamble.
- If a field is not stated in the doc, return "" (or [] for lists). Do NOT guess.
- Numbers and currency stay in their original form (no FX conversion).
- Keep "investment_highlights" close to the doc's actual bullets — paraphrase
  if needed but don't invent new ones.
- en-GB spelling.
"""


_PARSE_SYSTEM_IMAGE = (
    "You are an M&A intake assistant. The operator has sent you the rendered\n"
    "pages of a single inbound deal document — typically a sell-side teaser,\n"
    "CIM, expert-call deck, or research note.\n\n"
    "Your job is to extract structured facts from what is visible across the\n"
    "pages (both text and image content — charts, diagrams, table screenshots).\n\n"
    + _PARSE_SCHEMA_DOC
    + '\n- "image_notes": only include non-trivial visuals (charts, diagrams, tables).'
    + "\n  Skip logos, decorative shapes, page furniture."
)


_PARSE_SYSTEM_TEXT = (
    "You are an M&A intake assistant. The operator has sent you the extracted\n"
    "text from a single inbound deal document — typically a sell-side teaser,\n"
    "CIM, expert-call deck, or research note. The text was pulled by PDFium\n"
    "page-by-page; layout cues may be lost.\n\n"
    + _PARSE_SCHEMA_DOC
    + '\n- "image_notes": leave EMPTY ([]) — you cannot see visuals on this path.'
)


# Legacy alias retained for callers that imported the original constant.
_PARSE_SYSTEM = _PARSE_SYSTEM_IMAGE


# ── null coercion ─────────────────────────────────────────────────────────


def _coerce_nulls(node: Any) -> Any:
    """Recursively replace JSON null with empty string in dict/list trees.

    Local models occasionally emit ``null`` for fields the prompt specified
    as empty-string-when-absent — pydantic then rejects the payload. We
    coerce here so the model's intent (no value) maps to the schema default.
    """
    if node is None:
        return ""
    if isinstance(node, dict):
        return {k: _coerce_nulls(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_coerce_nulls(v) for v in node]
    return node


def _to_parsed(payload: Any) -> ParsedDocument:
    """Validate ``payload`` against ParsedDocument; salvage the summary on failure."""
    if not isinstance(payload, dict):
        return ParsedDocument()
    cleaned = _coerce_nulls(payload)
    try:
        return ParsedDocument(**cleaned)
    except Exception as e:  # noqa: BLE001
        log.warning("intake: ParsedDocument validation failed: %s", e)
        return ParsedDocument(summary=str(cleaned.get("summary", "") or ""))


# ── single-batch parsers ──────────────────────────────────────────────────


def parse_pages_multimodal(
    pages: list[RenderedPage],
    *,
    client: OllamaClient,
    model: str | None = None,
) -> ParsedDocument:
    """Parse one batch of rendered pages via the multimodal lane."""
    if not pages:
        raise OllamaError("parse_pages_multimodal: no pages")
    model = model or lane_to_model(pick_lane("multimodal-extraction", "confidential"))[1]
    user_prompt = (
        f"Inbound deal document — {len(pages)} page(s) attached "
        f"(page numbers {pages[0].page_number}-{pages[-1].page_number}).\n\n"
        "Extract structured facts per the schema. Return JSON only."
    )
    resp = client.chat(
        model=model, prompt=user_prompt, system=_PARSE_SYSTEM_IMAGE,
        json_mode=True, temperature=0.1,
        images=[p.png_base64 for p in pages],
    )
    return _to_parsed(parse_json_response(resp.content))


def parse_pages_text(
    text_blocks: list[ExtractedText],
    *,
    client: OllamaClient,
    model: str | None = None,
) -> ParsedDocument:
    """Parse one batch of extracted-text pages via the text lane."""
    if not text_blocks:
        raise OllamaError("parse_pages_text: no pages")
    model = model or lane_to_model(pick_lane("transcript-extraction", "confidential"))[1]
    body = "\n\n".join(f"=== PAGE {t.page_number} ===\n{t.text}" for t in text_blocks)
    # #sec-injection-guard 3a: scan the extracted PDF text (detect-and-audit;
    # NEVER blocks — a guard must not deny ingestion).
    scan_ingested_text(body, source="pdf-intake")
    user_prompt = (
        f"Inbound deal document — extracted text from "
        f"{len(text_blocks)} page(s) below (pages "
        f"{text_blocks[0].page_number}-{text_blocks[-1].page_number}).\n\n"
        f"{body}\n\n"
        "Extract structured facts per the schema. Return JSON only."
    )
    resp = client.chat(
        model=model, prompt=user_prompt, system=_PARSE_SYSTEM_TEXT,
        json_mode=True, temperature=0.1,
    )
    return _to_parsed(parse_json_response(resp.content))


# Legacy name preserved for the existing public API + tests.
def parse_document(
    pages: list[RenderedPage],
    *,
    client: OllamaClient,
    model: str | None = None,
) -> ParsedDocument:
    """Backward-compatible alias for ``parse_pages_multimodal``."""
    return parse_pages_multimodal(pages, client=client, model=model)


# ── batch merging ─────────────────────────────────────────────────────────


def _first_nonempty(values: Iterable[str]) -> str:
    for v in values:
        if v:
            return v
    return ""


def _first_nondefault_doc_kind(values: Iterable[str]) -> str:
    for v in values:
        if v and v != "other":
            return v
    # Fall back to whatever the first batch said (probably "other").
    for v in values:
        if v:
            return v
    return "other"


def merge_parsed(docs: list[ParsedDocument]) -> ParsedDocument:
    """Merge per-batch ``ParsedDocument`` outputs into a single result.

    - Identification fields take the first non-empty value across batches.
    - ``doc_kind`` prefers the first non-"other" classification.
    - ``financials`` / ``investment_highlights`` accumulate, deduped on a
      light-touch key (metric+period for financials, bullet text for
      highlights). Order of first appearance is preserved.
    - ``image_notes`` accumulate verbatim — page numbers are already
      absolute so no offsetting is needed.
    - ``summary`` takes the longest non-empty value (each batch summarises
      a sub-section; longest tends to be the most operator-useful precis).
    """
    if not docs:
        return ParsedDocument()
    if len(docs) == 1:
        return docs[0]

    seen_fins: set[tuple[str, str]] = set()
    fins: list[FinancialHighlight] = []
    for d in docs:
        for f in d.financials:
            key = (f.metric.strip().lower(), f.period.strip().lower())
            if key in seen_fins:
                continue
            seen_fins.add(key)
            fins.append(f)

    seen_hi: set[str] = set()
    highlights: list[str] = []
    for d in docs:
        for h in d.investment_highlights:
            k = h.strip().lower()
            if k in seen_hi or not k:
                continue
            seen_hi.add(k)
            highlights.append(h)

    images: list[ImageNote] = []
    for d in docs:
        images.extend(d.image_notes)

    summary = max((d.summary for d in docs), key=len, default="")

    return ParsedDocument(
        doc_kind=_first_nondefault_doc_kind(d.doc_kind for d in docs),  # type: ignore[arg-type]
        target_descriptor=_first_nonempty(d.target_descriptor for d in docs),
        target_revealed_name=_first_nonempty(d.target_revealed_name for d in docs),
        industry=_first_nonempty(d.industry for d in docs),
        sector=_first_nonempty(d.sector for d in docs),
        subsector=_first_nonempty(d.subsector for d in docs),
        geography=_first_nonempty(d.geography for d in docs),
        advisor=_first_nonempty(d.advisor for d in docs),
        confidentiality=_first_nonempty(d.confidentiality for d in docs),
        process_notes=_first_nonempty(d.process_notes for d in docs),
        financials=fins,
        investment_highlights=highlights,
        image_notes=images,
        summary=summary,
    )


# ── orchestrator ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class IngestResult:
    parsed: ParsedDocument
    mode: str                # "text" or "image"
    pages_processed: int
    batches: int
    avg_chars_per_page: float


def ingest_pdf(
    pdf_path: Path,
    *,
    client: OllamaClient,
    max_pages: int = DEFAULT_MAX_PAGES,
    batch_size: int = DEFAULT_BATCH_SIZE,
    mode: str = "auto",                # "auto" | "text" | "image"
    text_threshold: int = DEFAULT_TEXT_THRESHOLD,
    text_model: str | None = None,
    image_model: str | None = None,
    dpi: int = 150,
) -> IngestResult:
    """End-to-end: pick path (text vs image), batch pages, parse, merge.

    Selection rule when ``mode == "auto"``: extract text first; if the
    average non-empty page text length clears ``text_threshold`` chars,
    take the text path; otherwise fall back to rendering and the image
    path. Forcing ``mode="text"`` or ``mode="image"`` skips the auto
    decision and the unneeded extraction work.

    Batching: pages are processed in groups of ``batch_size``; the
    per-batch ``ParsedDocument`` outputs are merged deterministically.
    """
    n_pages = min(total_page_count(pdf_path), max_pages)
    if n_pages == 0:
        raise OllamaError("ingest_pdf: PDF has no pages")

    # Decide path.
    chose_text = False
    extracted: list[ExtractedText] = []
    avg_chars = 0.0
    if mode in ("auto", "text"):
        extracted = extract_text_per_page(pdf_path, max_pages=max_pages)
        avg_chars = (
            sum(len(t.text) for t in extracted) / max(len(extracted), 1)
        )
        if mode == "text":
            chose_text = True
        else:  # auto
            chose_text = avg_chars >= text_threshold

    # Run the chosen path in batches.
    batch_docs: list[ParsedDocument] = []
    if chose_text:
        for i in range(0, len(extracted), batch_size):
            batch = extracted[i : i + batch_size]
            batch_docs.append(
                parse_pages_text(batch, client=client, model=text_model)
            )
        mode_used = "text"
    else:
        rendered = render_pdf_to_images(pdf_path, dpi=dpi, max_pages=max_pages)
        for i in range(0, len(rendered), batch_size):
            batch = rendered[i : i + batch_size]
            batch_docs.append(
                parse_pages_multimodal(batch, client=client, model=image_model)
            )
        mode_used = "image"

    merged = merge_parsed(batch_docs)
    return IngestResult(
        parsed=merged,
        mode=mode_used,
        pages_processed=n_pages,
        batches=len(batch_docs),
        avg_chars_per_page=round(avg_chars, 1),
    )
