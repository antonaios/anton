"""#63 — ``@anton_skill`` + ``SkillCommand``: the skill governance wrapper.

ONE decorator every skill route plugs into so the author writes just the
business logic, and the cross-cutting controls are applied consistently +
automatically — instead of ~13 routes each hand-rolling the same boilerplate
(``load_skill_metadata`` → ``tool_call_hooks(...)`` → ``ctx.result = …`` → #76
capture → ``SkillScopeRefused`` → 403).

What it ABSORBS (today's per-route boilerplate)::

    @anton_skill("recall-query")
    def run_workflow_recall_query(req: RecallQueryRequest) -> RecallQueryResult:
        ...                         # just the analysis
        return result               # plain result OR SkillCommand

What it COMPOSES with (gets free — does NOT re-implement): setting up
``tool_call_hooks`` correctly fires the already-registered hooks — sensitivity
gate (#61), readiness preconditions (#74.2), workspace-write policy, per-tool
sub-caps (#74.5), audit start/complete (#60). The wrapper does context setup +
result/error normalisation, NOT governance logic.

Design (approved 2026-06-06, see session-briefs/SESSION-63-…):
  * **Sync** — ANTON's bridge is sync end-to-end; this wraps a sync fn.
  * **Registry is the source of truth** (Option A): ``@anton_skill(name)`` reads
    sensitivity / workspace_scope / cost ceilings / captures from SKILL.md via
    ``load_skill_metadata`` — never re-declares them inline (no drift).
  * **run_id correlation (#59):** reuses ``current_run_id()`` so a retry
    coalesces + (phase 2) suspend/resume correlates.
  * **Error taxonomy:** ``SkillScopeRefused`` → 403; an optional per-skill
    ``error_map`` (e.g. ``{EngineTimeout: 504}``); everything else PROPAGATES
    (``SkillPreconditionsNotMet`` → app handler 409; validation → 422; a genuine
    bug → 500) — never silently swallowed (the @safe_audit boundary: audit may
    fail quietly; the SKILL must not).
  * **Testability:** the undecorated fn is exposed as ``__wrapped_skill__``.

Still ADDITIVE: no skill is migrated to the wrapper yet (recall-query is the
phase-4 pilot). ``SkillCommand.goto`` / ``emit`` are parsed + stashed for the
future multi-step orchestrator but not acted on yet (infra-ahead-of-consumers).

Phase 2b adds cooperative SUSPEND/RESUME: a body raises :class:`SkillSuspended`
to pause and ask the operator a question; the wrapper persists a sanitized
checkpoint (``run_id``-keyed, see :mod:`routines.skills._runtime.suspensions`),
emits ``SkillInvocationSuspended``, and returns a 202 awaiting-payload. A later
``POST /api/skills/{run_id}/resume`` reloads the checkpoint and re-invokes the
body via :func:`resume_skill` — the substrate #65's ``OperatorProvider`` needs.
"""

from __future__ import annotations

import functools
import inspect
import json
import logging
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from fastapi import HTTPException
from fastapi.responses import JSONResponse

from routines.hooks.central_guards import (
    SkillPreconditionsNotMet,
    SkillScopeRefused,
    _strictest,
)
from routines.hooks.event_bus import bridge_event_bus
from routines.hooks.events import (
    SkillInvocationCompleted,
    SkillInvocationFailed,
    SkillInvocationRefused,
    SkillInvocationResumed,
    SkillInvocationStarted,
    SkillInvocationSuspended,
)
from routines.hooks.tool_dispatch import tool_call_hooks
from routines.shared.audit import hash_dict, sanitize_record
from routines.skills._runtime.capture import emit_deliverable_proposal
from routines.skills._runtime.guardrails import record_output_guardrails
from routines.skills._runtime.llm_call_counter import bind_run_id, current_run_id
from routines.skills._runtime.llm_gateway import (
    SkillLLMContext,
    reset_skill_llm_context,
    set_skill_llm_context,
)
from routines.skills._runtime.run_dedup import (
    RunInFlight,
    abandon_run,
    begin_run,
    complete_run,
)
from routines.skills._runtime.suspensions import (
    Suspension,
    expiry_from,
    get_suspension_store,
    now_iso,
)
from routines.skills.registry import load_skill_metadata

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# SkillCommand — the optional richer return for multi-step skills
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class SkillCommand:
    """Structured return for a skill (#63).

    A skill may return its **plain result** (back-compat — the wrapper boxes it)
    OR a ``SkillCommand`` for multi-step control. In phase 1 the wrapper always
    returns ``result`` to the route (FastAPI serialises it via ``response_model``)
    and records a COMPACT telemetry breadcrumb of the multi-step intent on the
    audit context — ``goto``, the ``emit`` count, and the ``update`` KEYS (not
    values, to keep the audit row clean + secret-free). The orchestrator that
    actually ACTS on ``goto`` / ``update`` / ``emit`` (#64 reducers + composites)
    is future and will consume the live ``SkillCommand`` return value, not this
    breadcrumb.

      * ``result`` — the user-facing output (a Pydantic model or dict).
      * ``update`` — a state delta for multi-step skills (merged via #64).
      * ``goto`` — the next step(s); ``None`` means "done".
      * ``emit`` — chat events / proposals to surface.
    """

    result: Any = None
    update: Optional[dict] = None
    goto: Optional[Any] = None  # str | list[str] | None
    emit: list[dict] = field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────────
