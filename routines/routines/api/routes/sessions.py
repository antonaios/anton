"""Sessions API routes.

  * POST   /api/sessions                       — create a new session
  * GET    /api/sessions                       — list sessions (filter by workspace + archived)
  * GET    /api/sessions/{id}                  — single session by id
  * POST   /api/sessions/{id}/messages         — append user message + route + return Anton response
  * POST   /api/sessions/{id}/archive          — flip the archived flag
  * GET    /api/sessions/{id}/history          — LangGraph-shaped StateSnapshot stream

Implements OUTSTANDING.md ## CONTRACTS · sessions (locked 2026-05-24).

Streaming note: the contract enumerates ``?stream_mode=updates|messages|values|custom``
on the messages endpoint. v1 ships the non-streaming JSON path (single
``{user_message, anton_message}`` envelope). The query param is accepted +
echoed in the response so the harness can wire the contract today; SSE
streaming lands behind it once #2 (StreamMode taxonomy on chat composer)
starts. See OUTSTANDING.md #2 for the follow-on.
"""

from __future__ import annotations

import logging
import time
from functools import lru_cache
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field, StringConstraints

from routines.api.deps import ROUTINES_REPO
from routines.api.middleware.session_lock import (
    SessionLockBusy,
    acquire_session_lock,
    release_session_lock,
)
from routines.sessions import router as sess_router
from routines.sessions.store import (
    Message,
    SessionStore,
    make_user_message,
)
from routines.shared import audit
from routines.skills._runtime.llm_call_counter import current_run_id

router = APIRouter()
log = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────────────
# Store singleton
# ────────────────────────────────────────────────────────────────────────────


@lru_cache(maxsize=1)
def _store() -> SessionStore:
    """Lazy single store instance — picks up ``AGENTIC_SESSIONS_DIR`` env var
    at first call, so tests can monkeypatch the env before importing."""
    return SessionStore()


# ────────────────────────────────────────────────────────────────────────────
# Pydantic IO (mirrors OUTSTANDING.md ## CONTRACTS · sessions)
# ────────────────────────────────────────────────────────────────────────────


WorkspaceType = Literal["project", "bd", "general"]
Mode = Literal["chat", "skill", "composite", "crew"]
Sensitivity = Literal["public", "internal", "confidential", "MNPI"]
StreamMode = Literal["updates", "messages", "values", "custom"]


class CreateSessionRequest(BaseModel):
    workspace_type: WorkspaceType
    workspace_name: str = Field(..., min_length=1)
    mode: Mode = "chat"
    verb: str | None = None
    title: str | None = None


class SessionDTO(BaseModel):
    id: str
    workspace_type: WorkspaceType
    workspace_name: str
    title: str
    mode: Mode
    verb: str | None = None
    created: str
    last_active: str
    archived: bool
    pinned: bool = False
    message_count: int
    # Chat-header "% context window" (dashboard 9efe949). snake_case here;
    # the dashboard camelizes to contextTokens / contextWindow and renders
    # min(100, round(context_tokens / context_window * 100)); null → "—".
    context_tokens: int | None = None
    context_window: int | None = None


class ListSessionsResponse(BaseModel):
    sessions: list[SessionDTO]


class MessageDTO(BaseModel):
    """Mirrors the OUTSTANDING ## CONTRACTS · sessions Message type. Optional
    fields stay omitted when empty so the wire stays small."""
    id: str
    session_id: str
    parent_message_id: str | None = None
    role: Literal["user", "anton"]
    who: str
    time: str
    created: str
    body: str | None = None
    kpis: list[dict[str, Any]] | None = None
    commentary: str | None = None
    chips: list[dict[str, Any]] | None = None
    steps: list[dict[str, Any]] | None = None
    running: bool = False
    runningText: str | None = None
    duration_ms: int | None = None
    route: str | None = None
    lane: Literal["chat", "skill", "composite", "crew"] | None = None
    parent_run_id: str | None = None
    crew_run_id: str | None = None


