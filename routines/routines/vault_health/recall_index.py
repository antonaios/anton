"""Recall-index health check (#recall-embed-context-overflow, 2026-06-11).

Surfaces notes that are invisible or degraded in /recall:

* **errors** — the last index run failed to index notes at all (no vector
  row, no FTS5 row → fully absent from /recall). The run-stats row in
  ``index_runs`` is the only durable trace of these.
* **degraded** — notes written lexical-only (NULL embedding) because the
  embed call failed at index time; the FTS5 lane still finds them, the
  vector lane does not. They self-heal on the next ``recall index`` run
  once the embed succeeds.

Read-only — inspects ``<vault>/.recall-index/index.db`` via
``routines.recall.index.index_health``; never writes the vault, never
calls Ollama (safe in the weekly cron sweep).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from routines.recall.index import DEFAULT_INDEX_DB, DEFAULT_INDEX_DIR, index_health

# Cap on paths quoted per finding — the count is the signal; the sample
# tells the operator where to look without flooding the sweep output.
_SAMPLE_PATHS = 10


@dataclass
class Finding:
    severity: str  # "WARNING" — degraded retrieval, not a safety gate
    check: str     # "index-missing" | "index-errors" | "index-degraded"
    detail: str


def _sample(paths: list[str]) -> str:
    shown = ", ".join(paths[:_SAMPLE_PATHS])
    extra = len(paths) - _SAMPLE_PATHS
    return f"{shown} (+{extra} more)" if extra > 0 else shown


def scan(vault: Path) -> list[Finding]:
    """Inspect the recall index for invisible/degraded notes. Empty = green."""
    findings: list[Finding] = []
    health = index_health(vault / DEFAULT_INDEX_DIR / DEFAULT_INDEX_DB)

    if not health["exists"]:
        return [Finding(
            "WARNING", "index-missing",
            "recall index not built — /recall is blind; run `recall index`",
        )]

    last = health["last_run"]
    if last is not None and last.get("errors"):
        findings.append(Finding(
            "WARNING", "index-errors",
            f"last index run ({last.get('run_at', '?')}) failed to index "
            f"{last['errors']} note(s) — fully absent from /recall: "
            f"{_sample(last.get('error_paths', []))}",
        ))

    if health["degraded"]:
        findings.append(Finding(
            "WARNING", "index-degraded",
            f"{health['degraded']} note(s) lexical-only (embed failed; FTS5 "
            f"lane only, no vector ranking): {_sample(health['degraded_paths'])}",
        ))

    return findings
