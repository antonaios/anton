"""Project chat endpoints (OUTSTANDING #42 / plan §6.6).

  * POST /api/projects/{code}/chat          — run one chat turn, persist, return
  * GET  /api/projects/{code}/chat/history  — read stored history (no write)

The POST handler implements the 8-step behaviour from the spec:

  1. Resolve the deal's sensitivity from ``Projects/{code}/00 Brief.md`` and
     gate the lane — ``confidential`` / ``MNPI`` force LOCAL Ollama (§4).
  2. Read ``_chat.md`` and take the last ``history_turns`` turns.
  3. Project-filtered recall (strict project scope — v1 decision #2).
  4. Build the LLM prompt (system + history + sources block + user message).
  5. Call the local LLM.
  6. Atomic-append both the user + assistant turns to ``_chat.md``.
  7. Audit at ``routines/runs/project-chat.jsonl`` with the memory-lane field
     ``episodic_source`` = ``_chat.md + recall hits`` (#41 lane fields).
  8. Return the ``ChatResponse``.

Token-burn ceiling: NONE (v1 decision #3 — Ollama is local/free; the #57/#67
gates already cover cloud lanes).
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterator
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from routines.api.deps import ROUTINES_REPO, VAULT
from routines.api.middleware.session_lock import (
    SessionLockBusy,
    acquire_session_lock,
    release_session_lock,
)
from routines.project_chat.cli import StreamDelta, StreamDone, run_turn, run_turn_stream
from routines.project_chat.pull import resolve_sensitivity
from routines.project_chat.reader import load_history
from routines.project_chat.writer import ChatLogCorruptError
from routines.project_chat.schema import ChatRequest, ChatResponse, ChatTurn
from routines.shared import audit
from routines.shared.ollama_client import OllamaClient, OllamaError
from routines.skills._runtime.llm_call_counter import current_run_id


def _chat_lock_key(code: str) -> str:
    """Coalescing-lock key for a project chat room (F-13). One in-flight turn
    per room: serialises appends to ``_chat.md`` and stops a double-click from
    double-executing the LLM + double-appending the turn."""
    return f"project-chat:{code}"

router = APIRouter()
log = logging.getLogger(__name__)


def _project_dir(code: str) -> Path | None:
    """Resolve ``Projects/<code>/`` under VAULT — fail-closed on traversal.

    Returns the resolved project directory if it sits under ``VAULT/Projects/``
    AND exists. Returns ``None`` on path-separator / ``..`` injection, an
    out-of-tree resolution, or a missing directory. Mirrors
    ``projects._resolve_brief_path``.
    """
    if not code or any(sep in code for sep in ("/", "\\")) or ".." in code.split():
        return None
    if code in (".", ".."):
        return None
    projects_root = (VAULT / "Projects").resolve()
    candidate = (projects_root / code).resolve()
    try:
        candidate.relative_to(projects_root)
    except ValueError:
        return None
    if not candidate.is_dir():
        return None
    return candidate


class ChatHistoryResponse(BaseModel):
    """Read-only history payload for the GET endpoint."""

    project: str
    turns: list[ChatTurn]


@router.post("/projects/{code}/chat", response_model=ChatResponse)
def project_chat(code: str, req: ChatRequest) -> ChatResponse:
    """Run one chat turn for ``code`` and persist it to ``_chat.md``.

    404 if the project doesn't exist; 400 if ``message`` is empty.
    """
    if _project_dir(code) is None:
        raise HTTPException(status_code=404, detail=f"project not found: {code}")
    if not req.message or not req.message.strip():
        raise HTTPException(status_code=400, detail="message must not be empty")

    # Use the request-boundary run-id (the middleware validated/minted it, F-13)
    # so a same-id retry coalesces instead of double-firing; fall back to a
    # fresh id outside the middleware (e.g. direct test calls).
    run_id = current_run_id() or audit.new_run_id()
    t0 = time.monotonic()
    sensitivity = resolve_sensitivity(VAULT, code)

    # F-13 (HR S-16): this session-less route had NO coalescing lock — a
    # double-click double-executed the LLM and double-appended to _chat.md.
    # Serialise one in-flight turn per room (keyed on the project code); a
    # same-id retry or a concurrent different-id turn returns 409.
    lock_key = _chat_lock_key(code)
    try:
        acquire_session_lock(lock_key, run_id)
    except SessionLockBusy as e:
        same_id = e.pending_run_id == run_id
        raise HTTPException(
            status_code=409,
            detail={
                "error": "chat_turn_in_flight",
                "project": code,
                "message": (
                    "a chat turn is already running for this project — "
                    + ("wait for it to finish (same run-id retry)" if same_id
                       else "another turn is in flight; retry shortly")
                ),
                "pending_run_id": e.pending_run_id,
            },
        ) from e

    audit_status = "ok"
    audit_error: str | None = None
    recall_hits = 0
    source_paths: list[str] = []
    try:
        client = OllamaClient()
        resp = run_turn(
            VAULT, code, req.message,
            client=client, history_turns=req.history_turns,
            cross_projects=req.cross_projects,
        )
        recall_hits = resp.recall_hits
        source_paths = [s.path for s in resp.sources]
        return resp
    except HTTPException:
        audit_status = "error"
        raise
    except ChatLogCorruptError as e:
        audit_status = "error"
        audit_error = f"{type(e).__name__}: {e}"
        log.error("project-chat: corrupt log for %s: %s", code, e)
        raise HTTPException(status_code=409, detail=str(e)) from e
    except OllamaError as e:
        # Model unreachable / empty response → nothing persisted (parity with the
        # stream endpoint's `ollama_error` event). 503, not a fabricated turn.
        audit_status = "error"
        audit_error = f"{type(e).__name__}: {e}"
        log.warning("project-chat: model unavailable for %s: %s", code, e)
        raise HTTPException(
            status_code=503,
            detail=f"Local model unavailable — nothing was saved. {e}",
        ) from e
    except Exception as e:  # noqa: BLE001 — user-facing surface
        audit_status = "error"
        audit_error = f"{type(e).__name__}: {e}"
        log.exception("project-chat: turn failed for %s", code)
        raise HTTPException(status_code=500, detail=f"Chat failed: {e}") from e
    finally:
        # F-13: release the coalescing lock so the next turn (or a retry after
        # this one completes) can acquire. Held across the whole turn — incl.
        # the _chat.md append — so a retry can't slip a duplicate in.
        release_session_lock(lock_key, run_id)
        # #41 memory-lane audit: episodic_source = the deal's _chat.md plus
        # the recall hits that grounded this turn. No semantic/procedural
        # targets — chat only writes episodic (the appended turns).
        try:
            audit.write_structured(
                actor={"type": "user", "id": "operator"},
                entity_type="vault_note",
                entity_id=f"Projects/{code}/_chat.md",
                action="chat",
                routine="project-chat",
                run_id=run_id,
                status=audit_status,
                audit_dir=ROUTINES_REPO / "runs",
                inputs={
                    "project": code,
                    "message": req.message,
                    "history_turns": req.history_turns,
                    "sensitivity": sensitivity,
                    # Confidentiality-boundary trail: record whether this turn ran
                    # with the relaxed cross-project scope (≤ internal out-of-deal).
                    "cross_projects": req.cross_projects,
                },
                outputs={"recall_hits": recall_hits},
                duration_ms=int((time.monotonic() - t0) * 1000),
                error=audit_error,
                episodic_source={
                    "chat_md": f"Projects/{code}/_chat.md",
                    "recall_hits": source_paths,
                },
            )
        except Exception as audit_err:  # noqa: BLE001 — audit never breaks the caller
            log.warning("project-chat audit write failed (suppressed): %s", audit_err)


def _sse(event: str, data: dict) -> str:
    """Frame one Server-Sent Event: a named ``event:`` + a JSON ``data:`` line."""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _stream_chat_events(code: str, req: ChatRequest) -> Iterator[str]:
    """SSE body generator for ``POST /projects/{code}/chat/stream``.

    Lifecycle of events emitted:
      * ``start`` — flushed immediately so the client transitions out of its
        "connecting" state and any proxy buffer is primed.
      * ``delta`` — one per token as the local model generates ``{"text": …}``.
      * ``done``  — the full ``ChatResponse`` (assistant turn + sources +
        recall_hits + duration_ms), emitted AFTER both turns are persisted.
      * ``error`` — a structured failure ``{"code", "message"}`` when synthesis
        or the write fails. NOTHING is persisted on an ``error`` event.

    Invariants preserved from the non-stream route (#42 v2):
      * Confidential/MNPI gating, strict project scope, and atomic dual-turn
        persistence all come from ``run_turn_stream`` → ``writer.append_turns``
        unchanged (the writer is never bypassed).
      * **No partial/truncated turn is ever persisted.** Two guarantees combine:
        (a) ``OllamaClient.chat_stream`` raises if the model stream ends before
        its ``done`` frame, so ``run_turn_stream`` reaches ``append_turns`` ONLY
        with a complete answer; (b) the generator is demand-driven, so a
        disconnect DURING token streaming freezes it at a ``delta`` yield and the
        write is never reached. A disconnect landing exactly at the final
        ``delta``→``done`` boundary may still persist the (complete) turn — that
        is recorded as ``ok`` (a successful write is never downgraded); only a
        disconnect BEFORE the write is ``cancelled``.
      * Same ``routines/runs/project-chat.jsonl`` audit row as the non-stream
        route, with ``inputs.stream = true``.
    """
    run_id = audit.new_run_id()
    t0 = time.monotonic()
    sensitivity = resolve_sensitivity(VAULT, code)

    audit_status = "ok"
    audit_error: str | None = None
    recall_hits = 0
    source_paths: list[str] = []
    persisted = False  # set once run_turn_stream has written BOTH turns
    try:
        client = OllamaClient()
        yield _sse("start", {"project": code})
        for event in run_turn_stream(
            VAULT, code, req.message,
            client=client, history_turns=req.history_turns,
            cross_projects=req.cross_projects,
        ):
            if isinstance(event, StreamDelta):
                yield _sse("delta", {"text": event.text})
            elif isinstance(event, StreamDone):
                # run_turn_stream persists BOTH turns BEFORE yielding StreamDone,
                # so by here the write has already succeeded — mark it so a
                # disconnect at the `done` yield below can't mislabel the audit.
                persisted = True
                resp = event.response
                recall_hits = resp.recall_hits
                source_paths = [s.path for s in resp.sources]
                yield _sse("done", {
                    "turn": resp.turn.model_dump(),
                    "sources": [s.model_dump() for s in resp.sources],
                    "recall_hits": resp.recall_hits,
                    "duration_ms": resp.duration_ms,
                    "cross_projects": resp.cross_projects,
                })
    except GeneratorExit:
        # Client disconnected. If the turn was already persisted (we reached the
        # StreamDone boundary, e.g. disconnect discovered only when sending the
        # `done` frame) the write SUCCEEDED — keep status "ok"; never downgrade a
        # successful write (SEV-2). Only a disconnect BEFORE the write — paused at
        # a delta yield, nothing persisted — is "cancelled". Re-raise either way
        # (a generator MUST propagate GeneratorExit).
        if not persisted:
            audit_status = "cancelled"
        raise
    except ChatLogCorruptError as e:
        audit_status = "error"
        audit_error = f"{type(e).__name__}: {e}"
        log.error("project-chat stream: corrupt log for %s: %s", code, e)
        yield _sse("error", {"code": "corrupt_log", "message": str(e)})
    except OllamaError as e:
        # Mid- or pre-stream model failure → discard the (partial) turn. The
        # back-compat non-stream route still substitutes a fallback one-liner;
        # streaming surfaces the failure so the client can retry cleanly.
        audit_status = "error"
        audit_error = f"{type(e).__name__}: {e}"
        log.warning("project-chat stream: model error for %s: %s", code, e)
        yield _sse("error", {"code": "ollama_error", "message": str(e)})
    except Exception as e:  # noqa: BLE001 — user-facing surface
        audit_status = "error"
        audit_error = f"{type(e).__name__}: {e}"
        log.exception("project-chat stream: turn failed for %s", code)
        yield _sse("error", {"code": "error", "message": f"Chat failed: {e}"})
    finally:
        # Same #41 memory-lane audit shape as the non-stream route. Best-effort —
        # an audit failure never breaks the stream. Runs on every terminal path
        # (done / error / cancelled) since the body holds no further yields.
        try:
            audit.write_structured(
                actor={"type": "user", "id": "operator"},
                entity_type="vault_note",
                entity_id=f"Projects/{code}/_chat.md",
                action="chat",
                routine="project-chat",
                run_id=run_id,
                status=audit_status,
                audit_dir=ROUTINES_REPO / "runs",
                inputs={
                    "project": code,
                    "message": req.message,
                    "history_turns": req.history_turns,
                    "sensitivity": sensitivity,
                    "stream": True,
                    # Confidentiality-boundary trail (parity with the non-stream route).
                    "cross_projects": req.cross_projects,
                },
                outputs={"recall_hits": recall_hits},
                duration_ms=int((time.monotonic() - t0) * 1000),
                error=audit_error,
                episodic_source={
                    "chat_md": f"Projects/{code}/_chat.md",
                    "recall_hits": source_paths,
                },
            )
        except Exception as audit_err:  # noqa: BLE001 — audit never breaks the caller
            log.warning("project-chat stream audit write failed (suppressed): %s", audit_err)


@router.post("/projects/{code}/chat/stream")
def project_chat_stream(code: str, req: ChatRequest) -> StreamingResponse:
    """Streaming sibling of ``POST /projects/{code}/chat`` (#42 v2).

    Returns ``text/event-stream`` — the assistant answer arrives token-by-token
    as ``delta`` events, then a terminal ``done`` (both turns persisted) or
    ``error`` event. Validation (404 unknown project / 400 empty message) runs
    BEFORE streaming starts, so those stay real HTTP statuses; once the stream
    is open every failure is an ``error`` SSE event (the status line is gone).
    The non-stream route is preserved verbatim for back-compat.
    """
    if _project_dir(code) is None:
        raise HTTPException(status_code=404, detail=f"project not found: {code}")
    if not req.message or not req.message.strip():
        raise HTTPException(status_code=400, detail="message must not be empty")

    return StreamingResponse(
        _stream_chat_events(code, req),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # Defeat proxy/uvicorn response buffering so deltas flush live.
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/projects/{code}/chat/history", response_model=ChatHistoryResponse)
def project_chat_history(code: str) -> ChatHistoryResponse:
    """Return the deal's stored chat history without writing. 404 if unknown."""
    if _project_dir(code) is None:
        raise HTTPException(status_code=404, detail=f"project not found: {code}")
    turns = load_history(VAULT, code)
    return ChatHistoryResponse(project=code, turns=turns)
