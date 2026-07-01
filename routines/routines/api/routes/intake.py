"""POST /api/workflows/pdf-intake — text-first extraction with multimodal fallback.

Takes a server-side path to a PDF, runs the intake pipeline (auto-pick
text vs image path → batch → parse → merge → optional vault write),
returns the parsed payload + the vault path.

Exposure posture (#sec-read-path-policy, Shannon DAST 2026-06-12; this
docstring previously credited CORS, which never gates non-browser
requests): the bridge binds to 127.0.0.1 only, this router carries the
explicit ``_loopback_only`` dependency (the #25b guard credentials /
budgets / operator-config use), and browser-side protection is the F-1
security middleware (origin allowlist + unconditional JSON content type).
The operator-supplied ``path`` is bounded by the central read-roots
policy (``routines.shared.read_policy`` — vault, Corporate Finance drive,
project workspaces, plus ``AGENTIC_INTAKE_READ_ROOTS`` extensions),
checked lexically BEFORE any filesystem touch.

Multipart upload is deferred — the operator drives ingestion either via
the CLI watch command (drop PDFs into a watched folder) or by POSTing a
path here.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from routines.api.deps import RUNS_DIR, VAULT
# The #25b loopback dependency — the SAME helper credentials/budgets use,
# so test overrides and any future fix bind on one function.
from routines.api.routes.credentials import _loopback_only
from routines.hooks import tool_call_hooks
from routines.intake.parse import (
    DEFAULT_BATCH_SIZE, DEFAULT_MAX_PAGES, DEFAULT_TEXT_THRESHOLD, ingest_pdf,
)
from routines.intake.pdf_render import PDFRenderError
from routines.intake.schema import ParsedDocument
from routines.intake.writer import write_intake_note
from routines.shared import audit
from routines.shared.ollama_client import OllamaClient, OllamaError
from routines.shared.read_policy import ReadPolicyViolation, ensure_read_allowed

router = APIRouter(dependencies=[Depends(_loopback_only)])
log = logging.getLogger(__name__)


class PDFIntakeRequest(BaseModel):
    path: str = Field(..., description="Absolute path to a PDF readable by the bridge process")
    max_pages: int = Field(DEFAULT_MAX_PAGES, ge=1, le=80)
    batch_size: int = Field(DEFAULT_BATCH_SIZE, ge=1, le=20)
    mode: str = Field("auto", pattern="^(auto|text|image)$")
    text_threshold: int = Field(DEFAULT_TEXT_THRESHOLD, ge=0, le=10000)
    dry_run: bool = Field(False, description="Skip the vault write; return only the parsed payload")


class PDFIntakeResponse(BaseModel):
    parsed: ParsedDocument
    note_path: str | None = None      # vault-relative POSIX when dry_run=False
    mode: str = ""                     # "text" | "image" — which path the orchestrator picked
    pages_processed: int = 0
    batches: int = 0
    avg_chars_per_page: float = 0.0
    duration_ms: int = 0
    run_id: str = ""


@router.post("/workflows/pdf-intake", response_model=PDFIntakeResponse)
def workflow_pdf_intake(req: PDFIntakeRequest) -> PDFIntakeResponse:
    """Run the intake pipeline on a single PDF; optionally write the vault note."""
    started = time.monotonic()
    pdf = Path(req.path)
    # Read-roots policy BEFORE any filesystem touch (#sec-read-path-policy):
    # ``is_file()`` on an attacker-shaped UNC path would dial out to the
    # named share, and its 400-vs-404 split would be an existence oracle
    # for paths the policy refuses anyway. Lexical check first.
    try:
        ensure_read_allowed(pdf, op="pdf-intake read")
    except ReadPolicyViolation as e:
        raise HTTPException(status_code=403, detail=str(e)) from e
    if not pdf.is_file():
        raise HTTPException(status_code=400, detail=f"PDF not found at {req.path!r}")

    # PDF intake reads arbitrary operator-supplied paths (CIMs / teasers /
    # filings); default sensitivity to confidential — CIMs are MNPI-by-
    # default per [[CLAUDE]] §4 but lifting that high blocks the call
    # under the Ollama-only rule until /triage lane lands.
    with tool_call_hooks(
        tool_name="pdf_intake",
        sensitivity="confidential",
        tool_input=req.model_dump(),
    ) as ctx:
        result = _pdf_intake_impl(req, pdf, started)
        ctx.result = result.model_dump()
        return result


def _pdf_intake_impl(req: PDFIntakeRequest, pdf: Path, started: float) -> PDFIntakeResponse:
    run_id = audit.new_run_id()
    client = OllamaClient()

    try:
        result = ingest_pdf(
            pdf, client=client,
            max_pages=req.max_pages, batch_size=req.batch_size,
            mode=req.mode, text_threshold=req.text_threshold,
        )
    except PDFRenderError as e:
        audit.write_structured(
            actor={"type": "user", "id": "operator"},
            entity_type="vault_note",
            entity_id=str(pdf),
            action="ingest",
            routine="pdf-intake", run_id=run_id, status="error",
            audit_dir=RUNS_DIR,
            inputs={"pdf": str(pdf), "via": "bridge"},
            error=str(e), duration_ms=int((time.monotonic() - started) * 1000),
        )
        raise HTTPException(status_code=422, detail=f"PDF render failed: {e}") from e
    except OllamaError as e:
        audit.write_structured(
            actor={"type": "user", "id": "operator"},
            entity_type="vault_note",
            entity_id=str(pdf),
            action="ingest",
            routine="pdf-intake", run_id=run_id, status="error",
            audit_dir=RUNS_DIR,
            inputs={"pdf": str(pdf), "via": "bridge"},
            error=str(e), duration_ms=int((time.monotonic() - started) * 1000),
        )
        raise HTTPException(status_code=502, detail=f"Parse failed: {e}") from e

    when = datetime.now(timezone.utc)
    note_path: str | None = None
    if not req.dry_run:
        path = write_intake_note(VAULT, result.parsed, source_pdf=pdf,
                                  run_id=run_id, when=when)
        try:
            note_path = path.relative_to(VAULT).as_posix()
        except ValueError:
            note_path = str(path)

    duration_ms = int((time.monotonic() - started) * 1000)
    audit.write_structured(
        actor={"type": "user", "id": "operator"},
        entity_type="vault_note",
        entity_id=str(pdf),
        action="ingest",
        routine="pdf-intake", run_id=run_id, status="ok",
        audit_dir=RUNS_DIR,
        inputs={
            "pdf": str(pdf), "via": "bridge",
            "mode": result.mode,
            "pages_processed": result.pages_processed,
            "batches": result.batches,
            "avg_chars_per_page": result.avg_chars_per_page,
            "dry_run": req.dry_run,
        },
        outputs={
            "note_path": note_path,
            "doc_kind": result.parsed.doc_kind,
            "financial_rows": len(result.parsed.financials),
            "highlights": len(result.parsed.investment_highlights),
            "image_notes": len(result.parsed.image_notes),
        },
        duration_ms=duration_ms,
        episodic_source=str(pdf),
        semantic_target=note_path,
    )

    return PDFIntakeResponse(
        parsed=result.parsed,
        note_path=note_path,
        mode=result.mode,
        pages_processed=result.pages_processed,
        batches=result.batches,
        avg_chars_per_page=result.avg_chars_per_page,
        duration_ms=duration_ms,
        run_id=run_id,
    )
