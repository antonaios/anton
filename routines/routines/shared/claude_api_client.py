"""Cloud Claude lane via the ``anthropic`` Python SDK (API key auth).

Sibling to :mod:`routines.shared.claude_subprocess_client`. The
subprocess client is the cost-optimal path (consumes the operator's
MAX-tier Agent SDK monthly credit pre-API-rates starting 2026-06-15).
This module is the fallback for when the subprocess is unavailable
(binary missing, auth broken, plan credit exhausted) AND the operator
has explicitly opted in by setting ``ANTHROPIC_API_KEY`` in the env.

Authentication is via ``ANTHROPIC_API_KEY``. Calls draw against
standard API credits — distinct from the subprocess plan-credit
bucket. The dispatcher stamps ``provider_override = "claude-api"`` so
burn-rate queries cleanly segregate the two sources.

Mirrors the [[ClaudeSubprocessClient]] interface — sync, single client
class, one ``chat()`` method, ``ClaudeAPIResponse`` dataclass with the
same field names (``content`` / ``model`` / ``input_tokens`` /
``output_tokens`` / ``raw``) so the dispatcher's call sites are
interchangeable.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

from routines.shared.llm_errors import ClaudeCreditExhausted, is_credit_exhaustion_text

logger = logging.getLogger(__name__)


ANTHROPIC_API_KEY_ENV = "ANTHROPIC_API_KEY"
# Default ceiling per response; #57 budget gate caps upstream regardless.
# Anthropic's API requires ``max_tokens`` to be set on every Messages call.
DEFAULT_MAX_TOKENS = 4096


@dataclass(frozen=True)
class ClaudeAPIResponse:
    """Result of one Anthropic Messages API call.

    Mirrors :class:`routines.shared.claude_subprocess_client.ClaudeResponse`'s
    field names so the dispatcher's call sites don't have to branch on
    which backend produced the row:

      * ``content``       — concatenated text from the response blocks
      * ``model``         — dated model id the API actually served
                            (e.g. ``claude-opus-4-8-20260101``)
      * ``input_tokens``  — ``usage.input_tokens`` from the API
      * ``output_tokens`` — ``usage.output_tokens`` from the API
      * ``raw``           — dict view of the SDK response; callers use
                            it for fields not materialised on the
                            dataclass (e.g. ``stop_reason``).
    """

    content: str
    model: str
    input_tokens: int | None
    output_tokens: int | None
    raw: dict[str, Any]


class ClaudeAPIClient:
    """Sync wrapper around ``anthropic.Anthropic().messages.create``.

    Construction reads ``ANTHROPIC_API_KEY`` from the env unless an
    explicit key is passed. The Anthropic SDK is imported lazily inside
    ``__init__`` so this module is import-safe even when the SDK isn't
    installed — only ``ClaudeAPIClient()`` will raise.

    One instance per bridge process is fine; the dispatcher holds a
    lazy module-level reference.
    """

    def __init__(self, api_key: str | None = None) -> None:
        resolved_key = api_key or os.environ.get(ANTHROPIC_API_KEY_ENV)
        if not resolved_key:
            raise RuntimeError(
                f"ClaudeAPIClient: no API key provided and "
                f"{ANTHROPIC_API_KEY_ENV} env var is unset. Set the env "
                "var or pass api_key= explicitly."
            )
        # Lazy SDK import — keeps the module importable on systems
        # without the anthropic package, so unit tests that monkey-patch
        # ``ClaudeAPIClient.__init__`` (e.g. dispatcher tests) don't
        # require the dep.
        try:
            from anthropic import Anthropic
        except ImportError as e:
            raise RuntimeError(
                "ClaudeAPIClient: the `anthropic` SDK is not installed. "
                "Run `pip install -e .` from the routines repo to pick "
                "it up from pyproject.toml dependencies."
            ) from e
        self._sdk_client = Anthropic(api_key=resolved_key)

    # ------------------------------------------------------------------ chat

    def chat(
        self,
        model: str,
        prompt: str,
        *,
        system: str | None = None,
        json_mode: bool = False,
        temperature: float | None = None,
        max_tokens: int | None = None,
        images: list | None = None,         # accepted, ignored (multimodal routes Ollama per CLAUDE.md §4)
        timeout_sec: float | None = None,   # accepted; honoured via SDK kwarg when set
    ) -> ClaudeAPIResponse:
        """Run a single-turn completion via Anthropic Messages API.

        Args:
            model: short alias (``"opus"`` / ``"haiku"``) or full id.
                Short aliases are mapped via :func:`_model_alias` to the
                same family names the CLI accepts.
            prompt: user message.
            system: optional system prompt; passed via the API's
                native ``system`` parameter (NOT prepended like in the
                subprocess client — the API supports it natively).
            json_mode: if True, append a "respond with JSON only" hint
                to the system block. Best-effort — Anthropic doesn't
                have a hard JSON mode flag.
            temperature: optional; passed straight through to the API.
            max_tokens: optional cap. Defaults to
                :data:`DEFAULT_MAX_TOKENS` because the API requires
                this field on every call.
            images: accepted for interface symmetry with the
                subprocess client; ignored (multimodal routes Ollama).
            timeout_sec: optional wall-clock timeout, in seconds.

        Returns:
            :class:`ClaudeAPIResponse` with the parsed payload.

        Raises:
            RuntimeError: if the SDK call fails for any reason
                (auth, rate-limit, network, malformed response).
                The dispatcher catches this and surfaces a graceful
                error body — same UX as the subprocess client.
        """
        # Compose the system prompt — append the JSON nudge if requested.
        effective_system: str | None = system
        if json_mode:
            json_hint = (
                "Respond with valid JSON only — no prose, no markdown fences."
            )
            effective_system = (
                f"{system}\n\n{json_hint}" if system else json_hint
            )

        kwargs: dict[str, Any] = {
            "model": self._model_alias(model),
            "max_tokens": max_tokens if max_tokens is not None else DEFAULT_MAX_TOKENS,
            "messages": [{"role": "user", "content": prompt}],
        }
        if effective_system is not None:
            kwargs["system"] = effective_system
        if temperature is not None:
            kwargs["temperature"] = temperature
        if timeout_sec is not None:
            kwargs["timeout"] = timeout_sec

        try:
            sdk_resp = self._sdk_client.messages.create(**kwargs)
        except Exception as e:  # noqa: BLE001 — surface as RuntimeError uniformly
            logger.error("anthropic API call failed: %s", e)
            # #llm-routing-postjune15 B4: classify a credit/quota-exhaustion
            # failure so the dispatcher can degrade to local Ollama (Decision 1).
            if is_credit_exhaustion_text(str(e)):
                raise ClaudeCreditExhausted(
                    f"anthropic API credit exhausted: {e}"
                ) from e
            raise RuntimeError(f"anthropic API call failed: {e}") from e

        return self._parse_response(sdk_resp)

    # --------------------------------------------------------- internals

    @staticmethod
    def _parse_response(sdk_resp: Any) -> ClaudeAPIResponse:
        """Convert an SDK ``Message`` into our flat dataclass.

        Defensive: the SDK shape is stable but we don't want a missing
        attribute to crash the dispatcher mid-turn. Missing fields fall
        back to empty/None and we still return a usable response.
        """
        # ``content`` is a list of TextBlock / ToolUseBlock; join text bits.
        content_blocks = getattr(sdk_resp, "content", []) or []
        text_parts: list[str] = []
        for block in content_blocks:
            block_type = getattr(block, "type", None)
            if block_type == "text":
                text_parts.append(getattr(block, "text", "") or "")
        content = "".join(text_parts)

        usage = getattr(sdk_resp, "usage", None)
        input_tokens = getattr(usage, "input_tokens", None) if usage else None
        output_tokens = getattr(usage, "output_tokens", None) if usage else None

        model_id = getattr(sdk_resp, "model", "") or ""

        # Best-effort raw dump for telemetry / debugging. Use the SDK's
        # ``model_dump`` if it's a Pydantic-style model, else fall back
        # to a minimal manual dict so callers always get something.
        raw: dict[str, Any]
        model_dump = getattr(sdk_resp, "model_dump", None)
        if callable(model_dump):
            try:
                raw = model_dump()
            except Exception:  # noqa: BLE001 — best-effort only
                raw = {}
        else:
            raw = {}
        if not raw:
            raw = {
                "model": model_id,
                "content": content,
                "stop_reason": getattr(sdk_resp, "stop_reason", None),
            }

        return ClaudeAPIResponse(
            content=content,
            model=model_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            raw=raw,
        )

    @staticmethod
    def _model_alias(model: str) -> str:
        """Map ANTON's lane short-names to Anthropic API model ids.

        Resolves to the SAME model family as the subprocess client so a
        switch between the two paths doesn't silently change which model
        the call hits (#llm-routing-postjune15 P4): ``opus`` → Opus 4.8,
        ``sonnet`` → Sonnet 4.6, ``haiku`` → Haiku 4.5.

        The one encoding difference is ``opus-1m``. On the Messages API
        the 1M context window is NATIVE to ``claude-opus-4-8`` — there is
        no ``[1m]`` id variant, and sending one would 404 — so ``opus-1m``
        maps to the plain ``claude-opus-4-8`` here. The subprocess (CLI)
        sibling instead uses the ``claude-opus-4-8[1m]`` suffix the Claude
        Code CLI needs to select the 1M window. Same family, same cost
        row (1M is billed at standard pricing); only the dispatch-string
        encoding differs.
        """
        return {
            "opus": "claude-opus-4-8",
            "opus-1m": "claude-opus-4-8",  # 1M is native on the API — no [1m] suffix (would 404)
            "sonnet": "claude-sonnet-4-6",
            "haiku": "claude-haiku-4-5",
        }.get(model, model)


__all__ = [
    "ANTHROPIC_API_KEY_ENV",
    "DEFAULT_MAX_TOKENS",
    "ClaudeAPIClient",
    "ClaudeAPIResponse",
]
