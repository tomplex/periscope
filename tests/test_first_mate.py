from periscope.first_mate import PaneDigest, FleetDigest, fleet_diverged
from periscope.first_mate import build_fleet_digest


def _pane(handle="@1", *, status_line="working", blocked=False, idle_s=0):
    return PaneDigest(
        handle=handle, name="w", session="s", status_line=status_line,
        blocked=blocked, pr=None, ci=None, idle_s=idle_s,
    )


def _fleet(panes, *, budget_pct=50, at=1000):
    return FleetDigest(panes=tuple(panes), budget_pct=budget_pct,
                       budget_resets_at=None, at=at)


def test_first_sight_is_divergent():
    diverged, reason = fleet_diverged(None, _fleet([_pane()]))
    assert diverged is True
    assert reason == "first_sight"


def test_identical_fleet_is_not_divergent():
    a = _fleet([_pane()])
    b = _fleet([_pane()], at=2000)  # only timestamp differs
    diverged, _ = fleet_diverged(a, b)
    assert diverged is False


def test_pane_going_blocked_is_divergent():
    prev = _fleet([_pane(blocked=False)])
    cur = _fleet([_pane(blocked=True)])
    diverged, reason = fleet_diverged(prev, cur)
    assert diverged is True
    assert "block" in reason.lower()


def test_status_line_change_is_divergent():
    prev = _fleet([_pane(status_line="running tests")])
    cur = _fleet([_pane(status_line="opening a PR")])
    diverged, _ = fleet_diverged(prev, cur)
    assert diverged is True


def test_pane_appearing_is_divergent():
    prev = _fleet([_pane("@1")])
    cur = _fleet([_pane("@1"), _pane("@2")])
    diverged, reason = fleet_diverged(prev, cur)
    assert diverged is True
    assert "@2" in reason


def test_pane_disappearing_is_divergent():
    prev = _fleet([_pane("@1"), _pane("@2")])
    cur = _fleet([_pane("@1")])
    diverged, reason = fleet_diverged(prev, cur)
    assert diverged is True
    assert "@2" in reason


def test_pr_ci_change_is_divergent():
    before = PaneDigest(handle="@1", name="w", session="s", status_line="working",
                        blocked=False, pr=1234, ci="⟳", idle_s=0)
    after = PaneDigest(handle="@1", name="w", session="s", status_line="working",
                       blocked=False, pr=1234, ci="✗", idle_s=0)  # CI went red
    diverged, _ = fleet_diverged(_fleet([before]), _fleet([after]))
    assert diverged is True


def test_small_budget_tick_is_not_divergent():
    prev = _fleet([_pane()], budget_pct=50)
    cur = _fleet([_pane()], budget_pct=51)
    diverged, _ = fleet_diverged(prev, cur)
    assert diverged is False


def test_large_budget_jump_is_divergent():
    prev = _fleet([_pane()], budget_pct=50)
    cur = _fleet([_pane()], budget_pct=70)
    diverged, _ = fleet_diverged(prev, cur)
    assert diverged is True


def test_idle_crossing_threshold_is_divergent():
    prev = _fleet([_pane(idle_s=10)])
    cur = _fleet([_pane(idle_s=10_000)])
    diverged, _ = fleet_diverged(prev, cur)
    assert diverged is True


def _view(handle, *, is_claude=True, status_line="working", blocked=False,
          pr=None, ci=None, idle_s=0):
    return {
        "handle": handle, "name": "win", "session": "sess",
        "is_claude": is_claude, "status_line": status_line, "blocked": blocked,
        "pr": pr, "ci": ci, "idle_s": idle_s,
    }


def test_build_filters_non_claude_panes():
    d = build_fleet_digest(
        panes=[_view("@1"), _view("@2", is_claude=False)], usage=None, now=1000,
    )
    assert [p.handle for p in d.panes] == ["@1"]


def test_build_maps_pane_fields():
    d = build_fleet_digest(
        panes=[_view("@1", status_line="opening PR", blocked=True, pr=1234, ci="✓", idle_s=42)],
        usage=None, now=1000,
    )
    p = d.panes[0]
    assert (p.handle, p.status_line, p.blocked, p.pr, p.ci, p.idle_s) == \
           ("@1", "opening PR", True, 1234, "✓", 42)


def test_build_extracts_budget_from_usage():
    usage = {"meters": {"session": {"percent": 61.7, "resets_at": 99999}}}
    d = build_fleet_digest(panes=[_view("@1")], usage=usage, now=1000)
    assert d.budget_pct == 62          # rounded
    assert d.budget_resets_at == 99999


