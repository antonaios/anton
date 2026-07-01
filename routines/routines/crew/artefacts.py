"""Bridge-side crew I/O — document pre-extraction + deliverable materialisation.

The crew subprocess runs in an isolated venv that CANNOT import ``routines.*``
(the boundary forbids it) and has NO PDF/DOCX libraries (the shared crews venv
is operator-gated; adding deps there risks the live bridge + sibling sessions —
the ``crew-venv-no-pdf`` project memory). Document extraction therefore lives on
the BRIDGE side of the boundary, in this venv (pypdf / pypdfium2 + python-docx).
Two jobs:

1. **Pre-extraction** (``prepare_crew_input``). Generalised at Phase-7
   integration to a SINGLE shared extractor (``extract_document_text``) used by
   two crews:
     * ``/triage`` (#32) — a CIM PDF → page-tagged text in ``args["pages"]``
       (``[{"page": n, "text": ...}]``); the crew's Ingestor keyword-indexes it.
     * ``/digest`` (#ingest-digest) — a drop dir of PDF/DOCX/MD → a path→text map
       in ``args["extracted_text"]``; the crew's analyzer reads pre-extracted
       text instead of opening files (the integration reconciliation that moves
       digest's extraction bridge-side onto the /triage precedent — the shared
       crews venv has no PDF/DOCX libs).
   Sensitive text only ever crosses the LOCAL stdin pipe — never the network.

2. **Materialisation** (``finalize``). A deliverable-producing crew returns its
   output as ``CrewOutput.documents`` (content, not a path) because it cannot
   write through the central write policy. The bridge writes each document via
   ``routines.shared.vault_writer.atomic_write`` — which runs
   ``routines.shared.write_policy.ensure_write_allowed`` fail-closed BEFORE any
   byte lands — then raises an Inbox flag pointing at the artefact.

The crew-supplied ``relative_path`` is UNTRUSTED: it is sanitised (no ``..`` /
absolute / UNC / control chars), confined under the verb's fixed write root, and
re-validated by the write policy. Triple-gated.

Generic by verb: ``prepare_crew_input``/``finalize`` are no-ops for verbs that
declare no preparer / no write root, so the shared route worker thread calls
them unconditionally.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from routines.shared.vault_writer import atomic_write
from routines.shared.write_policy import WorkspacePolicyViolation

logger = logging.getLogger(__name__)


class CrewInputError(ValueError):
    """A crew's input could not be prepared (e.g. a missing or unreadable PDF).
    The route maps it to a 400 (sync validation) or an ``error`` audit row
    (async prep)."""


# Per-verb deliverable write root. ``/triage`` always lands in the BD pipeline
# (autonomous-crews §3 + [[workspace-write-policy]] §2a), regardless of the
# workspace the operator invoked it from. Env-overridable so tests can point it
# at a tmp dir (paired with a write-policy root patch). The path sits under the
# ``<workspace-root>/`` static write root, so the policy allows it.
BD_WRITE_ROOT = Path(os.environ.get(
    "ANTON_TRIAGE_BD_ROOT",
    r"<workspace-root>\2. Business development",
))

# Vault-relative dir for the Inbox flag note (the ``inbox/`` write prefix).
_INBOX_FLAG_REL = "Inbox/triage"

# Generous upper bound on pages extracted: bounds both extraction time and the
# size of the text that crosses the stdin pipe in one JSON line. No real CIM is
# this long, so it only clips pathological input — and it is logged, never
# silent (overage pages are dropped with a warning).
_MAX_PDF_PAGES = int(os.environ.get("ANTON_TRIAGE_MAX_PAGES", "500"))

# Digest bridge-extraction bounds (mirror the intent of the crew-side
# extract.py caps it replaces). Per-doc char cap keeps the path→text map that
# crosses the stdin pipe bounded; the crew's analyzer trims further per call.
_DIGEST_MAX_PAGES = int(os.environ.get("ANTON_DIGEST_MAX_PAGES", "60"))
_DIGEST_MAX_CHARS = int(os.environ.get("ANTON_DIGEST_MAX_CHARS", "400000"))
_DIGEST_MAX_FILE_BYTES = int(os.environ.get("ANTON_DIGEST_MAX_BYTES", str(100 * 1024 * 1024)))
_DIGEST_EXT_TO_TYPE = {".pdf": "pdf", ".docx": "docx", ".md": "md", ".markdown": "md"}

_SLUG_RE = re.compile(r"[^A-Za-z0-9]+")
# Control chars / NUL — never valid in a path component.
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")


def _write_root_for(verb: str) -> Path | None:
    """The fixed deliverable root for ``verb``, or ``None`` if the verb writes
    no bridge-materialised documents."""
    return BD_WRITE_ROOT if verb == "triage" else None


def _slug(text: str) -> str:
    s = _SLUG_RE.sub("-", str(text or "").strip()).strip("-")
    return s or "unknown"


# ════════════════════════════════════════════════════════════════════════════
# 1. Pre-extraction
# ════════════════════════════════════════════════════════════════════════════


def _pdf_path_from_args(args: dict[str, Any]) -> str | None:
    raw = args.get("pdf_path") or args.get("pdf") or args.get("path")
    return str(raw) if raw else None


def validate_input(verb: str, args: dict[str, Any]) -> None:
    """Synchronous arg validation for the route handler (→ 400 on failure).

    Kept cheap (no extraction): ``/triage`` needs a ``pdf_path`` that points at
    an existing file. The expensive page extraction happens later, off the
    request thread, in :func:`prepare_crew_input`."""
    if verb != "triage":
        return
    pdf_path = _pdf_path_from_args(args or {})
    if not pdf_path:
        raise CrewInputError("triage requires args.pdf_path (path to the CIM/teaser PDF)")
    if not Path(pdf_path).is_file():
        raise CrewInputError(f"triage pdf_path not found or not a file: {pdf_path!r}")


def extract_pdf_pages(pdf_path: str | Path) -> list[dict[str, Any]]:
    """Extract a PDF to page-tagged text via ``pypdf`` (a routines dependency;
    the crew venv has none). Returns ``[{"page": 1, "text": "..."}, ...]`` for
    pages that yielded text. A per-page extraction error is skipped, not fatal —
    one bad page must not lose the whole CIM. Raises :class:`CrewInputError`
    when the file can't be opened at all, or yields zero extractable text (a
    scanned/image-only CIM needs OCR upstream — surfaced, not silently empty)."""
    from pypdf import PdfReader  # routines dep; deferred so the module imports anywhere
    from pypdf.errors import PyPdfError  # pypdf's base exception (6.x)

    pages: list[dict[str, Any]] = []
    truncated = False
    try:
        reader = PdfReader(str(pdf_path))
        # Iterating reader.pages can itself raise (e.g. an encrypted CIM) — keep
        # it inside the guard, not just the constructor.
        for i, page in enumerate(reader.pages, start=1):
            if i > _MAX_PDF_PAGES:
                truncated = True
                break
            try:
                text = (page.extract_text() or "").strip()
            except Exception as e:  # noqa: BLE001 — a malformed page must not abort ingestion
                logger.warning("triage: page %d of %s failed to extract (%s)", i, pdf_path, e)
                continue
            if text:
                pages.append({"page": i, "text": text})
    except (OSError, PyPdfError, ValueError) as e:
        raise CrewInputError(
            f"could not read PDF {str(pdf_path)!r}: {type(e).__name__}: {e} "
            f"(an encrypted CIM must be decrypted before /triage)"
        ) from e
    if not pages:
        raise CrewInputError(
            f"no extractable text in {str(pdf_path)!r} — a scanned/image-only "
            f"CIM needs OCR before /triage can read it"
        )
    if truncated:
        logger.warning(
            "triage: %s exceeded %d pages — triaging the first %d only",
            pdf_path, _MAX_PDF_PAGES, len(pages),
        )
    return pages


def _bound_text(text: str, max_chars: int) -> str:
    return text if len(text) <= max_chars else text[:max_chars]


def extract_document_text(
    path: str | Path, *, doc_type: str | None = None,
    max_pages: int = _DIGEST_MAX_PAGES, max_chars: int = _DIGEST_MAX_CHARS,
) -> str:
    """Bridge-side whole-document text extraction for ``/digest`` (PDF/DOCX/MD).

    The SHARED bridge extractor (with ``extract_pdf_pages`` for /triage's
    page-tagged need) that supersedes the crew-side ``_shared/digest/extract.py``
    — the crews venv has no PDF/DOCX libs, so digest extraction moves here onto
    the /triage precedent. PDF via pypdf, DOCX via python-docx, MD via a UTF-8
    read; ``doc_type`` selects the path (else inferred from the suffix). Output
    is bounded (file-size cap + page cap + char cap). Returns ``""`` for an
    unsupported type or a legitimately empty doc; raises :class:`CrewInputError`
    only when the file can't be read at all (missing / oversized / corrupt)."""
    path = Path(path)
    if not path.is_file():
        raise CrewInputError("digest: not a file")
    try:
        size = path.stat().st_size
    except OSError as e:
        raise CrewInputError(f"digest: could not stat file: {type(e).__name__}") from e
    if size > _DIGEST_MAX_FILE_BYTES:
        raise CrewInputError(
            f"digest: file too large ({size} bytes > {_DIGEST_MAX_FILE_BYTES} cap)"
        )

    kind = (doc_type or _DIGEST_EXT_TO_TYPE.get(path.suffix.lower(), "")).lower()
    if kind == "pdf":
        return _digest_extract_pdf(path, max_pages=max_pages, max_chars=max_chars)
    if kind == "docx":
        return _digest_extract_docx(path, max_chars=max_chars)
    if kind in ("md", "markdown", "txt"):
        try:
            return _bound_text(path.read_text(encoding="utf-8", errors="replace"), max_chars)
        except OSError as e:
            raise CrewInputError(f"digest: read failed: {type(e).__name__}") from e
    logger.debug("digest extract: unsupported type %r", kind)  # path-free
    return ""


