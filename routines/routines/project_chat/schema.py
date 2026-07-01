"""Pydantic schema for project chat (OUTSTANDING #42 / plan §6.6).

Four models, exactly as the spec locks them:

  * ``ChatSource`` — one retrieved recall hit (path + score + excerpt).
  * ``ChatTurn``   — one conversational turn (user or assistant). Assistant
    turns carry the sources that grounded the answer.
  * ``ChatRequest``  — the POST body for a new turn.
  * ``ChatResponse`` — the assistant's just-persisted turn + recall stats.

``ChatSource`` is defined before ``ChatTurn`` because the latter references
it in a default-empty list.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class ChatSource(BaseModel):
    """One recall hit surfaced as a citation for an assistant turn."""

    path: str          # vault-relative POSIX, e.g. "Projects/FALCON/02 Meeting Notes/2026-05-08.md"
    score: float       # recall match score
    excerpt: str       # the chunk text, ~200-300 chars


class ChatTurn(BaseModel):
    """One turn of the conversation. ``sources`` populated for assistant turns."""

    timestamp: str                          # ISO-8601 UTC
    role: Literal["user", "assistant"]
    text: str
    sources: list[ChatSource] = Field(default_factory=list)


class ChatRequest(BaseModel):
    """POST body for ``/api/projects/{code}/chat``."""

    project: str                            # deal code, e.g. "FALCON"
    message: str
    history_turns: int = 6                   # prior turns to include in the LLM window
    # #42 v2 — relaxed-scope toggle (operator decision 2026-06-04). Default OFF
    # keeps the v1 STRICT project scope (recall filtered to ``Projects/<code>/``).
    # When True, recall widens to the WHOLE vault, but out-of-deal content is
    # capped at ``≤ internal`` sensitivity so no confidential / MNPI material from
    # another deal (or the general vault) can bleed into this chat — see
    # ``pull.fetch_sources``. The current deal's own folder stays FULL tier.
    cross_projects: bool = False


class ChatResponse(BaseModel):
    """Return shape for a completed chat turn."""

    turn: ChatTurn                          # the assistant's new turn (already persisted)
    sources: list[ChatSource] = Field(default_factory=list)
    recall_hits: int = 0
    duration_ms: int = 0
    # Echoes the scope this turn actually ran under, so the dashboard can mark a
    # cross-scope answer clearly (the operator sees when an answer drew on other
    # deals / the general vault). False == v1 strict project scope.
    cross_projects: bool = False
