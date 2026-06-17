from periscope.first_mate import PaneDigest, FleetDigest, fleet_diverged


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