def _digest_extract_pdf(path: Path, *, max_pages: int, max_chars: int) -> str:
    """Whole-text PDF extraction via pypdf (the routines dep). Per-page errors
    are skipped (one bad page must not lose the doc); a total open failure is a
    :class:`CrewInputError`."""
    from pypdf import PdfReader  # routines dep; deferred so the module imports anywhere
    from pypdf.errors import PyPdfError

    parts: list[str] = []
    try:
        reader = PdfReader(str(path))
        for i, page in enumerate(reader.pages, start=1):
            if i > max_pages:
                break
            try:
                parts.append((page.extract_text() or ""))
            except Exception as e:  # noqa: BLE001 — a bad page must not abort the doc
                logger.warning("digest: a PDF page failed to extract (%s)", type(e).__name__)
                continue
            if sum(len(p) for p in parts) >= max_chars:
                break
    except (OSError, PyPdfError, ValueError) as e:
        raise CrewInputError(f"digest: PDF read failed: {type(e).__name__}") from e
    return _bound_text("\n".join(parts), max_chars)


def _digest_extract_docx(path: Path, *, max_chars: int) -> str:
    """DOCX paragraph text via python-docx (the routines dep)."""
    from docx import Document  # type: ignore[import-untyped]

    try:
        doc = Document(str(path))
        text = "\n".join(p.text for p in doc.paragraphs if p.text)
    except Exception as e:  # noqa: BLE001 — open/parse failure → one-doc error
        raise CrewInputError(f"digest: DOCX read failed: {type(e).__name__}") from e
    return _bound_text(text, max_chars)


