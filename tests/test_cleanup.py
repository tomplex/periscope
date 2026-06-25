"""Unit tests for cleanup signal evaluation.

The cleanup verb keys "which panes/activity belong to this worktree" off the
worktree's cwd, NOT tmux session identity — panes collapse into one shared
session, so session-equality is broken (mass-match or never-match). These
tests pin the two cwd-keyed signals: PR-link discovery (Signal 1) and the
idle/active check (Signal 4).
"""

from periscope import cleanup


def _project(name, session, repo):
    return {
        "name": name, "tmux_session": session, "repo": repo,
        "archived_at": None, "base_branch": "main",
    }


def test_signal1_linked_pr_keys_on_worktree_cwd(mocker):
    # Two panes in ONE shared session, each at a different worktree cwd, each
    # carrying its OWN linked_pr. Evaluating worktree A must pick up A's PR,
    # never B's — session-equality would have grabbed whichever matched first.
    mocker.patch.object(cleanup, "_pr_state", return_value="MERGED")
    mocker.patch.object(cleanup, "_is_branch_merged", return_value=False)
    mocker.patch.object(cleanup, "_remote_branch_exists", return_value=True)
    mocker.patch.object(cleanup, "_last_commit_age_days", return_value=1)
    mocker.patch.object(cleanup, "_is_dirty", return_value=False)

    project_by_pinned = {
        "/repo/a": _project("a", "shared", "/repo"),
        "/repo/b": _project("b", "shared", "/repo"),
    }
    windows_snapshot = {
        "%pa": {"linked_pr": 11, "last_seen": {"session": "shared", "cwd": "/repo/a"}},
        "%pb": {"linked_pr": 22, "last_seen": {"session": "shared", "cwd": "/repo/b"}},
    }

    cand = cleanup._evaluate_worktree(
        "/repo/a", "feat-a", "/repo", "main",
        project_by_pinned, windows_snapshot,
        alive_worktree_cwds={"/repo/a", "/repo/b"},
        idle_threshold=14,
    )
    assert cand is not None
    # _pr_state called with worktree A's PR number, never B's.
    assert cleanup._pr_state.call_args.args[1] == 11
    assert any(s["kind"] == "pr_merged" and "#11" in s["label"] for s in cand["signals"])


def test_signal4_idle_keys_on_live_pane_cwd(mocker):
    # No PR/branch signals fire; the only question is active-vs-idle. A worktree
    # is "active" iff a live pane sits IN it (its realpath'd cwd is in the
    # alive set), independent of tmux session.
    mocker.patch.object(cleanup, "_pr_state", return_value=None)
    mocker.patch.object(cleanup, "_is_branch_merged", return_value=False)
    mocker.patch.object(cleanup, "_remote_branch_exists", return_value=True)
    mocker.patch.object(cleanup, "_last_commit_age_days", return_value=99)
    mocker.patch.object(cleanup, "_is_dirty", return_value=False)

    project_by_pinned = {"/repo/a": _project("a", "shared", "/repo")}

    # A live pane sits in the worktree → NOT idle → healthy → no candidate.
    assert cleanup._evaluate_worktree(
        "/repo/a", "feat-a", "/repo", "main",
        project_by_pinned, {}, alive_worktree_cwds={"/repo/a"}, idle_threshold=14,
    ) is None

    # No live pane in the worktree → idle fires.
    cand = cleanup._evaluate_worktree(
        "/repo/a", "feat-a", "/repo", "main",
        project_by_pinned, {}, alive_worktree_cwds={"/repo/other"}, idle_threshold=14,
    )
    assert cand is not None
    assert any(s["kind"] == "idle" for s in cand["signals"])
