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
    import periscope.channels as channelsmod

    # Drive the startup-dialog poll deterministically (the stub exec never paints
    # 'auto mode on'): show the folder-trust dialog first, then the mounted TUI so
    # the loop dismisses one dialog and exits fast.
    calls = {"n": 0}
    def fake_snap(_target):
        calls["n"] += 1
        return "Do you trust the files in this folder?" if calls["n"] <= 2 else "auto mode on"
    monkeypatch.setattr(channelsmod, "_plain_pane_snapshot", fake_snap)

    # Capture send-keys so we can assert the model+lockdown flags AND the trust
    # dialog's dismissal Enter, both aimed at the captured pane id.
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
    assert m1.pane_id   # marker set with the REAL created pane id (not a name)
    live = {w["pane_id"] for w in list_windows()}
    assert m1.pane_id in live

    launch = next(a for a in sent if any("--append-system-prompt" in str(p) for p in a))
    cmd = " ".join(str(p) for p in launch)
    assert "--model sonnet" in cmd
    assert "--disallowedTools Bash,Edit,Write" in cmd
    # the folder-trust dialog was dismissed with a bare Enter on the captured pane
    assert ("send-keys", "-t", m1.pane_id, "Enter") in [tuple(a) for a in sent]