def _digest_supported_files(drop_dir: Path, *, recursive: bool) -> list[Path]:
    """Supported (PDF/DOCX/MD) files under ``drop_dir``, sorted for
    reproducibility. Mirrors the crew scanner's walk (skips dotfiles); the crew
    scanner still runs for dedupe/classify — this only enumerates what to
    pre-extract."""
    out: list[Path] = []
    it = drop_dir.rglob("*") if recursive else drop_dir.iterdir()
    for p in it:
        try:
            if p.is_file() and not p.name.startswith(".") \
                    and p.suffix.lower() in _DIGEST_EXT_TO_TYPE:
                out.append(p)
        except OSError:
            continue
    return sorted(out, key=lambda x: str(x).lower())


def extract_drop_dir_text(
    drop_dir: str | Path, *, recursive: bool = False,
) -> tuple[str, dict[str, str]]:
    """Bridge-extract every supported doc under ``drop_dir``.

    Returns ``(resolved_drop_dir, {path: text})``. The ``drop_dir`` is RESOLVED
    to an absolute, normalised path FIRST, and the map is keyed by
    ``str(resolved_child)`` — so when ``prepare_crew_input`` passes the SAME
    resolved ``drop_dir`` to the crew, the crew scanner walks the same canonical
    root and stamps a matching ``DocCandidate.path`` (codex correctness MED: a
    relative ``drop_dir`` would otherwise key the bridge map off the bridge cwd
    while the crew scans off the crew install cwd, and the analyzer would miss
    every pre-extracted doc).

    A per-doc extraction failure is logged (path-free) and skipped — its path is
    simply absent from the map, so the crew's analyzer falls back to its own
    (dep-gated) extractor and degrades that ONE doc, never the run. Raises
    :class:`CrewInputError` only when ``drop_dir`` isn't a directory."""
    try:
        root = Path(drop_dir).resolve()
    except OSError as e:
        raise CrewInputError(f"digest: could not resolve drop_dir: {type(e).__name__}") from e
    if not root.is_dir():
        raise CrewInputError("digest: drop_dir is not a directory")
    extracted: dict[str, str] = {}
    for p in _digest_supported_files(root, recursive=recursive):
        try:
            extracted[str(p)] = extract_document_text(p)
        except CrewInputError as e:
            # Path-free: a filename can encode a deal name. The crew sees this
            # doc as not-pre-extracted and handles it per-doc.
            logger.warning("digest: bridge extraction skipped a doc (%s)", e)
            continue
    return str(root), extracted


