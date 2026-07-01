"""Endpoints for the memory-promote routine."""

from __future__ import annotations

import subprocess
import sys
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from routines.api.deps import ROUTINES_REPO
from routines.api.job_registry import JobConcurrencyExceeded, launch_tracked
from routines.hooks import tool_call_hooks

router = APIRouter()


class PromoteRequest(BaseModel):
    stale_days: int = 30


class JobStarted(BaseModel):
    status: str
    pid: Optional[int] = None
    detail: Optional[str] = None


@router.post("/memory-promote/run-all", response_model=JobStarted)
def memory_promote_run_all(req: PromoteRequest) -> JobStarted:
    cmd = [
        sys.executable,
        "-m",
        "routines.promotion.cli",
        "run-all",
        "--stale-days",
        str(req.stale_days),
    ]
    with tool_call_hooks(
        tool_name="memory_promote",
        sensitivity="internal",   # writes proposals into the vault
        tool_input=req.model_dump(),
    ) as ctx:
        try:
            # F-17 (HR S-12): registry-gated launch — bounded concurrency so a
            # CSRF/double-fire loop can't spawn unbounded promote subprocesses.
            proc = launch_tracked(
                "memory-promote",
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
