"""POST /api/open + GET /api/open/catalog — thin shim over open_ops."""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from periscope import open_ops

router = APIRouter()


class OpenBody(BaseModel):
    path: str | None = None
    repo: str | None = None
    branch: str | None = None
    pr: int | None = None


def _to_descriptor(b: OpenBody) -> open_ops.Descriptor:
    if b.path and not (b.repo or b.branch or b.pr):
        return open_ops.PathTarget(path=b.path)
    if b.repo and b.branch and b.pr is None and not b.path:
        return open_ops.BranchTarget(repo=b.repo, branch=b.branch)
    if b.repo and b.pr is not None and not (b.path or b.branch):
        return open_ops.PRTarget(repo=b.repo, pr=b.pr)
    raise HTTPException(400, "exactly one of {path | repo+branch | repo+pr} required")


@router.post("/api/open")
def open_endpoint(body: OpenBody):
    descriptor = _to_descriptor(body)
    try:
        result = open_ops.open_target(descriptor)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"tmux_session": result.tmux_session, "repo": result.repo,
            "claude_pid": result.claude_pid, "ui": result.ui}


@router.get("/api/open/catalog")
def open_catalog():
    return open_ops.build_catalog()
