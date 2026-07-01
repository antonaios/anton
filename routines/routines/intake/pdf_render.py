"""PDF → page-image renderer for multimodal extraction.

Uses pypdfium2 (BSD-3, self-contained Python binding to PDFium). No
Poppler / system-deps required on Windows.

Renders each page to PNG bytes at a fixed DPI, returns base64 strings
ready for Ollama's chat ``images`` field. Tuned for legibility (charts,
small-text tables) over file size — the request is over loopback so a
few MB of base64 is fine.
"""

from __future__ import annotations

import base64
import io
import logging
import math
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


# 150 DPI is the sweet spot: legible chart axes and table cells without
# blowing token budget. 300 DPI roughly doubles tokens for marginal gain.
DEFAULT_DPI = 150
# Hard cap — past this, the LLM call slows + risks blowing context. Most
# teasers are 1-3 pages; CIMs run 20-50. We cap at 20 pages by default,
# overrideable via CLI.
DEFAULT_MAX_PAGES = 20

# ── F-16 bitmap-bomb hardening (HR S-11) ─────────────────────────────────────
# pdf-intake is reachable via the CSRF route AND the autonomous folder watcher,
# and pypdfium renders each page into a raw RGBA bitmap of
# ``width_px * height_px * 4`` bytes. The DEFAULT_MAX_PAGES cap bounds the page
# COUNT but NOT per-page DIMENSIONS — a PDF declaring a 200000×200000-pt page is
# a memory-amplification DoS the .docx/.pptx siblings already defend against.
# Cap the input file size, clamp each page's render scale so its bitmap fits a
# per-page ceiling, and cap cumulative bitmap bytes across the rendered pages.
MAX_INPUT_BYTES = 100 * 1024 * 1024            # 100 MB — CIMs are a few MB; generous headroom
MAX_PAGE_BITMAP_BYTES = 256 * 1024 * 1024      # 256 MB/page (A4 @150dpi ≈ 8.7 MB)
MAX_TOTAL_BITMAP_BYTES = 1024 * 1024 * 1024    # 1 GB cumulative across rendered pages


def _check_input_size(pdf_path: Path) -> None:
    """Reject an over-large input file BEFORE opening it (F-16)."""
    try:
        size = pdf_path.stat().st_size
    except OSError as e:
        raise PDFRenderError(f"could not stat {pdf_path.name}: {e}") from e
    if size > MAX_INPUT_BYTES:
        raise PDFRenderError(
            f"PDF too large: {size} bytes exceeds the {MAX_INPUT_BYTES}-byte intake cap"
        )


def _bitmap_bytes(width_pt: float, height_pt: float, scale: float) -> int:
    """Bytes the RGBA bitmap of a ``width_pt × height_pt`` page would occupy
    when rendered at ``scale`` (4 bytes/pixel)."""
    w_px = max(1, int(width_pt * scale))
    h_px = max(1, int(height_pt * scale))
    return w_px * h_px * 4


def _clamp_scale_to_bitmap_cap(
    width_pt: float,
    height_pt: float,
    scale: float,
    cap_bytes: int = MAX_PAGE_BITMAP_BYTES,
) -> float:
    """Reduce ``scale`` so the page bitmap fits ``cap_bytes`` (F-16).

    Returns the original scale when the page already fits; otherwise the
    largest scale whose bitmap is ≤ ``cap_bytes`` — preserving as much
    resolution as the cap allows for a legitimately large page while neutering
    a bomb (a 200000-pt page clamps to a near-zero effective DPI). Returns
    ``0.0`` for a degenerate (non-positive) page so the caller can skip it."""
    if width_pt <= 0 or height_pt <= 0:
        return 0.0
    if _bitmap_bytes(width_pt, height_pt, scale) <= cap_bytes:
        return scale
    # bytes = (w_pt·scale)·(h_pt·scale)·4 ≤ cap  ⇒  scale ≤ √(cap / (w·h·4))
    clamped = math.sqrt(cap_bytes / (width_pt * height_pt * 4.0))
    # The 1px-per-dimension floor in ``_bitmap_bytes`` breaks the continuous-
    # scaling assumption for a PATHOLOGICAL aspect ratio (e.g. 40_000_000 × 0.4
    # pt): the short side floors to 1px while the long side stays enormous, so
    # the floored bitmap can still bust the cap at the math-clamped scale.
    # Verify and SKIP (return 0.0) when that happens — a legitimate page never
    # has a dimension that floors to 1px (codex-5.5 validators r1).
    if _bitmap_bytes(width_pt, height_pt, clamped) > cap_bytes:
        return 0.0
    return clamped


@dataclass(frozen=True)
class RenderedPage:
    page_number: int          # 1-indexed
    png_base64: str           # raw base64 (no data: prefix) — Ollama format


@dataclass(frozen=True)
class ExtractedText:
    page_number: int          # 1-indexed
    text: str                 # raw text extracted by PDFium's textpage


class PDFRenderError(Exception):
    """Raised on PDF open / render failure."""