# SkillSuspended — cooperative human-input interrupt (#63 phase 2b)
# ─────────────────────────────────────────────────────────────────────────────


class SkillSuspended(Exception):
    """Raised by a skill body to PAUSE and ask the operator a question.

    The wrapper catches it, persists a SANITIZED checkpoint (``state``) keyed by
    the run's ``run_id`` (see :mod:`routines.skills._runtime.suspensions`),
    emits ``SkillInvocationSuspended``, and returns a ``202`` "awaiting" payload
    carrying the resume URL. A later ``POST /api/skills/{run_id}/resume`` reloads
    the checkpoint and re-invokes the body. This is the substrate #65's
    ``OperatorProvider`` builds on.

    **The resume contract** (cooperative — the skill carries its own
    continuation): on the resume run the body MUST branch on
    :func:`current_resume` and continue using ONLY ``ResumeContext.state`` (the
    checkpoint) + ``ResumeContext.input`` (the operator's answer) — the original
    request is ``None`` on resume. Anything the skill needs to continue belongs
    in ``state``. Because ``state`` is sanitized before it lands, do NOT stash a
    secret there (a paused skill needing a live secret to continue is a design
    smell — re-acquire it on resume).

    Args:
      * ``prompt`` — the operator-facing question.
      * ``state`` — the continuation checkpoint (JSON-able; sanitized on persist).
      * ``options`` — optional list of canned answers for the UI.
      * ``timeout_s`` — TTL; the suspension can't resume after it (default 600).
    """

    def __init__(
        self,
        prompt: str,
        *,
        state: Optional[dict] = None,
        options: Optional[list] = None,
        timeout_s: int = 600,
    ) -> None:
        super().__init__(prompt)
        self.prompt = prompt
        self.state: dict = state if state is not None else {}
        self.options = options
        self.timeout_s = timeout_s


class SkillResumeNotReady(Exception):
    """Raised by :func:`resume_skill` when a resume was CLAIMED but the body was
    never admitted — a readiness precondition (#74.2) lapsed between suspend and
    resume, so the central guard refused in ``tool_call_hooks.__enter__`` BEFORE
    the body ran. The resume route catches this to roll the claim back to
    pending (retryable) — distinct from a precondition failure raised by the body
    AFTER admission (which is a real run failure and must NOT roll back, or a
    retry would double-run side effects). (codex-5.5 R2 SEV-1.)"""


@dataclass
class ResumeContext:
    """Handed to a resuming skill via :func:`current_resume`. ``state`` is the
    sanitized checkpoint the skill stashed on ``SkillSuspended``; ``input`` is
    the operator's answer from the resume request body."""

    run_id: str
    state: dict
    input: Any = None


# The resume payload for the CURRENT body invocation. ``None`` on a first call
# (the skill runs from the top); a ``ResumeContext`` on a resume re-invocation.
# A ContextVar (not threading.local) so it survives the async/threadpool
# boundary the same way ``current_run_id`` does.
_resume_var: ContextVar[Optional[ResumeContext]] = ContextVar(
    "anton_skill_resume", default=None
)


def current_resume() -> Optional[ResumeContext]:
    """The resume context for the body running right now, or ``None`` on a
    first (non-resume) call. A suspending skill branches on this to continue
    from its checkpoint instead of re-running from the top."""
    return _resume_var.get()


# ─────────────────────────────────────────────────────────────────────────────
# Decorated-skill registry (for the optional boot fail-fast + resume re-invoke)
# ─────────────────────────────────────────────────────────────────────────────


# name -> the qualname that claimed it (so a duplicate claim by a DIFFERENT
# function — likely a copy-paste bug — is detectable). Import-order dependent by
# nature (only sees decorators already imported), so the boot validator must run
# AFTER the routers are imported (wired in phase 3).
_ANTON_SKILLS: dict[str, str] = {}
_ANTON_SKILL_DUPLICATES: list[str] = []


@dataclass
class _SkillEntry:
    """What the resume path needs to re-invoke a suspended skill's body:
    the undecorated ``fn`` plus the governance knobs the wrapper applies."""

    name: str
    fn: Callable
    error_map: dict
    capture: bool
    workspace_override: Optional[Callable]


# name -> entry. Populated at decoration; the resume endpoint looks a skill up
# here by the name persisted on its suspension row.
_ANTON_SKILL_ENTRIES: dict[str, _SkillEntry] = {}


