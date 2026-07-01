"""Assemble the chat prompt + call the local LLM.

``build_prompt`` is a pure function: it returns ``(system, user)`` strings in
a fixed, testable order — system persona, then the last-N prior turns
interleaved, then a "Sources retrieved:" block, then the new user message. The
system prompt enforces the en-GB principal-side voice + the source-citation
discipline (v1 decision #1 — expandable FOOTER citations only, no inline
superscripts).

``answer`` does the LLM call (local Ollama qwen3:14b). It RAISES ``OllamaError``
when the model is unreachable or returns nothing — the caller must not persist a
turn on failure, giving the non-stream endpoint the SAME no-half-write behaviour
as the streaming endpoint (#42 v2 error-parity: neither persists a turn on a
model error).
"""

from __future__ import annotations

import logging
from collections.abc import Iterator

from routines.project_chat.pull import ChatContext
from routines.project_chat.schema import ChatSource, ChatTurn
from routines.shared.ollama_client import OllamaClient, OllamaError

log = logging.getLogger(__name__)


DEFAULT_MODEL = "qwen3:14b"


_SYSTEM = """\
You are Anton — the operator's M&A copilot, answering questions about ONE deal.

You are scoped to a single project. Answer ONLY from the conversation history
and the retrieved sources below. If the sources don't cover the question, say
so plainly — do not invent facts, figures, or deal terms.

Voice rules:
- en-GB spelling throughout (e.g. "organise", "analyse", "favour", "£").
- Principal-side. Hedge-light. No corporate filler, no preamble.
- Answer the question directly; lead with the conclusion.
- Do NOT add inline citation markers or superscripts. The sources are listed
  separately in an expandable footer — reference them by name in prose if
  helpful ("per the buyer thesis"), but never with [1] / superscript markers.
- Plain prose. Use a short list only when the answer is genuinely a list.

Stay strictly within this project. Never reference other deals.
"""


def build_prompt(ctx: ChatContext) -> tuple[str, str]:
    """Build the ``(system, user)`` prompt pair in fixed order.

    The user message is composed as:

        [prior turns, oldest→newest, "you:" / "anton:" prefixed]
        Sources retrieved:
        - <path> (score N.NN)
          <excerpt>
        ...
        Question:
        <the new user message>

    History + sources blocks are omitted (with a short "(none)" marker) when
    empty so the order is stable for tests regardless of context richness.
    """
    parts: list[str] = []

    # 1. History — oldest first so the model reads the thread in order.
    if ctx.history:
        parts.append("Conversation so far:")
        for t in ctx.history:
            speaker = "you" if t.role == "user" else "anton"
            parts.append(f"{speaker}: {t.text}")
        parts.append("")

    # 2. Retrieved sources block.
    parts.append("Sources retrieved:")
    if ctx.sources:
        for s in ctx.sources:
            parts.append(f"- {s.path} (score {s.score:.2f})")
            if s.excerpt:
                parts.append(f"  {s.excerpt}")
    else:
        parts.append("(none — no project documents matched this question)")
    parts.append("")

    # 3. The new user message, last.
    parts.append("Question:")
    parts.append(ctx.message)

    return _SYSTEM, "\n".join(parts)


def answer(
    ctx: ChatContext,
    *,
    client: OllamaClient,
    model: str = DEFAULT_MODEL,
) -> str:
    """Call the local LLM for the assistant's answer text.

    RAISES :class:`OllamaError` when the model is unreachable OR returns an empty
    response. The caller (``run_turn``) must NOT persist a turn on a raise — this
    gives the back-compat non-stream endpoint the SAME no-half-write behaviour as
    the streaming endpoint (#42 v2 error-parity). An empty response is a failure,
    not a blank turn.
    """
    system, user = build_prompt(ctx)
    resp = client.chat(model=model, prompt=user, system=system)
    text = (resp.content or "").strip()
    if not text:
        raise OllamaError("local model returned an empty response")
    return text


def answer_stream(
    ctx: ChatContext,
    *,
    client,  # OllamaClient (duck-typed for tests — only ``chat_stream`` is used)
    model: str = DEFAULT_MODEL,
) -> Iterator[str]:
    """Yield the assistant's answer token-by-token from the local LLM.

    The streaming sibling of :func:`answer`. Builds the SAME ``(system, user)``
    prompt (so the en-GB / footer-citation discipline is identical) then
    delegates to ``client.chat_stream``.

    Like :func:`answer`, a model failure propagates as :class:`OllamaError` and
    NOTHING is persisted: a stream that fails mid-flight can't be half-persisted,
    so the error reaches the caller, the chat-stream route turns it into an SSE
    ``error`` event, and the (partial) turn is discarded. Both endpoints now share
    this no-half-write contract (#42 v2 error-parity).
    """
    system, user = build_prompt(ctx)
    yield from client.chat_stream(model=model, prompt=user, system=system)


def make_turns(ctx: ChatContext, answer_text: str, *, now_iso: str) -> tuple[ChatTurn, ChatTurn]:
    """Build the (user, assistant) ``ChatTurn`` pair to persist.

    Both turns share the same timestamp (``now_iso``) — they're one exchange.
    The assistant turn carries the retrieved sources; the user turn does not.
    """
    user_turn = ChatTurn(timestamp=now_iso, role="user", text=ctx.message, sources=[])
    assistant_turn = ChatTurn(
        timestamp=now_iso,
        role="assistant",
        text=answer_text,
        sources=list(ctx.sources),
    )
    return user_turn, assistant_turn


# Re-export so callers can build sources without importing schema directly.
__all__ = ["build_prompt", "answer", "answer_stream", "make_turns", "ChatSource", "DEFAULT_MODEL"]
