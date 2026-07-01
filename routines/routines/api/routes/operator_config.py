"""Operator-config API routes (#operator-tab).

  * GET /api/operator/config            — every section's current state +
                                          per-file mtime tokens + composed
                                          provider/key STATUS (no secrets)
  * PUT /api/operator/config/{section}  — apply one section's edit to its
                                          vault file (surgical, atomic,
                                          mtime-guarded, audited)

Contract notes:

  * The vault file IS the config store — these routes edit it in place;
    the Obsidian round-trip keeps working.
  * mtime tokens are STRINGS (``st_mtime_ns`` exceeds JS safe-integer
    range). PUT must echo the token from the last GET; a mismatch → 409
    with the current token so the tab can re-fetch.
  * The credentials block is STATUS ONLY in v1 — summaries from the #25
    encrypted store (never secret material), env-key presence booleans,
    Ollama reachability. Key WRITES stay on ``/api/credentials`` (v2 puts
    a form in front of it).
  * Loopback-only, same belt-and-braces guard as the credentials routes —
    this surface writes operator config and reads provider posture.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, ValidationError

from routines.api import deps
from routines.operatorconfig.blocks import SectionBlockNotFound
from routines.operatorconfig.models import SECTION_MODELS, WRITABLE_SECTIONS
from routines.operatorconfig.profile_edit import ProfileEditError
from routines.operatorconfig.store import ConflictError, read_config, write_section

log = logging.getLogger(__name__)


# Loopback-only guard — same invariant as routes/credentials.py (#25b):
# the bridge has no auth, so a non-loopback bind must not expose a
# config-write surface. Duplicated rather than imported (that module's
# guard is private; neither route should depend on the other's internals).
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "testclient"})


def _loopback_only(request: Request) -> None:
    client_host = request.client.host if request.client else None
    if client_host not in _LOOPBACK_HOSTS:
        log.warning(
            "operator-config endpoint refused non-loopback connection from %r",
            client_host,
        )
        raise HTTPException(
            status_code=403,
            detail=(
                f"operator-config endpoints are loopback-only; refusing "
                f"connection from {client_host!r}"
            ),
        )


router = APIRouter(dependencies=[Depends(_loopback_only)])


# ── Provider / key status (read-only composition) ────────────────────────


def _env_key_present(*names: str) -> bool:
    return any(os.environ.get(n, "").strip() for n in names)


def _credentials_status() -> dict[str, Any]:
    """STATUS ONLY — never secret material. Each sub-probe degrades to an
    ``error`` field rather than failing the whole GET."""
    out: dict[str, Any] = {}

    try:
        from routines.credentials import get_store

        out["credentials"] = [
            {
                "provider": s.provider,
                "kind": s.kind,
                "created": s.created,
                "last_used": s.last_used,
                "expires_at": s.expires_at,
            }
            for s in get_store().list_summaries()
        ]
    except Exception as e:  # noqa: BLE001 — DPAPI/store issues must not 500 the GET
        # Coarse code only on the wire (codex SEV-2 — raw exception strings
        # can embed paths/URLs/token fragments); detail goes to the log.
        log.warning("operator-config: credential store unavailable: %s", e)
        out["credentials"] = []
        out["credentials_error"] = "store_unavailable"

    # #operator-tab v2 — per-known-provider effective-source view (store /
    # store-over-env / env / none) from the env bridge. STATUS only.
    try:
        from routines.credentials.env_bridge import key_status

        out["keys"] = key_status()
        # TAVILY_API_KEYS (multi-key CSV form) isn't bridged — surface its
        # presence so the tavily row doesn't read "none" misleadingly.
        if (
            out["keys"].get("tavily", {}).get("effective") == "none"
            and _env_key_present("TAVILY_API_KEYS")
        ):
            out["keys"]["tavily"]["env"] = True
            out["keys"]["tavily"]["effective"] = "env"
    except Exception as e:  # noqa: BLE001
        log.warning("operator-config: key status failed: %s", e)
        out["keys"] = {}

    try:
        from routines.shared.ollama_client import OllamaClient

        h = OllamaClient(timeout=5).health()
        out["ollama"] = {
            "reachable": True,
            "version": h.get("version"),
            "models": h.get("models", []),
        }
    except Exception as e:  # noqa: BLE001
        log.info("operator-config: ollama health probe failed: %s", e)
        out["ollama"] = {"reachable": False, "error": "health_check_failed"}

    overrides = deps.VAULT / "_claude" / "provider_overrides.yaml"
    out["provider_overrides"] = {
        "path": "_claude/provider_overrides.yaml",
        "exists": overrides.exists(),
    }

    # claude / codex CLIs authenticate via their own login flows — the tab
    # only states that; there is no probe-able auth status here in v1.
    out["cli_auth"] = "terminal-managed"
    return out


# ── Routes ───────────────────────────────────────────────────────────────


@router.get("/operator/config")
def get_operator_config() -> dict[str, Any]:
    payload = read_config(deps.VAULT)
    payload["sections"]["credentials"] = _credentials_status()
    payload["writable_sections"] = list(WRITABLE_SECTIONS)
    return payload


class PutSectionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_mtime: Optional[str] = None   # token from the last GET (None = file absent)
    data: dict[str, Any]


@router.put("/operator/config/{section}")
def put_operator_config(section: str, payload: PutSectionPayload) -> dict[str, Any]:
    model_cls = SECTION_MODELS.get(section)
    if model_cls is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"unknown section {section!r} — writable sections: "
                f"{', '.join(WRITABLE_SECTIONS)}"
            ),
        )

    try:
        data = model_cls(**payload.data)
    except ValidationError as e:
        # include_context carries raw exception objects — not JSON-safe.
        raise HTTPException(
            status_code=422,
            detail=e.errors(include_url=False, include_context=False),
        ) from e

    try:
        info = write_section(
            deps.VAULT,
            section,
            data,
            expected_mtime=payload.expected_mtime,
            audit_dir=deps.RUNS_DIR,
        )
    except ConflictError as e:
        raise HTTPException(
            status_code=409,
            detail={
                "message": str(e),
                "current": e.current.as_dict(),
            },
        ) from e
    except ProfileEditError as e:
        # The file's shape defeats safe line surgery (structural comments
        # in a list block, missing key). Honest refusal beats mangling.
        raise HTTPException(status_code=422, detail=str(e)) from e
    except SectionBlockNotFound as e:
        # The file exists but its `## section` / fenced block is missing
        # or malformed — a hand-edit casualty. 422 with the section name
        # (safe), not a 500.
        raise HTTPException(
            status_code=422,
            detail=f"config file shape problem: {e} — restore the section "
                   "heading + fenced YAML block in Obsidian, then retry",
        ) from e

    return {"ok": True, "section": section, "file": info.as_dict()}
