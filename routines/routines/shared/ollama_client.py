"""Local Ollama HTTP client.

Wraps the chat (/api/chat) and embeddings (/api/embeddings) endpoints with
sensible defaults for vault-side work: long timeouts (qwen3:14b inference
can take 30-60s on this hardware), retries on transient failure, and
optional JSON-mode for structured extraction.

This is the *only* client routines should use to talk to Ollama. Direct
requests calls into Ollama from elsewhere is a maintenance hazard — keep
the wrapper centralised.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import requests

logger = logging.getLogger(__name__)


# 2026-06-02: pinned to IPv4 127.0.0.1 (was "localhost"). On Windows,
# "localhost" resolves to IPv6 ::1 first; Ollama runs in WSL (mirrored
# networking) bound to IPv4 127.0.0.1 only, so a "localhost" call hits ::1,
# finds no listener, and times out — the Windows bridge could not reach the
# WSL Ollama at all. 127.0.0.1 is unambiguous and also correct for native
# installs (Ollama binds 127.0.0.1 by default). Override per-instance if a
# routine needs a remote Ollama.
DEFAULT_BASE_URL = "http://127.0.0.1:11434"
DEFAULT_TIMEOUT_SECONDS = 240  # qwen3:14b on long transcripts can be slow
DEFAULT_RETRIES = 2
# 2026-05-27: bumped 4096 → 16384 per OUTSTANDING #16e — qwen3:14b
# supports it; ~2x VRAM for KV cache but still fits 12GB GPU. Silent
# truncation at 4k explained thin synthesis on long recall results.
# Pass-through to Ollama's `options.num_ctx` on every /api/chat call.
DEFAULT_CONTEXT_LENGTH = 16384


@dataclass(frozen=True)
class OllamaResponse:
    """Result of one Ollama call."""

    content: str
    model: str
    prompt_eval_count: int | None
    eval_count: int | None
    total_duration_ns: int | None

    @property
    def total_duration_seconds(self) -> float | None:
        return self.total_duration_ns / 1e9 if self.total_duration_ns else None


class OllamaError(Exception):
    """Raised on any failure to reach Ollama or parse its response."""


class OllamaClient:
    """Local Ollama wrapper. One instance per routine is fine; keep base_url default
    unless you're testing against a remote Ollama (rare)."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
        retries: int = DEFAULT_RETRIES,
        context_length: int = DEFAULT_CONTEXT_LENGTH,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.retries = retries
        # #16e: passed to Ollama as ``options.num_ctx`` on every chat
        # call. Override at instantiation if a specific routine needs a
        # narrower / wider window (none today).
        self.context_length = context_length
        # #ollama-trust-env (#no-mnpi-to-cloud, was cited as §5.4;
        # 2026-06-05): every call routes through a
        # Session pinned to ``trust_env=False`` so the local Ollama traffic
        # NEVER consults the ambient proxy env vars (``HTTP_PROXY`` /
        # ``HTTPS_PROXY`` / ``ALL_PROXY``) or ``~/.netrc``. ``requests`` (and
        # httpx) honour those for 127.0.0.1 too unless ``NO_PROXY`` exempts the
        # loopback — so without this, the local image/prompt bytes we send to
        # Ollama could be tunnelled off-box through an operator (or attacker)
        # proxy. This is the GLOBAL form of the per-call fix the #72 chip applied
        # locally: fail-closed at the client so no caller can forget it. All
        # five endpoints (/api/chat, /api/chat stream, /api/embeddings,
        # /api/version, /api/tags) go through ``self._session``.
        self._session = requests.Session()
        self._session.trust_env = False

    # ------------------------------------------------------------------ chat

    def chat(
        self,
        model: str,
        prompt: str,
        *,
        system: str | None = None,
        json_mode: bool = False,
        temperature: float = 0.2,
        max_tokens: int | None = None,
        images: list[str] | None = None,
    ) -> OllamaResponse:
        """Generate a completion from the chat endpoint.

        Args:
            model: e.g. "qwen3:14b" or "qwen3:8b" or "gemma4:e4b"
            prompt: the user message
            system: optional system prompt
            json_mode: if True, instruct Ollama to return JSON (it adds the
                       "Reply only with JSON" hint and sets format=json on the call)
            temperature: 0.0-1.0; lower is more deterministic. 0.2 is a good
                         default for structured extraction.
            max_tokens: optional cap on response length.
            images: optional list of base64-encoded image strings. Only
                    multimodal models (e.g. gemma4:e4b) accept these; on a
                    text-only model Ollama silently ignores them. Pass as raw
                    base64 (no data: prefix) per Ollama's /api/chat schema.

        Returns:
            OllamaResponse with content + metadata.

        Raises:
            OllamaError on transport / parse failure after retries.
        """
        messages: list[dict[str, Any]] = []
        if system:
            messages.append({"role": "system", "content": system})
        user_msg: dict[str, Any] = {"role": "user", "content": prompt}
        if images:
            user_msg["images"] = images
        messages.append(user_msg)

        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": temperature,
                # #16e: explicit num_ctx so we don't silently truncate at
                # Ollama's default (≈2-4k). See DEFAULT_CONTEXT_LENGTH.
                "num_ctx": self.context_length,
            },
            "think": False,  # qwen3 supports a "think" mode; disable for speed
        }
        if json_mode:
            body["format"] = "json"
        if max_tokens is not None:
            body["options"]["num_predict"] = max_tokens

        return self._request_with_retry("/api/chat", body, response_field="message")

    # ----------------------------------------------------------- chat (stream)

    def chat_stream(
        self,
        model: str,
        prompt: str,
        *,
        system: str | None = None,
        temperature: float = 0.2,
        max_tokens: int | None = None,
    ) -> Iterator[str]:
        """Yield assistant content deltas from /api/chat with ``stream=True``.

        Mirrors :meth:`chat` (same model / system / num_ctx / think=False) but
        streams: Ollama emits one JSON object per line, each carrying an
        incremental ``message.content`` chunk, terminated by a final
        ``{"done": true, ...}`` line. Each non-empty content delta is yielded as
        it arrives.

        Differences from :meth:`chat`:
          * **No retry.** A stream that fails partway can't be transparently
            resumed without re-emitting already-yielded tokens, so any failure —
            a connection error before the first byte, an HTTP error status, or a
            mid-stream transport/parse fault — raises :class:`OllamaError`. The
            caller decides what to do (the chat-stream route discards the partial
            turn rather than persisting half an answer).
          * **Caller-owned lifecycle.** Closing the generator (e.g. on client
            disconnect) releases the underlying HTTP connection via the
            ``finally`` below — so abandoning the stream stops the Ollama read.
        """
        messages: list[dict[str, Any]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": True,
            "options": {
                "temperature": temperature,
                "num_ctx": self.context_length,
            },
            "think": False,
        }
        if max_tokens is not None:
            body["options"]["num_predict"] = max_tokens

        try:
            resp = self._session.post(
                f"{self.base_url}/api/chat",
                json=body,
                timeout=self.timeout,
                stream=True,
            )
        except requests.exceptions.RequestException as e:
            raise OllamaError(f"Ollama stream request failed: {e}") from e
        try:
            resp.raise_for_status()
        except requests.exceptions.RequestException as e:
            resp.close()
            raise OllamaError(f"Ollama stream request failed: {e}") from e

        try:
            saw_done = False
            for line in resp.iter_lines():
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError as e:
                    raise OllamaError(f"Ollama stream line not JSON: {e}") from e
                if obj.get("error"):
                    raise OllamaError(f"Ollama stream error: {obj['error']}")
                chunk = (obj.get("message") or {}).get("content", "")
                if chunk:
                    yield chunk
                if obj.get("done"):
                    saw_done = True
                    break
            if not saw_done:
                # The stream closed before Ollama's terminating ``{"done": true}``
                # frame — the response is TRUNCATED (a clean EOF mid-generation,
                # or a proxy cut at a chunk boundary, neither of which raises a
                # transport error). Raise rather than let the caller persist a
                # partial answer as if it were complete (SEV-1).
                raise OllamaError(
                    "Ollama stream closed before a 'done' frame — response truncated"
                )
        except requests.exceptions.RequestException as e:
            # Transport fault mid-stream (read timeout, dropped connection).
            raise OllamaError(f"Ollama stream interrupted: {e}") from e
        finally:
            resp.close()

    # ------------------------------------------------------------ embeddings

    def embed(self, model: str, text: str) -> list[float]:
        """Get an embedding vector. Default model: nomic-embed-text (768 dim)."""
        body = {"model": model, "prompt": text}
        try:
            resp = self._session.post(
                f"{self.base_url}/api/embeddings",
                json=body,
                timeout=self.timeout,
            )
            resp.raise_for_status()
            data = resp.json()
        except requests.exceptions.HTTPError as e:
            # Surface Ollama's error body — a bare "500 Server Error" hides
            # the actionable detail (e.g. "the input length exceeds the
            # context length", the #recall-embed-context-overflow signature
            # callers dispatch on to retry with a shorter input).
            detail = ""
            if e.response is not None:
                detail = f" — {e.response.text[:200].strip()}"
            raise OllamaError(f"Embed request failed: {e}{detail}") from e
        except requests.exceptions.RequestException as e:
            raise OllamaError(f"Embed request failed: {e}") from e
        except json.JSONDecodeError as e:
            raise OllamaError(f"Embed response not JSON: {e}") from e

        embedding = data.get("embedding")
        if not isinstance(embedding, list):
            raise OllamaError(f"Embed response missing 'embedding' list: {data}")
        return embedding

    # --------------------------------------------------------------- health

    def health(self) -> dict[str, Any]:
        """Returns version + list of models. Use as a startup sanity check."""
        try:
            v = self._session.get(f"{self.base_url}/api/version", timeout=5).json()
            tags = self._session.get(f"{self.base_url}/api/tags", timeout=5).json()
        except requests.exceptions.RequestException as e:
            raise OllamaError(f"Ollama not reachable at {self.base_url}: {e}") from e
        return {"version": v.get("version"), "models": [m["name"] for m in tags.get("models", [])]}

    # ----------------------------------------------------------- internals

    def _request_with_retry(
        self,
        path: str,
        body: dict[str, Any],
        *,
        response_field: str,
    ) -> OllamaResponse:
        last_err: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                logger.debug(
                    "ollama call attempt=%d model=%s path=%s",
                    attempt + 1, body.get("model"), path,
                )
                resp = self._session.post(
                    f"{self.base_url}{path}",
                    json=body,
                    timeout=self.timeout,
                )
                resp.raise_for_status()
                data = resp.json()
                msg = data.get(response_field, {})
                if not isinstance(msg, dict):
                    # /api/generate returns content at top level under "response"
                    content = data.get("response", "")
                else:
                    content = msg.get("content", "")
                return OllamaResponse(
                    content=content,
                    model=data.get("model", body.get("model", "")),
                    prompt_eval_count=data.get("prompt_eval_count"),
                    eval_count=data.get("eval_count"),
                    total_duration_ns=data.get("total_duration"),
                )
            except (requests.exceptions.RequestException, json.JSONDecodeError) as e:
                last_err = e
                if attempt < self.retries:
                    backoff = 2 ** attempt
                    logger.warning(
                        "ollama call failed (attempt %d/%d): %s — retrying in %ds",
                        attempt + 1, self.retries + 1, e, backoff,
                    )
                    time.sleep(backoff)
                else:
                    break
        raise OllamaError(
            f"Ollama call failed after {self.retries + 1} attempts: {last_err}"
        )


# ------------------------------------------------------------ helper for callers


def parse_json_response(content: str) -> dict[str, Any]:
    """Robust JSON parser for LLM output. Handles common failure modes:
    - leading/trailing prose ("Here's the JSON: { ... }")
    - markdown code fences (```json ... ```)
    - trailing commas

    Raises OllamaError on unrecoverable parse failure.
    """
    s = content.strip()

    # Strip markdown fences if present
    if s.startswith("```"):
        # remove first line (```json or ```)
        s = s.split("\n", 1)[1] if "\n" in s else s
        # remove trailing ```
        if s.endswith("```"):
            s = s.rsplit("```", 1)[0]
        s = s.strip()

    # If model added a prose preamble, find first { and last }
    if not s.startswith("{"):
        first_brace = s.find("{")
        last_brace = s.rfind("}")
        if first_brace == -1 or last_brace == -1 or last_brace < first_brace:
            raise OllamaError(f"No JSON object found in response: {content[:200]!r}")
        s = s[first_brace : last_brace + 1]

    try:
        return json.loads(s)
    except json.JSONDecodeError as e:
        raise OllamaError(f"JSON parse failed: {e} -- snippet: {s[:200]!r}") from e