class AttachmentText(BaseModel):
    """A LOCALLY-extracted document attachment threaded into a chat turn
    (#chat-attachments). ``text`` is the text the attachments route extracted
    on-box — the binary's bytes never reach this path. Lengths are bounded
    defensively so a hostile / oversized payload can't blow the prompt; the
    canonical extraction cap is enforced upstream in the attachments route."""

    # filename for the "Attached document \"<filename>\":" injection header.
    filename: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255),
    ]
    # The extracted text. Capped at the upstream extraction ceiling (24k) with a
    # little headroom so a slightly-over payload is clamped rather than 422'd.
    text: Annotated[str, StringConstraints(max_length=32_000)]


class PostMessageRequest(BaseModel):
    text: str = Field(..., min_length=1)
    sensitivity_override: Sensitivity | None = Field(
        None,
        description=(
            "Optional, MUST be same-tier-or-stricter than the workspace default. "
            "Less-restrictive overrides return 403 — never silently downgrade."
        ),
    )
    model_override: Literal["minimax"] | None = Field(
        None,
        description=(
            "Optional operator model selection (#minimax-chat-model). 'minimax' "
            "routes this turn to the MiniMax cloud model — refused (403) in a "
            "local-routed (confidential/MNPI) workspace. Absent = default routing."
        ),
    )
    # #chat-attachments — LOCALLY-extracted document text to PREPEND to this
    # turn's prompt. List length bounded to 10 (defensive); each item's text is
    # already capped in AttachmentText. The injected-text turn rides the EXISTING
    # router unchanged — its sensitivity decision is NOT affected by attachments.
    attachments: Annotated[
        list[AttachmentText] | None, Field(default=None, max_length=10),
    ] = None


class PostMessageResponse(BaseModel):
    user_message: MessageDTO
    anton_message: MessageDTO
    route: str
    lane: str
    sensitivity: Sensitivity
    stream_mode: StreamMode | None = None


class MessagesListResponse(BaseModel):
    messages: list[MessageDTO]


class ArchiveResponse(BaseModel):
    ok: bool


class RenameSessionRequest(BaseModel):
    # strip_whitespace server-side so a whitespace-only title can't slip past.
    title: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=200),
    ]


class PinSessionRequest(BaseModel):
    pinned: bool


class DeleteResponse(BaseModel):
    ok: bool
    id: str


class StateSnapshotDTO(BaseModel):
    message_id: str
    parent_message_id: str | None = None
    created: str
    values: list[dict[str, Any]]
    next: str | None = None
    pending_writes: list[dict[str, Any]] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class HistoryResponse(BaseModel):
    snapshots: list[StateSnapshotDTO]


# ────────────────────────────────────────────────────────────────────────────
# Conversion helpers
# ────────────────────────────────────────────────────────────────────────────


def _msg_to_dto(m: Message) -> MessageDTO:
    return MessageDTO(
        id=m.id,
        session_id=m.session_id,
        parent_message_id=m.parent_message_id,
        role=m.role,
        who=m.who,
        time=m.time,
        created=m.created,
        body=m.body,
        kpis=m.kpis,
        commentary=m.commentary,
        chips=m.chips,
        steps=m.steps,
        running=m.running,
        runningText=m.runningText,
        duration_ms=m.duration_ms,
        route=m.route,
        lane=m.lane,
        parent_run_id=m.parent_run_id,
        crew_run_id=m.crew_run_id,
    )


def _session_to_dto(s) -> SessionDTO:  # noqa: ANN001 — store.Session dataclass
    return SessionDTO(**s.as_dict())


# ────────────────────────────────────────────────────────────────────────────
# Routes
# ────────────────────────────────────────────────────────────────────────────


