"""PDF intake routine.

Multimodal-aware ingestion of inbound deal documents — teasers, CIMs,
expert-call decks, IMs — that often carry image-embedded charts, deal
diagrams, and tabular screenshots that pure-text OCR loses.

Pipeline:
    1. Render each PDF page to PNG via pypdfium2 (no system deps).
    2. Pass page images to local gemma4:e4b via the multimodal-extraction
       routing lane.
    3. LLM returns structured JSON (target descriptor / sector / financials /
       process / highlights / image notes).
    4. Render as markdown into ``Inbox/Documents/`` with
       ``status: needs-review`` so it surfaces in Obsidian for triage.

Sensitivity: inbound docs may carry NDA or restricted-list material before
classification. Routing pins to local Ollama unconditionally.

Beyond the PDF pipeline, ``dispatch.to_markdown`` routes office formats
straight to markdown (``.pptx`` via the markitdown port, #72).
"""

from routines.intake.dispatch import SUPPORTED_SUFFIXES, is_supported, to_markdown

__all__ = ["SUPPORTED_SUFFIXES", "is_supported", "to_markdown"]