def test_build_handles_missing_usage():
    d = build_fleet_digest(panes=[_view("@1")], usage=None, now=1000)
    assert d.budget_pct is None
    assert d.budget_resets_at is None


def test_build_stamps_now():
    d = build_fleet_digest(panes=[], usage=None, now=4242)
    assert d.at == 4242
    assert d.panes == ()


from periscope.first_mate import (
    _curate_pane, _render_delta, heartbeat_decide,
)
from periscope.activity import FirstMateMarker


def test_curate_pane_derives_blocked_from_newest_need_human_alert():
    d = _curate_pane(
        handle="@1", name="w", session="s", is_claude=True, status_line="working",
        alerts=[{"kind": "info", "ts": 10}, {"kind": "need_human", "ts": 20}],
        pr=1234, ci="✓", focused_at=100, acted_at=90, now=130,
    )
    assert d["blocked"] is True
    assert d["idle_s"] == 30          # now - max(focused, acted)
    assert d["handle"] == "@1" and d["pr"] == 1234 and d["ci"] == "✓"


def test_curate_pane_not_blocked_when_newest_alert_is_not_need_human():
    d = _curate_pane(
        handle="@1", name="w", session="s", is_claude=True, status_line=None,
        alerts=[{"kind": "need_human", "ts": 10}, {"kind": "done", "ts": 20}],
        pr=None, ci=None, focused_at=0, acted_at=0, now=5,
    )
    assert d["blocked"] is False
    assert d["idle_s"] == 5           # max(0,0)=0 -> now-0


def test_render_delta_mentions_changed_panes_and_budget():
    cur = FleetDigest(panes=(PaneDigest("@1","w","s","run",True,None,None,0),),
                      budget_pct=71, budget_resets_at=None, at=2)
    text = _render_delta(cur, "@1 blocked")
    assert "@1" in text and "71" in text


def test_heartbeat_decide_pushes_on_divergence_when_marker_present():
    marker = FirstMateMarker(pane_id="%9", session_id=None, updated_at=1)
    prev = None
    cur = FleetDigest(panes=(), budget_pct=50, budget_resets_at=None, at=2)
    push = heartbeat_decide(prev=prev, cur=cur, marker=marker)
    assert push is not None and push.pane_id == "%9" and push.content


def test_heartbeat_decide_none_when_no_marker():
    cur = FleetDigest(panes=(), budget_pct=50, budget_resets_at=None, at=2)
    assert heartbeat_decide(prev=None, cur=cur, marker=None) is None


def test_heartbeat_decide_none_when_not_diverged():
    marker = FirstMateMarker(pane_id="%9", session_id=None, updated_at=1)
    a = FleetDigest(panes=(), budget_pct=50, budget_resets_at=None, at=1)
    b = FleetDigest(panes=(), budget_pct=50, budget_resets_at=None, at=2)
    assert heartbeat_decide(prev=a, cur=b, marker=marker) is None


def test_heartbeat_decide_pushes_on_ci_red_even_if_otherwise_nominal():
    marker = FirstMateMarker(pane_id="%9", session_id=None, updated_at=1)
    prev = FleetDigest(panes=(PaneDigest("@1","w","s","x",False,7,"✓",0),),
                       budget_pct=50, budget_resets_at=None, at=1)
    cur = FleetDigest(panes=(PaneDigest("@1","w","s","x",False,7,"✗",0),),
                      budget_pct=50, budget_resets_at=None, at=2)
    push = heartbeat_decide(prev=prev, cur=cur, marker=marker)
    assert push is not None      # CI ✓->✗ forces a push (also caught by pr/ci divergence)


