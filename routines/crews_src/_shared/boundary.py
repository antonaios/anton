"""Crew-side boundary contract + stdio helpers (#31).

Runs in the ISOLATED crew venv (``<repo>\\crews\\.venv``, Python 3.11).
Defines the crew-side half of the JSON-over-stdio contract:

  * ``CrewInput`` / ``CrewOutput`` / ``RoleLogEntry`` — the SAME shapes the
    bridge defines in ``routines/crew/types.py``. **Manually kept in sync;
    never imported across the boundary.** The bridge repo has a schema-parity
    test that loads this file by path and diffs the field sets.
  * ``read_input`` / ``write_result`` / ``write_error`` — stdio plumbing.
  * ``collect_roles_log`` — normalizes the role-entry dicts a crew collects
    while its roles act (see ``hello_world_crew._MeteredRole``) into
    ``RoleLogEntry`` shape for the audit mirror.

IMPORTANT: this module imports ONLY pydantic + stdlib — no metagpt — so an
input-parse error can still be reported over the boundary even when the
MetaGPT install is broken, and so the bridge-side parity test can load it
without a crew venv.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

WorkspaceType = Literal["project", "bd", "general"]
Sensitivity = Literal["public", "internal", "confidential", "MNPI"]


class CrewWorkspace(BaseModel):
    type: WorkspaceType
    name: str
    sensitivity_tier: Sensitivity | None = None


class CrewLLMConfig(BaseModel):
    # ``model_*`` fields clash with pydantic 2.9's default protected
    # namespace; without this the class DEFINITION emits UserWarnings on
    # stderr at import — before any redirect runs — and stderr non-empty is
    # a fault signal to the bridge (spec §2.4). Found by the real-boundary
    # smoke run, 2026-06-10.
    model_config = ConfigDict(protected_namespaces=())

    provider: Literal["ollama", "claude-cli"]
    base_url: str | None = None
    model_analyst: str | None = None
    model_reviewer: str | None = None
    model_synthesist: str | None = None
    # Generic role→model map (#33 /explore) — mirrors routines/crew/types.py.
    # Crews whose role names aren't Analyst/Reviewer/Synthesist resolve their
    # per-role model from here (``manifest.models_default`` verbatim). Empty
    # default keeps the bridge↔crew schema-parity test green.
    models: dict[str, str] = Field(default_factory=dict)
    # ── Cloud-lane promotion (#crew-cloud-promotion, Phase A) ─────────────────
    # Mirrors routines/crew/types.py. When the operator promotes ≥1 role to a
    # cloud lane, the bridge sets these and THOSE roles route their LLM calls
    # BACK to the bridge's gated loopback ``/api/crew/_llm`` (the subprocess
    # stays credential-free); local roles keep the direct-Ollama path.
    # ``role_lanes`` maps a promoted role → its cloud lane; ``run_id`` is the
    # crew's self-auth token; ``bridge_url`` the loopback endpoint. All default
    # empty → fully-local crews unchanged + schema-parity test green.
    role_lanes: dict[str, str] = Field(default_factory=dict)
    bridge_url: str | None = None
    run_id: str | None = None


class CrewInput(BaseModel):
    crew_verb: str
    run_id: str
    workspace: CrewWorkspace
    args: dict[str, Any] = Field(default_factory=dict)
    cost_cap_tokens: int
    llm_config: CrewLLMConfig


class Artefact(BaseModel):
    path: str
    sensitivity: str


class CrewDocument(BaseModel):
    """A deliverable the crew produced but CANNOT write itself — the crew venv
    has no access to ``routines.shared.write_policy`` (the boundary forbids it),
    so a deliverable-producing crew (e.g. ``/triage``) returns its content here
    and the BRIDGE materialises it through the central write policy. Kept in
    sync with ``routines/crew/types.py`` (parity-tested).

    ``relative_path`` is UNTRUSTED by the bridge — it is confined under the
    verb's write root + re-validated by the write policy before any byte
    lands."""

    relative_path: str
    content: str
    sensitivity: str


class RoleLogEntry(BaseModel):
    role: str
    action: str
    ts_start: str
    duration_ms: int
    token_count: int
    sensitivity: str
    status: Literal["ok", "error"]
    output_summary: str


class CrewOutput(BaseModel):
    run_id: str
    status: Literal["ok", "error", "cancelled", "timeout"]
    summary: str
    artefacts: list[Artefact] = Field(default_factory=list)
    # Bridge-materialised deliverables (content the crew can't write itself).
    # Default empty → crews that write nothing (hello_world) are unaffected.
    documents: list[CrewDocument] = Field(default_factory=list)
    # Structured CONCLUSION fields for the deliverable→vault capture loop
    # (#captures-to-vault-crews) — e.g. triage's red-flag counts, debate's
    # verdict. Default empty → crews that don't opt in are unaffected. The
    # bridge crew route reads this to emit an operator-gated
    # ``deliverable-outcome`` proposal. Kept in sync with the bridge mirror
    # ``routines/crew/types.py`` (schema-parity test).
    outcome: dict[str, Any] = Field(default_factory=dict)
    roles_log: list[RoleLogEntry] = Field(default_factory=list)
    token_count: int = 0
    duration_ms: int | None = None
    error: str | None = None


# ────────────────────────────────────────────────────────────────────────────
# stdio plumbing
# ────────────────────────────────────────────────────────────────────────────

# The REAL stdout, reserved for protocol envelopes. The bridge treats any
# non-JSON stdout line as a hard protocol violation, so after
# ``capture_protocol_stream()`` runs, stray ``print()``s from libraries land
# in a log file instead of corrupting the boundary.
_protocol_stream: Any = None


def protocol_stream() -> Any:
    """The stream protocol envelopes must be written to (the real stdout)."""
    return _protocol_stream if _protocol_stream is not None else sys.stdout


def capture_protocol_stream(stray_sink: Any = None) -> None:
    """Reserve the real stdout for the boundary protocol.

    Captures ``sys.stdout`` as the protocol stream, then rebinds
    ``sys.stdout`` to ``stray_sink`` (a writable file object; defaults to a
    devnull handle) so library ``print()``s — agentops banners, dependency
    chatter — can't inject non-JSON lines into the protocol. Call once at
    crew startup, right after logging redirection."""
    global _protocol_stream
    if _protocol_stream is not None:
        return  # already captured — idempotent
    _protocol_stream = sys.stdout
    if stray_sink is None:
        stray_sink = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115 — lives for the process
    sys.stdout = stray_sink


def read_input() -> CrewInput:
    """Read ONE line of JSON from stdin and validate it as ``CrewInput``.

    One LINE, not ``stdin.read()`` — the bridge keeps stdin OPEN after the
    input line so HumanProvider replies can flow back mid-run; a full read()
    would block until process exit and deadlock both sides. (The staged
    template used ``read()``; flagged + fixed at #31 build time.)
    """
    raw = sys.stdin.readline()
    if not raw.strip():
        raise ValueError("empty stdin — expected one line of CrewInput JSON")
    return CrewInput.model_validate_json(raw)


def write_result(result: CrewOutput) -> None:
    """Write the final result as a single JSON line on the protocol stream
    (the bridge demuxer recognises it by the ``status`` field + absent
    ``_kind`` tag)."""
    out = protocol_stream()
    out.write(result.model_dump_json() + "\n")
    out.flush()


def write_error(message: str) -> None:
    """Fatal-only stderr — used ONLY when no usable ``CrewOutput`` can be
    constructed (input parse failure, pre-validation crash). Everything else
    goes through ``write_result(status="error")`` so the bridge gets a
    structured envelope."""
    sys.stderr.write(message + "\n")
    sys.stderr.flush()


# ────────────────────────────────────────────────────────────────────────────
# crew-collected role entries → roles_log
# ────────────────────────────────────────────────────────────────────────────


def collect_roles_log(entries: Any) -> list[dict[str, Any]]:
    """Normalize crew-collected role entries into RoleLogEntry-shaped dicts.

    ADAPTED at #31 build time (real-boundary smoke run, 2026-06-10): the
    staged sketch walked ``env.history`` with per-message ``metadata`` — but
    on metagpt 0.8.x ``Environment.history`` is a debug STRING and
    ``Message`` is a closed pydantic model that cannot carry a ``metadata``
    field, so both halves of that design come back empty against the real
    install. Crews therefore collect their own entry dicts as their roles
    act (see ``hello_world_crew._MeteredRole``); this helper only
    validates/clamps the shape before it crosses the boundary. Malformed
    entries are skipped — telemetry must never kill a crew."""
    out: list[dict[str, Any]] = []
    for e in entries or []:
        if not isinstance(e, dict):
            continue
        try:
            out.append({
                "role": str(e.get("role") or "unknown"),
                "action": str(e.get("action") or "unknown"),
                "ts_start": str(e.get("ts_start") or ""),
                "duration_ms": int(e.get("duration_ms") or 0),
                "token_count": int(e.get("token_count") or 0),
                "sensitivity": str(e.get("sensitivity") or "unknown"),
                "status": "error" if e.get("status") == "error" else "ok",
                "output_summary": str(e.get("output_summary") or "")[:200],
            })
        except (TypeError, ValueError):
            continue
    return out


def to_json_line(payload: dict[str, Any]) -> str:
    """Serialize one stdout protocol envelope (HumanProvider asks etc.)."""
    return json.dumps(payload)


__all__ = [
    "CrewWorkspace",
    "CrewLLMConfig",
    "CrewInput",
    "Artefact",
    "CrewDocument",
    "RoleLogEntry",
    "CrewOutput",
    "protocol_stream",
    "capture_protocol_stream",
    "read_input",
    "write_result",
    "write_error",
    "collect_roles_log",
    "to_json_line",
]
