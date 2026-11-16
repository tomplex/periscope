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


class StartBody(BaseModel):
    repo: str = Field(min_length=1)  # a main checkout, as GET /api/open/catalog lists it
    branch: str = Field(min_length=1)
    ticket: str = Field(min_length=1)
    title: str = ""
    prompt: str = Field(min_length=1)


@router.post("/api/linear/start")
async def linear_start(body: StartBody):
    repo = os.path.realpath(body.repo)
    # Keyed on the label, as prepare is: it knows a repo only by its label, and the guard is shared.
    key = (os.path.basename(repo), body.branch)
    with branch_spawn.one_at_a_time(key):
        return await _start(body, repo, key)


async def _start(body: StartBody, repo: str, key: tuple[str, str]) -> dict:
    # Any Claude counts, attached or not: the route only reports it, and /api/select needs only a live pane.
    on_repo = [w for w in await state_hub.current_windows() if w.get("repo_key") == repo]
    claude = reviews.claude_on_branch(on_repo, {body.branch})
    branch_spawn.refuse_or_clear_recent_spawn(key, claude)
    if claude:
        return {"mode": "existing", "pid": claude["pid"]}
    try:
        wt = await asyncio.to_thread(worktree_spawn.spawn_worktree, repo, body.branch)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    spawned = await branch_spawn.spawn(key, wt["path"], body.prompt)
    store.set_window_fields(spawned["pid"], linked_linear=body.ticket, linked_linear_title=body.title or None)
    return {"mode": "spawned", "pid": spawned["pid"], "path": wt["path"], "reused": wt["reused"]}
