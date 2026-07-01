"""compose_pitch_payload — first compose-key implementation (#26b).

Promoted 2026-06-09 from ``proposed-2026-05-26-phase6/26b-compose-proxy/``
per STAGING-README §3 (import path updated to the promoted
``routines.api.routes.compose`` module; behaviour unchanged).

Shapes the inputs to the `/pitch` composite's Football Field step
(per ``composite-skills.md`` §3 + SYNAPSE-EVALUATION.md "Pitch skill —
concrete build path"). Reads the per-step shared_state subset Synapse
hands in, validates it, and returns a typed PitchPayload dict the next
TOOL step (PPT assembly) can consume.

**Placeholder note:** the real ``PitchPayload`` shape is operator-owned.
This module defines the bare-minimum structure visible from the spike
DAG so the round-trip pattern is exercised; operator extends with the
real LBO output triad (ftev, ebitda_multiple_low/mid/high), DCF range,
research bullets, buyer-list, and HiNotes context post-promotion.

Pattern (copy for new compose-keys):
  1. Declare an ``InputModel`` Pydantic class scoping the shared_state
     subset the handler reads.
  2. Declare an ``OutputModel`` Pydantic class scoping the shape the
     handler returns (consumed by the next Synapse step).
  3. Write a ``shape(inp) -> OutputModel`` method — pure function, no
     side effects, no I/O.
  4. Decorate with ``@register_compose_key`` so the module-import in
     ``routines/composite/compose/__init__.py`` registers the handler.

The handler is stateless — one instance per process, registered at
module-import time, called per request.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from routines.api.routes.compose import register_compose_key


# ────────────────────────────────────────────────────────────────────────────
# Input model — what the handler reads from shared_state
# ────────────────────────────────────────────────────────────────────────────


class LBOOutputs(BaseModel):
    """Subset of the LBO skill's output the pitch payload needs."""

    ftev: float = Field(..., description="Forward TEV from the LBO model.")
    ebitda_multiple_low: float = Field(..., ge=0)
    ebitda_multiple_mid: float = Field(..., gt=0)
    ebitda_multiple_high: float = Field(..., ge=0)


class DCFOutputs(BaseModel):
    """Subset of the DCF skill's output the pitch payload needs."""

    ev_low: float = Field(..., ge=0)
    ev_mid: float = Field(..., ge=0)
    ev_high: float = Field(..., ge=0)


class CompsOutputs(BaseModel):
    """Subset of the comps skill's output."""

    median_ev_ebitda: float = Field(..., ge=0)
    p25_ev_ebitda: float = Field(..., ge=0)
    p75_ev_ebitda: float = Field(..., ge=0)


class PitchPayloadInput(BaseModel):
    """The slice of shared_state the compose_pitch_payload handler reads.

    Synapse hands these in via the TOOL step's arg-gen LLM (see
    SYNAPSE-SPIKE-RESULTS Item 2 Finding #1). The orchestration JSON
    templates the keys from earlier-step outputs.
    """

    deal_codename: str = Field(..., min_length=1, max_length=120)
    sector: str = Field(..., min_length=1, max_length=80)
    lbo: LBOOutputs
    dcf: DCFOutputs
    comps: CompsOutputs
    research_summary: str = Field(default="", description="From research skill.")
    buyer_list: list[str] = Field(
        default_factory=list, description="From buyer-list skill (top 10)."
    )


# ────────────────────────────────────────────────────────────────────────────
# Output model — what the handler returns (Football Field consumes)
# ────────────────────────────────────────────────────────────────────────────


class FootballFieldBar(BaseModel):
    """One bar on the football-field chart."""

    method: str = Field(..., description="LBO / DCF / Comps median etc.")
    low: float = Field(..., ge=0)
    high: float = Field(..., ge=0)


class PitchPayloadOutput(BaseModel):
    """Football-field-ready payload for the PPT assembly step."""

    deal_codename: str
    sector: str
    valuation_summary: str = Field(
        ...,
        description="LLM-ready one-paragraph summary the synthesis step rewrites.",
    )
    football_field: list[FootballFieldBar]
    top_buyers: list[str] = Field(..., max_length=10)
    research_excerpt: str
    audit_trail: dict[str, Any] = Field(
        default_factory=dict,
        description="Provenance handles for the FF HITL approval gate.",
    )


# ────────────────────────────────────────────────────────────────────────────
# Handler
# ────────────────────────────────────────────────────────────────────────────


@register_compose_key
class ComposePitchPayload:
    """Stateless compose-key handler. Registered at module-import time."""

    key = "compose_pitch_payload"
    InputModel = PitchPayloadInput
    OutputModel = PitchPayloadOutput
    sensitivity = "confidential"
    description = (
        "Shapes LBO + DCF + Comps + research + buyer-list outputs into a "
        "Football-Field-ready PitchPayload for the /pitch composite's PPT "
        "assembly step. TRANSFORM substitute — see #26b spec."
    )

    def shape(self, inp: PitchPayloadInput) -> PitchPayloadOutput:
        """Pure-function shaping. No I/O, no LLM, no side effects."""
        bars = [
            FootballFieldBar(
                method="LBO (forward TEV)",
                low=inp.lbo.ftev * inp.lbo.ebitda_multiple_low / inp.lbo.ebitda_multiple_mid,
                high=inp.lbo.ftev * inp.lbo.ebitda_multiple_high / inp.lbo.ebitda_multiple_mid,
            ),
            FootballFieldBar(
                method="DCF",
                low=inp.dcf.ev_low,
                high=inp.dcf.ev_high,
            ),
            FootballFieldBar(
                method="Comps (P25-P75)",
                low=inp.comps.p25_ev_ebitda,
                high=inp.comps.p75_ev_ebitda,
            ),
        ]

        summary = (
            f"{inp.deal_codename} ({inp.sector}): valuation range "
            f"£{min(b.low for b in bars):.0f}m–£{max(b.high for b in bars):.0f}m "
            f"across LBO, DCF, and trading comps. Median comps multiple "
            f"{inp.comps.median_ev_ebitda:.1f}x EV/EBITDA."
        )

        return PitchPayloadOutput(
            deal_codename=inp.deal_codename,
            sector=inp.sector,
            valuation_summary=summary,
            football_field=bars,
            top_buyers=inp.buyer_list[:10],
            research_excerpt=inp.research_summary[:1500],
            audit_trail={
                "lbo_ftev": inp.lbo.ftev,
                "dcf_mid": inp.dcf.ev_mid,
                "comps_median": inp.comps.median_ev_ebitda,
            },
        )


__all__ = [
    "ComposePitchPayload",
    "PitchPayloadInput",
    "PitchPayloadOutput",
    "FootballFieldBar",
    "LBOOutputs",
    "DCFOutputs",
    "CompsOutputs",
]
