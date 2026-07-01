"""Folder watcher for the PDF intake routine.

Watches a folder for new PDF files and ingests each into the vault as it
arrives. Modelled on ``routines/hinotes/mover.py`` but the watcher fires
the full intake pipeline rather than just moving files.

Processed PDFs are moved to a ``processed/`` subfolder under the watched
directory so they're not re-ingested on restart. Failures land in
``failed/`` with the audit trail captured in ``runs/pdf-intake.jsonl``.

Usage from CLI:

    pdf-intake watch <user>/Downloads/incoming-teasers

For Outlook attachments: set up a rule in Outlook (or Power Automate) to
save attachments matching a sender / subject filter to the watched
folder. No M365 API access required — Outlook does the routing client-
side; the watcher takes it from there.
"""

from __future__ import annotations

import logging
import re
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from routines.intake.parse import (
    DEFAULT_BATCH_SIZE, DEFAULT_MAX_PAGES, DEFAULT_TEXT_THRESHOLD, ingest_pdf,
)
from routines.intake.writer import write_intake_note
from routines.shared import audit
from routines.shared.ollama_client import OllamaClient, OllamaError

logger = logging.getLogger("intake.watcher")


PROCESSED_DIR_NAME = "processed"
FAILED_DIR_NAME = "failed"
SETTLE_SECONDS = 3.0
SETTLE_DEADLINE_SECONDS = 60.0
SETTLE_STEP_SECONDS = 0.5


@dataclass(frozen=True)
class WatchConfig:
    source_dir: Path
    vault_root: Path
    audit_dir: Path
    pattern: re.Pattern[str] | None = None     # filename regex, default = all PDFs
    max_pages: int = DEFAULT_MAX_PAGES
    batch_size: int = DEFAULT_BATCH_SIZE
    text_threshold: int = DEFAULT_TEXT_THRESHOLD
    mode: str = "auto"                          # "auto" | "text" | "image"

    @property
    def processed_dir(self) -> Path:
        return self.source_dir / PROCESSED_DIR_NAME

    @property
    def failed_dir(self) -> Path:
        return self.source_dir / FAILED_DIR_NAME


class PDFIntakeHandler(FileSystemEventHandler):
    """Watchdog handler — on a new PDF, ingest it then archive the source."""

    def __init__(self, config: WatchConfig, client: OllamaClient) -> None:
        self.config = config
        self.client = client
        self._inflight: set[str] = set()

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._maybe_process(Path(event.src_path))

    def on_moved(self, event: FileSystemEvent) -> None:  # type: ignore[override]
        if event.is_directory:
            return
        dest = getattr(event, "dest_path", None)
        if dest:
            self._maybe_process(Path(dest))

    def _maybe_process(self, path: Path) -> None:
        try:
            if not self._is_candidate(path):
                return
            if not _wait_for_stable(path):
                logger.warning("pdf-intake.watch: %s never stabilised", path.name)
                return
            key = str(path.resolve())
            if key in self._inflight:
                return
            self._inflight.add(key)
            try:
                process_one(path, self.config, self.client)
            finally:
                self._inflight.discard(key)
        except Exception as e:  # noqa: BLE001
            logger.exception("pdf-intake.watch: handler error on %s: %s", path, e)

    def _is_candidate(self, path: Path) -> bool:
        if path.suffix.lower() != ".pdf":
            return False
        if path.name.startswith("."):
            return False
        # Skip files already inside the processed/ or failed/ subfolders.
        try:
            rel = path.resolve().relative_to(self.config.source_dir.resolve())
        except ValueError:
            return False
        if rel.parts and rel.parts[0] in (PROCESSED_DIR_NAME, FAILED_DIR_NAME):
            return False
        if self.config.pattern and not self.config.pattern.search(path.name):
            return False
        return True


