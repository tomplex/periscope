"""Spawn-into-workspace integration test (real tmux, gated on @needs_tmux)."""

import shutil

import pytest

needs_tmux = pytest.mark.skipif(not shutil.which("tmux"), reason="needs tmux")


@needs_tmux
def test_spawn_into_workspace_tags_pane(clean_state, fresh_activity_db,
                                        tmux_test_server, tmp_git_repo, monkeypatch):
    from periscope import activity, worktree_spawn
    from periscope.workspaces import create_workspace
    # Redirect the worktree into tmp_git_repo's parent so `git worktree add`
    # runs for real without polluting ~/dev/worktrees (mirrors test_open_ops).
    wt_dest = str(tmp_git_repo.parent / "wt-ws-feature")
    monkeypatch.setattr(worktree_spawn, "worktree_path", lambda repo, slug: wt_dest)

    ws = create_workspace(name="WS", base_repo=str(tmp_git_repo))
    from periscope.routes.workspaces import SpawnBody, workspaces_spawn
    result = workspaces_spawn(SpawnBody(workspace_id=ws["id"], branch="ws-feature"))
    pane_id = result["pane_id"]
    assert activity.get_pane_workspace(pane_id) == ws["id"]
