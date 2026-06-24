import shutil
import pytest

from periscope import commander, activity

needs_tmux = pytest.mark.skipif(not shutil.which("tmux"), reason="tmux not installed")


@needs_tmux
def test_spawn_commander_marks_a_live_pane(tmux_test_server, fresh_activity_db, monkeypatch):
    # tmux_test_server sets PERISCOPE_TMUX_SOCKET (isolated -L) + PERISCOPE_CLAUDE_EXEC=cat
    monkeypatch.setattr("periscope.config.is_prod", lambda: True)  # allow spawn under test
    from periscope.panes import list_windows

    commander._spawn_commander(now=1)
    m1 = activity.get_commander()
    assert m1 is not None
    live = {w["pane_id"] for w in list_windows()}
    assert m1.pane_id in live
