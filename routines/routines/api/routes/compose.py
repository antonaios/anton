"""Bridge-side ``_compose`` proxy — TRANSFORM substitute for Synapse (#26b).

Promoted 2026-06-09 from ``proposed-2026-05-26-phase6/26b-compose-proxy/``
per STAGING-README §3, adapted to the current substrate (see "Promotion
adaptations" below).

Synapse's TRANSFORM step type (``core/orchestration/steps.py:1087``)
hard-fails without Docker (spike Item 4). Rather than installing Docker
for one feature, every composite shape-transform becomes a bridge HTTP
endpoint that Synapse calls as a regular TOOL step.

Endpoint::

    POST /api/composite/_compose/<key>
    Body: ComposeRequest  → {shared_state_subset: dict, run_id?, composite_key?}
    Returns: ComposeResponse → {result: dict, key, run_id?}

Per-key handlers live under ``routines/composite/compose/<key>.py`` and
register themselves via the ``ComposeRegistry`` singleton at
module-import time (``routines.api.app`` imports the package so the
registrations fire at startup). The route handler looks up the key,
validates the request's ``shared_state_subset`` against the handler's
input model, and returns the handler's output as ``ComposeResponse.result``.

Every call is wrapped in ``tool_call_hooks`` (#22 tool jacket) so:
  * the registered ``@before_tool_call`` guards fire on every compose
    (workspace policy / skill-scope guards pass through — compose keys
    are not registered skills — but any future tool-name-matched guard
    applies automatically);
  * the audit row lands in ``routines/runs/tool.compose_<key>.jsonl``
    via the ``audit_tool_call`` after-hook (plus the #60 structured
    activity stream + SQLite index).

Promotion adaptations (vs the 2026-05-26 staged spike):
  * ``tool_call_hooks`` imported directly from ``routines.hooks`` — the
    staged try/except fallback shim is gone.
  * **#59 run-id correlation:** the hook context reuses the
    request-boundary ``current_run_id()`` (bound by ``RunIdMiddleware``)
    instead of minting a fresh id, matching the ``@anton_skill`` (#63)
    convention. ``None`` (direct unit-test calls) still mints.
  * **Input validation moved INSIDE the hook wrap** — the staged code
    documented "the audit row records the validation failure" but raised
    the 422 BEFORE entering the hooks, so no audit row materialised.
    Validation now runs in the hook body: a 422 fires the after-hooks
    with ``usage.status="error"`` and the audit trail is complete.
    (Audit ``error_class`` reads ``HTTPException`` on the 422/500 paths
    because the route converts to the wire shape before the context
    manager records — the structured detail still carries the real
    class via ``ShapingError.error_class``.)
  * Compose handlers are NOT ``@anton_skill`` (#63) routes: they have no
    SKILL.md / registry entry (they're shaping endpoints, not operator
    verbs), so the raw #22 tool jacket is the correct governance layer.
    If a compose key ever becomes operator-invocable it should migrate
    to a registered skill — operator decision, flagged in the session
    brief.

Sensitivity default is ``confidential`` because compose handlers
typically operate on deal-scoped data (PitchPayload, IcMemoPayload,
TeaserPayload) — overridable per-handler if a future compose-key is
demonstrably public.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ValidationError

from routines.api.routes._compose_types import (
    ComposeRequest,
    ComposeResponse,
    ShapingError,
)
from routines.hooks import tool_call_hooks
from routines.skills._runtime.llm_call_counter import current_run_id

# Per-key registry. Populated by ``register_compose_key()`` calls at
# module-import time from each ``routines/composite/compose/<key>.py``.
# Operator extends by importing new modules from
# ``routines/composite/compose/__init__.py``.

router = APIRouter()
log = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────────────
# Per-key handler protocol
# ────────────────────────────────────────────────────────────────────────────


class ComposeHandler(Protocol):
    """Per-key compose handler contract.

    Each handler exposes:
      * ``key`` — registered name (matches the URL segment).
      * ``InputModel`` — Pydantic model validating ``shared_state_subset``.
      * ``OutputModel`` — Pydantic model the handler returns.
      * ``shape(inp)`` — pure function ``InputModel → OutputModel``.
      * ``sensitivity`` — tier for hook stack (default ``confidential``).
      * ``description`` — one-line human-readable summary.
    """

    key: str
    InputModel: type[BaseModel]
    OutputModel: type[BaseModel]
    sensitivity: str
    description: str

    def shape(self, inp: BaseModel) -> BaseModel: ...


# ────────────────────────────────────────────────────────────────────────────
# Registry
# ────────────────────────────────────────────────────────────────────────────


class ComposeRegistry:
    """Process-wide registry of compose-key handlers.

    Thread-safe via Python's GIL for registration + lookup (single-writer
    pattern at module-import time, many-reader at request time).
    """

    def __init__(self) -> None:
        self._handlers: dict[str, ComposeHandler] = {}

    # Known sensitivity tiers (mirrors ``routines.hooks.types.Sensitivity``).
    # Partial mitigation for codex 2026-06-10 CONCERN 5 (see ``compose()``
    # docstring): a handler that mis-declares its tier fails LOUDLY at
    # registration (import/startup) time instead of leaning on the lane
    # gate's fail-closed unknown-tier coercion at request time.
    _VALID_SENSITIVITIES = frozenset(
        {"public", "internal", "confidential", "MNPI"}
    )

    def register(self, handler: ComposeHandler) -> None:
        sensitivity = getattr(handler, "sensitivity", None)
        if sensitivity not in self._VALID_SENSITIVITIES:
            raise ValueError(
                f"compose handler {handler.key!r} declares unknown "
                f"sensitivity {sensitivity!r}; must be one of "
                f"{sorted(self._VALID_SENSITIVITIES)}"
            )
        if handler.key in self._handlers:
            log.warning(
                "compose: key %r re-registered (was %r, now %r)",
                handler.key,
                type(self._handlers[handler.key]).__name__,
                type(handler).__name__,
            )
        self._handlers[handler.key] = handler

    def get(self, key: str) -> ComposeHandler | None:
        return self._handlers.get(key)

    def keys(self) -> list[str]:
        return sorted(self._handlers)


REGISTRY = ComposeRegistry()


def register_compose_key(handler: ComposeHandler) -> ComposeHandler:
    """Decorator / function for registering a handler.

    Usage in ``routines/composite/compose/<key>.py``::

        @register_compose_key
        class ComposePitchPayload:
            key = "compose_pitch_payload"
            InputModel = PitchPayloadInput
            OutputModel = PitchPayloadOutput
            sensitivity = "confidential"
            description = "Shapes the PitchPayload step output."

            def shape(self, inp: PitchPayloadInput) -> PitchPayloadOutput:
                ...

    Or imperatively::

        REGISTRY.register(ComposePitchPayload())
    """
    # Handlers can be classes (instantiated) or already-instantiated singletons.
    if isinstance(handler, type):
        instance = handler()
    else:
        instance = handler
    REGISTRY.register(instance)
    return instance


# ────────────────────────────────────────────────────────────────────────────
# Endpoints
# ────────────────────────────────────────────────────────────────────────────


@router.get("/composite/_compose")
def list_compose_keys() -> dict[str, list[dict[str, Any]]]:
    """Discovery endpoint — lists registered compose keys.

    Useful for the dashboard's composite-inspector (planned #47) and for
    the install_synapse.py custom-tools registration step (#26a) so the
    installer can keep its placeholder list in sync.

    Unrestricted on the loopback bridge per Phase 6's single-operator
    model (STAGING-README §4 operator checklist item — intentional).
    """
    return {
        "keys": [
            {
                "key": h.key,
                "description": h.description,
                "sensitivity": h.sensitivity,
                "input_schema": h.InputModel.model_json_schema(),
                "output_schema": h.OutputModel.model_json_schema(),
            }
            for h in (REGISTRY.get(k) for k in REGISTRY.keys())
            if h is not None
        ]
    }


@router.post(
    "/composite/_compose/{key}",
    response_model=ComposeResponse,
    responses={
        404: {"description": "Unknown compose key"},
        422: {"description": "shared_state_subset failed handler input validation"},
        500: {"description": "Handler raised an unexpected exception"},
    },
)
def compose(key: str, req: ComposeRequest) -> ComposeResponse:
    """Run a compose-key handler.

    Lookup → enter tool_call_hooks → validate → shape → return. The 404
    (unknown key) is the only path outside the hook wrap — there is no
    handler to attribute a sensitivity/audit row to.

    GOVERNANCE NOTE — DECIDED 2026-06-10 (operator): composites get a
    dedicated **compose governance ADAPTER** mirroring the ``@anton_skill``
    workspace/MNPI/cloud-lane gates (per-handler skill registration was
    rejected — compose keys are internal plumbing, not operator-facing
    verbs). The adapter is tracked as ``#26-governance-adapter`` and rides
    the first #27 ``/pitch`` slice; until it lands this route stays on the
    raw #22 ``tool_call_hooks`` jacket (codex 2026-06-10 CONCERN 5,
    partially mitigated). History: ``session-briefs/OVERNIGHT-2026-06-10-
    PHASE6-COMPOSITE-LANE.md`` §"Operator decision points". Partial
    mitigation in the meantime: ``ComposeRegistry.register`` rejects
    handlers declaring an unknown sensitivity tier, so every wrap below
    enters the hook stack with a tier the lane matrix actually recognises.
    """
    handler = REGISTRY.get(key)
    if handler is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"compose key {key!r} not registered. "
                f"Known keys: {REGISTRY.keys()}"
            ),
        )

    # Hook stack: every compose goes through the #22 tool jacket — audit
    # row (``runs/tool.compose_<key>.jsonl``) + any tool-name-matched
    # guards. Validation runs INSIDE the wrap so a 422 also materialises
    # an audit row (promotion fix — see module docstring).
    with tool_call_hooks(
        tool_name=f"compose_{key}",
        # Composite contexts are deal-scoped; default to project/deal.
        # The composite passes ``composite_key`` so audit rows can be
        # filtered by parent composite downstream.
        workspace_type="project",
        workspace_name=req.composite_key or "unknown_composite",
        sensitivity=handler.sensitivity,  # type: ignore[arg-type]
        tool_input=req.shared_state_subset,
        # #59 — reuse the request-boundary X-ANTON-Run-Id (bound by
        # RunIdMiddleware) so retries coalesce in the audit trail. None
        # (direct unit-test call) → tool_call_hooks mints, preserving
        # the staged behaviour.
        run_id=current_run_id(),
    ) as ctx:
        # Validate request body against the handler's declared input model.
        # Explicit (not via FastAPI body typing) because the per-key model
        # is resolved at runtime from the registry.
        try:
            validated_input = handler.InputModel.model_validate(
                req.shared_state_subset
            )
        except ValidationError as e:
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "shared_state_subset failed input validation",
                    "key": key,
                    "run_id": req.run_id,
                    "validation_errors": e.errors(include_url=False),
                },
            ) from e

        try:
            shaped = handler.shape(validated_input)
        except HTTPException:
            raise
        except Exception as e:  # noqa: BLE001 — handler-side bug surface
            # CODEX FIX (2026-06-10 CONCERN 2): the 500 body used to echo
            # ``str(e)``, which can leak confidential input values / paths
            # to the wire (compose inputs are deal-scoped). Full detail
            # goes to the bridge log only; the response carries a generic
            # message plus ``error_class`` + ``run_id`` for correlation.
            log.exception(
                "compose handler %r raised (run_id=%s)", key, req.run_id
            )
            raise HTTPException(
                status_code=500,
                detail=ShapingError(
                    error=(
                        "compose handler raised an unexpected exception; "
                        "details are in the bridge log — correlate via "
                        "error_class + run_id"
                    ),
                    error_class=type(e).__name__,
                    key=key,
                    run_id=req.run_id,
                ).model_dump(),
            ) from e

        result_dict = shaped.model_dump()
        response = ComposeResponse(
            result=result_dict,
            key=key,
            run_id=req.run_id,
        )
        ctx.result = response.model_dump()
        return response


__all__ = [
    "router",
    "REGISTRY",
    "register_compose_key",
    "ComposeHandler",
    "ComposeRequest",
    "ComposeResponse",
    "ShapingError",
]
