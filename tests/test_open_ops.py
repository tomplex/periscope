import os
import shutil
import subprocess

import pytest
from periscope import open_ops, projects
from periscope.tmux import _tmux_mutate

needs_tmux = pytest.mark.skipif(not shutil.which("tmux"), reason="tmux not installed")


def test_fetch_pr_into_worktree_rejects_non_positive_pr(tmp_git_repo):
    with pytest.raises(ValueError, match="pr must be positive"):
        projects.fetch_pr_into_worktree(str(tmp_git_repo), 0)


def test_fetch_pr_into_worktree_worktree_add_failure_cleans_up_branch(
    tmp_git_repo, monkeypatch
):
    """If git worktree add fails after the branch was fetched, the orphan
    local branch is deleted so a retry doesn't hit a non-fast-forward 409.
    This is the critical rollback path that lived in the old pr-review route.
    """
    # Create the local branch (simulating a successful _fetch_pr_branch).
    subprocess.run(
        ["git", "branch", "pr-42"],
        cwd=tmp_git_repo, check=True, capture_output=True,
    )
    wt_dest = str(tmp_git_repo.parent / "wt-pr-42")
    monkeypatch.setattr(projects, "worktree_path", lambda repo, slug: wt_dest)
    monkeypatch.setattr(projects, "_resolve_pr_metadata",
        lambda repo, pr: {"headRefName": "feature-x", "isCrossRepository": False,
                          "baseRefName": "main", "state": "OPEN"})
    monkeypatch.setattr(projects, "_fetch_pr_branch", lambda *a, **k: None)

    # Stub _run: let mkdir calls (routed via Path.mkdir, not _run) pass, but
    # make the git worktree add call fail by intercepting it in _run.
    real_run = projects._run

    def patched_run(cmd, **kwargs):
        if "worktree" in cmd and "add" in cmd:
            return (1, "fatal: simulated worktree add failure")
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(projects, "_run", patched_run)

    from fastapi import HTTPException as FastHTTPException
    with pytest.raises(FastHTTPException) as exc_info:
        projects.fetch_pr_into_worktree(str(tmp_git_repo), 42)
    assert exc_info.value.status_code == 500

    # The orphan branch must be gone — confirmed by running git branch.
    result = subprocess.run(
        ["git", "-C", str(tmp_git_repo), "branch", "--list", "pr-42"],
        capture_output=True, text=True,
    )
    assert "pr-42" not in result.stdout, "orphan branch was not cleaned up after worktree add failure"


def test_fetch_pr_into_worktree_409_if_wt_path_exists(tmp_git_repo, monkeypatch):
    """If the target worktree path already exists on disk (e.g. a duplicate
    PR review attempt), fetch_pr_into_worktree raises HTTPException(409) before
    calling git worktree add — so the existing worktree is never clobbered.
    """
    existing_wt = str(tmp_git_repo.parent / "wt-pr-99")
    os.makedirs(existing_wt, exist_ok=True)
    monkeypatch.setattr(projects, "worktree_path", lambda repo, slug: existing_wt)
    monkeypatch.setattr(projects, "_resolve_pr_metadata",
        lambda repo, pr: {"headRefName": "dup-branch", "isCrossRepository": False,
                          "baseRefName": "main", "state": "OPEN"})
    monkeypatch.setattr(projects, "_fetch_pr_branch", lambda *a, **k: None)

    from fastapi import HTTPException as FastHTTPException
    with pytest.raises(FastHTTPException) as exc_info:
        projects.fetch_pr_into_worktree(str(tmp_git_repo), 99)
    assert exc_info.value.status_code == 409
    assert "already exists" in exc_info.value.detail


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


@needs_tmux
def test_ensure_session_spawns_when_dead(tmp_git_repo, clean_state, tmux_test_server):
    repo = str(tmp_git_repo)
    proj = open_ops.ensure_project(repo, repo)
    session, claude_pid = open_ops.ensure_session(proj, repo)
    assert session == proj["tmux_session"] and claude_pid
    assert _tmux_mutate("has-session", "-t", session)[0] is True


@needs_tmux
def test_ensure_session_focuses_when_live_and_ours(tmp_git_repo, clean_state, tmux_test_server):
    repo = str(tmp_git_repo)
    proj = open_ops.ensure_project(repo, repo)
    s1, pid1 = open_ops.ensure_session(proj, repo)
    s2, pid2 = open_ops.ensure_session(proj, repo)   # must NOT spawn a 2nd session
    assert s1 == s2 and pid1 == pid2


@needs_tmux
def test_ensure_session_dedupes_foreign_name(tmp_git_repo, clean_state, tmux_test_server):
    repo = str(tmp_git_repo)
    proj = open_ops.ensure_project(repo, repo)
    # Occupy the recorded name with an unrelated session in a different cwd.
    _tmux_mutate("new-session", "-d", "-s", proj["tmux_session"], "-c", "/tmp")
    session, claude_pid = open_ops.ensure_session(proj, repo)
    assert session != proj["tmux_session"]      # deduped
    assert projects.get_project(repo)["tmux_session"] == session  # row updated


def test_resolve_worktree_session_non_git_returns_none(tmp_path, clean_state):
    # No worktree to anchor a rail item to → caller falls back to its session.
    assert open_ops.resolve_worktree_session(str(tmp_path)) is None


