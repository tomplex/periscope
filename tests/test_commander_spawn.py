import shutil
import pytest

from periscope import commander, activity

needs_tmux = pytest.mark.skipif(not shutil.which("tmux"), reason="tmux not installed")


@needs_tmux
def test_spawn_commander_marks_a_live_pane(tmux_test_server, fresh_activity_db, monkeypatch):
    # tmux_test_server sets PERISCOPE_TMUX_SOCKET (isolated -L) + PERISCOPE_CLAUDE_EXEC=cat
    monkeypatch.setattr("periscope.config.is_prod", lambda: True)  # allow spawn under test
    from periscope.panes import list_windows
    import periscope.tmux as tmuxmod

    # Capture the send-keys launch command so we can assert the model + lockdown
    # flags land on the spawned claude (read-only orchestrator).
    sent = []
    real_mutate = tmuxmod._tmux_mutate

    def spy(*args, **kwargs):
        if args and args[0] == "send-keys":
            sent.append(args)
        return real_mutate(*args, **kwargs)

    monkeypatch.setattr(tmuxmod, "_tmux_mutate", spy)

    commander._spawn_commander(now=1)
    m1 = activity.get_commander()
    assert m1 is not None
    assert m1.pane_id   # marker set with a real pane id
    live = {w["pane_id"] for w in list_windows()}
    assert m1.pane_id in live

    launch = next(a for a in sent if any("--append-system-prompt" in str(p) for p in a))
    cmd = " ".join(str(p) for p in launch)
    assert "--model sonnet" in cmd
    assert "--disallowedTools Bash,Edit,Write" in cmd
