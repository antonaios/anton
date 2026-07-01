"""Crew-side LLM provider that routes a PROMOTED role's calls BACK through the
bridge's gated dispatcher (#crew-cloud-promotion, Phase A).

Runs in the ISOLATED crew venv (Python 3.11). When the operator promotes a crew
role to a frontier cloud lane, the bridge sets ``role_lanes`` / ``bridge_url`` /
``run_id`` on the ``CrewLLMConfig`` and the per-role factory
(``ollama_config.build_ollama_llm_for_role``) hands that role a ``BridgeLLM``
instead of a direct Ollama client. Each ``aask`` POSTs ``{run_id, role, prompt,
system}`` to the bridge's loopback ``/api/crew/_llm``; the BRIDGE runs the
sensitivity + budget gates and dispatches to the cloud provider, then returns
the completion. The crew subprocess therefore holds NO cloud credentials — that
is the entire point (the load-bearing containment layer).

Stdlib-only (``urllib`` + ``json``) — no metagpt, no requests — so it matches
the boundary module's no-heavy-deps discipline and stays importable anywhere.
The crews drive the LLM themselves (``await self.llm.aask(prompt)`` inside their
``_MeteredAction``), so the duck-typed surface this exposes — ``aask`` +
``cost_manager`` — is everything they touch.
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from typing import Any, Optional

# Per-call ceiling, matched to ollama_config._STRUCTURED_TIMEOUT_S. The bridge's
# wall-clock cap (cost_cap_seconds) is the real bound; this stops one wedged
# cloud call from eating the whole crew budget before it fails.
_BRIDGE_TIMEOUT_S = 300


class BridgeLLMError(RuntimeError):
    """A promoted bridge LLM call failed — network error, non-200 (a gate
    refusal / budget block / cloud dispatch error), or an unparseable response.
    The crew role surfaces this as an error rather than silently degrading to a
    weaker local model masquerading as a frontier answer."""


class _TokenMeter:
    """Minimal cost-manager stand-in. The crews' ``_llm_total_tokens`` reads
    ``total_prompt_tokens`` + ``total_completion_tokens`` off ``llm.cost_manager``
    — provide exactly those so promoted-role token telemetry keeps working."""

    def __init__(self) -> None:
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0

    def add(self, prompt_tokens: Any, completion_tokens: Any) -> None:
        try:
            self.total_prompt_tokens += int(prompt_tokens or 0)
            self.total_completion_tokens += int(completion_tokens or 0)
        except (TypeError, ValueError):
            pass


def _fold_usage(cost_manager: Any, prompt_tokens: Any, completion_tokens: Any) -> None:
    """Fold one promoted call's token usage into ``cost_manager`` — BEST-EFFORT.

    The crew's ``_llm_total_tokens`` reads ``total_prompt_tokens`` +
    ``total_completion_tokens`` off ``llm.cost_manager``. That object is our
    :class:`_TokenMeter` UNTIL MetaGPT's Role machinery binds its OWN ``CostManager``
    (same two fields, but NO ``add`` method) — so prefer ``add`` when present, else
    increment the fields directly. Metering must NEVER crash an already-successful
    generation: the live promotion smoke (2026-06-17) died here on ``CostManager.add``
    AFTER a good cloud dispatch, throwing the frontier answer away."""
    try:
        adder = getattr(cost_manager, "add", None)
        if callable(adder):
            adder(prompt_tokens, completion_tokens)
            return
        pt = int(prompt_tokens or 0)
        ct = int(completion_tokens or 0)
        cost_manager.total_prompt_tokens = (
            getattr(cost_manager, "total_prompt_tokens", 0) or 0) + pt
        cost_manager.total_completion_tokens = (
            getattr(cost_manager, "total_completion_tokens", 0) or 0) + ct
    except Exception:
        pass  # token bookkeeping is best-effort — never fail the dispatch


class BridgeLLM:
    """Duck-typed MetaGPT-LLM stand-in for a promoted crew role.

    Surface the crews use: ``aask(prompt)`` (async, returns text) + a
    ``cost_manager`` carrying token totals. ``config`` is exposed as ``None``
    because a couple of crews ``getattr(self.llm, "config", None)`` on the
    local-structured-extraction path (which is never a promoted role)."""

    def __init__(
        self,
        *,
        bridge_url: Optional[str],
        run_id: Optional[str],
        role: str,
        model: Optional[str] = None,
    ) -> None:
        if not bridge_url or not run_id:
            # The bridge only sets these when a role is genuinely promoted; a
            # missing value means a wiring bug — fail loud, never silently local.
            raise BridgeLLMError(
                f"BridgeLLM for role {role!r} is missing bridge_url/run_id — "
                "promotion was not wired correctly bridge-side"
            )
        self.bridge_url = bridge_url
        self.run_id = run_id
        self.role = role
        self.model = model            # informational only; the bridge re-derives it
        self.cost_manager = _TokenMeter()
        self.config = None

    async def aask(
        self,
        msg: Any,
        system_msgs: Any = None,
        format_msgs: Any = None,   # accepted + ignored — MetaGPT call-compat
        images: Any = None,
        timeout: Any = None,
        stream: Any = None,
        **_kwargs: Any,
    ) -> str:
        """Route one generation call to the bridge.

        MetaGPT's Ollama ``aask`` DROPS ``system_msgs`` ([[crew-phase7-smoke-tuning]]);
        this threads them explicitly to the bridge as ``system``. The blocking
        POST runs off the event loop so a crew's bounded-concurrency role fan-out
        is unaffected (same pattern as ``ollama_structured_chat``)."""
        system = _join(system_msgs)
        prompt = _as_text(msg)
        data = await asyncio.to_thread(self._post, prompt, system)
        usage = data.get("usage") if isinstance(data, dict) else None
        if isinstance(usage, dict):
            _fold_usage(
                self.cost_manager,
                usage.get("prompt_tokens"), usage.get("completion_tokens"),
            )
        content = data.get("content") if isinstance(data, dict) else None
        return content or ""

    def _post(self, prompt: str, system: Optional[str]) -> dict:
        payload = json.dumps({
            "run_id": self.run_id,
            "role": self.role,
            "prompt": prompt,
            "system": system,
        }).encode("utf-8")
        req = urllib.request.Request(
            self.bridge_url, data=payload,
            headers={
                "Content-Type": "application/json",
                # The bridge's CSRF guard rejects state-changing requests that
                # carry no allowed Origin and no same-origin/non-browser
                # Sec-Fetch-Site attestation. The crew subprocess is a loopback,
                # non-browser client — so it attests "none", the sanctioned
                # non-browser signal (found by the live promotion smoke
                # 2026-06-17: without it /api/crew/_llm returns 403 forbidden_origin).
                "Sec-Fetch-Site": "none",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=_BRIDGE_TIMEOUT_S) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:300]
            except Exception:  # noqa: BLE001 — best-effort error body
                pass
            raise BridgeLLMError(
                f"bridge /api/crew/_llm returned {e.code} for role "
                f"{self.role!r}: {detail}"
            ) from e
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
            raise BridgeLLMError(
                f"bridge /api/crew/_llm call failed for role {self.role!r}: {e}"
            ) from e


def _join(system_msgs: Any) -> Optional[str]:
    if not system_msgs:
        return None
    if isinstance(system_msgs, (list, tuple)):
        return "\n".join(str(m) for m in system_msgs)
    return str(system_msgs)


def _as_text(msg: Any) -> str:
    if isinstance(msg, str):
        return msg
    if isinstance(msg, (list, tuple)):
        return "\n".join(str(m) for m in msg)
    return str(msg)


__all__ = ["BridgeLLM", "BridgeLLMError"]
