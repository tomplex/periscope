"""Tests for periscope.worktree_spawn."""

import shutil

import pytest

needs_tmux = pytest.mark.skipif(not shutil.which("tmux"), reason="tmux not installed")


@needs_tmux
def test_layout_two_window_stamps_both_windows(tmp_git_repo, tmux_test_server):
    from periscope.tmux import tmux
    from periscope.worktree_spawn import _layout_two_window
    session = "open-test-both-stamp"
    claude_pid, shell_pid = _layout_two_window(session, str(tmp_git_repo))
    assert claude_pid and shell_pid and claude_pid != shell_pid
    for win in ("claude", "shell"):
        out = tmux("display-message", "-t", f"{session}:{win}",
                   "-p", "#{@periscope_id}").strip()
        assert out, f"{win} window not stamped"