def prepare_crew_input(verb: str, crew_input: dict[str, Any]) -> dict[str, Any]:
    """Augment ``crew_input`` with whatever the crew needs but cannot fetch
    itself.

      * ``/triage`` — extract the PDF to ``args["pages"]`` (+ filename stem +
        run date).
      * ``/digest`` — bridge-extract every supported drop-dir doc into
        ``args["extracted_text"]`` (path→text), so the crew's analyzer reads
        pre-extracted text rather than opening files in a venv with no PDF/DOCX
        libs (the integration move bridge-side).

    No-op for other verbs. Runs on the worker thread (off the request path)
    because extraction can be slow. Raises :class:`CrewInputError` on an
    unreadable input — the worker audits it as an ``error`` run."""
    if verb == "triage":
        args = dict(crew_input.get("args") or {})
        pdf_path = _pdf_path_from_args(args)
        if not pdf_path:
            raise CrewInputError("triage requires args.pdf_path")
        args["pages"] = extract_pdf_pages(pdf_path)
        args.setdefault("pdf_stem", Path(pdf_path).stem)
        args.setdefault("date", datetime.now(timezone.utc).date().isoformat())
        out = dict(crew_input)
        out["args"] = args
        return out
    if verb == "digest":
        args = dict(crew_input.get("args") or {})
        drop_dir = str(args.get("drop_dir") or "").strip()
        if not drop_dir:
            raise CrewInputError("digest requires args.drop_dir")
        # Resolve drop_dir bridge-side and pass the SAME resolved form to the
        # crew, so the crew scanner's DocCandidate.path matches the extracted_text
        # map keys (codex correctness MED).
        resolved_drop_dir, extracted = extract_drop_dir_text(
            drop_dir, recursive=bool(args.get("recursive", False)),
        )
        args["drop_dir"] = resolved_drop_dir
        args["extracted_text"] = extracted
        out = dict(crew_input)
        out["args"] = args
        return out
    return crew_input


# ════════════════════════════════════════════════════════════════════════════
# 2. Materialisation + Inbox flag
# ════════════════════════════════════════════════════════════════════════════


