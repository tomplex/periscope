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
    panes._agent_transition_baseline.clear()
    panes._spinner_last_seen.clear()
    panes._agent_last_seen.clear()
    from periscope import window_view
    window_view._view_cache.clear()
    window_view._codex_last_valid.clear()
    window_view._codex_pid_panes.clear()
    with _CHANNELS_LOCK:
        _CHANNEL_ALERTS.clear()
        _CHANNEL_UNREAD.clear()
        _MCP_SESSIONS.clear()
    yield
    panes._focused_at.clear()
    panes._acted_at.clear()
    panes._completed_at.clear()
    panes._prev_state.clear()
    panes._agent_transition_baseline.clear()
    panes._spinner_last_seen.clear()
    panes._agent_last_seen.clear()
    from periscope import window_view
    window_view._view_cache.clear()
    window_view._codex_last_valid.clear()
    window_view._codex_pid_panes.clear()
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


def _stub_subsystems(mocker, *, pane_content="", git=None, pr=None, lgtm=None,
                     linked_pr=None):
    """Mock the read-only caches the view consumes. `linked_pr` defaults to None
    (a cold SWR miss) — patched unconditionally so no real `gh pr view` thread
    ever fires from a test (leaked-thread + live-network hazard)."""
    mocker.patch("periscope.window_view.capture", return_value=pane_content)
    mocker.patch("periscope.window_view.cached_git_state", return_value=git or {})
    mocker.patch("periscope.window_view.cached_pr_state", return_value=pr or {})
    mocker.patch("periscope.window_view.cached_lgtm_state", return_value=lgtm)
    mocker.patch("periscope.window_view.cached_linked_pr_state",
                 return_value=linked_pr)


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
    assert view["agent"] is None
    assert view["state"] == "shell"


