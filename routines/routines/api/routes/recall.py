"""Endpoints for the recall skill.

#16e — every `/api/recall` POST writes an audit row to
``routines/runs/recall.jsonl`` capturing query / project / sensitivity
ceiling / limit / hit count / synthesise flag / duration / status.
Operator uses this for compliance review + cost telemetry tie-in.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from routines.api.deps import ROUTINES_REPO, VAULT
from routines.api.job_registry import JobConcurrencyExceeded, launch_tracked
from routines.hooks import tool_call_hooks
from routines.shared import audit

router = APIRouter()
log = logging.getLogger(__name__)


# ── module-level OllamaClient singleton (#eff-hotpath-batch) ──────────────────
# BEFORE: every /api/recall POST did ``OllamaClient()``, whose ``__init__``
# builds a fresh ``requests.Session()`` (new connection pool) — discarded after
# a single embed call. AFTER: one client is created lazily and reused across
# requests. ``OllamaClient`` holds no per-request mutable state (only config +
# the Session, which ``requests`` documents as safe to share for concurrent
# sends — each request gets its own connection from the pool), so reuse leaks
# nothing between callers and keeps the keep-alive pool warm. The lock makes
# the lazy create race-free under the threadpool.
import threading as _threading

_ollama_client = None
_ollama_client_lock = _threading.Lock()


def _get_ollama_client():
    """Return the process-wide singleton OllamaClient, creating it on first use.

    Imported lazily inside the getter so a missing routines dep surfaces as
    the same ImportError the route already handles (rather than at module
    import time)."""
    global _ollama_client
    if _ollama_client is not None:
        return _ollama_client
    with _ollama_client_lock:
        if _ollama_client is None:
            from routines.shared.ollama_client import OllamaClient
            _ollama_client = OllamaClient()
        return _ollama_client


# ── per-hit provenance helpers (#recall-detail / task_97c4527d) ──────────────
_SENS_TIERS = frozenset({"public", "internal", "confidential"})


def _display_sensitivity(meta: object) -> str:
    """The hit's declared sensitivity tier for the dashboard chip. Reads the
    note's frontmatter ``sensitivity`` (the SAME field the retrieval gate filters
    on), normalised to public/internal/confidential/MNPI; an absent/unknown value
    defaults to ``internal`` (the route's documented retrieval default). Metadata
    only — never reads note content."""
    raw = (
        str(meta.get("sensitivity", "internal")).strip().lower()
        if isinstance(meta, dict) else "internal"
    )
    if raw == "mnpi":
        return "MNPI"
    if raw in _SENS_TIERS:
        return raw
    return "internal"


def _note_mtime(path: str) -> Optional[str]:
    """The hit note's last-modified time as an ISO-8601 UTC string, for the
    dashboard's date + STALE affordance. Stats the vault file (metadata only — no
    content read); returns None when the path is missing / unreadable."""
    try:
        p = Path(path)
        full = p if p.is_absolute() else (VAULT / p)
        return datetime.fromtimestamp(full.stat().st_mtime, tz=timezone.utc).isoformat()
    except (OSError, ValueError, TypeError):
        return None


class RecallRequest(BaseModel):
    query: str = Field(..., min_length=1)
    limit: int = Field(default=10, ge=1, le=50)
    synthesise: bool = False
    project: Optional[str] = None
    max_sensitivity: Optional[str] = None  # "public" | "internal" | "confidential" | "MNPI"
    # #54-rerank — opt-in cross-encoder rerank of the fused top-k. Default
    # OFF (adds ~1-3s + needs the optional `recall` extra installed). When
    # the reranker lib/model is absent the stage skips gracefully.
    rerank: bool = False
    # #45 graph leg — opt-in 3rd RRF channel boosting candidates structurally
    # near the top hits (wikilink proximity, cap 2 hops). Default OFF;
    # rebuilds the in-memory vault graph on call. Best-effort — a graph fault
    # degrades to vector+fts.
    graph: bool = False
    # #45-expansion leg — opt-in injection of the top hits' wikilink-graph
    # neighbours as NEW candidates (who-touched-style relational recall), fused
    # as an additive RRF leg. Independent of `graph`; default OFF. Injected
    # neighbours pass the SAME sensitivity gate as every candidate. Best-effort.
    graph_expand: bool = False


class RecallSource(BaseModel):
    rank: int
    path: str
    score: float
    # #recall-detail (task_97c4527d) — per-hit provenance for the dashboard
    # sources rail: the note's declared sensitivity tier + last-modified time
    # (ISO-8601 UTC). Optional / additive (older readers ignore them); METADATA
    # ONLY — the tier is already implied by the path (which is surfaced) and the
    # route only returns hits at/under the request's sensitivity ceiling, so this
    # leaks no content.
    sensitivity: Optional[str] = None
    mtime: Optional[str] = None
    # #54b hybrid-recall component fields. Optional / additive so older
    # consumers that only read `rank/path/score` keep working unchanged.
    # PRESERVED under #54-rrf: vector_score/fts_score still carry each
    # channel's RAW normalised score in [0,1] (cosine / FTS5-rank).
    vector_score: Optional[float] = None
    fts_score: Optional[float] = None
    importance: Optional[int] = None
    expires_decay: Optional[float] = None
    # #54-rrf fusion fields. Per-channel RRF contributions (1/(k+rank))
    # that summed into the fusion, their sum, and the #54-contradiction
    # multiplier. Optional / additive (older readers ignore them).
    vector_rrf: Optional[float] = None
    fts_rrf: Optional[float] = None
    rrf_score: Optional[float] = None
    contradiction_penalty: Optional[float] = None
    # Source-tier provenance multiplier (gbrain-pattern adoption). The
    # RESOLVED tier (1–3; explicit ``source_tier`` frontmatter, else path
    # default) and the post-fusion multiplier it contributed. Optional /
    # additive (older readers ignore them) — same precedent as
    # importance/expires_decay, which are likewise always populated.
    source_tier: Optional[int] = None
    tier_multiplier: Optional[float] = None
    # #45 graph leg — per-channel RRF contribution from the graph-proximity
    # channel (sibling to vector_rrf/fts_rrf). None when the channel was off
    # or the note was unreachable within the hop cap. Optional / additive.
    graph_rrf: Optional[float] = None
    # #45-expansion NOTE: the expansion leg's per-channel contribution
    # (``graph_expand_rrf`` on the NoteHit) is DELIBERATELY NOT surfaced on this
    # response DTO while the leg is off-by-default / experimental — adding a
    # field here would put a ``"graph_expand_rrf": null`` key on EVERY off-path
    # response, breaking the #no-mnpi-to-cloud byte-identity guarantee (was
    # cited as §5.4) for existing callers
    # (Codex SEV-1). It is available in-process on the NoteHit + via
    # ``recall query --graph-expand --explain``; surface it on the DTO together
    # with the default-on promotion (#45-recall-graph-default-on).


class RecallResponse(BaseModel):
    query: str
    hits: list[RecallSource]
    synthesis: Optional[str] = None


class IndexRequest(BaseModel):
    rebuild: bool = False


class JobStarted(BaseModel):
    status: str
    pid: Optional[int] = None
    detail: Optional[str] = None


@router.post("/recall", response_model=RecallResponse)
def recall(req: RecallRequest) -> RecallResponse:
    """In-process recall query. Imports the recall package and calls retrieve
    + (optionally) synthesise directly. Returns structured JSON.

    The bridge is loopback-only, so we keep the call in-process to surface
    proper error responses to the UI rather than parsing CLI stdout.
    """
    # Sensitivity: recall reads the full vault index. Respect the request's
    # max_sensitivity ceiling if given; otherwise treat as ``internal`` —
    # recall surfaces vault content beyond pure public-knowledge.
    requested_sens = (req.max_sensitivity or "internal").lower()
    if requested_sens not in ("public", "internal", "confidential", "mnpi"):
        requested_sens = "internal"
    sens = "MNPI" if requested_sens == "mnpi" else requested_sens

    # #16e audit — capture status + duration in a try/finally so the
    # row materialises whether the call succeeds, raises, or short-circuits.
    # #54b extension: the per-result component breakdown is included on
    # success rows; ``sources`` is initialised here so the finally block
    # can reference it even if the call raises before assignment.
    run_id = audit.new_run_id()
    t0 = time.monotonic()
    audit_status = "ok"
    audit_error: Optional[str] = None
    hits_count = 0
    sources: list[RecallSource] = []

    try:
        with tool_call_hooks(
            tool_name="recall_query",
            sensitivity=sens,  # type: ignore[arg-type]
            tool_input=req.model_dump(),
        ) as ctx:
            try:
                from routines.recall import retrieve as rtv
                from routines.recall import synthesise as syn
            except ImportError as e:
                # Routines deps not installed in the active interpreter.
                raise HTTPException(
                    status_code=500,
                    detail=(
                        f"Routines package not importable: {e}. "
                        "Install routines deps in the FastAPI interpreter "
                        "(see routines/api/README.md)."
                    ),
                ) from e

            client = _get_ollama_client()
            f = rtv.Filter(
                # Use the NORMALISED, defaulted ceiling (``sens``) — NOT the raw
                # ``req.max_sensitivity`` — so omitting the field caps retrieval
                # at ``internal`` (the route's documented default) instead of
                # applying NO ceiling. Without this the retrieval ceiling
                # diverged from the tier recorded on the audit row / central
                # guard, surfacing confidential/MNPI notes on an "internal" call
                # (#no-mnpi-to-cloud declared-vs-actual mismatch — was cited
                # as §5.4; codex-5.5 REAL-DEFECT).
                sensitivity_max=sens,
                project=req.project,
            )

            try:
                hits = rtv.query(
                    req.query, vault_root=VAULT, client=client, filter_=f,
                    limit=req.limit, rerank=req.rerank, graph=req.graph,
                    graph_expand=req.graph_expand,
                )
            except Exception as e:  # noqa: BLE001 — user-facing surface
                log.exception("recall query failed")
                raise HTTPException(status_code=500, detail=f"Recall failed: {e}") from e

            sources = [
                RecallSource(
                    rank=i + 1,
                    path=h.path,
                    score=float(h.score),
                    sensitivity=_display_sensitivity(getattr(h, "metadata", None)),
                    mtime=_note_mtime(getattr(h, "path", "")),
                    vector_score=(float(h.vector_score)
                                  if getattr(h, "vector_score", None) is not None else None),
                    fts_score=(float(h.fts_score)
                               if getattr(h, "fts_score", None) is not None else None),
                    importance=(int(h.importance)
                                if getattr(h, "importance", None) is not None else None),
                    expires_decay=(float(h.expires_decay)
                                   if getattr(h, "expires_decay", None) is not None else None),
                    vector_rrf=(float(h.vector_rrf)
                                if getattr(h, "vector_rrf", None) is not None else None),
                    fts_rrf=(float(h.fts_rrf)
                             if getattr(h, "fts_rrf", None) is not None else None),
                    rrf_score=(float(h.rrf_score)
                               if getattr(h, "rrf_score", None) is not None else None),
                    contradiction_penalty=(
                        float(h.contradiction_penalty)
                        if getattr(h, "contradiction_penalty", None) is not None else None),
                    graph_rrf=(float(h.graph_rrf)
                               if getattr(h, "graph_rrf", None) is not None else None),
                    source_tier=(int(h.source_tier)
                                 if getattr(h, "source_tier", None) is not None else None),
                    tier_multiplier=(
                        float(h.tier_multiplier)
                        if getattr(h, "tier_multiplier", None) is not None else None),
                )
                for i, h in enumerate(hits)
            ]
            hits_count = len(sources)

            synthesis: Optional[str] = None
            if req.synthesise and hits:
                try:
                    synthesis = syn.synthesise(
                        req.query, hits, client=client, vault_root=VAULT
                    )
                except Exception as e:  # noqa: BLE001
                    log.exception("synthesis failed")
                    synthesis = f"(synthesis failed: {e})"

            result = RecallResponse(query=req.query, hits=sources, synthesis=synthesis)
            ctx.result = result.model_dump()
            return result
    except HTTPException as e:
        audit_status = "error"
        audit_error = f"HTTP {e.status_code}: {e.detail}"
        raise
    except Exception as e:  # noqa: BLE001
        audit_status = "error"
        audit_error = f"{type(e).__name__}: {e}"
        raise
    finally:
        # #16e: per-query audit row in routines/runs/recall.jsonl. Feeds
        # compliance review + the LLM cost telemetry surface (different
        # JSONL from the hook-stack's tool.recall_query.jsonl on purpose
        # — this row is keyed on the recall-specific shape).
        try:
            audit.write_structured(
                actor={"type": "user", "id": "operator"},
                entity_type="session",
                entity_id=run_id,
                action="query",
                routine="recall",
                run_id=run_id,
                status=audit_status,
                audit_dir=ROUTINES_REPO / "runs",
                inputs={
                    "query": req.query,
                    "project": req.project,
                    "sens_max": req.max_sensitivity,
                    "limit": req.limit,
                    "synthesise_bool": req.synthesise,
                },
                outputs={
                    "hits_count": hits_count,
                    # #54b — per-result component breakdown so the
                    # dashboard's "why was this surfaced?" view + future
                    # tuning can replay scoring decisions. Additive; older
                    # JSONL readers ignore unknown keys.
                    "results": [s.model_dump() for s in sources] if audit_status == "ok" else [],
                },
                duration_ms=int((time.monotonic() - t0) * 1000),
                error=audit_error,
            )
        except Exception as audit_err:  # noqa: BLE001
            # Audit writes never break the caller. Operator sees the
            # warning in the bridge log if disk is full / unwritable.
            log.warning("recall audit write failed (suppressed): %s", audit_err)


@router.post("/recall/index", response_model=JobStarted)
def recall_index(req: IndexRequest) -> JobStarted:
    """Reindex is long-running — fire-and-forget subprocess, return PID."""
    cmd = [sys.executable, "-m", "routines.recall.cli", "index"]
    if req.rebuild:
        cmd.append("--rebuild")
    with tool_call_hooks(
        tool_name="recall_index",
        sensitivity="internal",
        tool_input=req.model_dump(),
    ) as ctx:
        try:
            # F-17 (HR S-12): registry-gated launch — bounded concurrency so a
            # CSRF/double-fire loop can't spawn unbounded reindex subprocesses.
            proc = launch_tracked(
                "recall-index",
                cmd,
                cwd=str(ROUTINES_REPO),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except JobConcurrencyExceeded as e:
            raise HTTPException(status_code=409, detail=str(e)) from e
        except OSError as e:
            raise HTTPException(status_code=500, detail=f"Failed to launch: {e}") from e
        result = JobStarted(status="started", pid=proc.pid)
        ctx.result = result.model_dump()
        return result