def _claim_skill_name(name: str, fn: Callable) -> None:
    qn = f"{getattr(fn, '__module__', '?')}.{getattr(fn, '__qualname__', repr(fn))}"
    prior = _ANTON_SKILLS.get(name)
    if prior is not None and prior != qn:
        _ANTON_SKILL_DUPLICATES.append(f"{name!r}: {prior} vs {qn}")
    _ANTON_SKILLS[name] = qn


def registered_anton_skill_names() -> frozenset[str]:
    """Names that a ``@anton_skill`` decorator has claimed this process."""
    return frozenset(_ANTON_SKILLS)


def get_skill_entry(name: str) -> Optional[_SkillEntry]:
    """The registered entry for ``name`` (or None) — used by the resume route to
    re-invoke the right body."""
    return _ANTON_SKILL_ENTRIES.get(name)


def validate_anton_skills() -> list[str]:
    """Return errors for any ``@anton_skill`` name not present in the SKILL.md
    registry (the decorator reads its governance from there — a name with no
    SKILL.md is a misconfiguration) OR claimed by two distinct functions. Empty =
    all resolve. Callable at boot for a fail-fast check once skills are migrated
    (no consumers in phase 1 → trivially empty). Run AFTER router import."""
    errors: list[str] = []
    for name in sorted(_ANTON_SKILLS):
        try:
            load_skill_metadata(name)
        except KeyError:
            errors.append(
                f"@anton_skill({name!r}) has no matching SKILL.md registration"
            )
    for dup in _ANTON_SKILL_DUPLICATES:
        errors.append(f"@anton_skill duplicate name claim — {dup}")
    return errors


def _reset_anton_skills_for_tests() -> None:
    """Clear the decorated-skill bookkeeping (test isolation only)."""
    _ANTON_SKILLS.clear()
    _ANTON_SKILL_DUPLICATES.clear()
    _ANTON_SKILL_ENTRIES.clear()


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _first_request(args: tuple, kwargs: dict) -> Any:
    """Find the skill's request model among the call args. Prefer a value that
    looks like a request (Pydantic model or carries the workspace convention);
    fall back to the first positional / kwarg."""
    for v in list(args) + list(kwargs.values()):
        if hasattr(v, "model_dump") or hasattr(v, "workspace_type"):
            return v
    if args:
        return args[0]
    if kwargs:
        return next(iter(kwargs.values()))
    return None


def _extract_workspace(
    inp: Any, override: Optional[Callable[[Any], tuple[str, str, str]]],
) -> tuple[str, str, str]:
    """(workspace_type, workspace_name, sensitivity) for the call. Reads the
    conventional request fields (``workspace_type`` / ``workspace_name`` /
    ``workspace_sensitivity``) with fail-safe defaults, or a caller override."""
    if override is not None:
        return override(inp)
    wt = getattr(inp, "workspace_type", None) or "general"
    wn = getattr(inp, "workspace_name", None) or "default"
    ws = getattr(inp, "workspace_sensitivity", None) or "public"
    return str(wt), str(wn), str(ws)


def _tool_input(inp: Any) -> dict:
    """Best-effort dict of the request for the audit row. The audit pipeline
    sanitises this downstream (sanitize_record / #audit-sanitize-coverage)."""
    dump = getattr(inp, "model_dump", None)
    if callable(dump):
        try:
            return dump(exclude_none=True)
        except Exception:  # noqa: BLE001 — never block the call on audit shaping
            return {}
    return inp if isinstance(inp, dict) else {}


def _result_dict(result: Any) -> Any:
    """Dict form of the result for ``ctx.result`` (after-hook audit summary)."""
    dump = getattr(result, "model_dump", None)
    if callable(dump):
        try:
            return dump()
        except Exception:  # noqa: BLE001
            pass
    if isinstance(result, dict):
        return result
    return {"result": result}


def _record_skill_command(ctx: Any, cmd: "SkillCommand") -> None:
    """Record a COMPACT, non-sensitive breadcrumb of a multi-step command on the
    audit context (phase 1 doesn't act on it — see ``SkillCommand`` docstring).
    Only ``update`` KEYS are recorded, never values."""
    if cmd.goto is None and cmd.update is None and not cmd.emit:
        return
    ctx.usage["skill_command"] = {
        "goto": cmd.goto,
        "emit": len(cmd.emit),
        "update_keys": sorted(cmd.update.keys()) if isinstance(cmd.update, dict) else None,
    }


def _safe_hash(payload: Any) -> str:
    """Best-effort stable hash of the input for the lifecycle event (never
    raises — observability must not break the skill)."""
    try:
        return hash_dict(payload) if isinstance(payload, dict) else ""
    except Exception:  # noqa: BLE001
        return ""


