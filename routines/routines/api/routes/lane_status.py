"""Routing lane-status endpoint (#llm-routing-postjune15 G1 -- Mission B).

A read-only readout of the cloud-dispatch FALLBACK LADDER per lane, for the
dashboard's "Claude lane health" strip. It mirrors what the dispatcher actually
does (verified against ``_dispatch_cloud_claude`` / ``_dispatch_cloud_codex``)
and is deliberately TRUTHFUL about the current posture rather than drawing a
tidy-but-wrong ladder:

  * Claude lane (ORCHESTRATION -- synthesis / planning):
        OAuth subprocess (claude -p)  ->  Anthropic API key  ->  local Ollama (floor)
    The Agent-SDK credit is the BUDGET behind rung 1 (a $-cap), NOT a separate
    transport; it is currently PARKED (Anthropic reversed the structure).
  * Codex lane (ANALYSIS -- cross-check):
        OAuth subprocess (codex exec)  ->  [OpenAI API: NOT WIRED YET]
    Single-rung today; no API fallback + no Ollama degrade (returns a graceful
    error). The API rung is a structurally-anticipated future extension.

Rung ``state`` is CONFIGURATION (would-attempt), from CLI availability + the
API-key env -- NOT a runtime probe. The paused headless billing is an
operational fact that surfaces as a call-time error, so it is deliberately NOT
asserted as a rung flag here (that would go stale); the burn panel shows the
actual recent provider mix.

Loopback-only, same pattern as ``routing_matrix`` / ``skills_providers``.
"""

from __future__ import annotations

import logging
import os

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from routines.shared.routing import lane_to_model

log = logging.getLogger(__name__)

# The Agent-SDK credit budget env (#75 / postjune15 B-series): seeded into a
# provider:anthropic:* budget policy when set. CURRENTLY PARKED -- Anthropic
# reversed the SDK-credit structure, so the operator does not set it.
_SDK_CREDIT_ENV = "AGENTIC_AGENT_SDK_CREDIT_USD"

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "testclient"})


def _loopback_only(request: Request) -> None:
    client_host = request.client.host if request.client else None
    if client_host not in _LOOPBACK_HOSTS:
        log.warning("routing endpoint refused non-loopback connection from %r", client_host)
        raise HTTPException(
            status_code=403,
            detail=(
                "routing endpoints are loopback-only; refusing "
                f"connection from {client_host!r}"
            ),
        )


router = APIRouter(
    prefix="/api/routing",
    tags=["routing"],
    dependencies=[Depends(_loopback_only)],
)


class Rung(BaseModel):
    rung: str         # oauth-subprocess | anthropic-api | ollama-degrade | openai-api
    transport: str    # human-readable transport label
    state: str        # available | unavailable | armed | absent | floor | not-wired
    detail: str       # one-line explanation


class CloudLane(BaseModel):
    lane: str         # claude | codex
    purpose: str      # orchestration | analysis
    rungs: list[Rung]


class SdkCredit(BaseModel):
    state: str        # parked | configured
    env: str
    detail: str


class LaneStatusResponse(BaseModel):
    claude: CloudLane
    codex: CloudLane
    sdk_credit: SdkCredit
    # Determinability caveat -- the rung states are CONFIG, not a runtime probe.
    note: str


@router.get("/lane-status", response_model=LaneStatusResponse)
def get_lane_status() -> LaneStatusResponse:
    """The per-lane cloud-dispatch fallback ladder + each rung's configured state.

    Read-only: composes from the dispatcher's boot-time readiness
    (``router.lane_readiness()``) + the SDK-credit env. No dispatch is performed
    and no runtime probe is made (so it can't go stale claiming a false "live").
    """
    from routines.sessions import router as dispatch_router  # lazy: avoid api->sessions cycle

    ready = dispatch_router.lane_readiness()
    # Derive the local floor's model from the lane map (single-source) rather than
    # hardcode it -- keeps this readout from drifting if the lane is ever rebumped.
    _, ollama_model = lane_to_model("ollama")

    claude = CloudLane(
        lane="claude",
        purpose="orchestration",
        rungs=[
            Rung(
                rung="oauth-subprocess",
                transport="claude -p (OAuth / subscription)",
                state="available" if ready["claude_cli"] else "unavailable",
                detail=(
                    "Headless Claude CLI, authed via the Max/Pro OAuth login -- the "
                    "preferred rung. Spends the plan / Agent-SDK credit (see sdk_credit)."
                ),
            ),
            Rung(
                rung="anthropic-api",
                transport="Anthropic Messages API (ANTHROPIC_API_KEY)",
                state="armed" if ready["anthropic_api"] else "absent",
                detail=(
                    "Pay-per-use fallback when the subprocess fails / its credit is "
                    "exhausted. Armed only when ANTHROPIC_API_KEY is set."
                ),
            ),
            Rung(
                rung="ollama-degrade",
                transport=f"local Ollama ({ollama_model})",
                state="floor",
                detail=(
                    "Structural floor: on terminal credit-exhaustion the Claude lane "
                    "is DESIGNED to degrade here -- subject to the local Ollama box "
                    "being up (this endpoint does not probe its liveness)."
                ),
            ),
        ],
    )

    codex = CloudLane(
        lane="codex",
        purpose="analysis",
        rungs=[
            Rung(
                rung="oauth-subprocess",
                transport="codex exec (OAuth / subscription)",
                state="available" if ready["codex_cli"] else "unavailable",
                detail="Headless Codex CLI, authed via the ChatGPT OAuth login. The only rung today.",
            ),
            Rung(
                rung="openai-api",
                transport="OpenAI API",
                state="not-wired",
                detail=(
                    "Structurally anticipated (mirrors the Claude API fallback) but NOT "
                    "wired yet; on CLI failure Codex returns a graceful error, with no "
                    "Ollama degrade. Tracked as a future enhancement."
                ),
            ),
        ],
    )

    sdk_configured = bool(os.environ.get(_SDK_CREDIT_ENV))
    sdk_credit = SdkCredit(
        state="configured" if sdk_configured else "parked",
        env=_SDK_CREDIT_ENV,
        detail=(
            "A $-budget behind the Claude OAuth rung (not a separate transport). "
            + (
                "Currently set via the env."
                if sdk_configured
                else "Currently parked -- Anthropic reversed the SDK-credit structure."
            )
        ),
    )

    return LaneStatusResponse(
        claude=claude,
        codex=codex,
        sdk_credit=sdk_credit,
        note=(
            "Rung states are CONFIGURATION (would-attempt), from CLI availability + "
            "the API-key env -- not a runtime probe. Live cloud dispatch can still be "
            "operationally paused (e.g. headless billing) without changing these "
            "flags; see the burn panel for the actual recent provider mix."
        ),
    )