@router.post("/sessions", response_model=SessionDTO, status_code=201)
def create_session(req: CreateSessionRequest) -> SessionDTO:
    """Create a new session. Returns the index row."""
    store = _store()
    run_id = audit.new_run_id()
    t0 = time.monotonic()
    try:
        sess = store.create_session(
            workspace_type=req.workspace_type,
            workspace_name=req.workspace_name,
            mode=req.mode,
            verb=req.verb,
            title=req.title,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e

    audit.write_structured(
        actor={"type": "user", "id": "operator"},
        entity_type="session",
        entity_id=sess.id,
        action="create",
        routine="sessions.create",
        audit_dir=ROUTINES_REPO / "runs",
        run_id=run_id,
        status="ok",
        inputs=req.model_dump(),
        outputs={"session_id": sess.id},
        duration_ms=int((time.monotonic() - t0) * 1000),
        details={
            "status": "ok",
            "workspace_type": req.workspace_type,
            "workspace_name": req.workspace_name,
            "mode": req.mode,
            "verb": req.verb,
        },
    )
    return _session_to_dto(sess)


@router.get("/sessions", response_model=ListSessionsResponse)
def list_sessions(
    workspace_type: WorkspaceType | None = Query(None),
    workspace_name: str | None = Query(None),
    archived: bool = Query(False),
    limit: int = Query(100, ge=1, le=500),
) -> ListSessionsResponse:
    sessions = _store().list_sessions(
        workspace_type=workspace_type,
        workspace_name=workspace_name,
        archived=archived,
        limit=limit,
    )
    return ListSessionsResponse(sessions=[_session_to_dto(s) for s in sessions])


@router.get("/sessions/{session_id}", response_model=SessionDTO)
def get_session(session_id: str) -> SessionDTO:
    sess = _store().get_session(session_id)
    if sess is None:
        raise HTTPException(status_code=404, detail=f"session not found: {session_id}")
    return _session_to_dto(sess)


@router.post("/sessions/{session_id}/messages", response_model=PostMessageResponse)
def post_message(
    session_id: str,
    req: PostMessageRequest,
    stream_mode: StreamMode | None = Query(
        None,
        description=(
            "Reserved per OUTSTANDING ## CONTRACTS. v1 returns one JSON envelope; "
            "SSE streaming lands behind #2 (StreamMode taxonomy)."
        ),
    ),
) -> PostMessageResponse:
    """Append the user's message, route to an LLM lane, persist Anton's
    response, return both. Fails closed on sensitivity violations.

    #59 — coalesced by ``session_id`` via the per-session lock. Concurrent
    POSTs to the same session are serialised: the first acquires, the
    second returns 409 (``session_lock_busy``) until the first releases
    or the prior run goes stale (60s). Same-``run_id`` retries also 409
    so the client polls/waits instead of double-firing the LLM.
    """
    store = _store()
    sess = store.get_session(session_id)
    if sess is None:
        raise HTTPException(status_code=404, detail=f"session not found: {session_id}")

    # The X-ANTON-Run-Id middleware always binds a value before any route
    # runs — ``current_run_id()`` is never None inside a request. Fall
    # back to ``audit.new_run_id()`` defensively (e.g. middleware
    # unregistered in a future refactor) so the route still works.
    run_id = current_run_id() or audit.new_run_id()
    t0 = time.monotonic()

    # Coalesce per session_id. Acquire BEFORE any state mutation
    # (append_message) so a same-id retry can't slip a duplicate user
    # message into the store between the previous run's last write and
    # its lock release. NAMESPACE the key (``session:``) so it can never
    # collide with another route's key in the shared lock table (e.g.
    # project-chat's ``project-chat:<code>``) (codex-5.5 F-13 r1).
    lock_key = f"session:{session_id}"
    try:
        acquire_session_lock(lock_key, run_id)
    except SessionLockBusy as e:
        same_id = e.pending_run_id == run_id
        raise HTTPException(
            status_code=409,
            detail={
                "error": "session_lock_busy",
                "session_id": session_id,
                "pending_run_id": e.pending_run_id,
                "acquired_age_sec": e.acquired_age_sec,
                "same_run_id_retry": same_id,
                "human_message": (
                    f"This request (run {run_id[:8]}…) is already in flight — "
                    "wait for the response, don't retry."
                    if same_id else
                    f"Another request is in flight for this session "
                    f"(run {e.pending_run_id[:8]}…). Wait or retry shortly."
                ),
            },
        ) from e

    try:
        # 1. Append user message
        user_msg = store.append_message(session_id, make_user_message(req.text))

        # 2. Decide route + dispatch
        history = store.get_messages(session_id)
        try:
            anton_msg, decision = sess_router.route_and_respond(
                sess,
                user_msg,
                sensitivity_override=req.sensitivity_override,
                model_override=req.model_override,
                message_history=history[:-1],   # exclude the just-appended user msg
                attachments=(
                    [a.model_dump() for a in req.attachments]
                    if req.attachments else None
                ),
            )
        except sess_router.SensitivityViolation as e:
            # Fail-closed: audit the refused dispatch and return 403.
            audit.write_structured(
                actor={"type": "user", "id": "operator"},
                entity_type="session",
                entity_id=session_id,
                action="message",
                routine="sessions.messages",
                audit_dir=ROUTINES_REPO / "runs",
                run_id=run_id,
                status="error",
                inputs={
                    "session_id": session_id,
                    "workspace_type": sess.workspace_type,
                    "workspace_name": sess.workspace_name,
                    "sensitivity_override": req.sensitivity_override,
                },
                error=str(e),
                duration_ms=int((time.monotonic() - t0) * 1000),
                details={
                    "status": "error",
                    "sensitivity_override": req.sensitivity_override,
                    "error": str(e),
                },
            )
            raise HTTPException(status_code=403, detail=str(e)) from e

        # 3. Persist Anton response
        anton_msg = store.append_message(session_id, anton_msg)

        # 3b. Refresh the session's context-window reading (dashboard 9efe949).
        # Only when this turn produced a real prompt-token count — a stub /
        # error / unwired-cloud turn leaves the last good reading in place.
        if decision.context_tokens is not None:
            store.update_session_context(
                session_id,
                context_tokens=decision.context_tokens,
                context_window=decision.context_window,
            )

        audit.write_structured(
            actor={"type": "user", "id": "operator"},
            entity_type="session",
            entity_id=session_id,
            action="message",
            routine="sessions.messages",
            audit_dir=ROUTINES_REPO / "runs",
            run_id=run_id,
            status="ok",
            inputs={
                "session_id": session_id,
                "workspace_type": sess.workspace_type,
                "workspace_name": sess.workspace_name,
                "stream_mode": stream_mode,
                "sensitivity_override": req.sensitivity_override,
            },
            outputs={
                "lane": decision.lane,
                "sensitivity": decision.sensitivity,
                "user_message_id": user_msg.id,
                "anton_message_id": anton_msg.id,
            },
            duration_ms=int((time.monotonic() - t0) * 1000),
            details={
                "status": "ok",
                "lane": decision.lane,
                "sensitivity": decision.sensitivity,
            },
        )

        return PostMessageResponse(
            user_message=_msg_to_dto(user_msg),
            anton_message=_msg_to_dto(anton_msg),
            route=anton_msg.route or "",
            lane=decision.lane,
            sensitivity=decision.sensitivity,
            stream_mode=stream_mode,
        )
    finally:
        release_session_lock(lock_key, run_id)


@router.get("/sessions/{session_id}/messages", response_model=MessagesListResponse)
def list_messages(session_id: str) -> MessagesListResponse:
    """Full ordered message list for a session — lets ChatCanvas render an
    existing thread on tab-switch / page-refresh. No pagination in v1;
    revisit if a session crosses 500 messages."""
    store = _store()
    if store.get_session(session_id) is None:
        raise HTTPException(status_code=404, detail=f"session not found: {session_id}")
    messages = store.get_messages(session_id)
    return MessagesListResponse(messages=[_msg_to_dto(m) for m in messages])


@router.post("/sessions/{session_id}/archive", response_model=ArchiveResponse)
def archive_session(session_id: str) -> ArchiveResponse:
    updated = _store().archive_session(session_id)
    if not updated:
        raise HTTPException(status_code=404, detail=f"session not found: {session_id}")
    return ArchiveResponse(ok=True)


@router.post("/sessions/{session_id}/rename", response_model=SessionDTO)
def rename_session(session_id: str, req: RenameSessionRequest) -> SessionDTO:
    """Set a session's title (#session-ops). 404 if unknown; the title is
    stripped + length-validated server-side, and internal whitespace collapsed
    to single spaces so a direct API call can't garble the sidebar with newlines
    (review S3-4)."""
    store = _store()
    title = " ".join(req.title.split())  # collapse newlines/tabs/runs → single spaces
    if not store.rename_session(session_id, title):
        raise HTTPException(status_code=404, detail=f"session not found: {session_id}")
    sess = store.get_session(session_id)
    if sess is None:  # raced with a delete — treat as gone
        raise HTTPException(status_code=404, detail=f"session not found: {session_id}")
    return _session_to_dto(sess)


@router.post("/sessions/{session_id}/pin", response_model=SessionDTO)
def pin_session(session_id: str, req: PinSessionRequest) -> SessionDTO:
    """Pin / unpin a session — pinned sessions sort first (#session-ops)."""
    store = _store()
    if not store.set_session_pinned(session_id, req.pinned):
        raise HTTPException(status_code=404, detail=f"session not found: {session_id}")
    sess = store.get_session(session_id)
    if sess is None:
        raise HTTPException(status_code=404, detail=f"session not found: {session_id}")
    return _session_to_dto(sess)


@router.delete("/sessions/{session_id}", response_model=DeleteResponse)
def delete_session(session_id: str) -> DeleteResponse:
    """Hard-delete a session (#session-ops). Drops the index row + (via FK
    cascade) its messages/writes; the raw transcript JSONL is kept as audit.
    Irreversible — audited. 404 if unknown; 409 if a message turn is in flight
    (deleting mid-turn would cascade the user message and 500 the pending Anton
    append — codex review SEV-2). Held under the same per-session lock the
    message route uses, so the two can never interleave."""
    store = _store()
    lock_key = f"session:{session_id}"
    run_id = current_run_id() or audit.new_run_id()
    try:
        acquire_session_lock(lock_key, run_id)
    except SessionLockBusy as e:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "session_busy",
                "session": session_id,
                "message": "a turn is in flight for this session — retry shortly",
                "pending_run_id": e.pending_run_id,
            },
        ) from e
    try:
        if not store.delete_session(session_id):
            raise HTTPException(status_code=404, detail=f"session not found: {session_id}")
        audit.write_structured(
            actor={"type": "user", "id": "operator"},
            entity_type="session",
            entity_id=session_id,
            action="delete",
            routine="sessions.delete",
            audit_dir=ROUTINES_REPO / "runs",
            run_id=run_id,
            status="ok",
            outputs={"session_id": session_id},
        )
    finally:
        release_session_lock(lock_key, run_id)
    return DeleteResponse(ok=True, id=session_id)


@router.get("/sessions/{session_id}/history", response_model=HistoryResponse)
def get_history(session_id: str) -> HistoryResponse:
    """LangGraph-shaped audit. One ``StateSnapshot`` per message; ``values``
    is the cumulative log up to that message, ``pending_writes`` is anything
    in-flight that hasn't committed yet."""
    store = _store()
    if store.get_session(session_id) is None:
        raise HTTPException(status_code=404, detail=f"session not found: {session_id}")

    snaps = store.get_history(session_id)
    return HistoryResponse(snapshots=[
        StateSnapshotDTO(
            message_id=s.message_id,
            parent_message_id=s.parent_message_id,
            created=s.created,
            values=s.values,
            next=s.next,
            pending_writes=s.pending_writes,
            metadata=s.metadata,
        )
        for s in snaps
    ])