def _emit(event_cls: type, **fields: Any) -> None:
    """Construct AND fire a lifecycle event on the bridge bus — fully
    best-effort. Construction happens INSIDE the guard (codex 2a SEV-3) so a bad
    field / model-validation error can't break the skill either; the bus also
    swallows handler errors. L1 — the wrapper is the publisher that was missing;
    subscribers in central_guards record the lifecycle to the audit feed."""
    try:
        bridge_event_bus.emit(event_cls(**fields))
    except Exception as e:  # noqa: BLE001 — observability never breaks the skill
        logger.warning("anton_skill: lifecycle event emit failed (non-fatal): %s", e)


def _run_capture(name: str, meta: Any, result_dict: Any) -> None:
    """#76 deliverable capture — best-effort (NEVER fails the skill). Mirrors the
    per-route capture call; the wrapper centralises it so every capture-declaring
    skill gets it uniformly."""
    try:
        from routines.api.deps import VAULT  # lazy: avoid import at module load
        emit_deliverable_proposal(
            name, meta.captures_to_vault, result_dict,
            vault_root=VAULT, sensitivity=meta.sensitivity,
        )
    except Exception as e:  # noqa: BLE001 — capture is non-critical
        logger.warning("anton_skill(%s): #76 capture failed (non-fatal): %s", name, e)


# ─────────────────────────────────────────────────────────────────────────────
# Suspend / resume (#63 phase 2b)
# ─────────────────────────────────────────────────────────────────────────────


def _sanitize_state(state: Any) -> dict:
    """B6 — run the persisted checkpoint through the audit sanitizer so a paused
    skill never parks a secret in cleartext. Reuses ``sanitize_record`` (which
    sanitizes the ``details`` sub-tree: API-key patterns, field-name-aware
    redaction, base64-blob truncation — recursively). Fail-safe: on any sanitize
    error, persist an EMPTY state (B6 wins over resume-fidelity — never persist
    an unscrubbed blob)."""
    payload = state if isinstance(state, dict) else {"value": state}
    try:
        return sanitize_record({"details": payload})["details"]
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "anton_skill: suspended-state sanitize failed; persisting empty "
            "state (resume will see {}): %s", e,
        )
        return {}


def _json_safe(value: Any) -> Any:
    """Coerce to a JSON-serialisable value via a round-trip (``default=str`` for
    stragglers). Used so the SAME normalised ``options`` are persisted AND
    returned in the 202 reply — a non-JSON option can't persist a suspension +
    emit Suspended and THEN blow up building the JSONResponse (codex-5.5
    SEV-2)."""
    if value is None:
        return None
    try:
        return json.loads(json.dumps(value, default=str))
    except Exception:  # noqa: BLE001
        return str(value)


def _handle_suspend(
    ctx: Any, name: str, sens: str, s: "SkillSuspended", t0: float,
) -> Suspension:
    """Persist a cooperative suspension (B2 — a pending row a resume claims once)
    + stamp the tool-side audit status + emit the lifecycle event. Returns the
    persisted :class:`Suspension` (with its minted ``resume_token``) so the
    caller can build the 202 reply."""
    created = now_iso()
    expires = expiry_from(created, s.timeout_s)
    options = _json_safe(list(s.options)) if s.options is not None else None
    susp = Suspension(
        run_id=ctx.run_id,
        skill=name,
        prompt=str(s.prompt),
        state=_sanitize_state(s.state),
        options=options,
        workspace_type=ctx.workspace.type,
        workspace_name=ctx.workspace.name,
        sensitivity=str(sens),
        created=created,
        expires_at=expires,
    )
    get_suspension_store().put(susp)  # mints susp.resume_token
    # A suspend is a clean cooperative pause, NOT an error — stamp the tool-side
    # audit row 'suspended' (setdefault in tool_call_hooks won't override it).
    ctx.usage["status"] = "suspended"
    _emit(
        SkillInvocationSuspended,
        run_id=ctx.run_id, skill=name,
        workspace_type=ctx.workspace.type, workspace_name=ctx.workspace.name,
        sensitivity=sens, prompt=str(s.prompt)[:500], expires_at=expires,
        duration_ms=int((time.monotonic() - t0) * 1000),
    )
    return susp


def _awaiting_response(susp: Suspension) -> JSONResponse:
    """The 202 "awaiting your input" reply. Returned as a ``JSONResponse`` so
    FastAPI passes it through UNTOUCHED — bypassing the route's ``response_model``
    (the happy-path result shape), which a suspend deliberately doesn't match."""
    return JSONResponse(
        status_code=202,
        content={
            "status": "suspended",
            "run_id": susp.run_id,
            "skill": susp.skill,
            "prompt": susp.prompt,
            "options": susp.options,
            # the client MUST echo this on resume — it's the anti-ABA claim key.
            "resume_token": susp.resume_token,
            "expires_at": susp.expires_at,
            "resume_url": f"/api/skills/{susp.run_id}/resume",
        },
    )


