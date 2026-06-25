"""Per-window view assembly (`build_window_view`).

Tests cover the smoothing chain, done-vs-idle refinement, linked-PR
override, channel-attached lookup, and stamp-update detection. The
function mutates panes._completed_at and _prev_state as a side effect;
tests assert against those mutations directly when relevant.
"""

import pytest


@pytest.fixture(autouse=True)
def reset_panes_and_channels():
    """Clear in-memory pane + channel state between tests."""
    from periscope import panes
    from periscope.channels import (
        _CHANNEL_ALERTS,
        _CHANNEL_UNREAD,
        _CHANNELS_LOCK,
        _MCP_SESSIONS,
    )
    panes._focused_at.clear()
    panes._acted_at.clear()
    panes._completed_at.clear()
    panes._prev_state.clear()
    panes._spinner_last_seen.clear()
    panes._claude_last_seen.clear()
    from periscope import window_view
    window_view._view_cache.clear()
    with _CHANNELS_LOCK:
        _CHANNEL_ALERTS.clear()
        _CHANNEL_UNREAD.clear()
        _MCP_SESSIONS.clear()
    yield
    panes._focused_at.clear()
    panes._acted_at.clear()
    panes._completed_at.clear()
    panes._prev_state.clear()
    panes._spinner_last_seen.clear()
    panes._claude_last_seen.clear()
    from periscope import window_view
    window_view._view_cache.clear()
    with _CHANNELS_LOCK:
        _CHANNEL_ALERTS.clear()
        _CHANNEL_UNREAD.clear()
        _MCP_SESSIONS.clear()


def _window(**overrides) -> dict:
    """Minimal tmux-window dict for tests."""
    base = {
        "session": "main", "index": 0, "name": "claude",
        "active": True, "cwd": "/tmp/nowhere",
        "pid": "abc12345", "pane_id": "%5",
    }
    base.update(overrides)
    return base


def _stub_subsystems(mocker, *, pane_content="", git=None, pr=None, lgtm=None):
    """Mock the read-only caches the view consumes."""
    mocker.patch("periscope.window_view.capture", return_value=pane_content)
    mocker.patch("periscope.window_view.cached_git_state", return_value=git or {})
    mocker.patch("periscope.window_view.cached_pr_state", return_value=pr or {})
    mocker.patch("periscope.window_view.cached_lgtm_state", return_value=lgtm)


def test_view_returns_target_and_focused_at(mocker, clean_state):
    from periscope.window_view import build_window_view
    _stub_subsystems(mocker)
    w = _window()
    view, _ = build_window_view(w, now_ts=1000)
    assert view["target"] == "main:0"
    assert view["focused_at"] == 0  # never engaged through periscope


def test_view_classifies_non_claude_as_shell(mocker, clean_state):
    from periscope.window_view import build_window_view
    _stub_subsystems(mocker, pane_content="$ ls\nREADME.md\n$")
    view, _ = build_window_view(_window(), now_ts=1000)
    assert view["is_claude"] is False
    assert view["state"] == "shell"


def test_view_promotes_blank_state_to_working_when_spinner_present(mocker, clean_state):
    """Spinner hysteresis: if we have a spinner but parse_pane returned
    a blank/idle state, the spinner promotes the state to 'working'."""
    from periscope.window_view import build_window_view

    mocker.patch("periscope.window_view.capture", return_value="claude pane")
    # parse_pane returns is_claude=True, state=idle, but has a spinner.
    mocker.patch(
        "periscope.window_view.parse_pane",
        return_value={
            "is_claude": True, "state": "idle",
            "spinner": "Envisioning",
        },
    )
    _stub_subsystems(mocker, pane_content="x")
    # Re-patch capture since _stub_subsystems clobbered it above.
    mocker.patch("periscope.window_view.capture", return_value="claude pane")
    view, _ = build_window_view(_window(), now_ts=1000)
    assert view["state"] == "working"


def test_view_done_refinement_promotes_idle_to_done_after_busy(mocker, clean_state):
    """If the previous state was working and current is idle, the
    completed_at stamp bumps. If acked_at < completed_at and the pane
    is_claude, the state becomes 'done'."""
    from periscope import panes
    from periscope.window_view import build_window_view

    pid = "abc12345"
    # Seed prior state: was working last poll.
    panes._prev_state[pid] = "working"

    mocker.patch(
        "periscope.window_view.parse_pane",
        return_value={"is_claude": True, "state": "idle", "spinner": None},
    )
    _stub_subsystems(mocker, pane_content="x")
    mocker.patch("periscope.window_view.capture", return_value="x")

    view, stamp = build_window_view(_window(pid=pid), now_ts=5000)
    assert view["state"] == "done"
    assert view["completed_at"] == 5000
    # Stamp update should record the new completed_at for persistence.
    assert stamp == (pid, 5000, 0)


