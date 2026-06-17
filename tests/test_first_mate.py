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
