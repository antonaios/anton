"""Chat document-attachment extraction (#chat-attachments).

The operator attaches a document to a chat session; the bridge saves the
binary to the deal's filesystem folder, extracts its text LOCALLY (CPU
parsers for Office/text formats, the local Ollama lane for scanned PDFs —
NEVER a cloud model), and injects only that extracted TEXT into the chat
turn. The binary's bytes (and any image pixels) never reach a cloud lane.

Public surface:
  * ``extract_text(path) -> ExtractedDoc`` — local-only text extraction,
    dispatched by file extension.
  * ``ExtractedDoc`` — the extraction result (text, char count, truncation
    flag, the local lane used).
"""

from __future__ import annotations

from routines.attachments.extract import (
    MAX_EXTRACTED_CHARS,
    SUPPORTED_EXTENSIONS,
    AttachmentExtractError,
    ExtractedDoc,
    UnsupportedAttachment,
    extract_text,
)

__all__ = [
    "MAX_EXTRACTED_CHARS",
    "SUPPORTED_EXTENSIONS",
    "AttachmentExtractError",
    "ExtractedDoc",
    "UnsupportedAttachment",
    "extract_text",
]