# ─────────────────────────────────────────────────────────────────────────────
# The decorator
# ─────────────────────────────────────────────────────────────────────────────


# Bare broad/catch-all exception types an error_map must NOT map (mapping them
# would mask a genuine coding bug as an "expected" HTTP error). A custom domain
# subclass of any of these is still allowed — only the BARE types are forbidden.
_FORBIDDEN_ERROR_MAP_TYPES: frozenset[type] = frozenset({
    Exception, ValueError, TypeError, KeyError, IndexError, LookupError,
    RuntimeError, ArithmeticError, AttributeError, OSError, AssertionError,
    NotImplementedError, StopIteration,
})


def anton_skill(
    name: str,
    *,
    capture: bool = True,
    error_map: Optional[dict[type, int]] = None,
    workspace: Optional[Callable[[Any], tuple[str, str, str]]] = None,
) -> Callable[[Callable], Callable]:
    """Wrap a skill route handler with the standard governance jacket (#63).

    Args:
      * ``name`` — the SKILL.md name; governance (sensitivity / workspace_scope /
        cost ceilings / captures) is read from the registry by this name.
      * ``capture`` — run the #76 deliverable capture when the skill declares
        ``captures_to_vault`` (default True).
      * ``error_map`` — optional ``{ExceptionType: http_status}`` for
        skill-specific errors (e.g. ``{EngineTimeout: 504}``). ``SkillScopeRefused``
        (→403) is handled universally; everything not mapped PROPAGATES.
      * ``workspace`` — optional ``inp -> (type, name, sensitivity)`` override;
        defaults to reading the conventional request fields.

    The decorated fn may return its plain result or a :class:`SkillCommand`.
    The undecorated fn is preserved at ``__wrapped_skill__`` for unit tests.

    **Decorator order:** ``@router.post(...)`` must wrap the ``@anton_skill``
    result (``@router.post(...)`` ABOVE ``@anton_skill`` in source), so the route
    registers the governed wrapper — not the bare function.
    """
    # Foot-gun guard: error_map must only carry SKILL-SPECIFIC exception types.
    # A broad built-in (ValueError/KeyError/RuntimeError/…) or governance type
    # would let the map reclassify a genuine CODING BUG as an "expected" HTTP
    # error (masking it) — reject those at decoration time. A custom domain
    # exception that SUBCLASSES a built-in is fine (only the bare broad types are
    # forbidden). (codex-5.5 R1 + arch-review SEV-2.)
    for exc_type in (error_map or {}):
        if (
            not isinstance(exc_type, type)
            or not issubclass(exc_type, Exception)            # must be a catchable Exception
            or exc_type in _FORBIDDEN_ERROR_MAP_TYPES          # bare broad/catch-all built-ins
            or issubclass(exc_type, (SkillScopeRefused, HTTPException))  # governance/HTTP incl. subclasses
        ):
            raise ValueError(
                f"anton_skill({name!r}): error_map key {exc_type!r} is not allowed — "
                f"map only SPECIFIC skill-domain exceptions, never a bare broad "
                f"built-in (ValueError/RuntimeError/…), Exception/BaseException, or a "
                f"governance type (a custom subclass of a built-in is fine)"
            )

    def decorate(fn: Callable) -> Callable:
        _claim_skill_name(name, fn)
        _ANTON_SKILL_ENTRIES[name] = _SkillEntry(
            name=name, fn=fn, error_map=error_map or {}, capture=capture,
            workspace_override=workspace,
        )

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            # First (non-resume) call: governance context is extracted from the
            # request INSIDE _governed (so a workspace-override that raises
            # SkillScopeRefused still maps to 403 — codex-5.5 SEV-2).
            inp = _first_request(args, kwargs)
            rid = current_run_id()  # #59 — the request-boundary id (reuse, don't mint)
            if rid is None:
                # No stable request id (no #59 middleware / a direct unit-test
                # call) → no dedup window; run plainly.
                return _governed(
                    name, fn, args, kwargs,
                    inp=inp, workspace_override=workspace, gov_override=None,
                    hooks_run_id=None, error_map=error_map, capture=capture, resume=None,
                )
            # L2 idempotency: coalesce a retried run_id so a network retry can't
            # double-fire side effects (duplicate proposals / double budget
            # charge). inflight dup → 409; completed dup → REPLAY a fresh
            # reconstruction of the first outcome; failure → abandon so a
            # transient error stays retryable. The owner token guards against a
            # stale-reclaimed attempt completing/abandoning the wrong run.
            try:
                begin = begin_run(rid)
            except RunInFlight as e:
                raise HTTPException(status_code=409, detail=str(e)) from e
            if not begin.proceed:
                return begin.replay  # idempotent replay of the original outcome
            token = begin.token
            try:
                result = _governed(
                    name, fn, args, kwargs,
                    inp=inp, workspace_override=workspace, gov_override=None,
                    hooks_run_id=rid, error_map=error_map, capture=capture, resume=None,
                )
            except BaseException:
                abandon_run(rid, token)  # body raised → drop the entry (retryable)
                raise
            complete_run(rid, token, result, is_response=isinstance(result, JSONResponse))
            return result

        wrapper.__anton_skill_name__ = name  # type: ignore[attr-defined]
        wrapper.__wrapped_skill__ = fn       # type: ignore[attr-defined]
        # CRITICAL for FastAPI: the wrapper's real signature is (*args, **kwargs),
        # so FastAPI can't recover the route's body model from it. Pin
        # __signature__ to the original fn — with annotations RESOLVED to real
        # types (eval_str=True). The route modules use `from __future__ import
        # annotations`, so the annotations are STRINGS; FastAPI would resolve them
        # against the WRAPPER's __globals__ (this module), where the route's model
        # isn't defined → it mis-classifies the body model as a query param (422).
        # Resolving here via the skill fn's own globals fixes it. (codex-5.5 R1
        # SEV-3 — the TestClient route test caught this.)
        try:
            wrapper.__signature__ = inspect.signature(fn, eval_str=True)  # type: ignore[attr-defined]
        except (ValueError, TypeError, NameError):
            try:
                wrapper.__signature__ = inspect.signature(fn)  # type: ignore[attr-defined]
            except (ValueError, TypeError):  # builtins / unintrospectable — leave as-is
                pass
        return wrapper

    return decorate


