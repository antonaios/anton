"""Chat document-attachment upload + local extraction (#chat-attachments).

  * POST /api/sessions/{session_id}/attachments  — multipart/form-data, single
    ``file`` field. Saves the binary to the session's deal filesystem folder,
    extracts its text LOCALLY, injection-scans it, and returns the extracted
    text so the dashboard can thread it into the next chat turn.

SECURITY POSTURE (§5.2 / CLAUDE.md #no-mnpi-to-cloud — a reviewer checks each):

  1. The uploaded bytes are written to local disk and passed ONLY to the local
     extractors in ``routines.attachments.extract``. They are NEVER placed in a
     prompt, chat history, or any cloud request. Only the extracted TEXT is
     returned (the chat turn that injects it rides the EXISTING router, whose
     sensitivity gate is untouched).
  2. Extraction is local-only (CPU parsers / local Ollama) — see
     ``routines.attachments.extract``. Never a cloud model.
  3. Loopback-only: the router carries the shared ``_loopback_only`` dependency
     (the same one credentials / budgets / intake use).
  4. PATH SAFETY: the uploaded filename is sanitised to a safe basename
     (separators / ``..`` / leading dots stripped); the final save path is
     resolved and asserted (resolved-prefix check) to be INSIDE the resolved
     deal folder — any escape is refused (400).
  5. The extracted text is injection-scanned (detect-and-audit; never blocks).
  6. CAPS: >25 MB → 413; unsupported extension → 422; extracted text capped at
     ~24k chars (truncate + flag). The size cap is enforced in TWO places: an
     EARLY ``Content-Length`` header check rejects an over-cap upload before the
     body is consumed, and the streamed-copy running-total check is the
     defence-in-depth bound that still holds when ``Content-Length`` is
     absent / understated. (Starlette pre-buffers a multipart body to a spooled
     temp file before the handler runs, so the early header check is what keeps
     a declared-oversize upload from being spooled at all.)
  7. AUDIT records filename + byte size + extracted char count ONLY — never the
     document text.
"""

from __future__ import annotations

import logging
import re
import time
import unicodedata
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from pydantic import BaseModel

from routines.api.deps import RUNS_DIR, VAULT
# Reuse the SAME loopback dependency credentials/budgets/intake bind on, so a
# test override or any future fix lands on one function.
from routines.api.routes.credentials import _loopback_only
from routines.api.routes.workspaces import _roots_for, _validate_name
from routines.attachments.extract import (
    SUPPORTED_EXTENSIONS,
    AttachmentExtractError,
    ExtractedDoc,
    UnsupportedAttachment,
    extract_text,
)
from routines.guards.injection import scan_ingested_text
from routines.sessions.store import Session, SessionStore
from routines.shared import audit, profile as profile_mod

router = APIRouter(dependencies=[Depends(_loopback_only)])
log = logging.getLogger(__name__)


# Hard cap on the uploaded file size (§invariant 8). A teaser/CIM is a few MB;
# 25 MB is generous headroom. Enforced in two layers: (1) an EARLY
# ``Content-Length`` header check in the route rejects a declared-oversize
# upload BEFORE the body is read (Starlette would otherwise spool the whole
# multipart body to a temp file before the handler runs); (2) the streamed-copy
# running-total check below aborts once the bytes actually written cross the cap
# — the defence-in-depth bound that still holds when ``Content-Length`` is
# missing or lying.
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
_STREAM_CHUNK = 1024 * 1024  # 1 MB read chunks while measuring the size

# Where general-workspace uploads land under the workspace dir.
_GENERAL_UPLOADS_SUBDIR = "uploads"
# Where project / bd uploads land under the deal folder.
_DEAL_RECEIVED_SUBDIR = "5. Received from Client"


# ────────────────────────────────────────────────────────────────────────────
# Store singleton — share the sessions store instance
# ────────────────────────────────────────────────────────────────────────────


