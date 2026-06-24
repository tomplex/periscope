import shutil
import pytest

from periscope import commander, activity

needs_tmux = pytest.mark.skipif(not shutil.which("tmux"), reason="tmux not installed")


@needs_tmux
def test_supervisor_spawns_and_respawns(tmux_test_server, fresh_activity_db, monkeypatch):
    # tmux_test_server sets PERISCOPE_TMUX_SOCKET (isolated -L) + PERISCOPE_CLAUDE_EXEC=cat
    monkeypatch.setattr("periscope.config.is_prod", lambda: True)  # allow spawn under test
    from periscope.panes import list_windows

    # First pass: no marker -> spawns a commander window + marks it.
    commander.supervisor_pass(now=1)
    m1 = activity.get_commander()
    assert m1 is not None
    live = {w["pane_id"] for w in list_windows()}
    assert m1.pane_id in live

    # Second pass: marker alive -> idempotent, no new window.
    before = len(list_windows())
    commander.supervisor_pass(now=2)
    assert len(list_windows()) == before
    assert activity.get_commander().pane_id == m1.pane_id

    # Kill the marked pane -> third pass respawns + re-marks.
    from periscope.tmux import _tmux_mutate
    _tmux_mutate("kill-window", "-t", f"{commander.COMMANDER_SESSION}:{commander.COMMANDER_WINDOW}")
    commander.supervisor_pass(now=3)
    m3 = activity.get_commander()
    assert m3 is not None and m3.pane_id in {w["pane_id"] for w in list_windows()}
