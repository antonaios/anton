"""File-extension dispatch for intake → markdown conversion.

The PDF intake pipeline (``parse.py`` + ``pdf_render.py``) is its own
LLM-driven extraction path producing a structured ``ParsedDocument``. This
module is the lightweight sibling for office formats that convert *directly*
to markdown for the recall + chat lanes.

Shape mirrors ``routines.hinotes.processor._read_transcript`` — branch on the
lower-cased suffix, lazy-import the per-format dependency with a clear error,
and ``raise ValueError`` for anything unsupported.

Currently wired:
  * ``.pptx`` → :func:`routines.shared.pptx_to_markdown.pptx_to_markdown` (#72)
"""

from __future__ import annotations

from pathlib import Path

# Suffixes this dispatcher can turn into markdown. Extend as new office
# converters land (e.g. a future ``.docx`` markdown path).
SUPPORTED_SUFFIXES = {".pptx"}


def is_supported(path: str | Path) -> bool:
    return Path(path).suffix.lower() in SUPPORTED_SUFFIXES


def to_markdown(path: str | Path) -> str:
    """Convert a supported office document to markdown.

    Accepts a ``str`` or ``Path`` (the converter itself also takes a file-like,
    but dispatch routes by extension so it needs a real path). Raises
    ``ValueError`` for an unsupported extension or a missing optional dependency.
    """
    path = Path(path)
    suffix = path.suffix.lower()

    if suffix == ".pptx":
        try:
            from routines.shared.pptx_to_markdown import pptx_to_markdown
        except ImportError as e:
            # Only translate a genuinely-missing python-pptx into the friendly
            # hint; let an unrelated import failure (a bug in routing /
            # ollama_client / the converter) propagate as itself.
            if (e.name or "").split(".")[0] == "pptx":
                raise ValueError(f"python-pptx required for .pptx files: {e}") from e
            raise
        return pptx_to_markdown(path)

    raise ValueError(f"unsupported intake format: {suffix} ({path})")