def extract_text_per_page(
    pdf_path: Path,
    *,
    max_pages: int = DEFAULT_MAX_PAGES,
) -> list[ExtractedText]:
    """Extract per-page text via PDFium's textpage API.

    Used by the text-first path in the intake orchestrator — when a PDF
    has machine-readable text, qwen3:14b on the extracted strings beats
    gemma4:e4b on rendered images for structured fact extraction.

    Returns one ``ExtractedText`` per page, in page order. Pages with no
    extractable text come back with ``text == ""`` and the caller can
    fall back to the image path on that signal.

    Raises ``PDFRenderError`` on open failure (mirrors ``render_pdf_to_images``).
    """
    try:
        import pypdfium2 as pdfium
    except ImportError as e:  # pragma: no cover
        raise PDFRenderError(
            "pypdfium2 not installed; run `pip install pypdfium2`"
        ) from e

    if not pdf_path.exists():
        raise PDFRenderError(f"PDF not found: {pdf_path}")
    _check_input_size(pdf_path)

    try:
        pdf = pdfium.PdfDocument(str(pdf_path))
    except Exception as e:  # noqa: BLE001
        raise PDFRenderError(f"PDF open failed for {pdf_path.name}: {e}") from e

    out: list[ExtractedText] = []
    try:
        for i in range(min(len(pdf), max_pages)):
            page = pdf[i]
            try:
                tp = page.get_textpage()
                try:
                    text = tp.get_text_range() or ""
                finally:
                    tp.close()
                out.append(ExtractedText(page_number=i + 1, text=text))
            finally:
                page.close()
    finally:
        pdf.close()

    return out


def total_page_count(pdf_path: Path) -> int:
    """Return the number of pages in ``pdf_path`` without rendering anything."""
    try:
        import pypdfium2 as pdfium
    except ImportError as e:  # pragma: no cover
        raise PDFRenderError("pypdfium2 not installed") from e
    if not pdf_path.exists():
        raise PDFRenderError(f"PDF not found: {pdf_path}")
    pdf = pdfium.PdfDocument(str(pdf_path))
    try:
        return len(pdf)
    finally:
        pdf.close()


def render_pdf_to_images(
    pdf_path: Path,
    *,
    dpi: int = DEFAULT_DPI,
    max_pages: int = DEFAULT_MAX_PAGES,
) -> list[RenderedPage]:
    """Render up to ``max_pages`` pages of ``pdf_path`` to base64 PNGs.

    Args:
        pdf_path: source PDF.
        dpi: render resolution. PDFium internally treats this as the scale
             factor against 72-DPI native, so 150 ≈ 2.083× scale.
        max_pages: hard cap on the number of pages rendered. Pages beyond
                   the cap are silently dropped (a warning is logged).

    Returns:
        List of ``RenderedPage`` in page order.

    Raises:
        PDFRenderError on any open / render failure.
    """
    try:
        import pypdfium2 as pdfium
    except ImportError as e:  # pragma: no cover — pypdfium2 is a hard dep
        raise PDFRenderError(
            "pypdfium2 not installed; run `pip install pypdfium2`"
        ) from e

    if not pdf_path.exists():
        raise PDFRenderError(f"PDF not found: {pdf_path}")
    _check_input_size(pdf_path)

    try:
        pdf = pdfium.PdfDocument(str(pdf_path))
    except Exception as e:  # noqa: BLE001
        raise PDFRenderError(f"PDF open failed for {pdf_path.name}: {e}") from e

    total_pages = len(pdf)
    if total_pages > max_pages:
        logger.warning(
            "pdf-intake: %s has %d pages; rendering first %d only",
            pdf_path.name, total_pages, max_pages,
        )

    scale = dpi / 72.0
    out: list[RenderedPage] = []
    total_bitmap_bytes = 0
    try:
        for i in range(min(total_pages, max_pages)):
            page = pdf[i]
            try:
                # F-16: clamp the render scale so this page's RGBA bitmap fits
                # the per-page cap (a malicious huge page clamps to a harmless
                # near-zero DPI; a legit page is unchanged), and stop once the
                # cumulative bitmap budget is exhausted.
                width_pt, height_pt = page.get_size()
                page_scale = _clamp_scale_to_bitmap_cap(width_pt, height_pt, scale)
                if page_scale <= 0.0:
                    logger.warning(
                        "pdf-intake: %s page %d has degenerate dimensions "
                        "(%.0f×%.0f pt); skipping", pdf_path.name, i + 1, width_pt, height_pt,
                    )
                    continue
                if page_scale < scale:
                    logger.warning(
                        "pdf-intake: %s page %d (%.0f×%.0f pt) exceeds the per-page "
                        "bitmap cap; clamping render scale %.4f → %.4f",
                        pdf_path.name, i + 1, width_pt, height_pt, scale, page_scale,
                    )
                projected = _bitmap_bytes(width_pt, height_pt, page_scale)
                if total_bitmap_bytes + projected > MAX_TOTAL_BITMAP_BYTES:
                    logger.warning(
                        "pdf-intake: %s cumulative bitmap cap reached; stopping "
                        "before page %d", pdf_path.name, i + 1,
                    )
                    break
                total_bitmap_bytes += projected
                pil = page.render(scale=page_scale).to_pil()
                buf = io.BytesIO()
                pil.save(buf, format="PNG", optimize=True)
                b64 = base64.b64encode(buf.getvalue()).decode("ascii")
                out.append(RenderedPage(page_number=i + 1, png_base64=b64))
            finally:
                page.close()
    finally:
        pdf.close()

    return out
