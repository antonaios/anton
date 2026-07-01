"""Cloud OpenAI lane via ``codex exec`` (CLI subprocess) — Tier 1 of the LLM-
routing arch (LLM-ROUTING-2026-06-02.md §Tier 1, 2026-06-03).

Mirrors :class:`ClaudeSubprocessClient` so the dispatcher in
``routines.sessions.router`` can treat the two cloud providers
interchangeably: same constructor pattern, same ``chat()`` method, same
``CodexResponse`` field shape (``content`` / ``model`` /
``input_tokens`` / ``output_tokens`` / ``raw``).

Per IMPLEMENTATION-PLAN §4 + LLM-ROUTING §Tier 1:

  * Authentication is via the standalone ``codex`` CLI's OAuth (run
    ``codex login`` — uses the operator's ChatGPT Plus / Pro / Enterprise
    subscription; NOT via an OpenAI API key). API-key auth would route to
    a separate billing relationship.
  * ChatGPT Plus £20 / mo subscription is sufficient for the cross-check
    + occasional batch routines this architecture uses. OpenAI's docs note
    a 5-hour rolling rate limit on subscription-auth Codex CLI calls; not
    constraining for ANTON's workload.
  * The bridge subprocess-calls ``codex exec`` for one-shot completions.

Path discovery (matches the Claude resolution order):
  1. Constructor ``bin_path`` argument (test injection).
  2. ``AGENTIC_CODEX_CLI_PATH`` env var (operator-tunable; durable
     regardless of PATH state).
  3. ``shutil.which("codex")`` — succeeds when the bridge inherited PATH.
  4. Known install locations:
       * ``~/.local/bin/codex.exe`` / ``codex`` (npm-global default on
         platforms with a stable install dir).
       * ``$APPDATA/npm/codex.cmd`` (Windows npm-global install fallback).
  5. Raise ``RuntimeError`` → pre-flight sets ``_CODEX_CLI_AVAILABLE = False``
     → dispatcher falls back to Claude or stub.

**Output-format note:** ``codex exec`` writes the model response to stdout.
v1 of this client treats stdout as plain text (``content``). When OpenAI
exposes a ``--json`` flag with token-count metadata (or when a future
dispatcher needs token tracking for the OpenAI lane), the client picks the
JSON path automatically. Token counts default to ``None`` in v1 — burn
telemetry still rolls up calls by provider via the ``provider_override``
stamp on the hook context.
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

logger = logging.getLogger(__name__)


CODEX_CLI_BIN = "codex"
CODEX_CLI_TIMEOUT_SEC = 120.0
CODEX_CLI_PATH_ENV = "AGENTIC_CODEX_CLI_PATH"

# Codex-review SEV-1 (2026-06-03): ``codex exec`` is an AGENT subprocess,
# not a pure chat completion endpoint — it can use local tools (file
# read/write, command execution) under whatever cwd the parent passes.
# This wrapper hard-constrains it to chat-completion semantics by:
#   (1) running the subprocess in a scratch tempdir (cwd is bounded),
#   (2) piping the prompt via stdin instead of argv (avoids OS argv-size
#       limits AND flag-interpretation if a prompt starts with '-'),
#   (3) refusing prompts above CODEX_MAX_PROMPT_BYTES (1 MiB cap protects
#       against accidental multi-MB prompts hitting OS limits even via
#       stdin, and the cap fires BEFORE we spawn anything).
# Future hardening to consider: pass codex CLI flags that disable tool
# use + approvals when those flags land in the CLI surface.
CODEX_MAX_PROMPT_BYTES = 1 * 1024 * 1024   # 1 MiB


@dataclass(frozen=True)
class CodexResponse:
    """Result of one ``codex exec`` invocation.

    Field shape mirrors :class:`ClaudeResponse` so the dispatcher /
    telemetry can normalise both providers behind one interface:

      * ``content``        — response text (codex stdout, unwrapped).
      * ``model``          — model id the CLI used (best-effort —
                              echoed from ``--model`` or ``"gpt-5"``).
      * ``input_tokens``   — None in v1 (CLI doesn't expose by default).
      * ``output_tokens``  — None in v1.
      * ``raw``            — raw JSON dict if ``--json`` was used + parse
                              succeeded; else ``{"stdout": "<text>"}``.
    """

    content: str
    model: str
    input_tokens: int | None
    output_tokens: int | None
    raw: dict[str, Any]


class CodexSubprocessClient:
    """Sync wrapper around ``codex exec <prompt>``.

    Construction discovers the binary via the resolution order in the
    module docstring; raises ``RuntimeError`` if nothing is found. The
    error message points at install + ``codex login`` so the operator
    knows how to fix it.

    One instance per bridge process is fine; the dispatcher holds a
    lazy module-level singleton (same pattern as ClaudeSubprocessClient).
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
        temperature: float | None = None,   # accepted, ignored in v1 (no CLI flag)
        max_tokens: int | None = None,      # accepted, ignored in v1
        images: list | None = None,         # accepted, ignored (multimodal → Ollama per §4)
        timeout_sec: float = CODEX_CLI_TIMEOUT_SEC,
    ) -> CodexResponse:
        """Run a single-turn completion via ``codex exec``.

        Args:
            model: short alias (``"gpt-5"`` / ``"gpt-5-mini"``) or full id.
                Short aliases mapped via ``_model_alias``.
            prompt: user message.
            system: optional system prompt; prepended to ``prompt`` in v1
                (same fall-back as ClaudeSubprocessClient — no ``--system``
                flag wired yet).
            json_mode: if True, append a "respond with JSON only" hint
                to the system block. Best-effort.
            temperature / max_tokens / images: accepted for interface
                symmetry with ClaudeSubprocessClient + OllamaClient.chat;
                ignored in v1 (no CLI flag).
            timeout_sec: subprocess wall-clock timeout.

        Returns:
            ``CodexResponse`` with the captured stdout as ``content``.

        Raises:
            ``TimeoutError`` if the subprocess exceeds ``timeout_sec``.
            ``RuntimeError`` if the CLI returned non-zero rc OR stdout is
                empty (codex exec doesn't have an ``is_error`` flag; we
                treat empty stdout + non-zero rc as failure).
        """
        full_prompt = self._compose_prompt(prompt, system=system, json_mode=json_mode)

        # Codex-review SEV-3 (2026-06-03): cap prompt size BEFORE spawning.
        # 1 MiB is well above any realistic single-turn chat prompt and well
        # below the smallest OS argv/stdin pipe-buffer concern. The cap
        # protects against accidental multi-MB context blobs landing here.
        prompt_bytes = full_prompt.encode("utf-8", errors="replace")
        if len(prompt_bytes) > CODEX_MAX_PROMPT_BYTES:
            raise RuntimeError(
                f"codex exec prompt size {len(prompt_bytes)} bytes exceeds "
                f"cap of {CODEX_MAX_PROMPT_BYTES} (1 MiB). Trim the context "
                "or invoke via a different lane."
            )

        # Codex-review SEV-1 (2026-06-03): hard-constrain the agent
        # subprocess. ``codex exec`` is an agent that can use local tools —
        # running it in the bridge's cwd with no sandbox would expose every
        # file the bridge user can read to whatever the model asks for.
        # Mitigations applied at every call:
        #   * cwd = a fresh tempdir per call (the agent's filesystem
        #     starting point is bounded to a scratch directory we own)
        #   * prompt piped via stdin (no argv flag-interpretation; no
        #     argv-size limit; see SEV-3)
        #
        # Codex-review-round-4 SEV-2 fix (2026-06-03): an earlier draft
        # *also* redirected CODEX_HOME to the scratch dir + stripped env
        # to a minimal allowlist as a "defence in depth" — that hid the
        # operator's OAuth login (``codex login status`` returns "Not
        # logged in") and broke the OpenAI lane completely in production.
        # The fix: keep cwd-sandbox + stdin + size cap; do NOT redirect
        # CODEX_HOME; do NOT strip env. Auth must work.
        #
        # Future hardening: when codex CLI exposes flags to disable tool
        # use + auto-approve mode + bound config from the command line,
        # wire them in here.
        cmd = [
            self.bin_path,
            "exec",
            "--model", self._model_alias(model),
            "-",   # read prompt from stdin
        ]

        try:
            with tempfile.TemporaryDirectory(prefix="codex_chat_") as scratch:
                proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=timeout_sec,
                    text=True,
                    # 2026-06-17: pin UTF-8 (same cp1252 fix as the claude client).
                    # text=True alone encodes stdin via locale.getpreferredencoding()
                    # = cp1252 on Windows → UnicodeEncodeError on a prompt char OUTSIDE
                    # cp1252 ('→' U+2192, emoji, CJK/math symbols), crashing the stdin
                    # writer thread. Prompt is UTF-8 byte-capped above; errors="replace"
                    # bears only on the stdout decode (resilience over a decode crash).
                    encoding="utf-8",
                    errors="replace",
                    input=full_prompt,
                    cwd=scratch,
                    # F-5 (HR S-5): minimal allowlisted env — OS plumbing +
                    # OAuth/config-home vars (CODEX_HOME / XDG_* / USERPROFILE
                    # so the operator's ~/.codex auth.json resolves), with the
                    # provider API keys DROPPED. ``codex exec`` is an agent that
                    # can run local tools; it must not inherit ANTHROPIC/OPENAI/
                    # FIRECRAWL/… keys. The 2026-06-03 over-strip that broke
                    # OAuth is avoided by KEEPING the config-home vars (NOT
                    # redirecting CODEX_HOME) — see subprocess_env.py.
                    env=minimal_cli_env(extra_keep=(CODEX_CLI_PATH_ENV,)),
                )
        except subprocess.TimeoutExpired as e:
            raise TimeoutError(
                f"codex exec exceeded {timeout_sec}s timeout"
            ) from e

        content = (proc.stdout or "").strip()

        if proc.returncode != 0:
            err_msg = proc.stderr.strip() or content[:200] or "unknown error"
            logger.error(
                "codex exec failed (rc=%d): %s",
                proc.returncode, err_msg,
            )
            raise RuntimeError(f"codex exec failed: {err_msg}")

        if not content:
            raise RuntimeError(
                "codex exec returned empty stdout (no response captured); "
                "check that `codex login` is current."
            )

        # Best-effort: try to parse as JSON in case the CLI wrapped the
        # response (some versions of `codex exec --json` do). Falls back to
        # a plain-text payload if parsing fails.
        raw: dict[str, Any]
        try:
            raw = json.loads(content)
            if not isinstance(raw, dict):
                raw = {"stdout": content}
        except (json.JSONDecodeError, TypeError):
            raw = {"stdout": content}

        # Token counts not exposed by the CLI in v1; leave None. The
        # dispatcher's telemetry stamps `provider_override="codex"` so
        # burn-by-provider rolls up correctly without per-call counts.
        return CodexResponse(
            content=content,
            model=self._model_alias(model),
            input_tokens=raw.get("input_tokens"),
            output_tokens=raw.get("output_tokens"),
            raw=raw,
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
        """Map ANTON's lane short-names to OpenAI Codex CLI model ids.

        The CLI accepts the family alias + dated ids; we pass family
        aliases so we automatically pick up newer dated releases.
        """
        return {
            "gpt":        "gpt-5",
            "gpt-mini":   "gpt-5-mini",
            "opus":       "gpt-5",        # convenience: route Claude's "opus" to Codex's flagship
            "haiku":      "gpt-5-mini",   # convenience: route Claude's "haiku" to Codex's smaller model
        }.get(model, model)

    @staticmethod
    def _resolve_bin_path(bin_path: str | None) -> str:
        """Discover the codex binary per the resolution order in the
        module docstring. Raises RuntimeError if no candidate works."""
        if bin_path:
            return bin_path

        env_path = os.environ.get(CODEX_CLI_PATH_ENV)
        if env_path:
            return env_path

        which = shutil.which(CODEX_CLI_BIN)
        if which:
            return which

        # Known install locations.
        candidates = [
            Path.home() / ".local" / "bin" / "codex.exe",
            Path.home() / ".local" / "bin" / "codex",
        ]
        appdata = os.environ.get("APPDATA")
        if appdata:
            candidates.append(Path(appdata) / "npm" / "codex.cmd")
        for cand in candidates:
            if cand.exists():
                return str(cand)

        raise RuntimeError(
            f"codex CLI not found. Install via `npm i -g @openai/codex` then "
            f"run `codex login` (ChatGPT Plus OAuth). Or set "
            f"{CODEX_CLI_PATH_ENV} to the binary path."
        )
