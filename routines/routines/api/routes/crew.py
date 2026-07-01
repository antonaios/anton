"""Crew lane bridge routes (#31) — MetaGPT crews over a subprocess boundary.

Endpoints:
  * ``POST /api/crew/{verb}/run``            — guard + launch (202, async)
  * ``POST /api/crew/runs/{run_id}/cancel``  — kill by PID (idempotent)
  * ``GET  /api/crew/runs/{run_id}?verb=``   — assembled run record from audit
  * ``GET  /api/crew/runs/{run_id}/events``  — SSE (human asks + completion)
  * ``POST /api/crew/runs/{run_id}/human-input`` — operator reply to a
    mid-crew HumanProvider ask
  * ``GET  /api/crew/manifest``              — registered crews + metadata

NEVER imports metagpt — the boundary is ``routines.crew.proxy``
(``tests/crew/test_crew_routes.py::test_bridge_does_not_import_metagpt``
locks the invariant repo-wide).

The sensitivity gate fires BEFORE the subprocess starts: the route resolves
the crew tier (``routines.crew.registry``), synthesizes an
``LLMCallHookContext`` and hands it to the SAME ``enforce_sensitivity_lane``
guard that protects single skills — no new guard logic (spec §5.1). A refusal
is 403 + a ``refused`` audit row; no process is spawned, no tokens are spent.

ADAPTED from the staged ``bridge_proxy_pattern.py``:
  * ``WorkspaceCtx`` → ``WorkspaceRef`` (hooks/types.py post-#22 name);
    ``LLMCallHookContext`` grew required ``prompt`` since the sketch.
  * audit via ``routines.crew.audit_mirror`` (``write_structured`` substrate)
    instead of the legacy ``audit.write``.
  * cancel terminates via the registered Popen HANDLE (F-40 — raw-PID kill
    races PID reuse) + the pid-store *cancelled mark* so the worker thread
    can audit ``cancelled`` (not ``error``) — the sketch had no way to tell
    the two apart.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from pathlib import Path
from queue import Empty, Queue
from typing import Any, AsyncGenerator, Literal

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from routines.api.deps import RUNS_DIR as _DEFAULT_RUNS_DIR, VAULT
from routines.crew import (
    artefacts,
    audit_mirror,
    overrides as crew_overrides,
    proxy,
    registry,
    run_context,
)
from routines.crew.pid_store import pid_store
from routines.crew.types import (
    CrewStatus,
    CrewWorkspace,
    RoleLogEntry,
    Sensitivity,
)
from routines.hooks.central_guards import (
    SensitivityViolation,
    enforce_sensitivity_lane,
)
from routines.hooks.types import LLMCallHookContext, SkillRef, WorkspaceRef
from routines.shared import audit
from routines.skills._runtime.capture import emit_deliverable_proposal

router = APIRouter()
log = logging.getLogger(__name__)

# Module-level so tests can monkeypatch the audit target without touching
# the shared deps module.
RUNS_DIR: Path = _DEFAULT_RUNS_DIR

# Operator absent → a mid-crew ask fails the run rather than hanging it.
HUMAN_REPLY_TIMEOUT_S = int(os.environ.get("ANTON_CREW_HUMAN_REPLY_TIMEOUT_S", "300"))

# Backstop for SSE event queues nobody ever consumed (codex-5.5 xhigh,
# 2026-06-10): the generator drops the queue after delivering the terminal
# event, but a run whose SSE channel was never opened would leak its queue
# forever. The timer fires well after any reasonable poll.
EVENT_QUEUE_TTL_S = int(os.environ.get("ANTON_CREW_EVENT_TTL_S", "600"))


# ────────────────────────────────────────────────────────────────────────────
# Request / response models
# ────────────────────────────────────────────────────────────────────────────


class CrewRunRequest(BaseModel):
    """POST /api/crew/{verb}/run body."""

    workspace: CrewWorkspace
    args: dict[str, Any] = Field(default_factory=dict)
    session_id: str | None = None
    sensitivity_override: Sensitivity | None = Field(
        None,
        description=(
            "Must be same-tier-or-stricter than the workspace default. "
            "Crews with a manifest lock (the future /triage always-MNPI "
            "rule) refuse every differing override with 403."
        ),
    )


class CrewRunResponse(BaseModel):
    run_id: str
    verb: str
    status: CrewStatus
    sse_url: str
    poll_url: str


class CrewCancelResponse(BaseModel):
    run_id: str
    status: Literal["cancelled", "already_terminated"]


class CrewHumanInputRequest(BaseModel):
    msg_id: str
    response: str


class CrewRunRecord(BaseModel):
    """GET /api/crew/runs/{run_id} — parent + role audit rows, assembled."""

    run_id: str
    verb: str
    status: str
    workspace: CrewWorkspace
    sensitivity: str
    duration_ms: int | None = None
    token_count: int = 0
    summary: str | None = None
    artefacts: list[dict[str, Any]] = Field(default_factory=list)
    roles_log: list[RoleLogEntry] = Field(default_factory=list)
    error: str | None = None
    started_at: str
    completed_at: str | None = None


class CrewManifestResponse(BaseModel):
    crews: list[registry.CrewManifestEntry]


# ────────────────────────────────────────────────────────────────────────────
# SSE event queues + human-input reply plumbing
# ────────────────────────────────────────────────────────────────────────────


_event_queues: dict[str, Queue] = {}
_event_queues_lock = threading.Lock()

_human_reply_q: dict[tuple[str, str], Queue] = {}
_human_reply_lock = threading.Lock()


def _get_or_create_event_queue(run_id: str) -> Queue:
    with _event_queues_lock:
        q = _event_queues.get(run_id)
        if q is None:
            q = Queue()
            _event_queues[run_id] = q
        return q


def _push_event(run_id: str, event: str, data: dict[str, Any]) -> None:
    _get_or_create_event_queue(run_id).put({"event": event, "data": data})


def _drop_event_queue(run_id: str) -> None:
    with _event_queues_lock:
        _event_queues.pop(run_id, None)


def _await_human_input(run_id: str, envelope: dict[str, Any]) -> str:
    """Called from the crew worker thread when stdout yields a
    ``human_input_required`` envelope. Publishes the prompt to the run's SSE
    channel, then blocks until the operator replies via
    ``POST /human-input`` (or times out → the run fails)."""
    msg_id = str(envelope.get("msg_id") or "")
    key = (run_id, msg_id)
    # Register the PENDING ask before announcing it (codex-5.5 SEV-2): the
    # reply endpoint only accepts replies for keys registered here, so a
    # stale/spoofed POST can't pre-create queues — and the SSE event that
    # triggers the operator's reply is guaranteed to find the queue waiting.
    with _human_reply_lock:
        q = _human_reply_q.setdefault(key, Queue())
    _push_event(run_id, "human_input_required", {
        "msg_id": msg_id,
        "prompt": envelope.get("prompt", ""),
        "context": envelope.get("context", {}),
    })
    try:
        return q.get(timeout=HUMAN_REPLY_TIMEOUT_S)
    except Empty:
        raise RuntimeError(
            f"operator did not reply to human-input ask {msg_id!r} within "
            f"{HUMAN_REPLY_TIMEOUT_S}s"
        ) from None
    finally:
        with _human_reply_lock:
            _human_reply_q.pop(key, None)


# ────────────────────────────────────────────────────────────────────────────
# Server-side workspace anchor (#sec-crew-workspace-tier-validation 6b-2)
# ────────────────────────────────────────────────────────────────────────────


def _session_workspace_type(session_id: str | None) -> str | None:
    """Authoritative server-side workspace TYPE for the run's sensitivity floor
    (#sec-crew-workspace-tier-validation 6b-2). Reads the type stored on the
    session at creation — immutable, and not rewritable through this launch
    request — so the caller-supplied ``workspace.type`` can be floored against
    it. Returns ``None`` (→ ``resolve_workspace_anchor`` floors to confidential)
    when there is no ``session_id``, it doesn't resolve, or the store read
    fails: fail-closed, and never crash a launch on a session-store hiccup.

    Constructs ``SessionStore()`` directly (cheap; same SQLite file, honours
    ``AGENTIC_SESSIONS_DIR``) rather than importing the sessions ROUTE module,
    keeping the crew route decoupled from it."""
    if not session_id:
        return None
    try:
        from routines.sessions.store import SessionStore
        sess = SessionStore().get_session(session_id)
        # Read the attribute INSIDE the try (codex r1): a malformed / duck-typed
        # store result must fail-closed to confidential, never 500 the launch.
        return getattr(sess, "workspace_type", None) if sess is not None else None
    except Exception:  # noqa: BLE001 — a store read must never crash a launch
        log.warning(
            "crew: session workspace lookup failed for session_id=%r — treating "
            "as session-less (fail-closed to confidential)", session_id,
            exc_info=True,
        )
        return None


# ────────────────────────────────────────────────────────────────────────────
# POST /api/crew/{verb}/run
# ────────────────────────────────────────────────────────────────────────────


@router.post("/crew/{verb}/run", response_model=CrewRunResponse, status_code=202)
def run_crew(verb: str, req: CrewRunRequest) -> CrewRunResponse:
    """Resolve sensitivity → run the central guard → launch the crew
    subprocess on a worker thread. Refuses 403 BEFORE any process exists.

    WORKSPACE ANCHOR (#sec-crew-workspace-tier-validation 6b-2, Shannon run #2;
    closes the codex-5.5 xhigh SEV-1 recorded 2026-06-10): the workspace
    type/tier in the request body is CALLER-SUPPLIED, so the resolved tier is
    FLOORED against a SERVER-SIDE anchor before any lane decision —
    ``_session_workspace_type`` reads the immutable workspace type stored on
    ``req.session_id``'s session (chat parity with
    ``sessions/router.default_sensitivity_for``), and a session-less run floors
    to ``confidential``. Combined with the existing fail-closed resolution
    (declared tiers only TIGHTEN, overrides can't loosen, the guard re-tightens
    via ``_strictest``), a caller mislabeling the workspace TYPE can now only
    TIGHTEN the run — it can never downgrade a confidential workspace to open a
    cloud lane (the residual risk the SEV-1 note flagged once crews became
    cloud-promotable, #crew-cloud-promotion). The floor maxes at confidential,
    so it never manufactures MNPI provenance for the P5 lift. Net product rule:
    cloud-promoting a general/public crew run requires launching it from a
    session. Still refuses 403 (refused override) BEFORE any process exists."""
    try:
        manifest = registry.get_manifest(verb)
    except registry.CrewRegistryError:
        raise HTTPException(404, f"crew verb {verb!r} not registered") from None

    if not proxy.crew_venv_available():
        raise HTTPException(
            503,
            f"crew venv not installed at {proxy.CREW_PYTHON} — run "
            f"`python -m routines.crew.install.install_metagpt install`",
        )

    workspace = req.workspace
    run_id = audit.new_run_id()

    # 1. Resolve sensitivity + lane (registry matrix; 403 on refused override).
    try:
        crew_sensitivity = registry.resolve_crew_sensitivity(
            manifest, workspace.type, workspace.sensitivity_tier,
            req.sensitivity_override,
        )
    except registry.SensitivityRefused as e:
        audit_mirror.write_refusal(
            verb=verb, run_id=run_id, audit_dir=RUNS_DIR,
            workspace_type=workspace.type, workspace_name=workspace.name,
            reason=str(e),
        )
        raise HTTPException(403, str(e)) from e

    # 1b. Server-side workspace anchor (#sec-crew-workspace-tier-validation 6b-2,
    #     Shannon run #2). ``workspace.type`` above is caller-supplied and
    #     unverifiable; a mislabeled confidential→general would — now that crews
    #     can cloud-promote (#crew-cloud-promotion) — open a cloud lane on a
    #     downgraded tier. Floor the resolved tier with a SERVER-SIDE anchor (the
    #     session's stored, immutable workspace type; session-less → confidential)
    #     so the caller's claim can only TIGHTEN. Everything downstream re-keys off
    #     this floored tier — pick_lane, the central guard, resolve_crew_promotion,
    #     and the loopback /api/crew/_llm gate (RunLLMContext.sensitivity) — so the
    #     fix propagates without touching the gate itself.
    crew_sensitivity, _anchor_tightened = registry.apply_workspace_anchor(
        crew_sensitivity, _session_workspace_type(req.session_id),
    )
    if _anchor_tightened:
        log.warning(
            "crew %s/%s: caller workspace.type=%r (tier=%r) is LOOSER than the "
            "server-side workspace anchor — refusing the downgrade and tightening "
            "to %s (the run proceeds at the stricter tier; the parent audit row "
            "records the floored tier)",
            verb, run_id, workspace.type, workspace.sensitivity_tier, crew_sensitivity,
        )
    crew_lane = registry.pick_lane(crew_sensitivity)

    # 2. Central sensitivity-lane guard — same gate as skills + composites.
    guard_ctx = LLMCallHookContext(
        run_id=run_id,
        skill=SkillRef(
            name=f"crew.{verb}",
            metadata={
                "sensitivity": crew_sensitivity,
                "cost_cap_tokens": manifest.cost_cap_tokens,
            },
        ),
        workspace=WorkspaceRef(type=workspace.type, name=workspace.name),
        sensitivity=crew_sensitivity,
        lane=crew_lane,
        provider="ollama" if crew_lane.startswith("ollama") else "claude-cli",
        model=registry.model_for_lane(crew_lane, manifest),
        prompt="",  # pre-launch gate; no prompt content crosses the bridge
    )
    try:
        enforce_sensitivity_lane(guard_ctx)
    except SensitivityViolation as e:
        audit_mirror.write_refusal(
            verb=verb, run_id=run_id, audit_dir=RUNS_DIR,
            workspace_type=workspace.type, workspace_name=workspace.name,
            reason=str(e),
        )
        raise HTTPException(403, str(e)) from e
    # The guard may have tightened the tier (fail-closed _strictest).
    crew_sensitivity = guard_ctx.sensitivity

    # MNPI provenance for the Phase-C attestation lift (#crew-cloud-promotion):
    # an MNPI run is liftable to cloud ONLY when the MNPI is EXPLICIT
    # operator-assigned (the crew lock, a per-run override, or the workspace's
    # declared MNPI tier) — never an unknown tier _strictest-coerced to MNPI (the
    # §3a safeguard, mirroring sessions/router.py::decide_route). Computed from
    # the ORIGINAL request/workspace signals so a coercion can't manufacture
    # "explicit" provenance.
    mnpi_explicit = registry.mnpi_explicit_for_run(
        manifest,
        request_override=req.sensitivity_override,
        workspace_tier=workspace.sensitivity_tier,
        resolved_sensitivity=crew_sensitivity,
    )

    # 2c. Per-role cloud-lane promotion (#crew-cloud-promotion). Resolve which
    #     roles the operator promoted to a frontier cloud lane, gated by the run's
    #     EFFECTIVE (post-guard) sensitivity + provenance: public/internal always,
    #     confidential→Claude under enterprise (B), MNPI→attested provider under
    #     enterprise + explicit (C); anything else force-locals. The loopback
    #     /api/crew/_llm endpoint independently re-runs the FULL central
    #     sensitivity gate per promoted call (fail-closed in two places).
    promotion = crew_overrides.resolve_crew_promotion(
        manifest, crew_sensitivity, mnpi_explicit=mnpi_explicit,
    )
    role_lanes = {role: rp.lane for role, rp in promotion.cloud_roles.items()}
    if promotion.any_promoted:
        log.info(
            "crew %s/%s promoting roles %s to cloud (sensitivity=%s)",
            verb, run_id, sorted(promotion.cloud_roles), crew_sensitivity,
        )

    # 2b. Cheap, synchronous arg validation (→ 400) — AFTER the sensitivity
    #     gates so a refused run is always observed as a 403 + refusal row, not
    #     masked by a malformed-input 400 (security signal dominates). For
    #     /triage this checks the CIM pdf_path is present + a real file; the
    #     slow page extraction runs later on the worker thread. No-op for verbs
    #     without input requirements.
    try:
        artefacts.validate_input(verb, req.args)
    except artefacts.CrewInputError as e:
        raise HTTPException(400, str(e)) from e

    # 3. Parent "started" row — before the process exists, so a bridge crash
    #    mid-crew still leaves a trace.
    audit_mirror.write_parent_started(
        verb=verb, run_id=run_id, audit_dir=RUNS_DIR,
        workspace_type=workspace.type, workspace_name=workspace.name,
        sensitivity=crew_sensitivity, lane=crew_lane,
        args=req.args, cost_cap_tokens=manifest.cost_cap_tokens,
        session_id=req.session_id,
    )

    # 4. Build CrewInput + launch on a worker thread.
    crew_input = {
        "crew_verb": verb,
        "run_id": run_id,
        "workspace": {
            "type": workspace.type,
            "name": workspace.name,
            "sensitivity_tier": crew_sensitivity,
        },
        "args": req.args,
        "cost_cap_tokens": manifest.cost_cap_tokens,
        "llm_config": registry.build_llm_config(
            crew_lane, manifest,
            role_lanes=role_lanes,
            bridge_url=run_context.bridge_llm_url() if promotion.any_promoted else None,
            run_id=run_id,
        ),
    }
    # Wall-clock bound = the crew's declared cost_cap_seconds, clamped by the
    # proxy's global ceiling (codex-5.5 SEV-2: the manifest cap was declared
    # but never enforced — hello_world says 60s, the global default is 600s).
    timeout_s = min(float(manifest.cost_cap_seconds), float(proxy.WALL_CLOCK_TIMEOUT_S))
    # Register the run's AUTHORITATIVE LLM-routing context so the loopback
    # /api/crew/_llm endpoint can re-derive a promoted call's lane/model/
    # sensitivity server-side (never trusting the subprocess). Only promoted runs
    # need one; TTL-reaped a little past the wall-clock budget so a run that dies
    # without cleanup can't leak its context.
    if promotion.any_promoted:
        run_context.register(
            run_context.RunLLMContext(
                run_id=run_id, verb=verb, sensitivity=crew_sensitivity,
                workspace_type=workspace.type, workspace_name=workspace.name,
                cost_cap_tokens=manifest.cost_cap_tokens,
                cloud_roles=dict(promotion.cloud_roles),
                # The MNPI provenance the loopback /api/crew/_llm gate re-checks
                # per promoted call (#crew-cloud-promotion Phase C): True ONLY for
                # an explicit operator-assigned MNPI run (see mnpi_explicit above).
                # A non-MNPI or unknown-coerced run carries False, so the gate's
                # attestation lift can never fire for it (the §3a safeguard).
                mnpi_explicit=mnpi_explicit,
            ),
            ttl_s=timeout_s + 30.0,
        )
    threading.Thread(
        target=_run_crew_thread,
        args=(verb, manifest.module, crew_input, run_id, crew_sensitivity,
              workspace, RUNS_DIR, timeout_s),
        daemon=True,
        name=f"crew-{verb}-{run_id}",
    ).start()

    return CrewRunResponse(
        run_id=run_id,
        verb=verb,
        status="queued",
        sse_url=f"/api/crew/runs/{run_id}/events",
        poll_url=f"/api/crew/runs/{run_id}?verb={verb}",
    )


def _run_crew_thread(
    verb: str,
    module: str,
    crew_input: dict[str, Any],
    run_id: str,
    sensitivity: str,
    workspace: CrewWorkspace,
    runs_dir: Path,
    timeout_s: float,
) -> None:
    """Worker — launches the subprocess via the proxy, then writes the parent
    completion row + per-role child rows."""
    t0 = time.monotonic()
    result: dict[str, Any] | None = None
    error: str | None = None
    status: str
    crew_artefact = ""   # materialised deliverable path → #captures-to-vault-crews artefact pointer

    try:
        # Prepare bridge-side inputs the crew can't fetch itself (e.g. extract
        # the /triage CIM PDF → pages). Off the request thread; a bad PDF is a
        # clean error run, not a 500. No-op for verbs without a preparer.
        crew_input = artefacts.prepare_crew_input(verb, crew_input)
        result = proxy.launch_crew(
            module, crew_input, run_id, sensitivity,
            on_human_input=lambda env: _await_human_input(run_id, env),
            timeout_s=timeout_s,
        )
        status = str(result.get("status", "ok"))
        if result.get("error"):
            error = str(result["error"])
        if str(result.get("run_id") or "") != run_id:
            status = "error"
            error = (
                f"run_id mismatch: sent {run_id!r}, crew echoed "
                f"{result.get('run_id')!r}"
            )
        # Materialise crew-returned deliverables (e.g. the /triage memo) through
        # the central write policy + raise the Inbox flag. The crew venv can't
        # write the vault itself; the bridge does it here. A refused/failed
        # write downgrades the run to error — the operator asked for a memo that
        # didn't land. ``finalize`` also drops the raw document content from
        # ``result`` so it never reaches the audit / run record.
        if status == "ok" and result.get("documents"):
            _written, doc_errors = artefacts.finalize(
                verb=verb, run_id=run_id, result=result, sensitivity=sensitivity,
            )
            # The materialised deliverable path → the capture's artefact pointer.
            if _written:
                crew_artefact = str(_written[0].get("path") or "")
            if doc_errors:
                status = "error"
                joined = "; ".join(doc_errors)
                error = joined if not error else f"{error}; {joined}"
    except artefacts.CrewInputError as e:
        status, error = "error", str(e)
        log.error("crew %s/%s input prep failed: %s", verb, run_id, e)
    except proxy.CrewTimeoutError as e:
        status, error = "timeout", str(e)
        log.warning("crew %s/%s timeout: %s", verb, run_id, e)
    except proxy.CrewSubprocessError as e:
        if pid_store.was_cancelled(run_id):
            status, error = "cancelled", "operator cancelled"
        else:
            status, error = "error", str(e)
            log.error("crew %s/%s failed: %s", verb, run_id, e)
    except Exception as e:  # noqa: BLE001 — worker thread must never die silently
        status, error = "error", f"bridge proxy crashed: {type(e).__name__}: {e}"
        log.exception("crew %s/%s bridge-side crash", verb, run_id)

    # The subprocess is dead the moment launch_crew returns OR raises (its proxy
    # finally reaps the child), and the except chain above catches everything —
    # so drop the run's LLM-routing context HERE, before the audit writes that
    # could raise. No promoted /api/crew/_llm call can arrive after this point;
    # register()'s TTL reaper is the backstop for a run that never reaches here.
    # No-op for a fully-local (unregistered) run.
    run_context.drop(run_id)

    duration_ms = int((time.monotonic() - t0) * 1000)
    # Defence in depth: never let raw deliverable content reach the audit/run
    # record. ``finalize`` already drops it on the success path; this covers
    # the error/partial paths where documents may still be present in ``result``.
    if isinstance(result, dict):
        result.pop("documents", None)

    # #captures-to-vault-crews: emit the operator-gated ``deliverable-outcome``
    # proposal carrying the crew's CONCLUSION (the structured ``outcome`` the
    # crew returned). BEST-EFFORT — a capture miss never fails the run (the
    # deliverable/chat already succeeded). Gated on: a clean run, a non-empty
    # ``outcome`` (so a run with no identifier captures nothing), and the crew
    # having opted in (registry ``captures_to_vault``). The proposal + the later
    # Route-append are LOCAL vault writes only → MNPI-safe (no egress); the
    # proposal target is re-confined to the vault by ``_route_deliverable_outcome``.
    if status == "ok" and isinstance(result, dict) and result.get("outcome"):
        try:
            _cap = registry.get_manifest(verb).captures_to_vault
        except registry.CrewRegistryError:
            _cap = None
        if _cap is not None:
            try:
                emit_deliverable_proposal(
                    verb, _cap, {**result, "run_id": run_id},
                    vault_root=VAULT, sensitivity=sensitivity,
                    provenance_kind="crew", artefact=crew_artefact,
                    # #crew-cloud-promotion harmony: record which roles were
                    # promoted to cloud so the captured conclusion is honest about
                    # how it was generated (already in crew_input's llm_config).
                    promoted_roles=(crew_input.get("llm_config") or {}).get("role_lanes") or None,
                )
            except Exception:  # noqa: BLE001 — capture is non-critical
                log.warning(
                    "crew %s/%s: deliverable→vault capture failed (non-fatal)",
                    verb, run_id, exc_info=True,
                )

    audit_mirror.write_parent_completion(
        verb=verb, run_id=run_id, audit_dir=runs_dir, status=status,
        workspace_type=workspace.type, workspace_name=workspace.name,
        sensitivity=sensitivity, duration_ms=duration_ms,
        result=result, error=error,
    )
    if result and isinstance(result.get("roles_log"), list):
        audit_mirror.write_role_rows(
            verb=verb, run_id=run_id, audit_dir=runs_dir,
            roles_log=result["roles_log"],
        )

    _push_event(run_id, "crew_completed", {
        "status": status,
        "summary": (result or {}).get("summary"),
        "error": error,
        "duration_ms": duration_ms,
    })
    # TTL backstop — if no SSE client ever consumes the terminal event, the
    # queue must still go away (codex-5.5 xhigh SEV-3).
    reaper = threading.Timer(EVENT_QUEUE_TTL_S, _drop_event_queue, args=(run_id,))
    reaper.daemon = True
    reaper.start()


# ────────────────────────────────────────────────────────────────────────────
# POST /api/crew/runs/{run_id}/cancel
# ────────────────────────────────────────────────────────────────────────────


@router.post("/crew/runs/{run_id}/cancel", response_model=CrewCancelResponse)
def cancel_crew_run(run_id: str) -> CrewCancelResponse:
    """Terminate the crew subprocess via its registered Popen HANDLE.
    Idempotent — re-cancelling a finished run returns ``already_terminated``,
    not a 500.

    F-40 (CX A-05): never kill by raw PID — between the child's exit and the
    worker's registry pop the OS can recycle the number onto an unrelated
    process. The handle is pinned to the specific child, so ``terminate()``
    cannot cross-kill; an already-exited child reports ``poll() is not None``
    and is reported as already terminated instead of signalled."""
    proc = pid_store.get(run_id)
    if proc is None or proc.poll() is not None:
        return CrewCancelResponse(run_id=run_id, status="already_terminated")
    pid_store.mark_cancelled(run_id)
    try:
        # Windows: Popen.terminate() == TerminateProcess on the pinned
        # handle (immediate, unconditional). POSIX: SIGTERM; the worker
        # thread's proxy cleanup reaps the child either way.
        proc.terminate()
    except OSError as e:
        log.info("cancel %s: terminate(pid=%s) no-op (%s)", run_id, getattr(proc, "pid", None), e)
    audit.write_structured(
        actor={"type": "user", "id": "operator"},
        entity_type="crew_run",
        entity_id=run_id,
        action="cancel",
        routine="crew.cancel",
        run_id=run_id,
        status="cancelled",
        audit_dir=RUNS_DIR,
        inputs={"pid": getattr(proc, "pid", None)},
    )
    return CrewCancelResponse(run_id=run_id, status="cancelled")


# ────────────────────────────────────────────────────────────────────────────
# GET /api/crew/runs/{run_id}
# ────────────────────────────────────────────────────────────────────────────


def _read_rows(path: Path, run_id: str) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    out: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("run_id") == run_id:
                out.append(rec)
    return out


_TERMINAL = ("ok", "error", "cancelled", "timeout", "refused")


@router.get("/crew/runs/{run_id}", response_model=CrewRunRecord)
def get_crew_run(run_id: str, verb: str = Query(...)) -> CrewRunRecord:
    """Assemble the parent + role audit rows into one record. Caller passes
    ``verb`` so we read exactly one JSONL pair (no globbing).

    ``verb`` is validated against the registry BEFORE touching the filesystem
    (codex-5.5 SEV-2): a raw query param in a filename would let
    ``?verb=../../...`` read outside the audit dir."""
    try:
        registry.get_manifest(verb)
    except registry.CrewRegistryError:
        raise HTTPException(404, f"crew verb {verb!r} not registered") from None
    parents = _read_rows(RUNS_DIR / f"crew.{verb}.jsonl", run_id)
    if not parents:
        raise HTTPException(
            404, f"no audit row for crew={verb!r} run_id={run_id!r}"
        )
    started = parents[0]
    final = next(
        (r for r in reversed(parents) if r.get("status") in _TERMINAL), None,
    )
    current = final or parents[-1]
    roles = _read_rows(RUNS_DIR / f"crew.{verb}.roles.jsonl", run_id)
    outputs = current.get("outputs") or {}
    inputs = started.get("inputs") or {}

    return CrewRunRecord(
        run_id=run_id,
        verb=verb,
        status=str(current.get("status", "running")),
        workspace=CrewWorkspace(
            type=inputs.get("workspace_type", "general"),
            name=inputs.get("workspace_name", ""),
        ),
        sensitivity=str(inputs.get("sensitivity", "unknown")),
        duration_ms=current.get("duration_ms"),
        token_count=int(outputs.get("token_count") or 0),
        summary=outputs.get("summary"),
        artefacts=list(outputs.get("artefacts") or []),
        roles_log=[
            RoleLogEntry(
                role=str((r.get("inputs") or {}).get("role") or "?"),
                action=str((r.get("inputs") or {}).get("action") or "?"),
                # Crew-stamped role start time; the row ts (audit write time)
                # is only the fallback (codex-5.5 SEV-3).
                ts_start=str((r.get("inputs") or {}).get("ts_start")
                             or r.get("ts") or ""),
                duration_ms=int(r.get("duration_ms") or 0),
                token_count=int(((r.get("outputs") or {}).get("token_count")) or 0),
                sensitivity=str((r.get("inputs") or {}).get("sensitivity") or "unknown"),
                status="error" if r.get("status") == "error" else "ok",
                output_summary=str(((r.get("outputs") or {}).get("output_summary")) or ""),
            )
            for r in roles
        ],
        error=current.get("error"),
        started_at=str(started.get("ts") or ""),
        completed_at=str(final.get("ts")) if final else None,
    )


# ────────────────────────────────────────────────────────────────────────────
# GET /api/crew/runs/{run_id}/events — SSE
# ────────────────────────────────────────────────────────────────────────────


@router.get("/crew/runs/{run_id}/events")
async def crew_run_events(run_id: str) -> StreamingResponse:
    """SSE channel: ``human_input_required`` asks + the ``crew_completed``
    terminal event. Same surface shape as the composite events channel."""

    async def event_gen() -> AsyncGenerator[str, None]:
        q = _get_or_create_event_queue(run_id)
        try:
            while True:
                try:
                    evt = await asyncio.to_thread(q.get, True, 30.0)
                except Empty:
                    yield ": keepalive\n\n"
                    continue
                yield f"event: {evt['event']}\ndata: {json.dumps(evt['data'])}\n\n"
                if evt["event"] == "crew_completed":
                    break
        finally:
            _drop_event_queue(run_id)

    return StreamingResponse(event_gen(), media_type="text/event-stream")


# ────────────────────────────────────────────────────────────────────────────
# POST /api/crew/runs/{run_id}/human-input
# ────────────────────────────────────────────────────────────────────────────


@router.post("/crew/runs/{run_id}/human-input")
def post_human_input(run_id: str, req: CrewHumanInputRequest) -> dict[str, Any]:
    """Operator's reply to a mid-crew HumanProvider ask. The crew worker
    thread (``_await_human_input``) is blocked on this queue.

    Only PENDING asks are accepted (codex-5.5 SEV-2): a reply for an unknown
    ``run_id``/``msg_id`` is 404, never a silently-leaked queue."""
    key = (run_id, req.msg_id)
    with _human_reply_lock:
        q = _human_reply_q.get(key)
    if q is None:
        raise HTTPException(
            404,
            f"no pending human-input ask for run {run_id!r} msg {req.msg_id!r}",
        )
    q.put(req.response)
    return {"ok": True, "run_id": run_id, "msg_id": req.msg_id}


# ────────────────────────────────────────────────────────────────────────────
# GET /api/crew/manifest
# ────────────────────────────────────────────────────────────────────────────


@router.get("/crew/manifest", response_model=CrewManifestResponse)
def list_crew_manifest() -> CrewManifestResponse:
    """All registered crews + declared sensitivity + cost caps + roles —
    surface for the dashboard taxonomy tab and the Cmd-K completer."""
    return CrewManifestResponse(crews=registry.list_manifests())
