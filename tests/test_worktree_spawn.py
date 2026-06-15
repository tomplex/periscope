"""Tests for periscope.worktree_spawn."""

import pytest


def test_layout_two_window_stamps_both_windows(tmp_git_repo, tmux_test_server):
    from periscope.worktree_spawn import _layout_two_window
    from periscope.tmux import tmux
    session = "open-test-both-stamp"
    claude_pid, shell_pid = _layout_two_window(session, str(tmp_git_repo))
    assert claude_pid and shell_pid and claude_pid != shell_pid
    for win in ("claude", "shell"):
        out = tmux("display-message", "-t", f"{session}:{win}",
                   "-p", "#{@periscope_id}").strip()
        assert out, f"{win} window not stamped"
