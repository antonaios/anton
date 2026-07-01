"""GET /api/drafts — list draft outputs across Projects/<X>/12 Outputs/."""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Query
from pydantic import BaseModel

from routines.api.deps import VAULT

router = APIRouter()


class DraftItem(BaseModel):
    project: str
    path: str           # relative to vault root
    name: str
    mtime: str          # ISO timestamp
    ago: str
    size_bytes: int
    ext: str            # ".md" / ".docx" / ".xlsx" / ...


class DraftsResponse(BaseModel):
    items: list[DraftItem]


# Common output subfolder names in our vault Projects/<X>/ layout.
OUTPUT_DIRS = ("12 Outputs", "Outputs", "05 Outputs")


def _ago(secs: float) -> str:
    if secs < 60: return f"{int(secs)}s ago"
    if secs < 3600: return f"{int(secs / 60)}m ago"
    if secs < 86400: return f"{int(secs / 3600)}h ago"
    return f"{int(secs / 86400)}d ago"


@router.get("/drafts", response_model=DraftsResponse)
def list_drafts(
    project: Optional[str] = Query(None, description="Filter to one project (optional)"),
    limit: int = Query(50, ge=1, le=500),
) -> DraftsResponse:
    """Walk Projects/*/12 Outputs/ (and known synonyms), return draft list.

    Sorted by mtime desc. Idempotent — read-only.
    """
    projects_root = VAULT / "Projects"
    if not projects_root.exists():
        return DraftsResponse(items=[])

    rows: list[tuple[float, DraftItem]] = []
    now = datetime.now().timestamp()

    if project is not None:
        # F-30 (read-traversal): ``project`` is a caller-supplied query param
        # that was joined onto ``projects_root`` with no validation — a value
        # like ``../../_claude`` would enumerate OUTSIDE Projects/. Resolve and
        # require the result to stay under projects_root; reject separators / ``..``.
        if any(sep in project for sep in ("/", "\\")) or ".." in project.split() or project in (".", ".."):
            return DraftsResponse(items=[])
        candidate = (projects_root / project).resolve()
        try:
            candidate.relative_to(projects_root.resolve())
        except ValueError:
            return DraftsResponse(items=[])
        project_iter = [candidate]
    else:
        project_iter = [
            p for p in projects_root.iterdir() if p.is_dir() and not p.name.startswith(".")
        ]

    for proj_dir in project_iter:
        if not proj_dir.exists() or not proj_dir.is_dir():
            continue
        for sub in OUTPUT_DIRS:
            out_dir = proj_dir / sub
            if not out_dir.exists() or not out_dir.is_dir():
                continue
            for p in out_dir.rglob("*"):
                if not p.is_file() or p.name.startswith("."):
                    continue
                try:
                    st = p.stat()
                except OSError:
                    continue
                try:
                    rel = p.relative_to(VAULT).as_posix()
                except ValueError:
                    rel = str(p)
                ago_s = now - st.st_mtime
                rows.append((
                    st.st_mtime,
                    DraftItem(
                        project=proj_dir.name,
                        path=rel,
                        name=p.name,
                        mtime=datetime.fromtimestamp(st.st_mtime).isoformat(),
                        ago=_ago(ago_s),
                        size_bytes=st.st_size,
                        ext=p.suffix.lower(),
                    ),
                ))

    rows.sort(key=lambda r: r[0], reverse=True)
    return DraftsResponse(items=[item for _, item in rows[:limit]])
