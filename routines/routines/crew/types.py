"""Bridge-side crew boundary contract (#31).

``CrewInput`` / ``CrewOutput`` are the JSON-over-stdio contract between the
bridge (this venv, Python 3.14) and a crew subprocess (``<repo>\\crews``,
Python 3.11). The crew side defines the SAME shapes in
``crews_src/_shared/boundary.py`` — **manually kept in sync, never imported
across the boundary** (the whole point is that neither venv imports the
other's code). ``tests/crew/test_crew_routes.py`` has a schema-parity test
that loads the crew-side module by file path and diffs the field sets, so
drift fails CI rather than failing at 3am mid-crew.

Shape source: METAGPT-INTEGRATION-SPEC.md §2.3 (staged 2026-05-26), adapted:
  * spec's ``WorkspaceCtx`` naming → ``CrewWorkspace`` here; the hooks-layer
    dataclass is ``WorkspaceRef`` (the spec predates the #22 rename).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

WorkspaceType = Literal["project", "bd", "general"]
Sensitivity = Literal["public", "internal", "confidential", "MNPI"]
# "running"/"queued" are bridge-side lifecycle states; a crew subprocess only
# ever reports the first four.
CrewStatus = Literal["ok", "error", "cancelled", "timeout", "running", "queued"]


class CrewWorkspace(BaseModel):
    """Workspace context handed into the crew (type + name + resolved tier)."""

    type: WorkspaceType
    name: str
    sensitivity_tier: Sensitivity | None = None


class CrewLLMConfig(BaseModel):
    """Per-role model selection. The bridge resolves this from sensitivity +
    the crew manifest BEFORE launching; the crew module just consumes it.

    v1 is local-only per [[CLAUDE]] §5.2 — crews default to local-LLM lanes —
    so ``provider`` is effectively always ``"ollama"`` today. The
    ``"claude-cli"`` literal is kept so the contract doesn't need a rev when
    the operator approves cloud lanes for internal/public crews."""

    # Mirrors the crew side: ``model_*`` fields clash with pydantic's
    # protected ``model_`` namespace (warns on stderr under pydantic 2.9 in
    # the crew venv; kept symmetric here so the contract files stay diffable).
    model_config = ConfigDict(protected_namespaces=())

    provider: Literal["ollama", "claude-cli"]
    base_url: str | None = None
    model_analyst: str | None = None
    model_reviewer: str | None = None
    model_synthesist: str | None = None
    # Generic role→model map (#33 /explore): the three ``model_*`` fields above
    # are the hello_world legacy surface; crews with OTHER role names (explore's
    # VaultArchaeologist/FinancialAnalyst/IndustryAnalyst/Coordinator, triage's
    # six roles, debate's four) read their per-role model from here instead.
    # Carries ``manifest.models_default`` verbatim. Empty default = back-compat
    # (the parity test stays green; un-migrated callers are unaffected).
    models: dict[str, str] = Field(default_factory=dict)
    # ── Cloud-lane promotion (#crew-cloud-promotion, Phase A) ─────────────────
    # When the operator promotes ≥1 role to a frontier cloud lane, the bridge
    # sets these so the crew routes THOSE roles' LLM calls BACK to the bridge's
    # gated dispatcher (loopback ``/api/crew/_llm``) instead of calling a cloud
    # provider directly — the crew subprocess stays CREDENTIAL-FREE (the
    # load-bearing containment layer). Local roles keep the direct-Ollama path.
    # ``role_lanes`` maps a PROMOTED role → its cloud lane (absent role = local);
    # ``run_id`` is the crew's self-auth token to the bridge (echoes
    # ``CrewInput.run_id``); ``bridge_url`` is the loopback endpoint. All default
    # empty → a fully-local crew is byte-identical to v1 and the bridge↔crew
    # schema-parity test stays green. Keep IDENTICAL to the crew-side
    # ``crews_src/_shared/boundary.py`` CrewLLMConfig.
    role_lanes: dict[str, str] = Field(default_factory=dict)
    bridge_url: str | None = None
    run_id: str | None = None


class CrewInput(BaseModel):
    """Bridge → crew, single JSON document on stdin."""

    crew_verb: str           # "hello_world" | (later) "triage" | "explore" | "debate"
    run_id: str              # ANTON audit run_id (8-char hex)
    workspace: CrewWorkspace
    args: dict[str, Any] = Field(default_factory=dict)
    cost_cap_tokens: int
    llm_config: CrewLLMConfig


class Artefact(BaseModel):
    """A file the crew wrote, tagged with its sensitivity tier."""

    path: str
    sensitivity: str


class CrewDocument(BaseModel):
    """A deliverable the crew produced but cannot write itself (the crew venv
    has no access to ``routines.shared.write_policy``). The bridge materialises
    each one through the central write policy after a successful run — see
    ``routines.crew.artefacts``. Mirrors the crew-side
    ``crews_src/_shared/boundary.py`` ``CrewDocument`` (parity-tested).

    ``relative_path`` is CALLER (crew)-supplied and therefore UNTRUSTED — the
    bridge sanitises it, confines it under the verb's write root, and re-runs
    the write policy before any byte lands ([[workspace-write-policy]])."""

    relative_path: str
    content: str
    sensitivity: str


class RoleLogEntry(BaseModel):
    """One per-role row, serialized by the crew from MetaGPT's env.history."""

    role: str                # "Analyst" | "Reviewer" | "Synthesist" | …
    action: str              # "AnalyzeTopic" | "CritiqueAnalysis" | …
    ts_start: str            # ISO 8601
    duration_ms: int
    token_count: int
    sensitivity: str         # echoed from input (informational; guard ran before)
    status: Literal["ok", "error"]
    output_summary: str      # first 200 chars of the role's output


class CrewOutput(BaseModel):
    """Crew → bridge, single JSON line on stdout (the final result line)."""

    run_id: str              # echoed back, MUST match input
    status: Literal["ok", "error", "cancelled", "timeout"]
    summary: str             # 1-3 sentence summary for the chat bubble
    artefacts: list[Artefact] = Field(default_factory=list)
    # Deliverables the bridge materialises post-run (content the crew can't
    # write itself). Empty for crews that write nothing (hello_world).
    documents: list[CrewDocument] = Field(default_factory=list)
    # Structured CONCLUSION fields for the deliverable→vault capture loop
    # (#captures-to-vault-crews) — the crew route reads these to emit an
    # operator-gated ``deliverable-outcome`` proposal carrying the run's
    # headline metrics. Empty for crews that don't opt in. Mirrors the
    # crew-side ``crews_src/_shared/boundary.py`` CrewOutput (parity-tested).
    outcome: dict[str, Any] = Field(default_factory=dict)
    roles_log: list[RoleLogEntry] = Field(default_factory=list)
    token_count: int = 0     # total tokens consumed across all role LLM calls
    duration_ms: int | None = None       # bridge fills this in post-hoc
    error: str | None = None             # populated when status != "ok"


__all__ = [
    "WorkspaceType",
    "Sensitivity",
    "CrewStatus",
    "CrewWorkspace",
    "CrewLLMConfig",
    "CrewInput",
    "Artefact",
    "CrewDocument",
    "RoleLogEntry",
    "CrewOutput",
]
