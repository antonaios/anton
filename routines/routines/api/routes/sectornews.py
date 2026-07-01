"""Endpoints for the sector-news routine.

#21 polish — canonical route is ``POST /api/workflows/sector-news``, mirroring
the post-#60 surface used by the other migrated skills (``/api/workflows/lbo``,
``/api/workflows/comps``, ``/api/workflows/equity-research``,
``/api/workflows/pdf-intake``). The legacy ``POST /api/sector-news/run`` path
is preserved as a deprecated alias for ONE cycle so the dashboard's
``sectorNewsRun()`` helper (``dashboard/src/lib/api.ts``) keeps working until
the operator flips the .tsx caller; both paths dispatch to the same handler.
"""

from __future__ import annotations

import subprocess
import sys
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, field_validator

from routines.api.deps import ROUTINES_REPO
from routines.api.job_registry import JobConcurrencyExceeded, launch_tracked
from routines.hooks import tool_call_hooks
from routines.sectornews.sources import validate_sector_slug

router = APIRouter()


class SectorNewsRequest(BaseModel):
    sector: Optional[str] = None  # if None -> run-all
    days: int = 7
    limit: int = 15

    @field_validator("sector")
    @classmethod
    def _sector_is_a_name_not_a_path(cls, v: Optional[str]) -> Optional[str]:
        # #sec-read-path-policy: refuse traversal-shaped sector values with a
        # clean 422 BEFORE the subprocess fires. The chokepoint validation in
        # ``load_sector_config`` covers CLI/cron callers too, but from this
        # route it would only fail asynchronously inside the spawned run.
        return v if v is None else validate_sector_slug(v)


class JobStarted(BaseModel):
    status: str
    pid: Optional[int] = None
    detail: Optional[str] = None


def _run_sector_news(req: SectorNewsRequest) -> JobStarted:
    """Long-running — fire subprocess and return PID."""
    if req.sector:
        # F-26 (option-injection): ``req.sector`` is a caller-supplied POSITIONAL
        # arg. Put the options FIRST, then ``--`` to end option parsing, then the
        # sector — so a value like ``--days=999999`` or ``--help`` is treated as
        # the positional, never as an injected flag.
        cmd = [
            sys.executable,
            "-m",
            "routines.sectornews.cli",
            "run",
            "--days",
            str(req.days),
            "--limit",
            str(req.limit),
            "--",
            req.sector,
        ]
    else:
        cmd = [
            sys.executable,
            "-m",
            "routines.sectornews.cli",
            "run-all",
            "--days",
            str(req.days),
            "--limit",
            str(req.limit),
        ]
    with tool_call_hooks(
        tool_name="sector_news_run",
        sensitivity="public",
        tool_input=req.model_dump(),
    ) as ctx:
        try:
            # AUTHZ-VULN-13 (Shannon run #2): launch through the concurrency
            # registry (like earnings / recall / memory-promote) so a CSRF or
            # double-fire loop can't spawn UNBOUNDED detached sector-news
            # subprocesses — each one burns operator Firecrawl/Tavily API credit.
            # Cap is 1 in-flight per kind; a second concurrent launch → 409.
            proc = launch_tracked(
                "sector-news",
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


@router.post("/workflows/sector-news", response_model=JobStarted)
def workflow_sector_news(req: SectorNewsRequest) -> JobStarted:
    """Canonical post-#60 surface. See module docstring."""
    return _run_sector_news(req)


@router.post(
    "/sector-news/run",
    response_model=JobStarted,
    deprecated=True,
    include_in_schema=True,
)
def sector_news_run(req: SectorNewsRequest) -> JobStarted:
    """Deprecated alias — use ``/api/workflows/sector-news`` instead.

    Kept for one cycle so ``dashboard/src/lib/api.ts``'s ``sectorNewsRun()``
    keeps working until the operator updates the .tsx caller.
    """
    return _run_sector_news(req)
