"""Stage 1 — doc-scanner for the digest crew (#ingest-digest).

Inventories a drop directory deterministically: type-detect (PDF/DOCX/MD),
dedupe by content hash, and pre-classify sensitivity from PROJECT CONTEXT. No
LLM, no network, no text extraction — the reproducible front of the pipeline
(the public/private content classifier is the separate ``classifier`` module,
run per-doc once a bounded text sample exists).

"Sensitivity pre-classification from project context" here means exactly that:
a deal-doc pile lives inside a project, and the project's workspace tier is the
doc's starting tier. It is NOT a content judgement — a deal pile is
presumptively ``confidential`` (CLAUDE.md §5.4 / the design's §3 governance),
and the caller passes that tier in. Refining a doc's tier by content is a future
concern; the scanner's job is the conservative context default.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from _shared.digest.models import DocCandidate, ScanResult, Sensitivity

logger = logging.getLogger(__name__)

# Extension → DocType. Anything else is "unknown" (and unsupported).
_EXT_TO_TYPE = {
    ".pdf": "pdf",
    ".docx": "docx",
    ".md": "md",
    ".markdown": "md",
}

# Subdirs the pdf-intake watcher creates for processed/failed files — never
# treat those as fresh input if a recursive scan ever lands here.
_SKIP_DIR_NAMES = {"processed", "failed", ".git", "__pycache__"}

_HASH_CHUNK = 65536


def _detect_type(path: Path) -> str:
    return _EXT_TO_TYPE.get(path.suffix.lower(), "unknown")


def _content_sha256(path: Path) -> str:
    """Streaming sha256 of the file's bytes, or ``""`` if it can't be read
    (logged — an unhashable file can't be deduped, so it stays its own doc)."""
    try:
        h = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(_HASH_CHUNK), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError as e:
        # Path-free (a file name can encode a deal name): log the error TYPE
        # only, never the name or the OSError text (which embeds the path).
        logger.warning("digest scan: could not hash a file (%s)", type(e).__name__)
        return ""


def scan_drop_dir(
    drop_dir: str | Path,
    project: str,
    *,
    project_sensitivity: Sensitivity = "confidential",
    recursive: bool = False,
) -> ScanResult:
    """Inventory ``drop_dir`` into a :class:`ScanResult`.

    Files are visited in sorted path order so dedup ("first file with a hash
    wins") and the whole result are reproducible (matters under
    ``PYTHONHASHSEED=0``). ``project_sensitivity`` is the project-context tier
    stamped onto every candidate; it defaults to ``confidential`` — the
    fail-closed presumption for a deal-doc pile.

    Raises ``NotADirectoryError`` if ``drop_dir`` isn't a directory — a typo'd
    path should fail loudly, not silently scan nothing."""
    root = Path(drop_dir)
    if not root.is_dir():
        # Path-free message: this string can surface in the crew's top-level
        # crash envelope, which the bridge audits.
        raise NotADirectoryError("drop dir is not a directory")

    paths = _walk(root, recursive=recursive)

    candidates: list[DocCandidate] = []
    seen_hashes: dict[str, str] = {}   # sha256 → first path that had it
    duplicates = 0
    unsupported = 0

    for p in paths:
        doc_type = _detect_type(p)
        supported = doc_type != "unknown"
        try:
            size = p.stat().st_size
        except OSError:
            size = 0
        sha = _content_sha256(p) if supported else ""

        is_dup = False
        dup_of: str | None = None
        if sha:
            if sha in seen_hashes:
                is_dup = True
                dup_of = seen_hashes[sha]
            else:
                seen_hashes[sha] = str(p)

        if not supported:
            unsupported += 1
        elif is_dup:
            duplicates += 1

        candidates.append(DocCandidate(
            path=str(p),
            filename=p.name,
            doc_type=doc_type,  # type: ignore[arg-type]
            size_bytes=size,
            content_sha256=sha,
            sensitivity_hint=project_sensitivity,
            is_duplicate=is_dup,
            duplicate_of=dup_of,
            supported=supported,
        ))

    unique = sum(1 for c in candidates if c.supported and not c.is_duplicate)
    return ScanResult(
        drop_dir=str(root),
        project=project,
        project_sensitivity=project_sensitivity,
        candidates=candidates,
        total_files=len(candidates),
        unique_docs=unique,
        duplicates=duplicates,
        unsupported=unsupported,
    )


def _walk(root: Path, *, recursive: bool) -> list[Path]:
    """Sorted list of candidate files under ``root``. Skips dotfiles and the
    watcher's processed/failed subdirs."""
    out: list[Path] = []
    if recursive:
        for p in root.rglob("*"):
            if p.is_file() and not p.name.startswith(".") and not (
                set(part.lower() for part in p.relative_to(root).parts[:-1]) & _SKIP_DIR_NAMES
            ):
                out.append(p)
    else:
        for p in root.iterdir():
            if p.is_file() and not p.name.startswith("."):
                out.append(p)
    return sorted(out, key=lambda x: str(x).lower())


def unique_supported(scan: ScanResult) -> list[DocCandidate]:
    """The candidates stage 2 should analyze: supported, non-duplicate."""
    return [c for c in scan.candidates if c.supported and not c.is_duplicate]


__all__ = ["scan_drop_dir", "unique_supported"]