def _store() -> SessionStore:
    """Reuse the sessions route's cached store (same DB / env resolution)."""
    from routines.api.routes.sessions import _store as _sessions_store

    return _sessions_store()


# ────────────────────────────────────────────────────────────────────────────
# IO models
# ────────────────────────────────────────────────────────────────────────────


class AttachmentResponse(BaseModel):
    filename: str            # the safe basename actually saved (collision-suffixed)
    saved_relpath: str       # path relative to the workspace root, for display
    chars: int               # extracted char count (after truncation)
    truncated: bool
    text: str                # the LOCALLY-extracted text to inject into the turn
    sensitivity: str         # public | internal | confidential | MNPI of the workspace


# ────────────────────────────────────────────────────────────────────────────
# Filename sanitisation + path safety (§invariant 4)
# ────────────────────────────────────────────────────────────────────────────


# Allow letters, digits, space, dot, underscore, hyphen, parens. Everything else
# (path separators, control chars, shell-special chars) is replaced with '_'.
_SAFE_CHAR_RE = re.compile(r"[^A-Za-z0-9 ._()\-]")

# Windows reserved device names. A file whose stem (case-insensitive, before the
# first dot) is one of these is a DEVICE, not a regular file, on Windows — so
# ``CON.pdf`` would resolve to the console device rather than a file in the deal
# folder. We prefix the basename with ``_`` so it can never name a device.
_WINDOWS_RESERVED_NAMES: frozenset[str] = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)


def _safe_basename(raw: str) -> str:
    """Reduce an uploaded filename to a SAFE basename (§invariant 4).

    Strips any directory component (handles both ``/`` and ``\\`` regardless of
    host OS), removes ``..`` traversal tokens, strips leading dots (so a hidden /
    relative name like ``..`` or ``.bashrc`` can't slip through), and replaces
    every character outside the allow-list. Returns ``"attachment"`` if nothing
    safe survives, so the caller always has a valid stem.
    """
    name = unicodedata.normalize("NFKC", raw or "")
    # Take only the final path component, splitting on BOTH separators so a
    # Windows-style ``..\\..\\x`` from a *nix host (or vice-versa) is handled.
    name = name.replace("\\", "/").split("/")[-1]
    # Drop any residual traversal tokens and the leading-dot case.
    name = name.replace("..", "")
    name = _SAFE_CHAR_RE.sub("_", name)
    name = name.strip().lstrip(".").strip()
    # Collapse internal whitespace runs.
    name = " ".join(name.split())
    if not name or name in (".", ".."):
        return "attachment"
    # Block Windows reserved device names: if the stem (before the first dot,
    # case-insensitive) is a device name, prefix ``_`` so the basename can't
    # resolve to a device (e.g. ``CON.pdf`` → ``_CON.pdf``).
    stem = name.split(".", 1)[0]
    if stem.upper() in _WINDOWS_RESERVED_NAMES:
        name = "_" + name
    # Bound the length defensively (filesystem + display).
    return name[:200]


def _resolve_under(base_dir: Path, basename: str) -> Path:
    """Resolve ``base_dir / basename`` and ASSERT it stays inside ``base_dir``
    (resolved-prefix check). Refuse (HTTP 400) on any escape — defence in depth
    on top of ``_safe_basename`` (§invariant 4)."""
    base_resolved = base_dir.resolve()
    candidate = (base_resolved / basename).resolve()
    try:
        candidate.relative_to(base_resolved)
    except ValueError as e:
        log.warning(
            "attachment save path escaped the deal folder (basename=%r) — refusing",
            basename,
        )
        raise HTTPException(
            status_code=400,
            detail="attachment filename resolves outside the deal folder; refused",
        ) from e
    return candidate


