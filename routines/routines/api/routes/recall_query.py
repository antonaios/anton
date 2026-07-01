"""Recall-query skill bridge route (#21 — sixth SKILL.md migration).

``POST /api/workflows/recall-query`` — fires the existing #54b hybrid
retrieval pipeline (vector cosine + FTS5 BM25 + frontmatter
importance/expires triad) in-process and returns a structured
:class:`RecallQueryResult`. The handler:

  1. Reads the skill registry for governance metadata (sensitivity, scope,
     cost caps) — no inlined constants.
  2. Wraps the in-process ``recall.retrieve.query()`` call in the real
     ``tool_call_hooks`` context manager so ``enforce_skill_sensitivity``
     (#61) fires on the ``@before_tool_call`` path. For this skill
     (``workspace_scope: any``, ``sensitivity: internal``) the guard is a
     structural NO-OP for the common case; the only firing path is the
     cross-skill MNPI gate.
  3. Calls ``recall.retrieve.query()`` DIRECTLY (no subprocess — the
     routine is sub-second on a typical ~1k-note vault, pure SQLite query
     against the local index).
  4. Surfaces the full ``NoteHit`` score decomposition per Iron Law
     clause 1: every hit carries ``vector_score``, ``fts_score``,
     ``importance``, ``expires_decay``, ``final_score``, ``path``,
     ``excerpt``, ``rank``.
  5. Surfaces ``index_state`` (last_indexed_at, notes_indexed,
     chunks_indexed, fts_present) from the SQLite index so the operator
     can sanity-check freshness without grepping ``index.py``.

The existing ``POST /api/recall`` route stays live as the canonical
retrieval endpoint (called directly by Cmd-K + downstream consumers like
``morning_brief.pull`` and ``LBO /pitch``). This workflow route is the
SKILL-governed surface that flows through the central guard. Authored as
a SEPARATE file from ``recall.py`` for concurrency safety with any in-
flight recall tuning (per session brief, "DO NOT touch
routines/api/routes/recall.py except via additive routes").

Iron Law (two-clause):
  * CLAUSE 1 — every hit carries full score decomposition + path
  * CLAUSE 2 — retrieval != narration; this route returns HITS, no
    ``summary`` / ``narrative`` field is added to the response payload
    (a future ``recall-narrate`` skill would own narration).
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from routines.api.deps import RECALL_INDEX_DB, RECALL_INDEX_DIR, VAULT
from routines.recall import retrieve as _retrieve
from routines.shared import audit
from routines.skills._runtime.anton_skill import anton_skill
from routines.skills._runtime.llm_call_counter import current_run_id

router = APIRouter(prefix="/workflows", tags=["workflows"])
log = logging.getLogger(__name__)


# ── request / response models ────────────────────────────────────────────────


class RecallQueryFilter(BaseModel):
    """Optional frontmatter filter — passed through to ``recall.retrieve.Filter``.

    The route mirrors every field on the routine's Filter dataclass so the
    Iron Law's ``filter_applied`` guardrail can be honoured end-to-end
    (the response echoes back exactly what was requested)."""

    types: Optional[list[str]] = None
    sensitivity_max: Optional[str] = None
    project: Optional[str] = None
    sectors: Optional[list[str]] = None
    modified_after: Optional[str] = None
    modified_before: Optional[str] = None
    path_prefix: Optional[str] = None
    exclude_path_prefix: Optional[str] = None


class RecallQueryRequest(BaseModel):
    """On-demand recall-query request from the dashboard or Cmd-K.

    Defaults mirror the existing ``/api/recall`` shape; ``filter`` is the
    structured frontmatter filter. The route refuses an empty query with
    422 (per existing recall behaviour); the routine itself would return
    [] on an empty query but the 422 is the operator-facing surface."""

    query: str = Field(..., min_length=1)
    limit: int = Field(default=10, ge=1, le=50)
    filter: Optional[RecallQueryFilter] = None
    # #54-rerank — opt-in cross-encoder rerank of the fused top-k. Default
    # OFF. Skips gracefully when the optional `recall` extra is absent.
    rerank: bool = False
    # #45 graph leg — opt-in 3rd RRF channel boosting candidates structurally
    # near the top hits (wikilink proximity, cap 2 hops). Default OFF;
    # rebuilds the in-memory vault graph on call. Best-effort — degrades to
    # vector+fts on a graph fault.
    graph: bool = False
    # #45-expansion leg — opt-in injection of the top hits' wikilink-graph
    # neighbours as NEW candidates (who-touched-style relational recall), fused
    # as an additive RRF leg. Independent of `graph`; default OFF. Injected
    # neighbours pass the SAME sensitivity gate as every candidate.
    graph_expand: bool = False
    # workspace fields are conventional across all skill routes (#61) — for
    # this any-scope, internal skill they pass through the central guard
    # without effect (except for MNPI inputs, which the guard refuses).
    workspace_type: Literal["project", "bd", "general"] = "general"
    workspace_name: str = "default"
    workspace_sensitivity: Literal["public", "internal", "confidential", "MNPI"] = "internal"


class RecallHit(BaseModel):
    """One ranked hit — Iron Law clause 1: every field is required.

    The score decomposition (vector_score, fts_score, importance,
    expires_decay) is what makes a hit EXPLAINABLE. Truncating any of
    these breaks the operator's ability to sanity-check rank order.
    Path + excerpt + rank are the citation contract."""

    rank: int
    path: str
    excerpt: str
    vector_score: float
    fts_score: float
    importance: int
    expires_decay: float
    final_score: float
    # #54-rerank — cross-encoder relevance score, present only when the
    # opt-in rerank stage ran AND the reranker was available. Optional /
    # additive: None leaves the Iron-Law-required fields above intact.
    rerank_score: Optional[float] = None
    # #45 graph leg — per-channel RRF contribution from the graph-proximity
    # channel. Present only when the opt-in graph leg ran AND this note was
    # within the hop cap of a seed. Optional / additive (None = leg off /
    # unreachable), so the Iron-Law-required fields above stay intact.
    graph_rrf: Optional[float] = None
    # #45-expansion NOTE: the expansion leg's per-channel contribution is
    # DELIBERATELY NOT surfaced on this DTO while the leg is off-by-default —
    # a field here would put a ``"graph_expand_rrf": null`` key on every
    # off-path response, breaking the #no-mnpi-to-cloud byte-identity
    # guarantee (was cited as §5.4; Codex
    # SEV-1). It lives in-process on the NoteHit + the CLI --explain; surface
    # it on the DTO with the default-on promotion (#45-recall-graph-default-on).


class IndexState(BaseModel):
    """Index sidecar metadata surfaced in the response so the operator can
    sanity-check freshness without grepping ``index.py``.

    ``last_indexed_at`` is the ISO-8601 UTC mtime of the index DB file
    (deterministic, no schema dependency). ``notes_indexed`` /
    ``chunks_indexed`` are counts from the SQLite tables. ``fts_present``
    is True iff the ``recall_fts`` virtual table exists (the operator's
    cue to fire ``/recall index --rebuild`` if False on a populated
    vault)."""

    last_indexed_at: Optional[str] = None
    notes_indexed: int = 0
    chunks_indexed: int = 0
    fts_present: bool = False


class RecallQueryResult(BaseModel):
    """Structured recall-query result. Pure-return data — distinct from
    vault-health (writes report), deal-tracker (appends Excel row),
    sector-news (writes newsletter), LBO (populates XLSX).

    Iron Law clause 2: NO ``summary`` / ``narrative`` field — this is
    the hits-only contract. A future ``recall-narrate`` skill would own
    narration; this skill returns the hits."""

    status: Literal["ok", "error"]
    run_id: str
    query: str
    limit_applied: int
    filter_applied: RecallQueryFilter
    index_state: IndexState
    hits: list[RecallHit] = Field(default_factory=list)
    duration_ms: int = 0
    warnings: list[str] = Field(default_factory=list)
    error: Optional[str] = None


# ── helpers ──────────────────────────────────────────────────────────────────


def _read_index_state(vault_root: Path) -> IndexState:
    """Read the index sidecar metadata. Defensive: a missing DB or
    schema-mismatched DB returns an empty IndexState (operator sees
    ``fts_present: false`` + zero counts and knows to reindex)."""
    db_path = vault_root / RECALL_INDEX_DIR / RECALL_INDEX_DB
    if not db_path.exists():
        return IndexState()

    # last_indexed_at — mtime of the DB file, ISO-8601 UTC
    try:
        mtime = os.path.getmtime(db_path)
        last_indexed_at = (
            datetime.fromtimestamp(mtime, tz=timezone.utc)
            .isoformat(timespec="seconds")
        )
    except OSError:
        last_indexed_at = None

    notes_indexed = 0
    chunks_indexed = 0
    fts_present = False
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute("SELECT COUNT(*) FROM notes").fetchone()
            notes_indexed = int(row[0]) if row else 0
        except sqlite3.OperationalError:
            pass
        try:
            row = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()
            chunks_indexed = int(row[0]) if row else 0
        except sqlite3.OperationalError:
            pass
        try:
            row = conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='recall_fts'"
            ).fetchone()
            fts_present = row is not None
        except sqlite3.OperationalError:
            pass
        conn.close()
    except sqlite3.Error:
        # Defensive — schema-mismatched / locked DB falls back to zeros.
        pass

    return IndexState(
        last_indexed_at=last_indexed_at,
        notes_indexed=notes_indexed,
        chunks_indexed=chunks_indexed,
        fts_present=fts_present,
    )


# Canonical sensitivity rank (0 = least sensitive). ``workspace_sensitivity``
# is a validated Literal so it is always one of these; ``filter.sensitivity_max``
# is a free string, so it gets the fail-closed handling below.
_CEILING_RANK = {"public": 0, "internal": 1, "confidential": 2, "mnpi": 3}


def _canonical_sensitivity(token: str) -> str:
    """``mnpi`` → ``MNPI`` (the form ``_SENSITIVITY_ORDER`` + the guard use);
    every other token is already lower-canonical."""
    return "MNPI" if token == "mnpi" else token


def _effective_ceiling(req: "RecallQueryRequest") -> str:
    """The retrieval sensitivity ceiling actually applied (F-9 / HR S-10).

    Rules (codex-5.5 validators r1):
      * OMITTED ``filter.sensitivity_max`` → default to the declared workspace
        tier (the route used to apply NO ceiling → confidential/MNPI leaked on
        an ``internal`` query).
      * EXPLICIT-but-UNKNOWN ceiling → fail CLOSED to ``public`` (most
        restrictive) — NOT the declared default, which would let a typo widen
        retrieval (and would mask the unknown value from ``_passes_filter``'s
        own fail-closed).
      * The caller may TIGHTEN (request a stricter ceiling than declared) but
        never LOOSEN beyond the declared tier — the effective ceiling is the
        STRICTER of (requested, declared). (``#sec-server-side-workspace-tier``
        F-6 is deferred, so the declared tier is still client-trusted as the
        UPPER bound; this just stops the filter knob from exceeding it.)
    """
    declared = req.workspace_sensitivity.strip().lower()  # validated Literal → always known
    raw = req.filter.sensitivity_max if req.filter else None
    if raw is None:
        chosen = declared
    else:
        token = raw.strip().lower()
        if token not in _CEILING_RANK:
            token = "public"  # explicit but unknown → fail closed
        # tighten-only: clamp the requested ceiling to the declared tier.
        chosen = token if _CEILING_RANK[token] <= _CEILING_RANK[declared] else declared
    return _canonical_sensitivity(chosen)


def _filter_to_routine(
    req: "RecallQueryRequest", ceiling: str,
) -> _retrieve.Filter:
    """Translate the route's pydantic Filter into the routine's dataclass.

    F-9: ALWAYS returns a Filter carrying a non-empty ``sensitivity_max``
    (``ceiling``) — even when the request omitted ``filter`` entirely — so the
    retrieval ceiling can never be absent. The other (optional) frontmatter
    facets pass through when a filter was supplied."""
    rf = req.filter or RecallQueryFilter()
    return _retrieve.Filter(
        types=rf.types,
        sensitivity_max=ceiling,
        project=rf.project,
        sectors=rf.sectors,
        modified_after=rf.modified_after,
        modified_before=rf.modified_before,
        path_prefix=rf.path_prefix,
        exclude_path_prefix=rf.exclude_path_prefix,
    )


def _hit_out(rank: int, h: _retrieve.NoteHit) -> RecallHit:
    """Build a RecallHit from a NoteHit. Iron Law clause 1: every score
    component must be populated. The routine sets all 4 components on
    every hit (vector_score / fts_score / importance / expires_decay);
    the defensive ``or 0.0`` / ``or 0`` keeps the schema strict (non-
    Optional) even if a routine refactor ever forgets to set one — the
    operator sees a zero, not a None that breaks the contract."""
    # excerpt: prefer best_chunk_text (Phase 2 retrieval, includes the
    # matched-term context), fall back to tldr, then body_excerpt.
    excerpt = h.best_chunk_text or h.tldr or h.body_excerpt or ""
    return RecallHit(
        rank=rank,
        path=h.path,
        excerpt=excerpt,
        vector_score=float(h.vector_score) if h.vector_score is not None else 0.0,
        fts_score=float(h.fts_score) if h.fts_score is not None else 0.0,
        importance=int(h.importance) if h.importance is not None else 3,
        expires_decay=float(h.expires_decay) if h.expires_decay is not None else 1.0,
        final_score=float(h.score),
        rerank_score=(float(h.rerank_score)
                      if getattr(h, "rerank_score", None) is not None else None),
        graph_rrf=(float(h.graph_rrf)
                   if getattr(h, "graph_rrf", None) is not None else None),
    )


# ── route ────────────────────────────────────────────────────────────────────


@router.post("/recall-query", response_model=RecallQueryResult)
@anton_skill("recall-query")
def run_workflow_recall_query(req: RecallQueryRequest) -> RecallQueryResult:
    """Run a recall query on demand. See module docstring.

    #63 phase-4 PILOT: the first route migrated to ``@anton_skill``. The wrapper
    now owns the governance jacket — it reads the skill's governance from the
    registry, sets up ``tool_call_hooks`` (sensitivity gate / readiness / audit /
    sub-caps), emits the lifecycle events, dedups the run_id (#63 L2), and maps a
    ``SkillScopeRefused`` to 403. This body is JUST the analysis. Behaviour is
    identical to the hand-rolled version, except the result's ``run_id`` now
    reuses the request-boundary id (#59) so it correlates with the audit row +
    suspend/resume + dedup, instead of minting an unrelated one."""
    run_id = current_run_id() or audit.new_run_id()
    t0 = time.monotonic()
    # F-9 (HR S-10): resolve the EFFECTIVE sensitivity ceiling (declared tier
    # when omitted; never absent) and echo it on ``filter_applied`` so the
    # response honestly reflects the ceiling actually enforced — not the raw
    # (possibly empty) request filter.
    ceiling = _effective_ceiling(req)
    filter_applied = (req.filter or RecallQueryFilter()).model_copy(
        update={"sensitivity_max": ceiling}
    )
    warnings: list[str] = []

    # Surface index state up-front so a route failure (e.g. missing index) still
    # produces a useful payload via the exception path. Defensive: never raises.
    index_state = _read_index_state(VAULT)
    if not index_state.fts_present and index_state.notes_indexed > 0:
        warnings.append(
            "FTS sidecar absent on a populated index — "
            "scoring degrades to vector + importance only. "
            "Run /recall index --rebuild to restore the FTS lane."
        )

    # Lazy import of OllamaClient — the recall routine needs it to embed the
    # query. Deferred to call-time so the route module imports clean even if
    # ollama is offline (the call will raise then, surfaced as 500).
    try:
        from routines.shared.ollama_client import OllamaClient
    except ImportError as e:
        raise HTTPException(
            status_code=500,
            detail=(
                f"OllamaClient not importable: {e}. "
                "Install routines deps in the FastAPI interpreter."
            ),
        )

    client = OllamaClient()
    routine_filter = _filter_to_routine(req, ceiling)

    # Stage 1-4 — the hybrid pipeline (see SKILL.md Core Pattern). Iron Law
    # applies at the route boundary: an exception is NOT a clean pass — surface
    # verbatim. The wrapper PASSES THROUGH a body HTTPException (it lets the skill
    # choose its own status), so these 503/500 still reach the client unchanged.
    try:
        hits = _retrieve.query(
            req.query,
            vault_root=VAULT,
            client=client,
            filter_=routine_filter,
            limit=req.limit,
            rerank=req.rerank,
            graph=req.graph,
            graph_expand=req.graph_expand,
        )
    except FileNotFoundError as e:
        # Index missing — distinct from other failures, gets its own 503 so the
        # dashboard can show a "reindex" CTA.
        log.error("recall-query index missing: %s", e)
        raise HTTPException(
            status_code=503,
            detail=f"Recall index not found: {e}",
        )
    except Exception as e:  # noqa: BLE001 — query errors map to 500
        log.error("recall-query failed: %s", e, exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"recall query failed: {e}",
        )

    # Iron Law clause 1: every hit gets the full score decomposition.
    hits_out: list[RecallHit] = [_hit_out(i + 1, h) for i, h in enumerate(hits)]

    return RecallQueryResult(
        status="ok",
        run_id=run_id,
        query=req.query,
        limit_applied=req.limit,
        filter_applied=filter_applied,
        index_state=index_state,
        hits=hits_out,
        duration_ms=int((time.monotonic() - t0) * 1000),
        warnings=warnings,
    )
