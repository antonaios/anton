"""Endpoints for the earnings tracker routine (#44).

``POST /api/workflows/earnings`` — manual fire of the calendar-driven earnings
pipeline, mirroring the post-#60 workflow surface used by the other migrated
routines (``/api/workflows/sector-news``, ``/api/workflows/lbo``, …).

The pipeline is long-running (Firecrawl fetch + local-Ollama extraction per due
company), so — exactly like ``sector_news`` — this fires the CLI as a detached
subprocess and returns the PID immediately rather than blocking the request.
The subprocess writes its own audit row (``runs/earnings.jsonl``) and performs
the vault writes; the operator-gated variance proposals surface in
``GET /api/proposals/pending``.

Sensitivity: ``public`` — published results. Extraction still runs locally
(#no-mnpi-to-cloud — was cited as §5.4); the route just launches the CLI, so
no MNPI ever leaves the box here.
"""

from __future__ import annotations

import subprocess
import sys
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from routines.api.deps import ROUTINES_REPO
from routines.api.job_registry import JobConcurrencyExceeded, launch_tracked
from routines.hooks import tool_call_hooks

router = APIRouter(prefix="/workflows", tags=["workflows"])


class EarningsRequest(BaseModel):
    company: Optional[str] = None       # restrict to one company (note stem / name)
    as_of: Optional[str] = None         # run-date override YYYY-MM-DD; default today
    include_overdue: bool = True        # catch-up overdue companies (the default sweep)


class JobStarted(BaseModel):
    status: str
    pid: Optional[int] = None
    detail: Optional[str] = None


def _build_cmd(req: EarningsRequest) -> list[str]:
    # F-26 (option-injection): ``company`` / ``as_of`` are caller-supplied
    # values for flagged args. A value starting with ``-`` (e.g. ``--as-of``)
    # could be mis-parsed as another option by the CLI's argparse. Reject those
    # outright — neither a real company name nor an ISO date starts with ``-``.
    for label, value in (("company", req.company), ("as_of", req.as_of)):
        if value is not None and value.startswith("-"):
            raise HTTPException(
                status_code=422,
                detail=f"{label} must not start with '-' (option-injection guard)",
            )
    cmd = [sys.executable, "-m", "routines.earnings.cli", "run"]
    if req.company:
        cmd += ["--company", req.company]
    if req.as_of:
        cmd += ["--as-of", req.as_of]
    if not req.include_overdue:
        cmd.append("--no-overdue")
    return cmd


@router.post("/earnings", response_model=JobStarted)
def workflow_earnings(req: EarningsRequest) -> JobStarted:
    """Fire the earnings pipeline as a detached subprocess; return its PID."""
    cmd = _build_cmd(req)
    with tool_call_hooks(
        tool_name="earnings_run",
        sensitivity="public",
        tool_input=req.model_dump(),
    ) as ctx:
        try:
            # F-17 (HR S-12): launch through the concurrency registry so a
            # CSRF/double-fire loop can't spawn unbounded detached subprocesses.
            proc = launch_tracked(
                "earnings",
                cmd,
                cwd=str(ROUTINES_REPO),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except JobConcurrencyExceeded as e:
            raise HTTPException(status_code=409, detail=str(e)) from e
        except OSError as e:
            raise HTTPException(status_code=500, detail=f"Failed to launch: {e}") from e
        result = JobStarted(status="started", pid=proc.pid)
        ctx.result = result.model_dump()
        return result


__all__ = ["router"]
