"""Auto-mover: catch HiNotes exports in ~/Downloads/ and move them into the
vault's Inbox/HiNotes/incoming/ where the watcher processes them.

Companion to watcher.py. Runs as a second watchdog observer when the
operator passes --mover-from to `hinotes-watcher start`.

Pattern-matching is intentionally permissive — the export filename
varies (HiNotes desktop app vs web export vs manual rename). Default
matches anything that:
  - Has extension .docx | .pdf | .txt | .md | .vtt
  - AND either contains "hinotes" (any case) anywhere in the name, OR
    matches a user-supplied regex (--mover-pattern).
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from watchdog.events import FileSystemEvent, FileSystemEventHandler

logger = logging.getLogger(__name__)


DEFAULT_EXTS = {".docx", ".pdf", ".txt", ".md", ".vtt"}
DEFAULT_PATTERN = re.compile(r"hinotes", re.IGNORECASE)


@dataclass
class MoverConfig:
    source_dir: Path
    incoming_dir: Path
    pattern: re.Pattern[str] = DEFAULT_PATTERN
    extensions: frozenset[str] = frozenset(DEFAULT_EXTS)
    settle_seconds: float = 3.0


class DownloadsMoverHandler(FileSystemEventHandler):
    """Watchdog handler. On `on_created` / `on_modified`, check whether the
    file looks like a HiNotes export and if so, copy-then-delete it into
    the vault's incoming/ directory.

    We copy + delete rather than os.rename because Downloads is often on
    a different volume from the vault (cross-device link error).
    """

    def __init__(self, config: MoverConfig) -> None:
        self.config = config

    # Use on_closed where supported so we don't grab a half-written file.
    # Fall back to on_created/on_modified with a settle wait.
    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._handle(Path(event.src_path))

    def on_moved(self, event: FileSystemEvent) -> None:  # type: ignore[override]
        if not event.is_directory:
            dest = getattr(event, "dest_path", None)
            if dest:
                self._handle(Path(dest))

    def _handle(self, path: Path) -> None:
        try:
            if not self._is_match(path):
                return
            if not self._wait_for_stable(path):
                logger.warning("mover: %s never stabilised, skipping", path.name)
                return
            self._move(path)
        except Exception as e:  # noqa: BLE001
            logger.exception("mover: error handling %s: %s", path, e)

    def _is_match(self, path: Path) -> bool:
        if path.suffix.lower() not in self.config.extensions:
            return False
        if not self.config.pattern.search(path.name):
            return False
        if not path.exists():
            return False
        return True

    def _wait_for_stable(self, path: Path) -> bool:
        """Wait for file size to be stable for settle_seconds — handles
        download-in-progress files."""
        last_size = -1
        stable_for = 0.0
        step = 0.5
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            try:
                size = path.stat().st_size
            except OSError:
                return False
            if size == last_size:
                stable_for += step
                if stable_for >= self.config.settle_seconds:
                    return True
            else:
                stable_for = 0.0
                last_size = size
            time.sleep(step)
        return False

    def _move(self, path: Path) -> None:
        self.config.incoming_dir.mkdir(parents=True, exist_ok=True)
        target = self._unique_target(self.config.incoming_dir / path.name)
        logger.info("mover: %s → %s", path.name, target)
        # Copy-then-remove to handle cross-device moves.
        shutil.copy2(str(path), str(target))
        try:
            path.unlink()
        except OSError as e:
            logger.warning("mover: could not delete source %s: %s — leaving in place", path, e)

    @staticmethod
    def _unique_target(target: Path) -> Path:
        """If target exists, append -1 / -2 / ... before the extension."""
        if not target.exists():
            return target
        stem = target.stem
        suffix = target.suffix
        i = 1
        while True:
            cand = target.with_name(f"{stem}-{i}{suffix}")
            if not cand.exists():
                return cand
            i += 1


def sweep_existing(config: MoverConfig) -> int:
    """One-shot scan of source_dir at startup to catch files already
    present. Returns the number of files moved.
    """
    if not config.source_dir.exists():
        return 0
    moved = 0
    handler = DownloadsMoverHandler(config)
    for p in sorted(config.source_dir.iterdir(), key=lambda p: p.stat().st_mtime if p.exists() else 0):
        if p.is_file() and handler._is_match(p):  # noqa: SLF001 — intentional
            handler._move(p)  # noqa: SLF001
            moved += 1
    return moved


def expand_source(source: str) -> Path:
    """Resolve `~`, environment variables, and return an absolute path."""
    return Path(os.path.expandvars(os.path.expanduser(source))).resolve()


def default_source_dir() -> Optional[Path]:
    """Best-effort default — operator's Downloads folder on the host OS."""
    home = Path.home()
    candidates = [home / "Downloads", home / "downloads"]
    for c in candidates:
        if c.exists():
            return c
    return None