def test_assemble_pane_views_uses_curate_and_skips_non_claude(monkeypatch, fresh_activity_db):
    from periscope import first_mate, activity
    import periscope.channels as channels
    import periscope.panes as panes
    import periscope.git_pr as git_pr

    # Two windows; one is not Claude and must be dropped.
    panes_in = [
        ({"session": "s", "index": "1", "cwd": "/r", "pane_id": "%5", "pid": "@1"},
         {"is_claude": True}),
        ({"session": "s", "index": "2", "cwd": "/r", "pane_id": "%6", "pid": "@2"},
         {"is_claude": False}),
    ]
    # pane_status is keyed by tmux %N (pane_id), not @periscope_id — match real shape.
    monkeypatch.setattr(activity, "pane_status_lines", lambda: {"%5": ("running tests", 0, None)})
    monkeypatch.setattr(channels, "channel_state_for",
                        lambda pid: {"alerts": [{"kind": "need_human", "ts": 9}]})
    monkeypatch.setattr(git_pr, "cached_git_state", lambda p: {"branch": "b"})
    monkeypatch.setattr(git_pr, "cached_pr_state", lambda p, b: {"pr": 7, "ci": "✗"})
    monkeypatch.setattr(panes, "recency_stamps_for",
                        lambda t: {"focused_at": 100, "acted_at": 100})

    views = first_mate.assemble_pane_views(panes_in, now=130)
    assert len(views) == 1
    v = views[0]
    assert v["handle"] == "%5" and v["status_line"] == "running tests"   # handle = pane_id (%N)
    assert v["blocked"] is True and v["pr"] == 7 and v["ci"] == "✗"
    assert v["idle_s"] == 30


def test_run_worker_emits_pending_push_and_advances_last_sent(monkeypatch):
    import asyncio
    from periscope import activity, first_mate
    sent = []

    async def fake_emit(pane_id, content, meta=None):
        sent.append((pane_id, content))
        return True

    monkeypatch.setattr("periscope.channels.emit_channel_event", fake_emit)
    first_mate._LAST_SENT = None
    cur = first_mate.FleetDigest(panes=(), budget_pct=50, budget_resets_at=None, at=2)
    last_ctx = {"_fm_push": ("%9", "delta text", cur)}

    asyncio.run(activity._emit_pending_first_mate(last_ctx))   # sync test: drive the coro
    assert sent == [("%9", "delta text")]
    assert first_mate._LAST_SENT is cur          # advanced on ok
    assert "_fm_push" not in last_ctx            # consumed
    first_mate._LAST_SENT = None                 # reset module global for other tests


def test_run_worker_keeps_last_sent_on_failed_emit(monkeypatch):
    import asyncio
    from periscope import activity, first_mate

    async def fake_emit(pane_id, content, meta=None):
        return False                              # pane not attached

    monkeypatch.setattr("periscope.channels.emit_channel_event", fake_emit)
    first_mate._LAST_SENT = None
    cur = first_mate.FleetDigest(panes=(), budget_pct=50, budget_resets_at=None, at=2)
    last_ctx = {"_fm_push": ("%9", "delta", cur)}

    asyncio.run(activity._emit_pending_first_mate(last_ctx))
    assert first_mate._LAST_SENT is None          # NOT advanced -> next tick re-pushes


def test_supervisor_noop_when_marker_alive(monkeypatch, fresh_activity_db):
    from periscope import first_mate, activity
    import periscope.panes as panes
    activity.set_first_mate(pane_id="%9", session_id=None, at=1)
    monkeypatch.setattr(panes, "list_windows", lambda: [{"pane_id": "%9"}])
    called = []
    monkeypatch.setattr(first_mate, "_spawn_first_mate", lambda *, now: called.append(now))
    first_mate.supervisor_pass(now=5)
    assert called == []                       # alive -> no respawn


def test_supervisor_respawns_when_marker_missing(monkeypatch, fresh_activity_db):
    from periscope import first_mate
    import periscope.panes as panes
    monkeypatch.setattr(panes, "list_windows", lambda: [])
    called = []
    monkeypatch.setattr(first_mate, "_spawn_first_mate", lambda *, now: called.append(now))
    first_mate.supervisor_pass(now=5)
    assert called == [5]                      # no marker -> spawn


def test_supervisor_respawns_when_marked_pane_dead(monkeypatch, fresh_activity_db):
    from periscope import first_mate, activity
    import periscope.panes as panes
    activity.set_first_mate(pane_id="%9", session_id=None, at=1)
    monkeypatch.setattr(panes, "list_windows", lambda: [{"pane_id": "%7"}])  # %9 gone
    called = []
    monkeypatch.setattr(first_mate, "_spawn_first_mate", lambda *, now: called.append(now))
    first_mate.supervisor_pass(now=5)
    assert called == [5]


def test_spawn_leaves_marker_unset_on_empty_pane_id(monkeypatch, fresh_activity_db):
    # If display-message can't read the new window's %N, stamping pane_id="" would
    # be a marker never in the live set -> the supervisor respawns every tick (a
    # window/budget leak). The guard must leave the marker unset instead.
    from periscope import first_mate, activity
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

    first_mate._spawn_first_mate(now=1)
    assert activity.get_first_mate() is None   # no phantom marker -> no respawn loop
