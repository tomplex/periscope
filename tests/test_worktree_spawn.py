"""Tests for periscope.worktree_spawn."""

from tests.conftest import needs_tmux


@needs_tmux
def test_layout_two_window_stamps_both_windows(tmp_git_repo, tmux_test_server):
    from periscope import config
    from periscope.tmux import tmux
    from periscope.worktree_spawn import _layout_two_window
    session = config.MANAGED_SESSION
    claude_pid, shell_pid = _layout_two_window(session, str(tmp_git_repo))
    assert claude_pid and shell_pid and claude_pid != shell_pid
    # Under one shared session there can be many windows named "claude"/"shell",
    # so look up the stamped ids by window id, not by session:name (ambiguous).
    stamped = {}
    for row in tmux("list-windows", "-t", session, "-F",
                    "#{window_id} #{@periscope_id}").split("\n"):
        if not row.strip():
            continue
        wid, _, pid = row.partition(" ")
        stamped[wid] = pid.strip()
    pids = [p for p in stamped.values() if p]
    assert claude_pid in pids and shell_pid in pids
