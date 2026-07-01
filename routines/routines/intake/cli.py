"""CLI for the PDF intake routine.

Two commands:

    pdf-intake ingest <pdf>      — one-shot: render/extract, parse, write to vault
    pdf-intake watch  <folder>   — daemon: watch a folder, ingest each new PDF
"""

from __future__ import annotations

import logging
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import click

from routines.intake.parse import (
    DEFAULT_BATCH_SIZE, DEFAULT_MAX_PAGES, DEFAULT_TEXT_THRESHOLD, ingest_pdf,
)
from routines.intake.pdf_render import PDFRenderError
from routines.intake.watcher import WatchConfig, run_watch
from routines.intake.writer import write_intake_note
from routines.shared import audit
from routines.shared.ollama_client import OllamaClient, OllamaError

DEFAULT_VAULT = Path("/mnt/x/OS AI Vault")
DEFAULT_AUDIT_DIR = Path(__file__).resolve().parent.parent.parent / "runs"


@click.group()
@click.option("--debug", is_flag=True)
def main(debug: bool) -> None:
    """PDF intake — text-first extraction with multimodal fallback."""
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


# ── ingest ────────────────────────────────────────────────────────────────


@main.command("ingest")
@click.argument("pdf", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--vault", type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=DEFAULT_VAULT)
@click.option("--max-pages", type=int, default=DEFAULT_MAX_PAGES, show_default=True)
@click.option("--batch-size", type=int, default=DEFAULT_BATCH_SIZE, show_default=True,
              help="Pages per LLM call. Both qwen3 + gemma4 reliably handle <=6.")
@click.option("--mode", type=click.Choice(["auto", "text", "image"]), default="auto",
              show_default=True,
              help="auto: pick text vs image by content; text/image force a path.")
@click.option("--text-threshold", type=int, default=DEFAULT_TEXT_THRESHOLD,
              show_default=True,
              help="Auto-mode: avg chars/page above which we use the text path.")
@click.option("--ollama-url", default="http://localhost:11434")
@click.option("--dry-run", is_flag=True,
              help="Print parsed JSON to stdout instead of writing the note")
def ingest_cmd(
    pdf: Path, vault: Path,
    max_pages: int, batch_size: int, mode: str, text_threshold: int,
    ollama_url: str, dry_run: bool,
) -> None:
    """Parse a single PDF into a vault intake note."""
    started = time.monotonic()
    run_id = audit.new_run_id()
    client = OllamaClient(base_url=ollama_url)
    try:
        client.health()
    except OllamaError as e:
        click.echo(f"Ollama unreachable at {ollama_url}: {e}", err=True)
        sys.exit(2)

    click.echo(f"ingesting {pdf.name} (mode={mode}, batch_size={batch_size})...")
    try:
        result = ingest_pdf(
            pdf, client=client,
            max_pages=max_pages, batch_size=batch_size,
            mode=mode, text_threshold=text_threshold,
        )
    except (PDFRenderError, OllamaError) as e:
        click.echo(f"ingest failed: {e}", err=True)
        audit.write_structured(
            actor={"type": "system", "id": "routine:pdf-intake"},
            entity_type="vault_note",
            entity_id=str(pdf),
            action="ingest",
            routine="pdf-intake", run_id=run_id, status="error",
            audit_dir=DEFAULT_AUDIT_DIR,
            inputs={"pdf": str(pdf), "via": "cli"}, error=str(e),
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        sys.exit(2)

    click.echo(
        f"  -> mode={result.mode}, pages={result.pages_processed}, "
        f"batches={result.batches}, avg chars/page={result.avg_chars_per_page}"
    )

    if dry_run:
        click.echo("\n-- dry-run: parsed payload --\n")
        click.echo(result.parsed.model_dump_json(indent=2))
        return

    when = datetime.now(timezone.utc)
    path = write_intake_note(
        vault, result.parsed, source_pdf=pdf, run_id=run_id, when=when,
    )
    click.echo(f"\nOK wrote {path}")

    duration_ms = int((time.monotonic() - started) * 1000)
    audit.write_structured(
        actor={"type": "system", "id": "routine:pdf-intake"},
        entity_type="vault_note",
        entity_id=str(path),
        action="ingest",
        routine="pdf-intake", run_id=run_id, status="ok",
        audit_dir=DEFAULT_AUDIT_DIR,
        inputs={
            "pdf": str(pdf), "via": "cli", "mode": result.mode,
            "pages_processed": result.pages_processed,
            "batches": result.batches,
            "avg_chars_per_page": result.avg_chars_per_page,
        },
        outputs={
            "path": str(path),
            "doc_kind": result.parsed.doc_kind,
            "financial_rows": len(result.parsed.financials),
            "highlights": len(result.parsed.investment_highlights),
            "image_notes": len(result.parsed.image_notes),
        },
        duration_ms=duration_ms,
        episodic_source=str(pdf),
        semantic_target=str(path),
    )


# ── watch ─────────────────────────────────────────────────────────────────


@main.command("watch")
@click.argument("folder", type=click.Path(file_okay=False, path_type=Path))
@click.option("--vault", type=click.Path(exists=True, file_okay=False, path_type=Path),
              default=DEFAULT_VAULT)
@click.option("--pattern", default=None,
              help="Optional regex on filenames (default: every *.pdf)")
@click.option("--mode", type=click.Choice(["auto", "text", "image"]), default="auto",
              show_default=True)
@click.option("--batch-size", type=int, default=DEFAULT_BATCH_SIZE, show_default=True)
@click.option("--max-pages", type=int, default=DEFAULT_MAX_PAGES, show_default=True)
@click.option("--text-threshold", type=int, default=DEFAULT_TEXT_THRESHOLD, show_default=True)
@click.option("--ollama-url", default="http://localhost:11434")
def watch_cmd(
    folder: Path, vault: Path,
    pattern: str | None, mode: str,
    batch_size: int, max_pages: int, text_threshold: int,
    ollama_url: str,
) -> None:
    """Watch FOLDER for new PDFs and ingest each one as it arrives.

    Processed files move to FOLDER/processed/; failures to FOLDER/failed/.
    """
    client = OllamaClient(base_url=ollama_url)
    try:
        client.health()
    except OllamaError as e:
        click.echo(f"Ollama unreachable at {ollama_url}: {e}", err=True)
        sys.exit(2)

    config = WatchConfig(
        source_dir=folder, vault_root=vault, audit_dir=DEFAULT_AUDIT_DIR,
        pattern=re.compile(pattern) if pattern else None,
        max_pages=max_pages, batch_size=batch_size,
        text_threshold=text_threshold, mode=mode,
    )
    run_watch(config, client)


if __name__ == "__main__":
    main()