def _candidate_paths(base_dir: Path, basename: str):
    """Yield collision-free candidate save paths under ``base_dir`` for
    ``basename`` — first the bare name, then ``name (1).ext``, ``name (2).ext`` …
    and finally a timestamped fallback. EVERY candidate is re-checked with the
    resolved-prefix containment guard (``_resolve_under``) before it is yielded,
    so the actual save (an exclusive create in ``_save_streamed``) only ever
    touches paths proven to be inside the deal folder. We deliberately do NOT
    pre-check ``.exists()`` here — the caller's ``open(..., "xb")`` is the atomic
    collision test (no TOCTOU window)."""
    target = _resolve_under(base_dir, basename)
    yield target
    stem = target.stem
    suffix = target.suffix
    for i in range(1, 1000):
        yield _resolve_under(base_dir, f"{stem} ({i}){suffix}")
    # Pathological: 1000 collisions. Fall back to a timestamped name.
    stamp = str(int(time.time()))
    yield _resolve_under(base_dir, f"{stem} ({stamp}){suffix}")


# ────────────────────────────────────────────────────────────────────────────
# Deal-folder resolution
# ────────────────────────────────────────────────────────────────────────────


def _workspace_sensitivity(session: Session) -> str:
    """The session workspace's default sensitivity label (#chat-attachments).

    Mirrors ``routines.sessions.router._WORKSPACE_SENSITIVITY`` — project/bd →
    confidential, general → internal. Returned to the dashboard so it can show
    the operator what tier the injected text will route at."""
    return {
        "project": "confidential",
        "bd": "confidential",
        "general": "internal",
    }.get(session.workspace_type, "confidential")


def _resolve_save_dir(session: Session) -> tuple[Path, Path]:
    """Resolve ``(save_dir, workspace_root)`` for ``session``.

    project / bd: find the configured external root (via
    ``workspaces._roots_for``) that contains an existing dir ``<name>``; save
    under ``<deal>/5. Received from Client/``. general:
    ``<external_general_path>/<name>/uploads/`` (created if missing).

    Raises HTTPException(404) if no deal folder can be found, (500) if no root
    is configured.
    """
    name = _validate_name(session.workspace_name)
    prof = profile_mod.load(VAULT)
    roots = _roots_for(session.workspace_type, prof)
    if not roots:
        raise HTTPException(
            status_code=500,
            detail=(
                f"no filesystem root configured for workspace type "
                f"{session.workspace_type!r}; check profile.md"
            ),
        )

    if session.workspace_type == "general":
        # general → <external_general_path>/<name>/uploads/ (create if missing).
        workspace_root = roots[0] / name
        save_dir = workspace_root / _GENERAL_UPLOADS_SUBDIR
        save_dir.mkdir(parents=True, exist_ok=True)
        return save_dir, workspace_root

    # project / bd → the root whose <name> subdir EXISTS.
    for root in roots:
        candidate = root / name
        if candidate.is_dir():
            save_dir = candidate / _DEAL_RECEIVED_SUBDIR
            save_dir.mkdir(parents=True, exist_ok=True)
            return save_dir, candidate

    raise HTTPException(
        status_code=404,
        detail=(
            f"deal folder for workspace {session.workspace_name!r} not found under "
            f"any configured {session.workspace_type!r} root"
        ),
    )


# ────────────────────────────────────────────────────────────────────────────
# Upload streaming with the size cap
# ────────────────────────────────────────────────────────────────────────────