def test_view_promotes_blank_state_to_working_when_spinner_present(mocker, clean_state):
    """Spinner hysteresis: if we have a spinner but parse_pane returned
    a blank/idle state, the spinner promotes the state to 'working'."""
    from periscope.window_view import build_window_view

    mocker.patch("periscope.window_view.capture", return_value="claude pane")
    # parse_pane returns agent="claude", state=idle, but has a spinner.
    mocker.patch(
        "periscope.window_view.parse_pane",
        return_value={
            "agent": "claude", "state": "idle",
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
    agent identity, the state becomes 'done'."""
    from periscope import panes
    from periscope.window_view import build_window_view

    pid = "abc12345"
    # Seed prior state: was working last poll.
    panes._prev_state[pid] = "working"

    mocker.patch(
        "periscope.window_view.parse_pane",
        return_value={"agent": "claude", "state": "idle", "spinner": None},
    )
    _stub_subsystems(mocker, pane_content="x")
    mocker.patch("periscope.window_view.capture", return_value="x")

    view, stamp = build_window_view(_window(pid=pid), now_ts=5000)
    assert view["state"] == "done"
    assert view["completed_at"] == 5000
    # Stamp update should record the new completed_at for persistence.
    assert stamp == (pid, 5000, 0)


def _codex_view_stubs(mocker):
    _stub_subsystems(mocker, pane_content="")
    mocker.patch("periscope.window_view.codex_process_for_pane", return_value=True)


def test_codex_first_idle_is_baseline_not_done(mocker, clean_state):
    from periscope import window_view

    _codex_view_stubs(mocker)
    mocker.patch(
        "periscope.window_view._codex_observation",
        return_value=window_view._CodexObservation("idle", "session-a", "turn-a"),
    )

    view, stamp = window_view.build_window_view(
        _window(pane_pid="42"), now_ts=1000
    )

    assert view["agent"] == "codex"
    assert view["state"] == "idle"
    assert view["completed_at"] == 0
    assert stamp is None


def test_codex_same_turn_working_to_idle_completes_once(mocker, clean_state):
    from periscope import window_view

    _codex_view_stubs(mocker)
    opinion = mocker.patch(
        "periscope.window_view._codex_observation",
        side_effect=[
            window_view._CodexObservation("working", "session-a", "turn-a"),
            window_view._CodexObservation("idle", "session-a", "turn-a"),
            window_view._CodexObservation("idle", "session-a", "turn-a"),
        ],
    )
    w = _window(pane_pid="42")

    first, _ = window_view.build_window_view(w, now_ts=1000)
    second, stamp = window_view.build_window_view(w, now_ts=1001)
    third, _ = window_view.build_window_view(w, now_ts=1002)

    assert opinion.call_count == 3  # structured state bypasses quiet capture cache
    assert first["state"] == "working"
    assert second["state"] == third["state"] == "done"
    assert second["completed_at"] == third["completed_at"] == 1001
    assert stamp == ("abc12345", 1001, 0)


def test_codex_unknown_is_no_opinion_then_same_turn_idle_completes(
    mocker, clean_state
):
    from periscope import window_view

    _codex_view_stubs(mocker)
    mocker.patch(
        "periscope.window_view._codex_observation",
        side_effect=[
            window_view._CodexObservation("working", "session-a", "turn-a"),
            None,
            window_view._CodexObservation("idle", "session-a", "turn-a"),
        ],
    )
    w = _window(pane_pid="42")

    window_view.build_window_view(w, now_ts=1000)
    unknown, _ = window_view.build_window_view(w, now_ts=1010)
    done, _ = window_view.build_window_view(w, now_ts=1011)

    assert unknown["state"] == "unknown"
    assert unknown["completed_at"] == 0
    assert done["state"] == "done"
    assert done["completed_at"] == 1011


def test_codex_different_turn_idle_does_not_complete(mocker, clean_state):
    from periscope import window_view

    _codex_view_stubs(mocker)
    mocker.patch(
        "periscope.window_view._codex_observation",
        side_effect=[
            window_view._CodexObservation("working", "session-a", "turn-a"),
            None,
            window_view._CodexObservation("idle", "session-a", "turn-b"),
        ],
    )
    w = _window(pane_pid="42")

    window_view.build_window_view(w, now_ts=1000)
    window_view.build_window_view(w, now_ts=1010)
    view, stamp = window_view.build_window_view(w, now_ts=1011)

    assert view["state"] == "idle"
    assert view["completed_at"] == 0
    assert stamp is None


def test_codex_unverified_hook_binding_has_no_state_opinion(mocker):
    from periscope import window_view
    from periscope.session_binding_db import AgentSessionBinding

    mocker.patch(
        "periscope.window_view.activity.get_agent_session",
        return_value=AgentSessionBinding(
            "%5",
            "codex",
            "session-a",
            "/tmp/rollout.jsonl",
            1000,
            "codex-hook-unverified",
        ),
    )
    rollout = mocker.patch("periscope.window_view.rollout_edge_for")

    assert window_view._codex_observation("%5", True) is None
    rollout.assert_not_called()


def test_codex_does_not_surface_claude_channel_attention(mocker, clean_state):
    from periscope import window_view

    _codex_view_stubs(mocker)
    mocker.patch(
        "periscope.window_view._codex_observation",
        return_value=window_view._CodexObservation("idle", "session-a", "turn-a"),
    )
    mocker.patch(
        "periscope.window_view.channel_state_for",
        return_value={"attached": True, "unread": True, "alerts": [{"kind": "done"}]},
    )

    view, _ = window_view.build_window_view(_window(pane_pid="42"), now_ts=1000)

    assert view["channel_attached"] is False
    assert view["channel_unread"] is False
    assert view["channel_alerts"] == []


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
        return_value={"agent": "claude", "state": "idle", "spinner": None},
    )
    _stub_subsystems(mocker, pane_content="x")
    mocker.patch("periscope.window_view.capture", return_value="x")

    _, stamp = build_window_view(_window(pid=pid), now_ts=7000)
    assert stamp is None


def test_view_linked_pr_overrides_auto_detected(mocker, clean_state):
    """When _STATE["windows"][pid]["linked_pr"] is set, it overrides the
    auto-detected pr field. With the linked-PR resolver cold (None), the
    branch-keyed CI glyph — which is for a different query — is dropped."""
    from periscope.window_view import build_window_view

    pid = "abc12345"
    clean_state["windows"][pid] = {"linked_pr": 1234}

    mocker.patch(
        "periscope.window_view.parse_pane",
        return_value={"agent": "claude", "state": "idle", "spinner": None},
    )
    _stub_subsystems(
        mocker,
        pane_content="x",
        pr={"pr": "999", "ci": "✓"},
    )

    view, _ = build_window_view(_window(pid=pid), now_ts=1000)
    # `pr` is normalized to int regardless of source (auto-detect or linked).
    assert view["pr"] == 1234
    assert view["pr_linked"] is True
    assert "ci" not in view  # stale glyph dropped
    assert view.get("pr_state") is None


def test_view_linked_pr_merged_carries_state(mocker, clean_state):
    """A resolved merged PR carries pr_state='merged' and its own CI, so the
    rail can stop showing it as live open work."""
    from periscope.window_view import build_window_view

    pid = "abc12345"
    clean_state["windows"][pid] = {"linked_pr": 1234}
    mocker.patch(
        "periscope.window_view.parse_pane",
        return_value={"agent": "claude", "state": "idle", "spinner": None},
    )
    _stub_subsystems(
        mocker,
        pane_content="x",
        pr={"pr": "999", "ci": "✓"},
        linked_pr={"pr_state": "merged", "ci": None},
    )

    view, _ = build_window_view(_window(pid=pid), now_ts=1000)
    assert view["pr"] == 1234
    assert view["pr_state"] == "merged"
    assert view["ci"] is None


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
        return_value={"agent": "claude", "state": "idle", "spinner": None},
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
        return_value={"agent": "claude", "state": "idle", "spinner": None},
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
    assert view["state"] == "shell"  # error → agent=None → shell


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
        return_value={"agent": "claude", "state": "idle", "spinner": None},
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
        # agent + spinner → working. A bare "⠋ thinking…" line has no status
        # line and parses to shell, which would never exercise the working path.
        return_value=(
            "some output\n⠋ Thinking…\n"
            "  fdy | master | clean\n"
            "  24% | ↑235k ↓479 | $17.04 | Opus 4.7 (1M context)"
        ),  # parses agent + spinner → working
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


def test_window_view_drops_dead_project_workspace_fields(mocker, clean_state, fresh_activity_db):
    """track_id supersedes project_pinned_dir / workspace_id (and the
    project_name/project_archived fields the frontend never read off windows)
    — the view no longer ships any of them."""
    from periscope.window_view import build_window_view
    _stub_subsystems(mocker)
    view, _ = build_window_view(_window(pane_id="%9"), now_ts=1000)
    assert "project_pinned_dir" not in view
    assert "workspace_id" not in view
    assert "project_name" not in view
    assert "project_archived" not in view
    assert "track_id" in view


# ── mem_signal: cycle-hint tiers from claude process stats ───────────────

def test_mem_signal_none_when_healthy():
    from periscope.window_view import mem_signal
    assert mem_signal(None) is None
    assert mem_signal({"pid": 1, "rss_kb": 600_000, "age_s": 3600}) is None


def test_mem_signal_warn_on_rss():
    from periscope.window_view import mem_signal
    out = mem_signal({"pid": 1, "rss_kb": 2 * 1024 * 1024, "age_s": 60})
    assert out == {"tier": "warn", "rss_gb": 2.0, "age_s": 60}


def test_mem_signal_warn_on_age_alone():
    from periscope.window_view import mem_signal
    out = mem_signal({"pid": 1, "rss_kb": 500_000, "age_s": 3 * 86400})
    assert out["tier"] == "warn"
    assert out["rss_gb"] == 0.5


def test_mem_signal_bad_overrides_warn():
    from periscope.window_view import mem_signal
    out = mem_signal({"pid": 1, "rss_kb": 4_400_000, "age_s": 60})
    assert out["tier"] == "bad"
    assert out["rss_gb"] == 4.2


def test_view_surfaces_spawned_by_lineage(mocker, clean_state):
    """spawn_claude has recorded spawned_by since the tool shipped, but the
    payload never carried it — so a chain of delegated sessions rendered as
    unrelated tabs and the dashboard could not show A's work continuing in B."""
    from periscope.window_view import build_window_view
    pid = "d188cde1"
    clean_state["windows"][pid] = {"spawned_by": "2c453c8c"}

    mocker.patch(
        "periscope.window_view.parse_pane",
        return_value={"agent": "claude", "state": "idle", "spinner": None},
    )
    _stub_subsystems(mocker, pane_content="x")
    mocker.patch("periscope.window_view.capture", return_value="x")

    view, _ = build_window_view(_window(pid=pid), now_ts=1000)
    assert view["spawned_by"] == "2c453c8c"


def test_view_names_a_spawner_that_has_already_exited(mocker, clean_state):
    """The name is resolved off the PERSISTED block, not the live window list:
    leads exit, and on a real box the only surviving lineage was a 3-link chain
    whose middle pane was already dead. A live-only join renders nothing for
    exactly the long-chain case lineage exists to make legible."""
    from periscope.window_view import build_window_view
    pid = "b6bde664"
    clean_state["windows"][pid] = {"spawned_by": "d188cde1"}
    # The lead is gone from tmux, but its last_seen name survives (GC'd at 30d).
    clean_state["windows"]["d188cde1"] = {
        "last_seen": {"name": "model-migration", "session": "periscope"},
    }

    mocker.patch(
        "periscope.window_view.parse_pane",
        return_value={"agent": "claude", "state": "idle", "spinner": None},
    )
    _stub_subsystems(mocker, pane_content="x")
    mocker.patch("periscope.window_view.capture", return_value="x")

    view, _ = build_window_view(_window(pid=pid), now_ts=1000)
    assert view["spawned_by"] == "d188cde1"
    assert view["spawner_name"] == "model-migration"


def test_view_spawned_by_is_none_for_a_hand_created_pane(mocker, clean_state):
    """The key must always be present so the client can join on it uniformly."""
    from periscope.window_view import build_window_view
    pid = "solo0001"
    clean_state["windows"][pid] = {}

    mocker.patch(
        "periscope.window_view.parse_pane",
        return_value={"agent": "claude", "state": "idle", "spinner": None},
    )
    _stub_subsystems(mocker, pane_content="x")
    mocker.patch("periscope.window_view.capture", return_value="x")

    view, _ = build_window_view(_window(pid=pid), now_ts=1000)
    assert "spawned_by" in view and view["spawned_by"] is None
