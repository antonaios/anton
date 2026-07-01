"""Pydantic request/response shapes for the bridge ``_compose`` proxy (#26b).

The compose proxy is ANTON's TRANSFORM-substitute. Synapse's ``TRANSFORM``
step type hard-fails without Docker (2026-05-26 spike Item 4); rather than
installing Docker for one feature, we ship every composite shape transform
as a bridge-side endpoint that consumes a slice of ``shared_state`` and
returns the shaped dict.

This module defines the **generic** request/response envelope used by
``POST /api/composite/_compose/<key>``. Per-key implementations under
``routines/composite/compose/<key>.py`` define their own typed handlers
that read ``shared_state_subset`` and return ``result``.

Module is named ``_compose_types`` (not ``compose_types``) to avoid clashing
with the public ``compose`` route module on case-insensitive imports — per
the staging promotion plan (proposed-2026-05-26-phase6/STAGING-README.md §3).

Design choices:
  * ``shared_state_subset`` is intentionally ``dict[str, Any]`` at the
    envelope layer — per-key handlers validate the structure they care
    about (using their own Pydantic model). This keeps the envelope
    composable across composites whose shared-state shapes diverge.
  * ``result`` mirrors that pattern — generic dict on the wire, typed
    by the per-key handler.
  * ``run_id`` and ``key`` are echoed back so dashboards / SSE consumers
    can correlate the response with the originating Synapse step.

See ``routines/composite/compose/compose_pitch_payload.py`` for the
per-key implementation pattern.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ComposeRequest(BaseModel):
    """Wire-level request shape for ``POST /api/composite/_compose/<key>``.

    Sent by a Synapse TOOL step (the composite calls the proxy as if it
    were any other HTTP tool). The per-key handler validates
    ``shared_state_subset`` against its own model.

    Fields:
      * ``shared_state_subset`` — the slice of Synapse ``shared_state``
        the composite passed in. Synapse-side, this is built via the
        forced-tool arg-gen LLM (see SYNAPSE-SPIKE-RESULTS Item 2
        Finding #1) reading from the orch's input templates.
      * ``run_id`` — Synapse-side ``orchestration_runs/<run_id>.json``
        run id; the bridge audit mirror (#26c) correlates against this so
        the ``_compose`` row joins to the parent composite's run.
      * ``composite_key`` — the parent composite (e.g. ``pitch``,
        ``ic_memo``). Optional because some compose keys are shared.
    """

    shared_state_subset: dict[str, Any] = Field(
        ...,
        description="Slice of Synapse shared_state the per-key handler shapes.",
    )
    run_id: str | None = Field(
        default=None,
        description="Synapse run_id; used to correlate audit rows.",
    )
    composite_key: str | None = Field(
        default=None,
        description="Parent composite name (e.g. 'pitch', 'ic_memo').",
    )


class ComposeResponse(BaseModel):
    """Wire-level response shape from ``POST /api/composite/_compose/<key>``.

    Synapse-side, the TOOL step deserialises this as a JSON string into
    ``shared_state[output_key]`` (see SYNAPSE-SPIKE-RESULTS Item 2
    Finding #2). Downstream steps that need typed access run an
    ``EXTRACT_JSON`` between the TOOL and the consumer.

    Fields:
      * ``result`` — the shaped dict the per-key handler returned.
      * ``key`` — the compose-key that ran (echoed for correlation).
      * ``run_id`` — echoed from request if present.
    """

    result: dict[str, Any] = Field(
        ...,
        description="Shaped dict produced by the per-key handler.",
    )
    key: str = Field(..., description="Compose-key that ran.")
    run_id: str | None = Field(
        default=None,
        description="Echoed Synapse run_id for correlation.",
    )


class ShapingError(BaseModel):
    """Structured error response when a per-key handler raises.

    The route handler catches handler-raised exceptions and emits this
    payload with HTTP 422 (validation) or 500 (handler bug). Synapse's
    TOOL step receives the JSON-encoded error and stores it in
    shared_state — downstream steps see the failure via ``EXTRACT_JSON``
    + condition step, OR the composite halts via an ``IF_ELSE`` gate.
    """

    error: str = Field(..., description="Human-readable error message.")
    error_class: str = Field(..., description="Python exception class name.")
    key: str = Field(..., description="Compose-key that failed.")
    run_id: str | None = Field(
        default=None,
        description="Echoed Synapse run_id if provided.",
    )


__all__ = [
    "ComposeRequest",
    "ComposeResponse",
    "ShapingError",
]