@needs_tmux
def test_resolve_worktree_session_registers_and_keeps_name(
    tmp_git_repo, clean_state, tmux_test_server
):
    repo = str(tmp_git_repo)
    name, proj = open_ops.resolve_worktree_session(repo)
    assert name == proj["tmux_session"]          # name free → kept as-is
    assert proj["repo"] == repo
    assert repo in projects.all_projects()       # project registered
    # Creates no window — the caller spawns the pane itself.
    assert _tmux_mutate("has-session", "-t", name)[0] is False


@needs_tmux
def test_resolve_worktree_session_keeps_name_when_live_and_ours(
    tmp_git_repo, clean_state, tmux_test_server
):
    repo = str(tmp_git_repo)
    proj = open_ops.ensure_project(repo, repo)
    # A live session that already owns the worktree → caller new-tabs into it.
    _tmux_mutate("new-session", "-d", "-s", proj["tmux_session"], "-c", repo)
    name, _ = open_ops.resolve_worktree_session(repo)
    assert name == proj["tmux_session"]          # NOT deduped


@needs_tmux
def test_resolve_worktree_session_dedupes_foreign_name(
    tmp_git_repo, clean_state, tmux_test_server
):
    repo = str(tmp_git_repo)
    proj = open_ops.ensure_project(repo, repo)
    # The recorded name is occupied by an unrelated session in a different cwd.
    _tmux_mutate("new-session", "-d", "-s", proj["tmux_session"], "-c", "/tmp")
    name, _ = open_ops.resolve_worktree_session(repo)
    assert name != proj["tmux_session"]          # deduped
    assert projects.get_project(repo)["tmux_session"] == name  # row updated


def test_worktree_for_branch_matches_enumerated(tmp_git_repo, clean_state):
    repo = str(tmp_git_repo)
    from periscope.gitutil import detect_default_branch
    default = detect_default_branch(repo)
    assert open_ops.worktree_for_branch(repo, default) == os.path.realpath(repo)
    assert open_ops.worktree_for_branch(repo, "no-such-branch") is None


from periscope import store


@needs_tmux
def test_open_target_path_spawns_dormant_then_focuses(tmp_git_repo, clean_state, tmux_test_server):
    repo = str(tmp_git_repo)
    r1 = open_ops.open_target(open_ops.PathTarget(path=repo))
    assert r1.repo == repo and r1.claude_pid and r1.tmux_session
    assert r1.tmux_session in r1.ui["worktrees_by_repo"][repo]
    r2 = open_ops.open_target(open_ops.PathTarget(path=repo))   # idempotent focus
    assert r2.tmux_session == r1.tmux_session


def test_open_target_non_git_path_raises(tmp_path, clean_state):
    with pytest.raises(ValueError):
        open_ops.open_target(open_ops.PathTarget(path=str(tmp_path)))


@needs_tmux
def test_open_target_pr_stamps_linked_pr(tmp_git_repo, clean_state, tmux_test_server, monkeypatch):
    repo = str(tmp_git_repo)
    monkeypatch.setattr(projects, "fetch_pr_into_worktree",
        lambda r, pr: projects.PRWorktree(path=repo, base_branch="main",
                                          is_fork=False, local_branch="pr-9",
                                          pr_state="OPEN", name="pr-9"))
    res = open_ops.open_target(open_ops.PRTarget(repo=repo, pr=9))
    assert store.get_window(res.claude_pid).get("linked_pr") == 9


def test_place_in_rail_writes_keys(tmp_git_repo, clean_state):
    repo = str(tmp_git_repo)
    proj = open_ops.ensure_project(repo, repo)
    ui = open_ops.place_in_rail(proj["tmux_session"], proj, ["%1", "%2"])
    assert proj["repo"] in ui["repo_order"]
    assert proj["tmux_session"] in ui["worktrees_by_repo"][proj["repo"]]
    assert ui["panes_by_worktree"][proj["tmux_session"]] == ["%1", "%2"]
    assert store.get_ui() == ui          # returns exactly what was persisted


def test_build_catalog_lists_repo_and_main_worktree(tmp_git_repo, clean_state, monkeypatch):
    repo = str(tmp_git_repo)
    monkeypatch.setattr(open_ops, "_discover_repos", lambda: {repo})
    cat = open_ops.build_catalog()
    assert any(r["repo"] == repo for r in cat["repos"])
    assert any(w["path"] == os.path.realpath(repo) and w["is_main"]
               for w in cat["worktrees"])


def test_open_target_pr_rolls_back_worktree_on_open_failure(tmp_git_repo, clean_state, monkeypatch):
    repo = str(tmp_git_repo)
    discarded = {}
    monkeypatch.setattr(projects, "fetch_pr_into_worktree",
        lambda r, pr: projects.PRWorktree(path=repo, base_branch="main", is_fork=False,
                                          local_branch="pr-13", pr_state="OPEN", name="pr-13"))
    def boom(path):   # make the path-case fail
        raise RuntimeError("spawn failed")
    monkeypatch.setattr(open_ops, "_open_path", boom)
    monkeypatch.setattr(projects, "_discard_pr_worktree",
        lambda repo_, path, branch: discarded.update(repo=repo_, path=path, branch=branch))
    with pytest.raises(RuntimeError):
        open_ops.open_target(open_ops.PRTarget(repo=repo, pr=13))
    assert discarded == {"repo": repo, "path": repo, "branch": "pr-13"}