def process_one(pdf: Path, config: WatchConfig, client: OllamaClient) -> Path | None:
    """Ingest a single PDF; move source to processed/ or failed/.

    Returns the written vault-note path on success, ``None`` on failure.
    """
    started = time.monotonic()
    run_id = audit.new_run_id()
    logger.info("pdf-intake.watch: ingesting %s", pdf.name)

    try:
        result = ingest_pdf(
            pdf, client=client,
            max_pages=config.max_pages,
            batch_size=config.batch_size,
            text_threshold=config.text_threshold,
            mode=config.mode,
        )
    except (OllamaError, OSError) as e:
        duration_ms = int((time.monotonic() - started) * 1000)
        audit.write_structured(
            actor={"type": "system", "id": "routine:pdf-intake"},
            entity_type="vault_note",
            entity_id=str(pdf),
            action="ingest",
            routine="pdf-intake", run_id=run_id, status="error",
            audit_dir=config.audit_dir,
            inputs={"pdf": str(pdf), "via": "watch"},
            error=str(e), duration_ms=duration_ms,
        )
        _archive(pdf, config.failed_dir)
        logger.error("pdf-intake.watch: FAILED %s: %s", pdf.name, e)
        return None

    note_path = write_intake_note(
        config.vault_root, result.parsed,
        source_pdf=pdf, run_id=run_id,
    )
    duration_ms = int((time.monotonic() - started) * 1000)
    audit.write_structured(
        actor={"type": "system", "id": "routine:pdf-intake"},
        entity_type="vault_note",
        entity_id=str(note_path),
        action="ingest",
        routine="pdf-intake", run_id=run_id, status="ok",
        audit_dir=config.audit_dir,
        inputs={
            "pdf": str(pdf), "via": "watch", "mode": result.mode,
            "pages_processed": result.pages_processed,
            "batches": result.batches,
            "avg_chars_per_page": result.avg_chars_per_page,
        },
        outputs={
            "note_path": str(note_path),
            "doc_kind": result.parsed.doc_kind,
            "financial_rows": len(result.parsed.financials),
            "highlights": len(result.parsed.investment_highlights),
            "image_notes": len(result.parsed.image_notes),
        },
        duration_ms=duration_ms,
        episodic_source=str(pdf),
        semantic_target=str(note_path),
    )
    _archive(pdf, config.processed_dir)
    logger.info(
        "pdf-intake.watch: OK %s -> %s  [%s mode, %d batch(es), %dms]",
        pdf.name, note_path.name, result.mode, result.batches, duration_ms,
    )
    return note_path


def sweep_existing(config: WatchConfig, client: OllamaClient) -> int:
    """Process any PDFs that were already in the folder at startup."""
    config.processed_dir.mkdir(parents=True, exist_ok=True)
    config.failed_dir.mkdir(parents=True, exist_ok=True)
    pdfs = sorted(
        p for p in config.source_dir.glob("*.pdf")
        if not p.name.startswith(".")
    )
    if not pdfs:
        return 0
    logger.info("pdf-intake.watch: sweeping %d existing PDF(s)", len(pdfs))
    n = 0
    for p in pdfs:
        if process_one(p, config, client) is not None:
            n += 1
    return n


def run_watch(config: WatchConfig, client: OllamaClient) -> None:
    """Start the watchdog observer; block until KeyboardInterrupt."""
    config.source_dir.mkdir(parents=True, exist_ok=True)
    config.processed_dir.mkdir(parents=True, exist_ok=True)
    config.failed_dir.mkdir(parents=True, exist_ok=True)

    sweep_existing(config, client)

    handler = PDFIntakeHandler(config, client)
    observer = Observer()
    observer.schedule(handler, str(config.source_dir), recursive=False)
    observer.start()
    logger.info(
        "pdf-intake.watch: watching %s (mode=%s, batch=%d)  [Ctrl-C to stop]",
        config.source_dir, config.mode, config.batch_size,
    )
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        logger.info("pdf-intake.watch: stopping")
    finally:
        observer.stop()
        observer.join()


# ── helpers ───────────────────────────────────────────────────────────────


def _wait_for_stable(path: Path) -> bool:
    """Wait for file size to stabilise — handles still-downloading PDFs."""
    last_size = -1
    stable_for = 0.0
    deadline = time.monotonic() + SETTLE_DEADLINE_SECONDS
    while time.monotonic() < deadline:
        try:
            size = path.stat().st_size
        except OSError:
            return False
        if size == last_size and size > 0:
            stable_for += SETTLE_STEP_SECONDS
            if stable_for >= SETTLE_SECONDS:
                return True
        else:
            stable_for = 0.0
            last_size = size
        time.sleep(SETTLE_STEP_SECONDS)
    return False


def _archive(pdf: Path, target_dir: Path) -> None:
    """Move ``pdf`` into ``target_dir``, suffixing the filename if it collides."""
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / pdf.name
    if target.exists():
        stem, suffix = pdf.stem, pdf.suffix
        for i in range(1, 1000):
            candidate = target_dir / f"{stem}-{i}{suffix}"
            if not candidate.exists():
                target = candidate
                break
    try:
        shutil.move(str(pdf), str(target))
    except OSError as e:
        logger.warning("pdf-intake.watch: archive move failed (%s -> %s): %s",
                       pdf, target, e)
