import asyncio


def test_ensure_commander_single_flight(fresh_activity_db, monkeypatch):
    from periscope import commander, activity
    calls = {"n": 0}

    def fake_spawn(*, now):
        calls["n"] += 1
        activity.set_commander(pane_id="%7", session_id=None, at=now)

    monkeypatch.setattr(commander, "_spawn_commander", fake_spawn)
    # No live windows -> marker pane never "live" until set; both callers race.
    monkeypatch.setattr(commander, "list_windows", lambda: [{"pane_id": "%7"}])

    async def go():
        await asyncio.gather(commander.ensure_commander(), commander.ensure_commander())

    asyncio.run(go())
    assert calls["n"] == 1   # lock serialized; second caller sees the live marker


def test_spawn_leaves_marker_unset_on_empty_pane_id(monkeypatch, fresh_activity_db):
    # If display-message can't read the new window's %N, stamping pane_id="" would
    # be a marker never in the live set -> the caller respawns every tick (a
    # window/budget leak). The guard must leave the marker unset instead.
    from periscope import commander, activity
    import periscope.tmux as tmuxmod
    import periscope.config as config
    import periscope.channels as channels
    import periscope.pids as pids
    import periscope.open_ops as open_ops

    monkeypatch.setattr(config, "is_prod", lambda: True)
    monkeypatch.setattr(open_ops, "_session_live", lambda name: True)
    monkeypatch.setattr(tmuxmod, "_tmux_mutate", lambda *a, **k: (True, ""))
    monkeypatch.setattr(config, "claude_exec", lambda: "claude")
    monkeypatch.setattr(channels, "dismiss_dev_channels_consent_bg", lambda *a, **k: None)
    monkeypatch.setattr(pids, "stamp_new_window", lambda t: "")
    monkeypatch.setattr(tmuxmod, "tmux", lambda *a, **k: "")   # display-message read fails

    commander._spawn_commander(now=1)
    assert activity.get_commander() is None   # no phantom marker -> no respawn loop
