"""POST /api/linear/start — another app on this Mac starts Claude on a Linear issue's branch, or on a pull request.

The route makes the worktree, spawns Claude with a prompt through branch_spawn (the spawn path of
/api/reviews/prepare), and links the Linear issue to the new pane. With `pr` in place of `branch`, it checks the
pull request out as `pr-<N>` with ⌘K's PR helper, or uses the worktree already on `pr-<N>`, and links the PR too.
It answers with the new Claude's session id and the email of its Claude account. A `name` labels the new pane and
pins the label. A Claude already on the branch is only reported (mode "existing"): its prompt is never pasted
into a session that may be busy, and it keeps its name.
Selecting the pane is a separate request (POST /api/select), sent when the user asks for it.
"""

import asyncio
import os

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from periscope import branch_spawn, projects, reviews, state_hub, store, worktree_spawn, worktrees

router = APIRouter()


class StartBody(BaseModel):
    repo: str = Field(min_length=1)  # a main checkout, as GET /api/open/catalog lists it
    branch: str = ""  # the branch to start on, or
    pr: int | None = Field(default=None, gt=0)  # a pull request on the repo's origin, checked out as `pr-<N>`
    ticket: str = Field(min_length=1)
    title: str = ""
    prompt: str = Field(min_length=1)
    name: str = ""  # the new pane's rail label, pinned; empty lets the narrator name it


@router.post("/api/linear/start")
async def linear_start(body: StartBody):
    if bool(body.branch) == (body.pr is not None):
        raise HTTPException(400, "send exactly one of branch and pr")
    repo = os.path.realpath(body.repo)
    branch = body.branch if body.pr is None else f"pr-{body.pr}"
    # Keyed on the label, as prepare is: it knows a repo only by its label, and the guard is shared.
    key = (os.path.basename(repo), branch)
    with branch_spawn.one_at_a_time(key):
        return await _start(body, repo, branch, key)


async def _start(body: StartBody, repo: str, branch: str, key: tuple[str, str]) -> dict:
    # Any Claude counts, attached or not: the route only reports it, and /api/select needs only a live pane.
    on_repo = [w for w in await state_hub.current_windows() if w.get("repo_key") == repo]
    claude = reviews.claude_on_branch(on_repo, {branch})
    branch_spawn.refuse_or_clear_recent_spawn(key, claude)
    if claude:
        return {"mode": "existing", "pid": claude["pid"]}
    if body.pr is None:
        try:
            wt = await asyncio.to_thread(worktree_spawn.spawn_worktree, repo, branch)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        path, reused, pr_fields = wt["path"], wt["reused"], {}
    else:
        path, reused, pr_fields = await _pr_worktree(repo, body.pr)
    spawned = await branch_spawn.spawn(key, path, body.prompt, body.name.strip() or None)
    store.set_window_fields(
        spawned["pid"], linked_linear=body.ticket, linked_linear_title=body.title or None, **pr_fields
    )
    email = await asyncio.to_thread(store.account_email, spawned["account"])
    return {
        "mode": "spawned",
        "pid": spawned["pid"],
        "path": path,
        "reused": reused,
        "session_id": spawned["session_id"],
        "email": email,
    }


async def _pr_worktree(repo: str, pr: int) -> tuple[str, bool, dict]:
    """The worktree on `pr-<N>`, whether it was there already, and the card fields for the PR. Only a fetch
    asks GitHub whether the PR comes from a fork. The fetch's 409s become 400: this route's 409 means only
    that a start is running."""
    existing = await asyncio.to_thread(worktrees.for_branch, repo, f"pr-{pr}")
    if existing:
        return existing, True, {"linked_pr": pr}
    try:
        prwt = await asyncio.to_thread(projects.fetch_pr_into_worktree, repo, pr)
    except HTTPException as e:
        if e.status_code == 409:
            raise HTTPException(400, e.detail) from e
        raise
    return prwt.path, False, {"linked_pr": pr, "is_fork": prwt.is_fork}
