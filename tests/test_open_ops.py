import subprocess

import pytest
from periscope import open_ops, projects


def test_fetch_pr_into_worktree_returns_metadata(tmp_git_repo, monkeypatch):
    # Redirect worktree_path to a location inside tmp_git_repo so git worktree
    # add runs for real without touching ~/dev/worktrees.
    wt_dest = str(tmp_git_repo.parent / "wt-pr-7")
    monkeypatch.setattr(projects, "worktree_path", lambda repo, slug: wt_dest)
    # Create the local branch that worktree add will check out — normally
    # created by _fetch_pr_branch (which we stub out).
    subprocess.run(
        ["git", "branch", "pr-7"],
        cwd=tmp_git_repo, check=True, capture_output=True,
    )
    monkeypatch.setattr(projects, "_resolve_pr_metadata",
        lambda repo, pr: {"headRefName": "pr-7", "isCrossRepository": False,
                          "baseRefName": "main", "state": "OPEN"})
    monkeypatch.setattr(projects, "_fetch_pr_branch", lambda *a, **k: None)
    res = projects.fetch_pr_into_worktree(str(tmp_git_repo), 7)
    assert res.path and res.base_branch == "main" and res.is_fork is False


def test_ensure_project_registers_when_absent(tmp_git_repo, clean_state):
    repo = str(tmp_git_repo)
    proj = open_ops.ensure_project(repo, repo)
    assert proj["tmux_session"] and proj["repo"] == repo
    assert repo in projects.all_projects()


def test_ensure_project_idempotent_no_409(tmp_git_repo, clean_state):
    repo = str(tmp_git_repo)
    first = open_ops.ensure_project(repo, repo)
    again = open_ops.ensure_project(repo, repo)   # must NOT raise
    assert again["tmux_session"] == first["tmux_session"]
