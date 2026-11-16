"""POST /api/linear/start — another app on this Mac starts Claude on a Linear issue's branch.

The route makes the worktree, spawns Claude with a prompt through branch_spawn (the spawn path of
/api/reviews/prepare), and links the Linear issue to the new pane. A Claude already on the branch is
only reported (mode "existing"): its prompt is never pasted into a session that may be busy.
Selecting the pane is a separate request (POST /api/select), sent when the user asks for it.
"""

import asyncio
import os

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from periscope import branch_spawn, reviews, state_hub, store, worktree_spawn

router = APIRouter()

_spawns = branch_spawn.BranchSpawns("start")


class StartBody(BaseModel):
    repo: str = Field(min_length=1)  # a main checkout, as GET /api/open/catalog lists it
    branch: str = Field(min_length=1)
    ticket: str = Field(min_length=1)
    title: str = ""
    prompt: str = Field(min_length=1)


@router.post("/api/linear/start")
async def linear_start(body: StartBody):
    key = (os.path.realpath(body.repo), body.branch)
    with _spawns.one_at_a_time(key):
        return await _start(body, key)


async def _start(body: StartBody, key: tuple[str, str]) -> dict:
    repo, branch = key
    owner = reviews.owner_for(await state_hub.current_windows(), os.path.basename(repo), branch)
    if owner:
        _spawns.forget_spawn(key)
        return {"mode": "existing", "pid": owner["pid"]}
    _spawns.refuse_recent_spawn(key)
    try:
        wt = await asyncio.to_thread(worktree_spawn.spawn_worktree, repo, branch)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    spawned = await _spawns.spawn(key, wt["path"], body.prompt)
    store.set_window_fields(spawned["pid"], linked_linear=body.ticket, linked_linear_title=body.title or None)
    return {"mode": "spawned", "pid": spawned["pid"], "path": wt["path"], "reused": wt["reused"]}