def test_view_no_stamp_update_when_persisted_already_current(mocker, clean_state):
    """If the persisted completed_at/acked_at already match the in-memory
    values, no stamp update is needed."""
    from periscope import panes
    from periscope.window_view import build_window_view

    pid = "abc12345"
    panes._completed_at["main:0"] = 5000
    panes._acted_at["main:0"] = 6000
    clean_state["windows"][pid] = {"completed_at": 5000, "acked_at": 6000}

    mocker.patch(
        "periscope.window_view.parse_pane",
        return_value={"is_claude": True, "state": "idle", "spinner": None},
    )
    _stub_subsystems(mocker, pane_content="x")
    mocker.patch("periscope.window_view.capture", return_value="x")

    _, stamp = build_window_view(_window(pid=pid), now_ts=7000)
    assert stamp is None


def test_view_linked_pr_overrides_auto_detected(mocker, clean_state):
    """When _STATE["windows"][pid]["linked_pr"] is set, it overrides
    the auto-detected pr field and pops the stale CI glyph."""
    from periscope.window_view import build_window_view

    pid = "abc12345"
    clean_state["windows"][pid] = {"linked_pr": 1234}

    mocker.patch(
        "periscope.window_view.parse_pane",
        return_value={"is_claude": True, "state": "idle", "spinner": None},
    )
    _stub_subsystems(
        mocker,
        pane_content="x",
        pr={"pr": "999", "ci": "✓"},
    )
    mocker.patch("periscope.window_view.capture", return_value="x")

    view, _ = build_window_view(_window(pid=pid), now_ts=1000)
    # `pr` is normalized to int regardless of source (auto-detect or linked).
    assert view["pr"] == 1234
    assert view["pr_linked"] is True
    assert "ci" not in view  # stale glyph dropped


def test_view_surfaces_linked_linear(mocker, clean_state):
    from periscope.window_view import build_window_view
    pid = "abc12345"
    clean_state["windows"][pid] = {
        "linked_linear": "FAR-456",
        "linked_linear_title": "Fix the thing",
        "linked_linear_status": "In Progress",
    }

    mocker.patch(
        "periscope.window_view.parse_pane",
        return_value={"is_claude": True, "state": "idle", "spinner": None},
    )
    _stub_subsystems(mocker, pane_content="x")
    mocker.patch("periscope.window_view.capture", return_value="x")

    view, _ = build_window_view(_window(pid=pid), now_ts=1000)
    assert view["linked_linear"] == "FAR-456"
    assert view["linked_linear_title"] == "Fix the thing"
    assert view["linked_linear_status"] == "In Progress"


def test_view_channel_attached_reflects_mcp_session_presence(mocker, clean_state):
    from periscope.channels import _CHANNEL_UNREAD, _CHANNELS_LOCK, _MCP_SESSIONS
    from periscope.window_view import build_window_view

    pane_id = "%7"
    with _CHANNELS_LOCK:
        _MCP_SESSIONS[pane_id] = object()  # presence is the signal
        _CHANNEL_UNREAD[pane_id] = 3

    mocker.patch(
        "periscope.window_view.parse_pane",
        return_value={"is_claude": True, "state": "idle", "spinner": None},
    )
    _stub_subsystems(mocker, pane_content="x")
    mocker.patch("periscope.window_view.capture", return_value="x")

    view, _ = build_window_view(_window(pane_id=pane_id), now_ts=1000)
    assert view["channel_attached"] is True
    assert view["channel_unread"] == 3


def test_view_handles_capture_exception(mocker, clean_state):
    """If capture() raises, the view should still build with state=error."""
    from periscope.window_view import build_window_view

    mocker.patch("periscope.window_view.capture", side_effect=Exception("tmux died"))
    mocker.patch("periscope.window_view.cached_git_state", return_value={})
    mocker.patch("periscope.window_view.cached_pr_state", return_value={})
    mocker.patch("periscope.window_view.cached_lgtm_state", return_value=None)

    view, _ = build_window_view(_window(), now_ts=1000)
    assert view["state"] == "shell"  # error → is_claude=False → shell


def test_view_includes_track_id(mocker, clean_state, fresh_activity_db):
    """The view ships a resolved track_id (explicit pane_tracks tag wins)."""
    from periscope import activity
    from periscope.window_view import build_window_view

    activity.insert_track({"id": "tk_x", "name": "X", "repo": None,
                           "created_at": 1, "archived_at": None})
    activity.set_pane_track("%42", "tk_x")
    _stub_subsystems(mocker)
    view, _ = build_window_view(_window(pane_id="%42"), now_ts=1000)
    assert view["track_id"] == "tk_x"
    # The rail labels off track_name (no separate /api/state tracks payload).
    assert view["track_name"] == "X"