def _save_streamed(upload: UploadFile, candidates) -> tuple[Path, int]:
    """Exclusively-create the first non-colliding candidate path and stream
    ``upload`` into it in chunks, aborting (and cleaning up) if the running total
    exceeds ``MAX_UPLOAD_BYTES`` (§invariant 8).

    The write uses ``open(path, "xb")`` (O_CREAT|O_EXCL) so the collision check
    is ATOMIC — no check-then-open TOCTOU window. On ``FileExistsError`` we
    advance to the next pre-validated candidate and retry. ``candidates`` is an
    iterable of containment-checked paths from ``_candidate_paths``.

    This running-total guard is the defence-in-depth byte-count bound that holds
    even when the early ``Content-Length`` route check was skipped (header absent
    or understated); Starlette has already spooled the body to a temp file by
    this point, so this caps what we COPY into the deal folder, not what was
    received. Returns ``(dest, byte_count)``."""
    last_exc: FileExistsError | None = None
    for dest in candidates:
        try:
            out = open(dest, "xb")  # exclusive create — atomic collision test
        except FileExistsError as e:
            last_exc = e
            continue
        total = 0
        try:
            with out:
                while True:
                    chunk = upload.file.read(_STREAM_CHUNK)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_UPLOAD_BYTES:
                        out.close()
                        dest.unlink(missing_ok=True)
                        raise HTTPException(
                            status_code=413,
                            detail=(
                                f"attachment exceeds the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB cap"
                            ),
                        )
                    out.write(chunk)
        except HTTPException:
            raise
        except OSError as e:
            dest.unlink(missing_ok=True)
            raise HTTPException(
                status_code=500, detail="could not save attachment",
            ) from e
        return dest, total
    # Every candidate already existed (pathological — the timestamped fallback
    # collided too). Surface a clean 500 rather than silently overwriting.
    raise HTTPException(
        status_code=500, detail="could not save attachment",
    ) from last_exc


def _reject_oversize_by_content_length(request: Request) -> None:
    """Route DEPENDENCY: refuse (413) when the request DECLARES a body larger
    than the cap, BEFORE the multipart body is parsed/spooled.

    This runs as a FastAPI ``Depends`` so it executes during dependency
    resolution — *before* the ``file: UploadFile = File(...)`` body param is
    resolved (which is what makes Starlette spool the whole multipart body to a
    temp file). Rejecting here means a declared-oversize upload is refused
    without spooling 25 MB to disk first.

    Best-effort: a malformed / absent ``Content-Length`` is ignored here (the
    streamed-copy running-total guard in ``_save_streamed`` is the backstop). The
    declared length is the whole multipart envelope (slightly larger than the
    file), so this only ever rejects uploads that are already over the cap —
    never a borderline file.
    """
    raw = request.headers.get("content-length")
    if raw is None:
        return
    try:
        declared = int(raw)
    except (TypeError, ValueError):
        return
    if declared > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=(
                f"attachment exceeds the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB cap"
            ),
        )


# ────────────────────────────────────────────────────────────────────────────
# Route
# ────────────────────────────────────────────────────────────────────────────


