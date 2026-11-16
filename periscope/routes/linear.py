"""POST /api/linear/start — another app (todo-hub) starts Claude on a Linear issue's branch.

The steps are /api/reviews/prepare's spawn path: worktree, Claude with a prompt, rail placement,
plus the Linear link on the new pane. A Claude already on the branch is only reported (mode
"focused"): its prompt is never pasted into a session that may be busy. Selecting the pane is a
separate request (POST /api/select), sent when the user asks for it.
"""

import asyncio
import os
import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from periscope import channels, open_ops, reviews, state_hub, store, tracks, worktree_spawn
from periscope.log import log
from periscope.panes import list_windows

router = APIRouter()

# A start for the same (repo, branch) while one runs: a double press in the caller.
_starting: set[tuple[str, str]] = set()
# A spawned Claude shows as the owner only once its channel attaches and the next state lands
# (~10-20 s after the request returns); a start in that window is refused, not spawned twice.
_recent_spawn: dict[tuple[str, str], float] = {}
RECENT_SPAWN_S = 60.0


class StartBody(BaseModel):
    repo: str = Field(min_length=1)  # a main checkout, as GET /api/open/catalog lists it
    branch: str = Field(min_length=1)
    ticket: str = Field(min_length=1)
    title: str = ""
    prompt: str = Field(min_length=1)


@router.post("/api/linear/start")
async def linear_start(body: StartBody):
    key = (os.path.realpath(body.repo), body.branch)
    if key in _starting:
        raise HTTPException(409, "start already running")
    _starting.add(key)
    try:
        return await _start(body, key)
    finally:
        _starting.discard(key)


async def _start(body: StartBody, key: tuple[str, str]) -> dict:
    repo, branch = key
    owner = reviews.owner_for(state_hub.last_windows(), os.path.basename(repo), branch)
    if owner:
        _recent_spawn.pop(key, None)
        return {"mode": "focused", "pid": owner["pid"]}
    age = time.monotonic() - _recent_spawn.get(key, -RECENT_SPAWN_S)
    if age < RECENT_SPAWN_S:
        raise HTTPException(409, f"a pane was spawned {int(age)} s ago and hasn't attached yet")
    try:
        wt = await asyncio.to_thread(worktree_spawn.spawn_worktree, repo, branch)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    anchored = await asyncio.to_thread(open_ops.resolve_worktree_session, wt["path"])
    if not anchored:
        raise HTTPException(502, f"{wt['path']} is not inside a git repo")
    session, project = anchored
    try:
        spawned = await channels.spawn_claude_with_prompt(
            wt["path"], tracks.repo_default_track(project["repo"]), body.prompt, session=session
        )
    except channels.SpawnFailed as e:
        raise HTTPException(502, f"spawn failed: {e}") from e
    _recent_spawn[key] = time.monotonic()
    store.set_window_fields(spawned["pid"], linked_linear=body.ticket, linked_linear_title=body.title or None)

    # Same server-side rail placement as prepare's spawn.
    def place() -> None:
        pane_pids = [w["pid_raw"] for w in list_windows() if w["session"] == session and w.get("pid_raw")]
        open_ops.place_in_rail(session, project, pane_pids or [spawned["pid"]])

    try:
        await asyncio.to_thread(place)
    except Exception:
        # Placement only orders the rail; the Claude exists, and an error would invite a second spawn.
        log.warning("linear start: rail placement failed for %s %s", repo, branch, exc_info=True)
    return {"mode": "spawned", "pid": spawned["pid"], "path": wt["path"], "reused": wt["reused"]}