# ─────────────────────────────────────────────────────────────────────────────
# The governed run — shared by the first call AND the resume re-invocation
# ─────────────────────────────────────────────────────────────────────────────


def _governed(
    name: str,
    fn: Callable,
    args: tuple,
    kwargs: dict,
    *,
    inp: Any,
    workspace_override: Optional[Callable[[Any], tuple[str, str, str]]],
    gov_override: Optional[tuple[str, str, str]],
    hooks_run_id: Optional[str],
    error_map: Optional[dict[type, int]],
    capture: bool,
    resume: Optional[ResumeContext],
) -> Any:
    """Run ``fn`` inside the governance jacket. ONE code path for the first call
    (``resume is None``) and a resume re-invocation (``resume`` set). A first
    call derives the governance context from the request (``inp`` +
    ``workspace_override``) and reuses ``current_run_id()``; a resume passes the
    PERSISTED ``gov_override`` (workspace_type, workspace_name, sensitivity) +
    the suspended ``run_id``.

    Four terminal exits, each leaving exactly one lifecycle row for the segment:
    Completed · Failed · Suspended (the cooperative pause) · plus a PRE-admission
    governance refusal in ``__enter__`` (the body never ran) — which emits a
    ``SkillInvocationRefused`` event (#anton-skill-refusal-audit) so the
    #no-mnpi-to-cloud gate (was cited as §5.4) is auditable even though no
    Started/Completed/Failed segment exists."""
    # Defaults so the perimeter ``except SkillScopeRefused`` can emit a Refused
    # event even when the refusal is raised DURING governance setup (e.g. a
    # workspace-override that raises before _extract_workspace returns), where the
    # real values below are not yet bound. ``admitted`` stays False until
    # __enter__ succeeds, distinguishing a perimeter refusal from a body failure.
    admitted = False
    ws_type, ws_name, req_sens, sens = "general", "default", "?", "?"
    try:
        meta = load_skill_metadata(name)
        # Workspace extraction is INSIDE the try so a workspace-override that
        # raises SkillScopeRefused maps to 403 (codex-5.5 SEV-2). On resume the
        # context is the persisted gov (the request is None).
        if resume is None:
            ws_type, ws_name, req_sens = _extract_workspace(inp, workspace_override)
        else:
            ws_type, ws_name, req_sens = gov_override  # type: ignore[misc]
        # Fail-closed: STRICTEST of the request/persisted tier and the skill's
        # declared floor (registry). A request can only TIGHTEN (codex-5.5 R1
        # SEV-1). Idempotent on resume (the persisted tier is already strictest).
        sens = _strictest(req_sens, meta.sensitivity)  # type: ignore[arg-type]
        # ``admitted`` flips True the instant tool_call_hooks.__enter__ succeeds
        # (before-hooks passed). It separates a PRE-admission governance refusal
        # (raised in __enter__ — the body never ran) from a POST-admission
        # failure (the body ran). On resume that distinction decides whether the
        # claim is safely rolled back (codex-5.5 R2 SEV-1).
        admitted = False
        with tool_call_hooks(
            tool_name=name,
            workspace_type=ws_type,        # type: ignore[arg-type]
            workspace_name=ws_name,
            sensitivity=sens,
            tool_input=_tool_input(inp),
            skill_metadata=meta.to_hook_metadata(),  # ONE source — no 4-field drift (codex SEV-3)
            run_id=hooks_run_id,
        ) as ctx:
            admitted = True
            # L1 lifecycle: the skill is ADMITTED (before-hooks passed). A
            # governance REFUSAL raises in __enter__ above — so the lifecycle
            # events fire ONLY for admitted runs. A first call emits Started; a
            # resume emits Resumed (the prior Suspended already closed the first
            # segment).
            _t0 = time.monotonic()
            _terminal = False  # exactly one Completed|Failed|Suspended per segment
            if resume is None:
                _emit(
                    SkillInvocationStarted,
                    run_id=ctx.run_id, skill=name,
                    workspace_type=ctx.workspace.type, workspace_name=ctx.workspace.name,
                    sensitivity=sens, inputs_hash=_safe_hash(ctx.tool_input),
                )
            else:
                _emit(
                    SkillInvocationResumed,
                    run_id=ctx.run_id, skill=name,
                    workspace_type=ctx.workspace.type, workspace_name=ctx.workspace.name,
                    sensitivity=sens,
                )
            try:
                # Expose the resume payload + the skill-LLM context to the body
                # for the duration of the call (L3: llm() reads the latter to
                # route through the #no-mnpi-to-cloud (was cited as
                # §5.4)/budget/cap/Tier-2 gateway with THIS
                # skill's resolved sensitivity). Reset both on exit so a
                # threadpool worker doesn't leak them into the next request.
                _rtoken = _resume_var.set(resume)
                _lltoken = set_skill_llm_context(SkillLLMContext(
                    skill=name, run_id=ctx.run_id, sensitivity=str(sens),
                    workspace_type=ctx.workspace.type, workspace_name=ctx.workspace.name,
                    cost_cap_tokens=getattr(meta, "cost_ceiling_tokens", None),
                    # Per-skill default system prompt (#llm-skill-system-prompt);
                    # None when the SKILL.md declares none → llm() uses the
                    # platform persona.
                    system_prompt=getattr(meta, "llm_system_prompt", None),
                ))
                try:
                    # error_map is scoped to the SKILL BODY ONLY — never to
                    # governance setup / before-after hooks / wrapper internals
                    # (codex-5.5 R1 SEV-2).
                    try:
                        raw = fn(*args, **kwargs)
                    except SkillSuspended as s:
                        # 4th terminal exit — cooperative pause. Persist the
                        # sanitized checkpoint + emit Suspended + return 202. NOT
                        # a Completed/Failed; the segment ends here.
                        susp = _handle_suspend(ctx, name, sens, s, _t0)
                        _terminal = True
                        return _awaiting_response(susp)
                    except Exception as e:  # noqa: BLE001
                        # Failed carries the REAL error class (before any
                        # error_map remap) (codex 2a).
                        _emit(
                            SkillInvocationFailed,
                            run_id=ctx.run_id, skill=name,
                            workspace_type=ctx.workspace.type, workspace_name=ctx.workspace.name,
                            error_class=type(e).__name__, error=str(e)[:500],
                            duration_ms=int((time.monotonic() - _t0) * 1000),
                        )
                        _terminal = True
                        if isinstance(e, HTTPException):
                            raise  # skill chose its own status (recall-query 503/500)
                        for exc_type, status in (error_map or {}).items():
                            if isinstance(e, exc_type):
                                raise HTTPException(status_code=status, detail=str(e)) from e
                        raise  # genuine bug → propagate (FastAPI → 500), never swallow
                finally:
                    reset_skill_llm_context(_lltoken)
                    _resume_var.reset(_rtoken)
                cmd = raw if isinstance(raw, SkillCommand) else SkillCommand(result=raw)
                ctx.result = _result_dict(cmd.result)
                _record_skill_command(ctx, cmd)
                # #24 output-boundary guardrails: evaluate the skill's declared
                # guardrails against the structured result, stamp the verdicts
                # on ctx.usage (lands on the after-hook audit row) + write a
                # structured guardrail_verdict row. ADVISORY here (the body
                # already ran — a re-run would double side effects; the retry
                # teeth live around llm() in llm_with_guardrails). NEVER raises.
                record_output_guardrails(
                    name, getattr(meta, "guardrails", ()) or (),
                    result=ctx.result, usage=ctx.usage,
                    run_id=ctx.run_id, sensitivity=str(sens),
                )
                _emit(
                    SkillInvocationCompleted,
                    run_id=ctx.run_id, skill=name,
                    workspace_type=ctx.workspace.type, workspace_name=ctx.workspace.name,
                    sensitivity=sens, duration_ms=int((time.monotonic() - _t0) * 1000),
                )
                _terminal = True
                if capture and meta.captures_to_vault is not None:
                    _run_capture(name, meta, ctx.result)
                return cmd.result
            except Exception as e:  # noqa: BLE001 — terminal-event guarantee
                # Catches the re-raised body failure (already terminal) AND a
                # POST-body failure (cmd/_result_dict/_record before Completed).
                # Emit Failed ONCE if not already, so EVERY admitted segment ends
                # with exactly one terminal event (codex 2a SEV-3). Propagate.
                if not _terminal:
                    _emit(
                        SkillInvocationFailed,
                        run_id=ctx.run_id, skill=name,
                        workspace_type=ctx.workspace.type, workspace_name=ctx.workspace.name,
                        error_class=type(e).__name__, error=str(e)[:500],
                        duration_ms=int((time.monotonic() - _t0) * 1000),
                    )
                raise
    except HTTPException:
        # A skill that chose its own HTTP status (recall-query 503/500),
        # or an error_map mapping, passes through untouched.
        raise
    except SkillScopeRefused as e:
        # #anton-skill-refusal-audit — a PRE-admission #no-mnpi-to-cloud (was
        # cited as §5.4) scope/MNPI refusal is
        # caught HERE, outside the ``with tool_call_hooks`` block, so the
        # after-hook audit + the Started/Failed lifecycle never ran. Emit a
        # Refused event so the platform's most safety-critical gate leaves a trail
        # (activity.jsonl), not only the HTTP access log. Guarded by ``not
        # admitted`` so a body-raised SkillScopeRefused (already covered by the
        # Failed event inside the ``with``) is NOT double-counted. Best-effort via
        # _emit — observability never blocks the 403. Carries only workspace +
        # tier metadata + the refusal reason, never the refused request payload.
        if not admitted:
            _emit(
                SkillInvocationRefused,
                run_id=hooks_run_id or "",
                skill=name,
                workspace_type=ws_type,
                workspace_name=ws_name,
                sensitivity=str(sens),
                requested_sensitivity=str(req_sens),
                reason=str(e)[:500],
            )
        # F-32 (HR INV5-G1): a PRE-admission scope/MNPI refusal on a RESUME
        # must not consume the claim — without rollback the suspension is
        # stuck "resumed" forever even after the refusing condition clears.
        # Mirror the preconditions contract below: surface the dedicated
        # retryable signal so the route rolls the claim back to pending →
        # 409. A first call (or a post-admission refusal) keeps the 403.
        if resume is not None and not admitted:
            raise SkillResumeNotReady(
                f"governance refused the resume before admission: {e}"
            ) from e
        raise HTTPException(status_code=403, detail=str(e)) from e
    except SkillPreconditionsNotMet:
        # A readiness precondition refused in __enter__. On a RESUME, if this
        # happened BEFORE admission (the body never ran), surface a dedicated
        # retryable signal so the route rolls the claim back to pending — the
        # operator can retry once the precondition is met. A POST-admission
        # SkillPreconditionsNotMet (raised by the body / a nested guarded call
        # AFTER the run was admitted) is a real run failure: propagate as-is so
        # the route does NOT roll back (a retry would double-run). A first call
        # always propagates (→ app 409 handler), unchanged (codex-5.5 R2 SEV-1).
        if resume is not None and not admitted:
            raise SkillResumeNotReady(
                "skill readiness precondition not met at resume time"
            )
        raise
    # Everything else PROPAGATES: a metadata KeyError, request validation → 422,
    # a genuine bug → 500. error_map is NOT applied here — only around the body.


