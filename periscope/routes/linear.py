"""POST /api/linear/start — another app on this Mac starts Claude on a Linear issue's branch, or on a pull request.

The route makes the worktree, spawns Claude with a prompt through branch_spawn (the spawn path of
/api/reviews/prepare), and links the Linear issue to the new pane. With `pr` in place of `branch`, it checks the
pull request out as `pr-<N>` with ⌘K's PR helper, or fast-forwards the worktree already on `pr-<N>` to the PR's
head, and links the PR too. It answers with the new Claude's session id and the email of its Claude account, and
with a `warning` when a worktree problem did not stop the start. A `name` labels the new pane and pins the label.
A Claude already on the branch is only reported (mode "existing"): its prompt is never pasted into a session that
may be busy, and it keeps its name.
Selecting the pane is a separate request (POST /api/select), sent when the user asks for it.
"""

import asyncio
import os

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from periscope import branch_spawn, cleanup, projects, reviews, state_hub, store, worktree_spawn, worktrees
from periscope.repo_locks import repo_lock
from periscope.tmux import _run

router = APIRouter()


class StartBody(BaseModel):
    repo: str = Field(min_length=1)  # a main checkout, as GET /api/open/catalog lists it
    # Exactly one of: the branch to start on, or a pull request on the repo's origin, checked out as `pr-<N>`.
    branch: str = ""
    pr: int | None = Field(default=None, gt=0)
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
        path, reused, pr_fields, warning = wt["path"], wt["reused"], {}, wt.get("warning")
    else:
        path, reused, pr_fields, warning = await asyncio.to_thread(_pr_worktree, repo, body.pr, branch)
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
        **({"warning": warning} if warning else {}),
    }


def _pr_worktree(repo: str, pr: int, branch: str) -> tuple[str, bool, dict, str | None]:
    """The worktree on `branch` (`pr-<N>`), whether it was there already, the card fields for the PR, and a warning.
    Only a fetch asks GitHub whether the PR comes from a fork. The fetch's 409s become 400: this route's 409 means only
    that a start is running. A failed spawn keeps the worktree, and a retry uses it.
    A worktree already on the branch moves to the PR's head by a fast-forward. When the fetch fails, the worktree has
    local changes or the move is not a fast-forward, the worktree stays as it is and the warning tells why."""
    path = worktrees.for_branch(repo, branch)
    if not path:
        try:
            prwt = projects.fetch_pr_into_worktree(repo, pr)
        except HTTPException as e:
            if e.status_code == 409:
                raise HTTPException(400, e.detail) from e
            raise
        return prwt.path, False, {"linked_pr": pr, "is_fork": prwt.is_fork}, None
    # The fetch runs outside the repo lock, as in spawn_worktree. It writes the worktree's own FETCH_HEAD.
    code, out = _run(["git", "-C", path, "fetch", "origin", f"pull/{pr}/head"], timeout=60.0)
    if code != 0:
        problem = f"fetch failed ({out!r})"
    else:
        with repo_lock(repo):
            if _run(["git", "-C", path, "merge-base", "--is-ancestor", "FETCH_HEAD", "HEAD"])[0] == 0:
                problem = None  # The worktree has the PR's head already.
            elif cleanup._is_dirty(path):
                problem = "the worktree has local changes"
            else:
                code, _ = _run(["git", "-C", path, "merge", "--ff-only", "FETCH_HEAD"], timeout=60.0)
                problem = "it cannot fast-forward" if code else None
    warning = f"{branch} may be behind PR #{pr}: {problem}" if problem else None
    return path, True, {"linked_pr": pr}, warning