@router.post(
    "/sessions/{session_id}/attachments",
    response_model=AttachmentResponse,
    # EARLY size reject (defence-in-depth #1): the Content-Length guard runs as a
    # dependency so it fires BEFORE ``File(...)`` is parsed (i.e. before Starlette
    # spools the multipart body to a temp file). A missing / lying Content-Length
    # is still caught by the streamed-copy running-total guard in
    # ``_save_streamed``.
    dependencies=[Depends(_reject_oversize_by_content_length)],
)
def upload_attachment(
    session_id: str,
    file: UploadFile = File(...),
) -> AttachmentResponse:
    """Save a chat document-attachment to the deal folder, extract its text
    LOCALLY, injection-scan it, and return the text for chat injection."""
    started = time.monotonic()

    store = _store()
    sess = store.get_session(session_id)
    if sess is None:
        raise HTTPException(status_code=404, detail=f"session not found: {session_id}")

    run_id = audit.new_run_id()

    # Sanitise the filename and check the extension BEFORE saving anything
    # (§invariant 4 + the unsupported-type 422).
    safe_name = _safe_basename(file.filename or "attachment")
    ext = Path(safe_name).suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        raise HTTPException(
            status_code=422,
            detail=(
                f"unsupported attachment type {ext!r}; supported: "
                f"{sorted(SUPPORTED_EXTENSIONS)}"
            ),
        )

    # Resolve the deal folder + the collision-safe, escape-checked candidate
    # save paths (every candidate re-validated by the resolved-prefix guard).
    save_dir, workspace_root = _resolve_save_dir(sess)
    candidates = _candidate_paths(save_dir, safe_name)

    # Stream-save with the size cap, exclusively creating the first free
    # candidate (atomic collision test — no TOCTOU).
    dest, byte_size = _save_streamed(file, candidates)

    # Extract LOCALLY (§invariant 1+2). On failure, the saved binary stays on
    # disk (the operator may still want the file) but we surface a 422. The
    # client ``detail`` is GENERIC ("could not extract text from <filename>") and
    # the audit records only the error CLASS — never the raw exception string,
    # which can leak a parser/OS path or internal detail. The raw cause is logged
    # LOCALLY via the module logger only.
    try:
        extracted: ExtractedDoc = extract_text(dest)
    except (UnsupportedAttachment, AttachmentExtractError) as e:
        log.warning(
            "attachment extraction failed for %s (%s): %s",
            dest.name, type(e).__name__, e,
        )
        _audit_failure(run_id, sess, dest, byte_size, started, type(e).__name__)
        raise HTTPException(
            status_code=422,
            detail=f"could not extract text from {dest.name}",
        ) from e

    # Injection-scan the extracted text (detect-and-audit; NEVER blocks).
    scan_ingested_text(extracted.text, source="chat-attachment")

    saved_relpath = _relpath_for_display(dest, workspace_root)
    sensitivity = _workspace_sensitivity(sess)

    # AUDIT — filename + byte size + extracted char count ONLY (§invariant 7).
    # NEVER the document text. The saved relpath is recorded so the operator can
    # find the file; the text is deliberately omitted.
    audit.write_structured(
        actor={"type": "user", "id": "operator"},
        entity_type="session",
        entity_id=session_id,
        action="attach",
        routine="sessions.attachments",
        audit_dir=RUNS_DIR,
        run_id=run_id,
        status="ok",
        inputs={
            "session_id": session_id,
            "workspace_type": sess.workspace_type,
            "workspace_name": sess.workspace_name,
            "filename": dest.name,
            "byte_size": byte_size,
            "extension": ext,
        },
        outputs={
            "saved_relpath": saved_relpath,
            "chars": extracted.chars,
            "truncated": extracted.truncated,
            "lane": extracted.lane,
            "sensitivity": sensitivity,
        },
        duration_ms=int((time.monotonic() - started) * 1000),
    )

    return AttachmentResponse(
        filename=dest.name,
        saved_relpath=saved_relpath,
        chars=extracted.chars,
        truncated=extracted.truncated,
        text=extracted.text,
        sensitivity=sensitivity,
    )


def _relpath_for_display(dest: Path, workspace_root: Path) -> str:
    """Path relative to the workspace root (POSIX), for display. Falls back to
    the basename if ``dest`` isn't under the root (shouldn't happen — the
    resolved-prefix guard already proved containment in the save dir)."""
    try:
        return dest.resolve().relative_to(workspace_root.resolve()).as_posix()
    except ValueError:
        return dest.name


def _audit_failure(
    run_id: str,
    sess: Session,
    dest: Path,
    byte_size: int,
    started: float,
    error_class: str,
) -> None:
    """Audit an extraction failure — filename + size + error CLASS only (e.g.
    ``AttachmentExtractError``), never the raw exception string or the document
    text (§invariant 7). The raw cause is logged locally by the caller."""
    audit.write_structured(
        actor={"type": "user", "id": "operator"},
        entity_type="session",
        entity_id=sess.id,
        action="attach",
        routine="sessions.attachments",
        audit_dir=RUNS_DIR,
        run_id=run_id,
        status="error",
        inputs={
            "session_id": sess.id,
            "workspace_type": sess.workspace_type,
            "filename": dest.name,
            "byte_size": byte_size,
        },
        error=error_class,
        duration_ms=int((time.monotonic() - started) * 1000),
    )
