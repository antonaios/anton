"""Shared dependencies / config for the bridge."""

from __future__ import annotations

import os
import platform
from functools import lru_cache
from pathlib import Path


def _default_vault() -> Path:
    """Pick a sensible vault default for the host platform.

    The CLIs themselves default to a WSL-style path (`/mnt/x/OS AI Vault`),
    which is wrong on native Windows. Override with the standard env var
    when present, else pick the platform-appropriate default.
    """
    env = os.environ.get("AGENTIC_VAULT")
    if env:
        return Path(env)
    if platform.system() == "Windows":
        return Path("<vault>")
    return Path("/mnt/x/OS AI Vault")


VAULT = _default_vault()
ROUTINES_REPO = Path(__file__).resolve().parent.parent.parent
RUNS_DIR = ROUTINES_REPO / "runs"
RECALL_INDEX_DIR = ".recall-index"
RECALL_INDEX_DB = "index.db"


@lru_cache(maxsize=1)
def vault_paths():
    """Lazy import — `routines.shared.vault_writer` pulls heavy deps."""
    from routines.shared.vault_writer import VaultPaths

    return VaultPaths(VAULT)
