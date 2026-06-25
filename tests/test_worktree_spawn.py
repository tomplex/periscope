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


@needs_tmux
def test_layout_two_window_stamps_recency_by_session_index(tmp_git_repo, tmux_test_server):
    """The new claude window's recency must land on the session:index key the
    rail sort reads (window_view.py) — not the window id. A window-id-keyed
    note_action would silently lose the action bump (focused_at self-heals on
    the next poll; acted_at never re-derives)."""
    from periscope import config, panes
    from periscope.tmux import tmux
    from periscope.worktree_spawn import _layout_two_window
    session = config.MANAGED_SESSION
    claude_pid, _ = _layout_two_window(session, str(tmp_git_repo))
    # Find the freshly-stamped claude window's index via its @periscope_id.
    idx = ""
    for row in tmux("list-windows", "-t", session, "-F",
                    "#{window_index} #{@periscope_id}").split("\n"):
        wi, _, pid = row.partition(" ")
        if pid.strip() == claude_pid:
            idx = wi.strip()
            break
    assert idx, "claude window not found by periscope id"
    stamps = panes.recency_stamps_for(f"{session}:{idx}")
    assert stamps["acted_at"] > 0 and stamps["focused_at"] > 0
