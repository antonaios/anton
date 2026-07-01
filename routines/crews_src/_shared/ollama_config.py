"""Per-role Ollama LLM construction for ANTON crews (#31).

Runs in the crew venv only. Maps the bridge-resolved ``llm_config`` block of
``CrewInput`` onto a MetaGPT LLM instance per role. The bridge decides WHICH
provider/models a crew gets (sensitivity → lane resolution happens
bridge-side, before the subprocess exists); this module just instantiates.

metagpt import lives INSIDE the builder so the module itself stays importable
(and unit-testable) without a working MetaGPT install.
"""

from __future__ import annotations

import asyncio
import json
import urllib.request
from typing import Any

# Per-call hang guard for the direct structured call. The bridge's wall-clock
# cap (cost_cap_seconds, 600s) is the real bound; this per-request ceiling just
# stops ONE wedged Ollama call from eating most of that budget before it fails.
# 300s (was 540 = 90% of the wall): a true hang now fails at ~half the wall,
# leaving crew budget to error cleanly, while staying generously above a
# healthy grammar-constrained generation (codex review 2026-06-15).
_STRUCTURED_TIMEOUT_S = 300

_ROLE_MODEL_KEYS = {
    "Analyst": "model_analyst",
    "Reviewer": "model_reviewer",
    "Synthesist": "model_synthesist",
}


def ollama_base_url(raw: str | None) -> str:
    """Normalise the bridge-supplied base_url for MetaGPT's Ollama provider.

    Two machine-specific rules, both learned the hard way:
      * 127.0.0.1, never ``localhost`` — Ollama runs in WSL bound to IPv4;
        Windows resolves ``localhost`` to ::1 and the connect times out.
      * MetaGPT's Ollama provider expects the ``/api`` suffix on the base
        URL (it appends ``/chat`` etc. itself) — append when missing.
    """
    url = (raw or "http://127.0.0.1:11434").rstrip("/")
    url = url.replace("://localhost", "://127.0.0.1")
    if not url.endswith("/api"):
        url = url + "/api"
    return url


def model_for_role(role_name: str, llm_config: Any) -> str:
    """Resolve the model name for a role from the CrewLLMConfig.

    Resolution order (first hit wins):
      1. The generic ``models`` map (#33) keyed by the EXACT role name — the
         only surface that works for crews whose roles aren't the hello_world
         Analyst/Reviewer/Synthesist trio (explore, triage, debate).
      2. The legacy flat ``model_<role>`` field (hello_world back-compat).
      3. The spec's smoke-crew default (``qwen3:8b`` for a Reviewer, else
         ``qwen3:14b``) — so a missing/partial config never crashes a role.
    """
    models = getattr(llm_config, "models", None)
    if isinstance(models, dict):
        mapped = models.get(role_name)
        if mapped:
            return str(mapped)
    key = _ROLE_MODEL_KEYS.get(role_name)
    model = getattr(llm_config, key, None) if key else None
    if not model:
        model = "qwen3:8b" if role_name == "Reviewer" else "qwen3:14b"
    return str(model)


def build_ollama_llm_for_role(role_name: str, llm_config: Any) -> Any:
    """Instantiate the LLM for ``role_name``.

    Local roles get a MetaGPT Ollama LLM (the default — crews are local-first per
    [[CLAUDE]] §5.2). A role the operator PROMOTED to a cloud lane
    (#crew-cloud-promotion) gets a :class:`BridgeLLM` instead, which routes its
    calls BACK through the bridge's gated ``/api/crew/_llm`` — the subprocess
    holds no cloud keys. ``role_lanes`` (set bridge-side, only for promoted
    roles) is the switch; an absent role stays local.

    A non-ollama legacy ``provider`` with no promotion still raises ``ValueError``
    so a misconfigured cloud rollout fails loudly rather than calling the wrong
    backend silently."""
    # #crew-cloud-promotion: promoted role → route through the bridge.
    role_lanes = getattr(llm_config, "role_lanes", None)
    if isinstance(role_lanes, dict) and role_name in role_lanes:
        from _shared.bridge_llm import BridgeLLM
        return BridgeLLM(
            bridge_url=getattr(llm_config, "bridge_url", None),
            run_id=getattr(llm_config, "run_id", None),
            role=role_name,
            model=role_lanes.get(role_name),   # lane string — informational
        )

    provider = str(getattr(llm_config, "provider", "ollama"))
    if provider != "ollama":
        raise ValueError(
            f"crew LLM provider {provider!r} is not wired in v1 (local-only "
            f"lanes per CLAUDE.md §5.2)"
        )

    # metagpt imports deferred — see module docstring.
    from metagpt.configs.llm_config import LLMConfig as MGLLMConfig
    from metagpt.configs.llm_config import LLMType
    from metagpt.provider.llm_provider_registry import create_llm_instance
    from metagpt.utils.cost_manager import TokenCostManager

    cfg = MGLLMConfig(
        api_type=LLMType.OLLAMA,
        base_url=ollama_base_url(getattr(llm_config, "base_url", None)),
        model=model_for_role(role_name, llm_config),
    )
    llm = create_llm_instance(cfg)
    # TokenCostManager, not the default CostManager: Ollama models aren't in
    # metagpt's TOKEN_COSTS price table, so the default manager warns and
    # counts NOTHING — zeroing the roles_log token telemetry and the crew's
    # Layer-2 cost cap. TokenCostManager counts prompt/completion tokens
    # without pricing (it exists for exactly this self-hosted case).
    llm.cost_manager = TokenCostManager()
    return llm


async def ollama_structured_chat(
    base_url: str, model: str, prompt: str, schema: dict,
) -> tuple[dict, int]:
    """Direct Ollama ``/api/chat`` with ``format=schema`` — GRAMMAR-CONSTRAINS the
    decode to the JSON schema.

    MetaGPT's ``aask`` cannot pass Ollama's ``format`` param, so crews that need
    RELIABLE structured output call this instead of a free-text JSON *ask* the
    model may ignore. (Smoke 2026-06-15: on a real CIM, qwen3:14b wrote markdown
    prose for triage's pipe AND plain-JSON asks — 0 parsed; the same prompt under
    a ``format`` schema produced 10 well-formed, page-cited rows.)

    Forces ``/no_think``: a ``<think>`` block cannot satisfy the schema, and the
    constraint — not reasoning — is what guarantees the shape. Returns
    ``(parsed_obj, token_count)``; ``parsed_obj`` is ``{}`` if the (constrained)
    output still fails to parse. The blocking POST runs off the event loop so the
    crew's bounded-concurrency fan-out is unaffected.
    """
    url = ollama_base_url(base_url).rstrip("/") + "/chat"
    payload = json.dumps({
        "model": model,
        "stream": False,
        "format": schema,
        "messages": [{"role": "user", "content": "/no_think\n" + prompt}],
    }).encode("utf-8")

    def _post() -> dict:
        req = urllib.request.Request(
            url, data=payload, headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=_STRUCTURED_TIMEOUT_S) as resp:
            return json.loads(resp.read().decode("utf-8"))

    data = await asyncio.to_thread(_post)
    content = (data.get("message") or {}).get("content") or ""
    tokens = int(data.get("prompt_eval_count") or 0) + int(data.get("eval_count") or 0)
    try:
        obj = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        obj = {}
    return (obj if isinstance(obj, dict) else {}), tokens


__all__ = [
    "ollama_base_url",
    "model_for_role",
    "build_ollama_llm_for_role",
    "ollama_structured_chat",
]
