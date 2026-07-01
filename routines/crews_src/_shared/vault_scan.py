"""Shared vault-scan primitives for ANTON crews — STDLIB ONLY.

Extracted at Phase-7 integration from ``crews_src/explore_lib.py`` (#33) so a
SECOND crew (``/debate`` #36, operator decision 2: "wire /debate vault access
NOW") can reuse the exact same filesystem scan + frontmatter + wikilink logic
instead of growing a parallel copy. ``explore_lib`` now imports these names and
re-exports them (its public API is unchanged); ``debate_crew`` imports them
directly to load + cite REAL vault evidence per [[autonomous-crews]] §4.

WHY A SHARED MODULE (not just left in explore_lib): a crew module imports
``metagpt`` at top level, so importing ``explore_lib`` is fine but importing
``explore_crew`` from ``debate_crew`` would drag MetaGPT role/action wiring in.
This module — like ``_shared/boundary.py`` — imports ONLY the standard library,
so it is loadable by file path from the bridge venv's pytest suite (no metagpt,
no third-party) AND shareable across crews without coupling their MetaGPT glue.

Runs in the crew venv (Python 3.11) at runtime; loadable anywhere for tests.
The vault is a plain directory tree on disk — reading it is NOT a boundary
violation (the boundary forbids ``import routines.*``, not reading files).
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

# ════════════════════════════════════════════════════════════════════════════
# Vault root resolution (env override, machine-default fallback). Mirrors
# routines/api/deps.py::_default_vault — kept in sync by hand because the crew
# venv cannot import the bridge package across the process boundary.
# ════════════════════════════════════════════════════════════════════════════

_WINDOWS_VAULT = r"<vault>"
_POSIX_VAULT = "/mnt/x/OS AI Vault"

# System / archive / high-noise dirs a "what do we know about X" scan skips.
# (The vault is ~150 notes, so this is about SIGNAL, not performance: Daily
# logs + Obsidian internals would drown the real company/sector notes.)
SKIP_DIRS = frozenset({
    ".obsidian", ".git", ".trash", ".smart-connections",
    "Archive", "_processing", "attachments", "_attachments",
    "node_modules", "Daily",
})

_MAX_FILE_BYTES = 200_000          # don't slurp giant notes whole
_DEFAULT_SCAN_LIMIT = 12           # top-N vault hits handed to the LLM


def vault_root() -> Path:
    """Vault root: ``AGENTIC_VAULT`` env, else the platform default."""
    env = os.environ.get("AGENTIC_VAULT")
    if env:
        return Path(env)
    return Path(_WINDOWS_VAULT if os.name == "nt" else _POSIX_VAULT)


# ════════════════════════════════════════════════════════════════════════════
# Frontmatter + wikilink helpers (regex — no pyyaml dep so this stays loadable
# in any venv; we only need a handful of scalar fields).
# ════════════════════════════════════════════════════════════════════════════


def read_text(path: Path) -> str:
    """Read a note, bounded + lossy-decoded — never raises on a bad file."""
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read(_MAX_FILE_BYTES)
    except OSError:
        return ""


def split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Return (scalar frontmatter dict, body). Minimal YAML: top-level
    ``key: value`` scalar lines only (lists/nested maps are ignored — we only
    consume name/ticker/sector/sensitivity, all scalars)."""
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    block = text[3:end]
    body = text[end + 4:]
    fm: dict[str, str] = {}
    for line in block.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = re.match(r"^([A-Za-z0-9_-]+)\s*:\s*(.*)$", line)
        if not m:
            continue
        key, val = m.group(1).strip(), m.group(2).strip()
        if val and val[0] in "[{":          # skip list/map values
            continue
        fm[key.lower()] = val.strip().strip('"').strip("'")
    return fm, body


def to_wikilink(root: Path, path: Path) -> str:
    """Vault-relative ``[[wikilink]]`` (no extension), forward-slashed."""
    try:
        rel = path.relative_to(root)
    except ValueError:
        rel = path
    stem = rel.as_posix()
    if stem.lower().endswith(".md"):
        stem = stem[:-3]
    return f"[[{stem}]]"


# ════════════════════════════════════════════════════════════════════════════
# Vault scanning
# ════════════════════════════════════════════════════════════════════════════


def iter_vault_markdown(root: Path) -> list[Path]:
    """All ``*.md`` under ``root`` minus :data:`SKIP_DIRS` (recursive)."""
    out: list[Path] = []
    if not root.is_dir():
        return out
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            if fn.lower().endswith(".md"):
                out.append(Path(dirpath) / fn)
    return out


def excerpt(body: str, term: str, width: int = 160) -> str:
    """A one-line context snippet around the first case-insensitive hit."""
    flat = re.sub(r"\s+", " ", body).strip()
    idx = flat.lower().find(term.lower())
    if idx == -1:
        return flat[:width]
    start = max(0, idx - width // 3)
    return flat[start:start + width].strip()


def scan_vault_for_target(
    root: Path, target: str, limit: int = _DEFAULT_SCAN_LIMIT,
) -> dict[str, Any]:
    """Find notes related to ``target`` by filename + content + backlink.

    Returns ``{"target", "hits": [{wikilink, path, where, excerpt}], "cites":
    [wikilink, ...]}``. ``where`` is one of ``filename`` / ``backlink`` /
    ``content`` (in priority order; filename matches rank first). Pure
    filesystem — the "Smart Connections semantic neighbour" half of the spec
    is a documented v2 follow-on (no SC index reachable from a subprocess).
    """
    term = (target or "").strip()
    hits: list[dict[str, str]] = []
    if not term:
        return {"target": target, "hits": [], "cites": []}
    term_l = term.lower()
    wikilink_re = re.compile(
        r"\[\[[^\]]*" + re.escape(term_l) + r"[^\]]*\]\]", re.IGNORECASE,
    )
    for path in iter_vault_markdown(root):
        stem_l = path.stem.lower()
        text = read_text(path)
        _fm, body = split_frontmatter(text)
        where: str | None = None
        if term_l in stem_l:
            where = "filename"
        elif wikilink_re.search(text):
            where = "backlink"
        elif term_l in text.lower():
            where = "content"
        if where is None:
            continue
        hits.append({
            "wikilink": to_wikilink(root, path),
            "path": path.as_posix(),
            "where": where,
            "excerpt": excerpt(body or text, term),
        })
    # filename > backlink > content, stable within a tier.
    order = {"filename": 0, "backlink": 1, "content": 2}
    hits.sort(key=lambda h: order.get(h["where"], 9))
    hits = hits[:limit]
    return {
        "target": target,
        "hits": hits,
        "cites": [h["wikilink"] for h in hits],
    }


__all__ = [
    "SKIP_DIRS",
    "vault_root",
    "read_text",
    "split_frontmatter",
    "to_wikilink",
    "iter_vault_markdown",
    "excerpt",
    "scan_vault_for_target",
]
