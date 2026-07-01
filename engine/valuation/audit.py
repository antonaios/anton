"""Append-only audit log for engine runs.

Every engine run gets one JSONL line in `runs/audit.jsonl`. The line captures
who, what, when, the inputs/outputs hashes, the template hash at run time,
and the convergence-iteration count for any post-recalc hardcode step. The
audit log is the ground truth for "which file produced which numbers."
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from valuation.models import EngineRun


DEFAULT_AUDIT_LOG = Path("runs/audit.jsonl")


def write(run: EngineRun, *, audit_log: Path = DEFAULT_AUDIT_LOG) -> None:
    """Append a single audit record for this run."""
    audit_log.parent.mkdir(parents=True, exist_ok=True)

    record = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "run_id": run.run_id,
        "template": run.template_name,
        "version": run.template_version,
        "template_hash": run.template_hash,
        "inputs_hash": _hash_dict(run.inputs),
        "outputs_hash": _hash_dict(run.outputs),
        "output_path": str(run.output_path),
        "duration_ms": run.duration_ms,
        "convergence_iters": run.convergence_iters,
        "status": run.status,
        "notes": run.notes,
    }

    with audit_log.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def _hash_dict(d: dict[str, Any]) -> str:
    """Stable sha256 of a dict (sorted keys, JSON-serialised)."""
    blob = json.dumps(d, sort_keys=True, default=str).encode("utf-8")
    return f"sha256:{hashlib.sha256(blob).hexdigest()}"


def tail(n: int = 10, *, audit_log: Path = DEFAULT_AUDIT_LOG) -> list[dict[str, Any]]:
    """Return the last N audit records (newest last). Empty list if no log."""
    if not audit_log.exists():
        return []
    lines = audit_log.read_text(encoding="utf-8").strip().splitlines()
    out: list[dict[str, Any]] = []
    for line in lines[-n:]:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out
