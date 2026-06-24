"""Cleanup verb (Verb 5): GET candidates + POST bulk archive."""

import os

from fastapi import APIRouter
from pydantic import BaseModel

from periscope.cleanup import compute_candidates
from periscope.log import log
from periscope.panes import drop_target_focus, list_windows
from periscope.projects import (
    archive_project, all_projects, MAIN_KEY, placement_kill_set,
)
from periscope.tmux import _run, _tmux_mutate
from periscope import worktrees


router = APIRouter()


@router.get("/api/cleanup/candidates")
def cleanup_candidates(repo: str | None = None):
    """List cleanup candidates. Optional `repo` query param scopes to one
    repo's worktrees (for a future per-project view; phase 6 ships the
    global view as the primary UI)."""
    repo_filter = os.path.realpath(repo) if repo else None
    cands = compute_candidates(repo_filter)
    return {"candidates": cands}


class ArchiveItem(BaseModel):
    pinned_dir: str
    delete_branch: bool = False


class ArchiveBody(BaseModel):
    candidates: list[ArchiveItem]


@router.post("/api/cleanup/archive")
def cleanup_archive(body: ArchiveBody):
    """Bulk-archive selected candidates. For each:
      1. Archive the project row (if one exists) — sets archived_at.
      2. Kill the tmux session (if one exists).
      3. `git worktree remove --force` the path.
      4. Optionally `git branch -D` the worktree's branch (opt-in per row).

    Failures on individual rows don't stop the batch — collect them and
    return alongside successes.
    """
    archived: list[str] = []
    failed: list[dict] = []

    projects = all_projects()

    for item in body.candidates:
        pinned_dir = item.pinned_dir
        try:
            row = projects.get(pinned_dir)
            if pinned_dir == MAIN_KEY:
                raise ValueError("cannot archive __main__")

            # 1. Archive project row (if one exists).
            tmux_session = row.get("tmux_session") if row else None
            repo = row.get("repo") if row else None
            if row:
                archive_project(pinned_dir)

            # 2. Kill the worktree's placement set (panes whose rail placement
            # is this project), sparing any pane dragged into a live workspace.
            # pinned_dir IS the project key, so placement resolves directly;
            # the MAIN_KEY guard above means placement_kill_set never refuses.
            if tmux_session:
                windows = [w for w in list_windows()
                           if w.get("session") == tmux_session]
                for target, _pid in placement_kill_set(pinned_dir, windows):
                    _tmux_mutate("kill-pane", "-t", target)
                    drop_target_focus(target)

            # 3. Determine the repo for worktree removal. Untracked
            # worktrees have no project.repo; derive via git from the
            # worktree path itself.
            if not repo:
                code, common = _run(
                    ["git", "-C", pinned_dir, "rev-parse", "--git-common-dir"],
                    timeout=3.0,
                )
                if code == 0 and common:
                    common_abs = (
                        common if os.path.isabs(common)
                        else os.path.join(pinned_dir, common)
                    )
                    repo = os.path.realpath(os.path.dirname(common_abs))
                else:
                    repo = pinned_dir  # fallback; git will likely error

            # Capture the branch BEFORE removing the worktree (HEAD is
            # gone once the worktree's gitdir is gone).
            branch: str | None = None
            if item.delete_branch:
                code, b = _run(
                    ["git", "-C", pinned_dir, "rev-parse", "--abbrev-ref", "HEAD"],
                    timeout=3.0,
                )
                if code == 0 and b and b != "HEAD":
                    branch = b

            # 4. Remove the worktree.
            code, out = _run(
                ["git", "-C", repo, "worktree", "remove", "--force", pinned_dir],
                timeout=10.0,
            )
            if code != 0:
                # Worktree removal can fail for various reasons (git
                # state corrupted, permission denied). Surface but don't
                # halt the batch.
                failed.append({"pinned_dir": pinned_dir, "error": f"worktree remove: {out}"})
                # Skip branch-delete if worktree removal failed —
                # the branch might still be in use.
                continue

            worktrees.invalidate(repo)

            # 5. Optional branch delete. -D is force-delete; the user
            # opted in by checking the box, so don't be defensive.
            if item.delete_branch and branch:
                code, out = _run(
                    ["git", "-C", repo, "branch", "-D", branch], timeout=3.0
                )
                if code != 0:
                    # Non-fatal — surface the warning but the worktree
                    # is already gone.
                    log.warning(
                        "cleanup: branch -D %s on %s failed: %s",
                        branch, repo, out,
                    )

            archived.append(pinned_dir)
        except Exception as e:
            failed.append({"pinned_dir": pinned_dir, "error": str(e)})

    return {"archived": archived, "failed": failed}