def _safe_relpath(raw: Any) -> str | None:
    """Sanitise an UNTRUSTED crew-supplied relative path. Returns a clean POSIX
    relative path, or ``None`` when it is unusable.

    Rejects: empty, control chars, absolute (``/x`` or ``X:``), UNC (``\\\\``),
    and any ``.``/``..`` traversal component. The write policy is the final
    gate, but this refuses the obviously-hostile shapes before they reach it."""
    if not raw:
        return None
    s = str(raw).strip().replace("\\", "/")
    if not s or _CTRL_RE.search(s):
        return None
    if s.startswith("/") or s.startswith("//"):
        return None
    if ":" in s:  # drive-absolute (X:) or NTFS alternate-data-stream (note.md:payload)
        return None
    parts = [p for p in s.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        return None
    return "/".join(parts)


def materialize_documents(
    *,
    verb: str,
    documents: list[dict[str, Any]],
    sensitivity: str,
) -> tuple[list[dict[str, str]], list[str]]:
    """Write each crew ``document`` under the verb's write root via the central
    write policy. Returns ``(written_artefacts, errors)`` — ``written_artefacts``
    is the ``[{path, sensitivity}]`` list for the audit; ``errors`` carries any
    refused/failed write (the deliverable did not land — the route surfaces it)."""
    root = _write_root_for(verb)
    written: list[dict[str, str]] = []
    errors: list[str] = []
    if root is None:
        if documents:
            errors.append(f"crew {verb!r} returned documents but declares no write root")
        return written, errors

    for doc in documents or []:
        if not isinstance(doc, dict):
            errors.append("malformed document entry (not an object)")
            continue
        rel = _safe_relpath(doc.get("relative_path"))
        if rel is None:
            errors.append(f"unsafe document relative_path: {doc.get('relative_path')!r}")
            continue
        target = root / rel
        content = str(doc.get("content") or "")
        # Label the artefact with the BRIDGE-resolved run sensitivity, NEVER the
        # crew's self-declared ``doc["sensitivity"]`` (untrusted — a buggy crew
        # could under-label MNPI CIM content as "public"). The run tier is the
        # strictest the guard computed; all crew output is at most that tier.
        try:
            atomic_write(target, content)  # ensure_write_allowed fires inside
        except WorkspacePolicyViolation as e:
            errors.append(f"write refused for {rel!r}: {e}")
            continue
        except OSError as e:
            errors.append(f"write failed for {rel!r}: {type(e).__name__}: {e}")
            continue
        written.append({"path": str(target), "sensitivity": sensitivity})
    return written, errors


def _render_flag_note(
    *, entity: str, run_id: str, artefact_path: str, summary: str,
    sensitivity: str, date_iso: str,
) -> str:
    """The Inbox flag is a POINTER (metadata + path + one-line summary), never
    the CIM content — the memo itself holds the detail, in Corporate Finance."""
    return (
        "---\n"
        "type: triage-flag\n"
        f"entity: {entity}\n"
        f"date: {date_iso}\n"
        f"run_id: {run_id}\n"
        f"sensitivity: {sensitivity}\n"
        f"artefact: {artefact_path}\n"
        "---\n\n"
        f"# Triage flag — {entity}\n\n"
        f"{summary}\n\n"
        f"Memo: `{artefact_path}`\n\n"
        f"*Autonomous `/triage` (run `{run_id}`, {date_iso}). Review the memo, "
        f"then route to a project if this BD lead warrants a mandate.*\n"
    )


def raise_inbox_flag(
    *, run_id: str, entity: str, artefact_path: str, summary: str, sensitivity: str,
) -> Path | None:
    """Best-effort Inbox flag note in the vault (``Inbox/triage/``). Returns the
    path written, or ``None`` on any failure — a flag miss must NOT fail a run
    whose deliverable already landed. The note is a pointer only; no CIM content
    enters the vault."""
    try:
        from routines.api import deps  # lazy: honour a monkeypatched deps.VAULT
        vault_root = Path(deps.VAULT)
        date_iso = datetime.now(timezone.utc).date().isoformat()
        rel = f"{_INBOX_FLAG_REL}/{date_iso}-{_slug(entity)}-{_slug(run_id)}.md"
        target = vault_root / rel
        content = _render_flag_note(
            entity=entity, run_id=run_id, artefact_path=artefact_path,
            summary=summary, sensitivity=sensitivity, date_iso=date_iso,
        )
        atomic_write(target, content, vault_root=vault_root)
        return target
    except Exception as e:  # noqa: BLE001 — flag is best-effort
        logger.warning("triage: inbox flag not raised for run %s (%s)", run_id, e)
        return None


def _entity_from_artefacts(written: list[dict[str, str]], root: Path | None) -> str:
    """Recover a display entity from the written artefact path (its first path
    segment under the write root is the entity slug)."""
    if not written or root is None:
        return "unknown entity"
    try:
        rel = Path(written[0]["path"]).relative_to(root)
        first = rel.parts[0] if rel.parts else ""
        return first.replace("-", " ") or "unknown entity"
    except (ValueError, KeyError, IndexError):
        return "unknown entity"


def finalize(
    *,
    verb: str,
    run_id: str,
    result: dict[str, Any],
    sensitivity: str,
) -> tuple[list[dict[str, str]], list[str]]:
    """Materialise a successful run's documents + raise the Inbox flag.

    Returns ``(written_artefacts, errors)``. Mutates ``result`` IN PLACE to drop
    the raw ``documents`` content (it must never reach the audit / run record)
    and to record the written artefact paths. The Inbox flag is best-effort and
    never appears in ``errors``."""
    documents = result.get("documents") or []
    written, errors = materialize_documents(
        verb=verb, documents=documents, sensitivity=sensitivity,
    )
    # The content has been written (or refused) — never carry it onward.
    result["documents"] = []
    if written:
        result["artefacts"] = written
        entity = _entity_from_artefacts(written, _write_root_for(verb))
        raise_inbox_flag(
            run_id=run_id, entity=entity, artefact_path=written[0]["path"],
            summary=str(result.get("summary") or "CIM triaged."),
            sensitivity=sensitivity,
        )
    return written, errors


__all__ = [
    "CrewInputError",
    "BD_WRITE_ROOT",
    "validate_input",
    "extract_pdf_pages",
    "extract_document_text",
    "extract_drop_dir_text",
    "prepare_crew_input",
    "materialize_documents",
    "raise_inbox_flag",
    "finalize",
]
