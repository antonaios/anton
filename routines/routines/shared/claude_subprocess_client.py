"""Cloud Claude lane via ``claude -p`` Agent SDK subprocess.

Mirrors the [[OllamaClient]] convention in this directory: sync, single
client class, one ``chat()`` method, dataclass response. Used by the
chat dispatcher in ``routines.sessions.router`` to dispatch the
``claude-cli`` and ``claude-cli-haiku`` lanes — the first cloud-lane
wiring in ANTON.

Per #75 / OUTSTANDING: consumes the operator's MAX-tier monthly Agent
SDK credit (Pro $20 / Max 5x $100 / Max 20x $200) starting the
2026-06-15 cutover, falling back to standard API rates only when the
credit exhausts. Pre-cutover the call still works but draws standard
API credits — the mechanism only needs to be live before cutover.

Authentication is via the standalone ``claude`` CLI's own login (run
``claude`` interactively then ``/login``); NOT via
``ANTHROPIC_API_KEY``. API-key auth would route to API credits and
defeat the cost-relief goal.

Path discovery (in order, operator-decided):
  1. Constructor ``bin_path`` argument (test injection).
  2. ``AGENTIC_CLAUDE_CLI_PATH`` env var (operator-tunable; durable
     regardless of PATH state).
  3. ``shutil.which("claude")`` — succeeds when the bridge process
     inherited a PATH that includes the install dir.
  4. Known install locations:
       * ``~/.local/bin/claude.exe`` (Anthropic native installer
         Windows default — where the operator's lives).
       * ``~/.local/bin/claude`` (Unix).
       * ``$APPDATA/npm/claude.cmd`` (npm-global install fallback).
  5. Raise ``RuntimeError`` → pre-flight sets
     ``_CLAUDE_CLI_AVAILABLE=False`` → dispatcher uses the existing
     ``_stub_cloud_response()`` fallback.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from routines.shared.subprocess_env import minimal_cli_env
from routines.shared.llm_errors import ClaudeCreditExhausted, is_credit_exhaustion_text

logger = logging.getLogger(__name__)


CLAUDE_CLI_BIN = "claude"
CLAUDE_CLI_TIMEOUT_SEC = 120.0
CLAUDE_CLI_PATH_ENV = "AGENTIC_CLAUDE_CLI_PATH"

# F-23 (HR + CX A-03, confirmed×2): ``claude -p`` is an AGENT subprocess —
# the codex sibling got the chat-completion constraints (scratch cwd, stdin
# prompt, pre-spawn size cap) on day one and this client never did. Same
# rationale as codex_subprocess_client.py: stdin avoids OS argv limits AND
# flag interpretation of '-'-leading prompts; the scratch cwd bounds what an
# over-permissive CLI child can reach relative to '.'; the cap fires BEFORE
# anything spawns.
CLAUDE_MAX_PROMPT_BYTES = 1 * 1024 * 1024   # 1 MiB — mirrors CODEX_MAX_PROMPT_BYTES


@dataclass(frozen=True)
class ClaudeResponse:
    """Result of one ``claude -p`` invocation.

    Mirrors ``OllamaResponse``'s shape (sibling client) but with field
    names natural to the Claude CLI's JSON output:
      * ``content``        — the response text (``result`` in the raw payload)
      * ``model``          — dated model id from ``modelUsage`` (e.g.
                              ``claude-haiku-4-5-20251001``), not the
                              short alias the caller passed.
      * ``input_tokens``   — ``modelUsage[<model>].inputTokens``
      * ``output_tokens``  — ``modelUsage[<model>].outputTokens``
      * ``raw``            — full JSON payload; callers use this to read
                              ``total_cost_usd`` and other fields not
                              materialised on the dataclass.
    """

    content: str
    model: str
    input_tokens: int | None
    output_tokens: int | None
    raw: dict[str, Any]


class ClaudeSubprocessClient:
    """Sync wrapper around ``claude -p <prompt> --output-format json``.

    Construction discovers the binary via the resolution order in the
    module docstring; raises ``RuntimeError`` if nothing is found. The
    error message points at install + ``/login`` so the operator knows
    how to fix it.

    One instance per bridge process is fine; the dispatcher holds a
    lazy module-level singleton.
    """

    def __init__(self, bin_path: str | None = None) -> None:
        self.bin_path: str = self._resolve_bin_path(bin_path)

    # ------------------------------------------------------------------ chat

    def chat(
        self,
        model: str,
        prompt: str,
        *,
        system: str | None = None,
        json_mode: bool = False,
        temperature: float | None = None,   # accepted, ignored (no CLI flag in v1)
        max_tokens: int | None = None,      # accepted, ignored (no CLI flag in v1; #57 budget gate caps upstream)
        images: list | None = None,         # accepted, ignored (multimodal routes Ollama per CLAUDE.md §4)
        timeout_sec: float = CLAUDE_CLI_TIMEOUT_SEC,
    ) -> ClaudeResponse:
        """Run a single-turn completion.

        Args:
            model: short alias (``"opus"`` / ``"haiku"``) or full id
                (``"claude-haiku-4-5"``). Short aliases are mapped via
                ``_model_alias``.
            prompt: user message.
            system: optional system prompt; prepended to ``prompt`` in
                v1 (no ``--system`` flag wiring yet; v2 may use it).
            json_mode: if True, append a "respond with JSON only" hint
                to the system block. Best-effort — the CLI doesn't have
                a hard JSON mode like Ollama's ``format=json``.
            temperature / max_tokens / images: accepted for interface
                symmetry with ``OllamaClient.chat``; ignored in v1.
            timeout_sec: subprocess wall-clock timeout.

        Returns:
            ``ClaudeResponse`` with the parsed payload.

        Raises:
            ``TimeoutError`` if the subprocess exceeds ``timeout_sec``.
            ``RuntimeError`` if the CLI returned non-zero rc OR
                ``is_error: true`` (auth fail returns rc=0 +
                is_error=true; checking rc alone is insufficient).
            ``RuntimeError`` if the response stdout is not valid JSON.
        """
        full_prompt = self._compose_prompt(prompt, system=system, json_mode=json_mode)

        # F-23: refuse oversize prompts BEFORE spawning anything (parity with
        # CODEX_MAX_PROMPT_BYTES).
        prompt_bytes = len(full_prompt.encode("utf-8"))
        if prompt_bytes > CLAUDE_MAX_PROMPT_BYTES:
            raise ValueError(
                f"prompt is {prompt_bytes} bytes — exceeds the "
                f"{CLAUDE_MAX_PROMPT_BYTES}-byte claude -p cap"
            )

        # F-23: prompt travels via STDIN (``claude -p`` with no positional
        # prompt reads stdin in print mode), not argv — argv-size limits +
        # flag interpretation of '-'-leading prompts. The CLI's 3s
        # no-stdin-data wait doesn't apply: stdin carries data immediately.
        cmd = [
            self.bin_path,
            "-p",
            "--model", self._model_alias(model),
            "--output-format", "json",
        ]

        try:
            # F-23: scratch tempdir as cwd — bounds whatever the agent child
            # resolves relative to '.', instead of the bridge repo.
            with tempfile.TemporaryDirectory(prefix="claude_chat_") as scratch:
                proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=timeout_sec,
                    text=True,
                    # 2026-06-17 live-smoke fix: pin UTF-8. text=True alone encodes
                    # stdin via locale.getpreferredencoding() = cp1252 on Windows,
                    # which UnicodeEncodeErrors on a prompt char OUTSIDE cp1252 ('→'
                    # U+2192, emoji, most CJK/math symbols — note '—'/smart-quotes ARE
                    # in cp1252) and crashes the stdin writer thread → the dispatch
                    # 502s. The prompt is already UTF-8 byte-capped above, so UTF-8 is
                    # the intended wire codec. errors="replace" bears only on the
                    # stdout DECODE (the CLI emits UTF-8 JSON) — stay resilient to a
                    # stray byte rather than reintroduce an encoding crash.
                    encoding="utf-8",
                    errors="replace",
                    input=full_prompt,
                    cwd=scratch,
                    # F-5 (HR S-5): minimal allowlisted env — OS plumbing +
                    # OAuth/config-home vars (USERPROFILE / HOME / CLAUDE_CONFIG_DIR
                    # so the operator's ~/.claude login resolves), with the provider
                    # API keys DROPPED. The subprocess lane uses `claude login`
                    # (OAuth/plan-credit), NOT ANTHROPIC_API_KEY, so this does not
                    # break it — and a compromised CLI child no longer sees the
                    # bridge's provider keys. See subprocess_env.py.
                    env=minimal_cli_env(extra_keep=(CLAUDE_CLI_PATH_ENV,)),
                )
        except subprocess.TimeoutExpired as e:
            raise TimeoutError(
                f"claude -p exceeded {timeout_sec}s timeout"
            ) from e

        # Parse JSON FIRST. Auth-fail returns rc=0 with is_error=true —
        # checking returncode alone misses it.
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError as e:
            logger.error(
                "claude -p returned non-JSON (rc=%d): %s",
                proc.returncode, proc.stdout[:200],
            )
            raise RuntimeError(
                f"claude -p response parse failed: {e}"
            ) from e

        if proc.returncode != 0 or payload.get("is_error") is True:
            err_msg = (
                payload.get("result")
                or proc.stderr.strip()
                or "unknown error"
            )
            logger.error(
                "claude -p failed (rc=%d, is_error=%s): %s",
                proc.returncode, payload.get("is_error"), err_msg,
            )
            # #llm-routing-postjune15 B4: a plan-credit-exhaustion failure is
            # distinct from a generic outage — surface it as ClaudeCreditExhausted
            # so the dispatcher degrades to local Ollama rather than hard-erroring.
            if is_credit_exhaustion_text(err_msg):
                raise ClaudeCreditExhausted(f"claude -p credit exhausted: {err_msg}")
            raise RuntimeError(f"claude -p failed: {err_msg}")

        model_usage = payload.get("modelUsage") or {}
        actual_model = next(iter(model_usage.keys()), self._model_alias(model))
        usage_for_model = model_usage.get(actual_model) or {}

        return ClaudeResponse(
            content=payload.get("result", ""),
            model=actual_model,
            input_tokens=usage_for_model.get("inputTokens"),
            output_tokens=usage_for_model.get("outputTokens"),
            raw=payload,
        )

    # --------------------------------------------------------- internals

    @staticmethod
    def _compose_prompt(
        prompt: str, *, system: str | None, json_mode: bool,
    ) -> str:
        parts: list[str] = []
        if system:
            parts.append(f"System: {system}")
        if json_mode:
            parts.append(
                "Respond with valid JSON only — no prose, no markdown fences."
            )
        parts.append(prompt)
        return "\n\n".join(parts)

    @staticmethod
    def _model_alias(model: str) -> str:
        """Map ANTON's lane short-names to Claude CLI model ids.

        The CLI accepts the family alias (``haiku``,
        ``claude-haiku-4-5``) and the dated id
        (``claude-haiku-4-5-20251001``); we pin the family alias so we
        pick up newer dated releases as Anthropic ships them.

        #llm-routing-postjune15 P4 — pinned to the current generation:
        ``opus`` → Opus 4.8, ``sonnet`` → Sonnet 4.6, ``haiku`` → Haiku
        4.5. ``opus-1m`` is the 1M-context variant: the Claude Code CLI
        selects the 1M window via the ``[1m]`` id suffix (this very
        session's model id is ``claude-opus-4-8[1m]``), so it maps to
        ``claude-opus-4-8[1m]`` here — the suffix travels as ONE argv
        element (never shell-interpreted; see ``cmd`` in ``chat``). The
        Anthropic API has no such suffix (1M is native on
        ``claude-opus-4-8``); the sibling ``ClaudeAPIClient._model_alias``
        owns that encoding. Same family + same cost row either way.
        """
        return {
            "opus": "claude-opus-4-8",
            "opus-1m": "claude-opus-4-8[1m]",
            "sonnet": "claude-sonnet-4-6",
            "haiku": "claude-haiku-4-5",
        }.get(model, model)

    @staticmethod
    def _resolve_bin_path(bin_path: str | None) -> str:
        """Discover the claude binary per the resolution order in the
        module docstring. Raises ``RuntimeError`` if nothing is found."""
        # (a) Constructor argument — test injection or explicit override.
        if bin_path:
            return bin_path

        # (b) AGENTIC_CLAUDE_CLI_PATH env var.
        env_path = os.environ.get(CLAUDE_CLI_PATH_ENV)
        if env_path and Path(env_path).exists():
            return env_path

        # (c) shutil.which — succeeds when PATH includes the install dir.
        on_path = shutil.which(CLAUDE_CLI_BIN)
        if on_path:
            return on_path

        # (d) Known install locations.
        home = Path.home()
        appdata = os.environ.get("APPDATA", "")
        candidates: list[Path] = [
            home / ".local" / "bin" / "claude.exe",
            home / ".local" / "bin" / "claude",
        ]
        if appdata:
            candidates.append(Path(appdata) / "npm" / "claude.cmd")
        for candidate in candidates:
            if candidate.exists():
                return str(candidate)

        # Nothing found — fail with the operator-readable message the
        # brief specified. Pre-flight catches this and falls back to
        # the stub.
        raise RuntimeError(
            "claude CLI binary not found via constructor arg, "
            f"{CLAUDE_CLI_PATH_ENV} env var, PATH, or known install "
            "locations. Install per "
            "https://docs.claude.com/en/docs/claude-code/install AND "
            "run `claude` → `/login` to authenticate (see Session 16 brief)."
        )


__all__ = [
    "CLAUDE_CLI_BIN",
    "CLAUDE_CLI_PATH_ENV",
    "CLAUDE_CLI_TIMEOUT_SEC",
    "ClaudeResponse",
    "ClaudeSubprocessClient",
]