def test_view_track_id_falls_back_to_loose_for_non_git(mocker, clean_state, fresh_activity_db):
    """Untagged + non-git window resolves to the loose catchall."""
    from periscope import tracks
    from periscope.window_view import build_window_view

    _stub_subsystems(mocker, git={})  # cached_git_state → {} (no repo_key)
    view, _ = build_window_view(_window(pane_id="%99"), now_ts=1000)
    assert view["track_id"] == tracks.LOOSE_KEY


def test_view_persisted_acked_at_suppresses_done_state(mocker, clean_state):
    """When acked_at >= completed_at, state stays 'idle' (user has
    already engaged since the last completion)."""
    from periscope.window_view import build_window_view

    pid = "abc12345"
    clean_state["windows"][pid] = {"completed_at": 5000, "acked_at": 6000}

    mocker.patch(
        "periscope.window_view.parse_pane",
        return_value={"is_claude": True, "state": "idle", "spinner": None},
    )
    _stub_subsystems(mocker, pane_content="x")
    mocker.patch("periscope.window_view.capture", return_value="x")

    view, _ = build_window_view(_window(pid=pid), now_ts=7000)
    assert view["state"] == "idle"  # NOT promoted to "done"


def test_idle_pane_skips_recapture_when_activity_unchanged(mocker, clean_state):
    from periscope import window_view
    cap = mocker.patch("periscope.window_view.capture", return_value="")  # parses to shell/idle
    w = _window()
    w["activity"] = 500
    window_view.build_window_view(w, now_ts=1000)
    assert cap.call_count == 1
    # Second poll, same activity, cached state is quiet → capture NOT called again.
    window_view.build_window_view(w, now_ts=1001)
    assert cap.call_count == 1


def test_pane_recaptures_when_activity_advances(mocker, clean_state):
    from periscope import window_view
    cap = mocker.patch("periscope.window_view.capture", return_value="")
    w = _window()
    w["activity"] = 500
    window_view.build_window_view(w, now_ts=1000)
    w2 = _window()
    w2["activity"] = 700  # tmux saw new output
    window_view.build_window_view(w2, now_ts=1001)
    assert cap.call_count == 2


def test_working_pane_always_recaptures_even_if_activity_unchanged(mocker, clean_state):
    """Spinner grace + done-edge are non-idempotent, so a working pane is never
    skipped even when activity is stale."""
    from periscope import window_view
    cap = mocker.patch(
        "periscope.window_view.capture",
        # Real Claude status block (status line present) so parse_pane returns
        # is_claude + spinner → working. A bare "⠋ thinking…" line has no status
        # line and parses to shell, which would never exercise the working path.
        return_value=(
            "some output\n⠋ Thinking…\n"
            "  fdy | master | clean\n"
            "  24% | ↑235k ↓479 | $17.04 | Opus 4.7 (1M context)"
        ),  # parses is_claude + spinner → working
    )
    w = _window()
    w["activity"] = 500
    view, _ = window_view.build_window_view(w, now_ts=1000)
    assert view["state"] == "working"
    window_view.build_window_view(w, now_ts=1001)  # same activity
    assert cap.call_count == 2  # working pane re-captured


def test_skipped_pane_still_reflects_fresh_focus(mocker, clean_state):
    """A skipped (quiet) pane still gets fresh focused_at — only capture+parse
    is skipped, not the recency assembly."""
    from periscope import panes, window_view
    mocker.patch("periscope.window_view.capture", return_value="")
    w = _window()
    w["activity"] = 500
    window_view.build_window_view(w, now_ts=1000)
    panes.note_focus(f"{w['session']}:{w['index']}")  # focus shifts to this pane
    expected = panes._focused_at[f"{w['session']}:{w['index']}"]
    view, _ = window_view.build_window_view(w, now_ts=1001)  # activity unchanged → skip capture
    assert view["focused_at"] == expected


def test_window_view_emits_workspace_id(mocker, clean_state, fresh_activity_db):
    from periscope import activity
    from periscope.window_view import build_window_view
    from periscope.workspaces import create_workspace
    _stub_subsystems(mocker)
    ws = create_workspace(name="WS")
    activity.set_pane_workspace("%9", ws["id"])
    view, _ = build_window_view(_window(pane_id="%9"), now_ts=1000)
    assert view["workspace_id"] == ws["id"]


def test_window_view_workspace_id_none_when_untagged(mocker, clean_state, fresh_activity_db):
    from periscope.window_view import build_window_view
    _stub_subsystems(mocker)
    view, _ = build_window_view(_window(pane_id="%8"), now_ts=1000)
    assert view.get("workspace_id") is None
