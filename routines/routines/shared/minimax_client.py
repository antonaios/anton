"""MiniMax cloud chat client — OpenAI-compatible API (#minimax-chat-model).

Talks to ``https://api.minimax.io/v1/chat/completions`` with the operator's
stored MiniMax API key (credentials store, provider ``minimax``). This is the
ONLY client routines should use to reach MiniMax — keep the wrapper centralised
(mirrors the ``ollama_client`` / ``claude_api_client`` convention).

MiniMax is a CLOUD lane. NEVER call this for confidential / MNPI work — the
router gates that upstream (a MiniMax model-override is refused unless the
workspace already routes to the cloud).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import requests

from routines.credentials.store import get_store

logger = logging.getLogger(__name__)

# OpenAI-compatible chat-completions endpoint (verified against
# platform.minimax.io docs 2026-06-26). Bearer auth, no GroupId; the older
# ``max_tokens`` field is deprecated in favour of ``max_completion_tokens``.
DEFAULT_BASE_URL = "https://api.minimax.io/v1/chat/completions"
DEFAULT_TIMEOUT_SECONDS = 120
DEFAULT_MODEL = "MiniMax-M3"


@dataclass(frozen=True)
class MiniMaxResponse:
    """Result of one MiniMax chat call."""

    content: str
    model: str
    prompt_tokens: int | None
    completion_tokens: int | None


class MiniMaxError(Exception):
    """Raised on any failure to reach MiniMax or parse its response."""


class MiniMaxClient:
    """Thin OpenAI-compatible client. The API key is read lazily from the
    credentials store on each call (so a freshly-added key is picked up without
    a restart), unless one is injected (tests)."""

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        api_key: str | None = None,
    ) -> None:
        self.base_url = base_url
        self.timeout = timeout
        self._api_key = api_key

    def _key(self) -> str:
        if self._api_key:
            return self._api_key
        cred = get_store().get_credential("minimax")
        if cred is None or getattr(cred, "api_key", None) is None:
            raise MiniMaxError(
                "no MiniMax API key registered — add one in Operator → credentials"
            )
        return cred.api_key.get_secret_value()

    def chat(
        self,
        *,
        model: str | None,
        prompt: str,
        system: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> MiniMaxResponse:
        """One non-streaming chat turn. Raises ``MiniMaxError`` on any failure."""
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        body: dict[str, object] = {"model": model or DEFAULT_MODEL, "messages": messages}
        if temperature is not None:
            body["temperature"] = temperature
        if max_tokens is not None:
            body["max_completion_tokens"] = max_tokens   # MiniMax: max_tokens deprecated

        headers = {
            "Authorization": f"Bearer {self._key()}",
            "Content-Type": "application/json",
        }
        try:
            r = requests.post(self.base_url, json=body, headers=headers, timeout=self.timeout)
        except requests.RequestException as e:
            raise MiniMaxError(f"MiniMax request failed: {e}") from e

        if r.status_code != 200:
            raise MiniMaxError(f"MiniMax API HTTP {r.status_code}: {r.text[:300]}")

        try:
            data = r.json()
        except ValueError as e:
            raise MiniMaxError(f"MiniMax response was not JSON: {r.text[:300]}") from e

        choices = data.get("choices")
        if not choices or not isinstance(choices[0], dict):
            # OpenAI-compat error body OR MiniMax native ``base_resp`` error, or a
            # malformed choices shape — surface it as a MiniMaxError (review S3).
            err = data.get("error") or data.get("base_resp") or data
            raise MiniMaxError(f"MiniMax returned no usable choices: {str(err)[:300]}")

        content = (choices[0].get("message") or {}).get("content") or ""
        # MiniMax-M3 is a reasoning model that prefixes its answer with a
        # <think>…</think> chain-of-thought block in `content`. Strip a leading
        # think block so the chat shows the answer, not the reasoning.
        content = re.sub(r"(?is)^\s*<think>.*?</think>\s*", "", content)
        usage = data.get("usage") or {}
        return MiniMaxResponse(
            content=content,
            model=str(data.get("model") or model or DEFAULT_MODEL),
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
        )
