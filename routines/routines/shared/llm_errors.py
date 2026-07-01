"""Shared LLM-dispatch error types + classifiers (#llm-routing-postjune15 B4).

The cloud-Claude lanes (``claude -p`` subprocess + the Anthropic API client)
collapse every failure into a ``RuntimeError`` so the dispatcher can surface a
graceful body instead of a 500. Post-2026-06-15 the headless ``claude -p`` lane
consumes the operator's monthly Agent-SDK plan credit; when THAT credit is
exhausted the provider returns a distinct "no credit" failure we want to treat
differently from a generic outage — degrade to the local Ollama lane so the
user still gets an answer (#llm-routing-postjune15 B4, Decision 1).

``ClaudeCreditExhausted`` subclasses ``RuntimeError`` so the existing broad
``except RuntimeError`` paths still catch it; callers that care branch on the
subtype to route the graceful degradation.
"""

from __future__ import annotations

import re

__all__ = ["ClaudeCreditExhausted", "is_credit_exhaustion_text"]


class ClaudeCreditExhausted(RuntimeError):
    """A cloud Claude call failed specifically because the plan / API credit is
    exhausted (vs a generic outage, auth failure, timeout, or transient rate
    limit). A ``RuntimeError`` subclass so existing broad catches still handle
    it; the dispatcher branches on the subtype to degrade to local Ollama."""


# Positive markers of a credit / quota / plan-usage exhaustion. Compound where a
# bare word would over-match: ``insufficient`` alone hits "insufficient
# permissions" (a 403), bare ``exhaust`` hits "retries / resource exhausted" (an
# infra/transient failure) — both flagged in review (SEV-2/3). ``credit`` /
# ``quota`` are word-bounded; a real credit-exhaustion message always contains
# one of these (Anthropic: "credit balance is too low"; OpenAI: "exceeded your
# current quota"), so bare ``exhaust`` is dropped as redundant + noisy.
_CREDIT_POS_RE = re.compile(
    r"\bcredit\b"
    r"|\bquota\b"
    r"|usage\s+limit"
    r"|plan\s+limit"
    r"|insufficient\s+(?:credit|quota|balance|funds?|tokens?)"
    r"|billing"
    r"|out\s+of\s+(?:credit|quota)",
    re.IGNORECASE,
)

# A failure that LOOKS adjacent but must NOT degrade to local — auth / permission
# / transient rate-limit / overload / timeout / not-found. A negative match
# VETOES a positive one (e.g. "insufficient permissions", "quota — rate limited"),
# so a non-credit failure is never mistaken for credit exhaustion.
_CREDIT_NEG_RE = re.compile(
    r"rate[\s_-]*limit"
    r"|unauthor"
    r"|forbidden"
    r"|permission"
    r"|invalid[\s_-]*(?:api[\s_-]*)?key"
    r"|authenticat"
    r"|overload"
    r"|timed?[\s_-]*out|timeout"
    r"|not[\s_-]*found",
    re.IGNORECASE,
)


def is_credit_exhaustion_text(text: str | None) -> bool:
    """True if ``text`` looks like a credit / quota / plan-usage exhaustion
    failure message.

    Matches the strings the ``claude -p`` CLI (its ``result`` / stderr) and the
    Anthropic SDK ("credit balance is too low", ``billing_error``) surface when
    the plan / API credit runs out. A negative-marker match (auth / permission /
    transient rate-limit / overload / timeout / not-found) VETOES a positive one,
    so a non-credit failure is never mistaken for credit exhaustion. Conservative
    by design — see the module docstring."""
    if not text:
        return False
    if _CREDIT_NEG_RE.search(text):
        return False
    return bool(_CREDIT_POS_RE.search(text))