def resume_skill(
    name: str,
    *,
    run_id: str,
    state: dict,
    gov: tuple[str, str, str],
    operator_input: Any = None,
) -> Any:
    """Re-invoke a suspended skill's body with the operator's answer.

    Called by ``POST /api/skills/{run_id}/resume`` AFTER it has atomically
    claimed the suspension (so this never double-runs). Re-binds the SAME
    ``run_id`` (so per-tool counters + any nested ``current_run_id()`` correlate)
    and runs the body through :func:`_governed` in resume mode: the body branches
    on :func:`current_resume` and continues from ``state`` — the original request
    is ``None`` on resume. Returns the skill result (a completed run) OR another
    202 ``JSONResponse`` (a multi-step skill that suspended again)."""
    entry = _ANTON_SKILL_ENTRIES.get(name)
    if entry is None:
        raise HTTPException(
            status_code=500,
            detail=f"skill {name!r} is not registered with @anton_skill — cannot resume",
        )
    resume_ctx = ResumeContext(run_id=run_id, state=state, input=operator_input)
    with bind_run_id(run_id):
        return _governed(
            entry.name, entry.fn, (None,), {},
            inp=None, workspace_override=None, gov_override=gov, hooks_run_id=run_id,
            error_map=entry.error_map, capture=entry.capture, resume=resume_ctx,
        )


__all__ = [
    "anton_skill",
    "SkillCommand",
    "SkillSuspended",
    "SkillResumeNotReady",
    "ResumeContext",
    "current_resume",
    "resume_skill",
    "get_skill_entry",
    "registered_anton_skill_names",
    "validate_anton_skills",
]
