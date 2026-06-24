"""Workspaces REST routes.

Create (with optional initial pane tags = "promote"), patch, archive, tag,
untag, and spawn-into-workspace. The entity lives in periscope.workspaces;
the per-tab tag map lives in periscope.activity (pane_workspaces table).
"""
import subprocess

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from periscope import activity, open_ops, worktree_spawn
from periscope.workspaces import (
    archive_workspace,
    create_workspace,
    get_workspace,
    update_workspace,
)
from periscope.workspaces import get_workspace as _get_ws

router = APIRouter()


class CreateBody(BaseModel):
    name: str
    base_repo: str | None = None
    base_worktree: str | None = None
    tag_panes: list[str] | None = None  # promote: tag these pane_ids on create


@router.post("/api/workspaces")
def workspaces_create(body: CreateBody):
    ws = create_workspace(
        name=body.name, base_repo=body.base_repo, base_worktree=body.base_worktree,
    )
    for pane_id in body.tag_panes or []:
        activity.set_pane_workspace(pane_id, ws["id"])
    return {"ok": True, **ws}


class PatchBody(BaseModel):
    workspace_id: str
    name: str | None = None
    base_repo: str | None = None
    base_worktree: str | None = None


@router.post("/api/workspaces/patch")
def workspaces_patch(body: PatchBody):
    fields = {k: v for k, v in body.model_dump(exclude={"workspace_id"}).items()
              if k in body.model_fields_set}
    if not update_workspace(body.workspace_id, **fields):
        raise HTTPException(404, f"no workspace {body.workspace_id!r}")
    return {"ok": True, **get_workspace(body.workspace_id)}


class ArchiveBody(BaseModel):
    workspace_id: str


@router.post("/api/workspaces/archive")
def workspaces_archive(body: ArchiveBody):
    if not archive_workspace(body.workspace_id):
        raise HTTPException(404, f"no workspace {body.workspace_id!r}")
    return {"ok": True, **get_workspace(body.workspace_id)}


class TagBody(BaseModel):
    workspace_id: str
    pane_id: str


@router.post("/api/workspaces/tag")
def workspaces_tag(body: TagBody):
    if not get_workspace(body.workspace_id):
        raise HTTPException(404, f"no workspace {body.workspace_id!r}")
    activity.set_pane_workspace(body.pane_id, body.workspace_id)
    return {"ok": True}


class UntagBody(BaseModel):
    pane_id: str


@router.post("/api/workspaces/untag")
def workspaces_untag(body: UntagBody):
    activity.set_pane_workspace(body.pane_id, None)
    return {"ok": True}


class SpawnBody(BaseModel):
    workspace_id: str
    branch: str


@router.post("/api/workspaces/spawn")
def workspaces_spawn(body: SpawnBody):
    ws = _get_ws(body.workspace_id)
    if not ws:
        raise HTTPException(404, f"no workspace {body.workspace_id!r}")
    base_repo = ws.get("base_repo")
    if not base_repo:
        raise HTTPException(400, "workspace has no base_repo to spawn from")
    base_branch = None
    base_wt = ws.get("base_worktree")
    if base_wt:
        # base_worktree is a PATH; spawn_worktree wants a branch NAME.
        out = subprocess.run(
            ["git", "-C", base_wt, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True,
        )
        base_branch = out.stdout.strip() or None
    spawn = worktree_spawn.spawn_worktree(
        base_repo, body.branch, base_branch=base_branch, fetch=False,
    )
    result = open_ops.open_target(open_ops.PathTarget(path=spawn["path"]))
    activity.set_pane_workspace(result.claude_pane_id, body.workspace_id)
    return {"ok": True, "workspace_id": body.workspace_id,
            "pane_id": result.claude_pane_id, "path": spawn["path"], "ui": result.ui}
