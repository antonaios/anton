"""HiNotes transcript watcher daemon.

Watches `Inbox/HiNotes/incoming/` for new files and dispatches them to
processor.process_one. Real-time: typically <2 seconds from drop to processing
start.

Entrypoint: `hinotes-watcher` CLI script (see pyproject.toml).

Usage:

    hinotes-watcher start                       # default vault path
    hinotes-watcher start --vault <vault>
    hinotes-watcher process-once <file>          # one-shot, useful for tests
    hinotes-watcher health                       # sanity check Ollama + paths

For always-on operation, install the systemd --user unit at
`scripts/hinotes-watcher.service`.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

import click
from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from routines.hinotes.mover import (
    DownloadsMoverHandler, MoverConfig, default_source_dir, expand_source, sweep_existing,
)
from routines.hinotes.processor import process_one
from routines.shared.ollama_client import OllamaClient, OllamaError
from routines.shared.vault_writer import VaultPaths

logger = logging.getLogger("hinotes")


DEFAULT_VAULT = Path("/mnt/x/OS AI Vault")
DEFAULT_AUDIT_DIR = Path(__file__).resolve().parent.parent.parent / "runs"
SUPPORTED_SUFFIXES = {".txt", ".md", ".srt", ".docx", ".pdf"}


# =========================================================================== watchdog


class HiNotesEventHandler(FileSystemEventHandler):
    """File-system event handler that ignores everything except real new
    transcript files in the incoming directory."""

    def __init__(self, paths: VaultPaths, client: OllamaClient, audit_dir: Path) -> None:
        self.paths = paths
        self.client = client
        self.audit_dir = audit_dir
        self._processing: set[str] = set()  # avoid double-processing on burst events

    def on_created(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        path = Path(event.src_path)
        self._maybe_process(path, "created")

    def on_moved(self, event: FileSystemEvent) -> None:
        # Some file managers create-then-rename; treat dest as a create
        if event.is_directory:
            return
        path = Path(event.dest_path)
        self._maybe_process(path, "moved")

    def _maybe_process(self, path: Path, trigger: str) -> None:
        if path.suffix.lower() not in SUPPORTED_SUFFIXES:
            logger.debug("skip non-transcript file: %s", path.name)
            return
        if path.name.startswith("."):
            logger.debug("skip hidden file: %s", path.name)
            return

        # Wait for write to complete (some apps create empty file, then write
        # in chunks). Poll size for a couple of seconds.
        if not _wait_for_stable_size(path):
            logger.warning("file size unstable, skipping: %s", path)
            return

        # Avoid double-processing on burst events
        key = str(path.resolve())
        if key in self._processing:
            return
        self._processing.add(key)
        try:
            logger.info("processing (%s): %s", trigger, path.name)
            result = process_one(
                path,
                paths=self.paths,
                client=self.client,
                audit_dir=self.audit_dir,
            )
            if result.status == "ok":
                logger.info(
                    "OK %s -> %s [project=%s, ppl=%d, cos=%d, %dms]",
                    path.name,
                    result.structured_note_path.name if result.structured_note_path else "?",
                    result.project_matched or "(none)",
                    len(result.people_stubbed),
                    len(result.companies_stubbed),
                    result.duration_ms,
                )
            elif result.status == "skipped":
                logger.info("SKIPPED %s (already processed)", path.name)
            else:
                logger.error("FAILED %s: %s", path.name, result.error)
        finally:
            self._processing.discard(key)


def _wait_for_stable_size(path: Path, *, max_wait_seconds: float = 10.0) -> bool:
    """Wait for the file size to be stable for 2 consecutive checks. Returns
    False if it never stabilises within max_wait_seconds (file probably broken)."""
    end = time.monotonic() + max_wait_seconds
    last = -1
    stable_for = 0.0
    while time.monotonic() < end:
        try:
            current = path.stat().st_size
        except FileNotFoundError:
            return False
        if current == last and current > 0:
            stable_for += 0.5
            if stable_for >= 1.0:
                return True
        else:
            stable_for = 0.0
            last = current
        time.sleep(0.5)
    return False


# =========================================================================== cli


@click.group()
@click.option("--debug", is_flag=True, help="Verbose logging")
def main(debug: bool) -> None:
    """HiNotes transcript watcher CLI."""
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


@main.command()
@click.option(
    "--vault",
    type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path),
    default=DEFAULT_VAULT,
    help="Path to vault root",
)
@click.option(
    "--audit-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=DEFAULT_AUDIT_DIR,
    help="Where to write audit log JSONL",
)
@click.option(
    "--ollama-url",
    default="http://localhost:11434",
    help="Ollama base URL",
)
@click.option(
    "--mover-from",
    default=None,
    help="Optional second watch dir (e.g. ~/Downloads) — files matching "
         "the HiNotes pattern are moved into Inbox/HiNotes/incoming. "
         "Pass '~' to auto-detect the user's Downloads folder.",
)
@click.option(
    "--mover-pattern",
    default=None,
    help="Regex (case-insensitive) to match HiNotes filenames. Default: "
         "'hinotes' anywhere in the name.",
)
def start(vault: Path, audit_dir: Path, ollama_url: str,
          mover_from: str | None, mover_pattern: str | None) -> None:
    """Start the watcher. Runs in the foreground until Ctrl-C."""
    paths = VaultPaths(vault)
    client = OllamaClient(base_url=ollama_url)

    # Health checks
    _ensure_directory(paths.hinotes_incoming)
    _ensure_directory(paths.hinotes_processed)
    _ensure_directory(paths.captures)
    _ensure_directory(paths.hinotes_unrouted)
    _ensure_directory(audit_dir)

    try:
        h = client.health()
        logger.info("ollama up: version=%s models=%s", h["version"], ", ".join(h["models"]))
    except OllamaError as e:
        logger.error("ollama not reachable: %s", e)
        sys.exit(2)

    handler = HiNotesEventHandler(paths=paths, client=client, audit_dir=audit_dir)
    observer = Observer()
    observer.schedule(handler, str(paths.hinotes_incoming), recursive=False)
    observer.start()
    logger.info("watching %s", paths.hinotes_incoming)

    # Optional: second observer for the auto-mover (Downloads → incoming).
    mover_observer: Observer | None = None
    if mover_from is not None:
        import re as _re

        source = expand_source(mover_from) if mover_from not in ("~", "") else (default_source_dir() or Path.home() / "Downloads")
        if not source.exists():
            logger.warning("mover: source dir %s does not exist — skipping auto-mover", source)
        else:
            pattern = _re.compile(mover_pattern, _re.IGNORECASE) if mover_pattern else None
            config = MoverConfig(
                source_dir=source,
                incoming_dir=paths.hinotes_incoming,
                pattern=pattern or MoverConfig.__dataclass_fields__["pattern"].default,
            )
            swept = sweep_existing(config)
            if swept:
                logger.info("mover: swept %d pre-existing file(s) from %s", swept, source)
            mover_handler = DownloadsMoverHandler(config)
            mover_observer = Observer()
            mover_observer.schedule(mover_handler, str(source), recursive=False)
            mover_observer.start()
            logger.info("mover: watching %s for HiNotes files", source)

    logger.info("press Ctrl-C to stop")

    # Sweep incoming/ once at startup in case files were dropped while watcher
    # was down
    _sweep_incoming(paths.hinotes_incoming, handler)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("shutting down")
    finally:
        observer.stop()
        observer.join()
        if mover_observer is not None:
            mover_observer.stop()
            mover_observer.join()


@main.command("process-once")
@click.argument("transcript", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option(
    "--vault",
    type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path),
    default=DEFAULT_VAULT,
)
@click.option(
    "--audit-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=DEFAULT_AUDIT_DIR,
)
@click.option(
    "--ollama-url",
    default="http://localhost:11434",
)
def process_once_cmd(
    transcript: Path, vault: Path, audit_dir: Path, ollama_url: str,
) -> None:
    """Process a single transcript file end-to-end. Useful for tests or
    one-off runs without starting the watcher."""
    paths = VaultPaths(vault)
    client = OllamaClient(base_url=ollama_url)
    _ensure_directory(audit_dir)
    result = process_one(transcript, paths=paths, client=client, audit_dir=audit_dir)
    if result.status == "ok":
        click.echo(f"OK   {result.structured_note_path}")
        click.echo(f"     transcript_md = {result.transcript_md_path}")
        click.echo(f"     project       = {result.project_matched or '(none — Captures)'}")
        click.echo(f"     people stubbed: {len(result.people_stubbed)}")
        click.echo(f"     companies stubbed: {len(result.companies_stubbed)}")
        click.echo(f"     duration: {result.duration_ms}ms")
    elif result.status == "skipped":
        click.echo(f"SKIPPED (already processed): hash={result.file_hash[:18]}")
    else:
        click.echo(f"ERROR: {result.error}")
        sys.exit(1)


@main.command()
@click.option(
    "--vault",
    type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path),
    default=DEFAULT_VAULT,
)
@click.option("--ollama-url", default="http://localhost:11434")
def health(vault: Path, ollama_url: str) -> None:
    """Sanity-check Ollama + vault paths. Use as a startup probe."""
    paths = VaultPaths(vault)
    click.echo(f"vault root:        {paths.root} ({'exists' if paths.root.exists() else 'MISSING'})")
    click.echo(f"hinotes incoming:  {paths.hinotes_incoming} "
               f"({'exists' if paths.hinotes_incoming.exists() else 'will be created on start'})")
    click.echo(f"hinotes processed: {paths.hinotes_processed} "
               f"({'exists' if paths.hinotes_processed.exists() else 'will be created on start'})")
    click.echo(f"captures:          {paths.captures} "
               f"({'exists' if paths.captures.exists() else 'will be created on start'})")
    click.echo(f"projects: {paths.list_projects()}")
    click.echo()
    client = OllamaClient(base_url=ollama_url)
    try:
        h = client.health()
        click.echo(f"ollama version:    {h['version']}")
        click.echo(f"ollama models:     {', '.join(h['models'])}")
        required = ("qwen3:14b", "qwen3:8b", "nomic-embed-text")
        missing = [m for m in required if not any(t.startswith(m) for t in h["models"])]
        if missing:
            click.echo(f"MISSING required models: {missing}")
            sys.exit(2)
        else:
            click.echo("all required models present")
    except OllamaError as e:
        click.echo(f"OLLAMA NOT REACHABLE: {e}")
        sys.exit(2)


# =========================================================================== helpers


def _ensure_directory(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


def _sweep_incoming(incoming: Path, handler: HiNotesEventHandler) -> None:
    """Process any files already sitting in incoming/ at startup."""
    for path in sorted(incoming.iterdir()):
        if path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES:
            handler._maybe_process(path, "startup-sweep")


if __name__ == "__main__":
    main()
