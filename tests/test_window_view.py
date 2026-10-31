"""Per-window view assembly (`build_window_view`).

Tests cover the smoothing chain, done-vs-idle refinement, linked-PR
override, channel-attached lookup, and stamp-update detection. The
function mutates panes._completed_at and _prev_state as a side effect;
tests assert against those mutations directly when relevant.
"""

import hashlib

import pytest


@pytest.fixture(autouse=True)
def reset_panes_and_channels(monkeypatch):
    """Clear in-memory pane + channel state between tests.

    Also neutralizes the account scan: the real `pane_config_dirs` shells out
    to the developer's live tmux server plus one `ps` per claude it finds, so an
    unpatched view build would fork dozens of processes per test. Tests that
    care re-patch it with their own map.
    """
    from periscope import panes, session_status, window_view

    monkeypatch.setattr(session_status, "pane_config_dirs", dict)
    window_view._pane_accounts_cache = None
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
    panes._needs_since.clear()
    panes._agent_transition_baseline.clear()
    panes._spinner_last_seen.clear()
    panes._agent_last_seen.clear()
    from periscope import window_view

    window_view._view_cache.clear()
    window_view._codex_last_valid.clear()
    window_view._codex_pid_panes.clear()
    window_view._permission_miss_checked.clear()
    with _CHANNELS_LOCK:
        _CHANNEL_ALERTS.clear()
        _CHANNEL_UNREAD.clear()
        _MCP_SESSIONS.clear()
    yield
    window_view._pane_accounts_cache = None
    panes._focused_at.clear()
    panes._acted_at.clear()
    panes._completed_at.clear()
    panes._prev_state.clear()
    panes._needs_since.clear()
    panes._agent_transition_baseline.clear()
    panes._spinner_last_seen.clear()
    panes._agent_last_seen.clear()
    from periscope import window_view

    window_view._view_cache.clear()
    window_view._codex_last_valid.clear()
    window_view._codex_pid_panes.clear()
    window_view._permission_miss_checked.clear()
    with _CHANNELS_LOCK:
        _CHANNEL_ALERTS.clear()
        _CHANNEL_UNREAD.clear()
        _MCP_SESSIONS.clear()


def _window(**overrides) -> dict:
    """Minimal tmux-window dict for tests."""
    base = {
        "session": "main",
        "index": 0,
        "name": "claude",
        "active": True,
        "cwd": "/tmp/nowhere",
        "pid": "abc12345",
        "pane_id": "%5",
    }
    base.update(overrides)
    return base


def _stub_subsystems(mocker, *, pane_content="", git=None, pr=None, linked_pr=None):
    """Mock the read-only caches the view consumes. `linked_pr` defaults to None
    (a cold SWR miss) — patched unconditionally so no real `gh pr view` thread
    ever fires from a test (leaked-thread + live-network hazard)."""
    mocker.patch("periscope.window_view.capture", return_value=pane_content)
    mocker.patch("periscope.window_view.cached_git_state", return_value=git or {})
    mocker.patch("periscope.window_view.cached_pr_state", return_value=pr or {})
    mocker.patch("periscope.window_view.cached_linked_pr_state", return_value=linked_pr)


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
            "agent": "claude",
            "state": "idle",
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
    assert stamp == (pid, 5000, 0, 0)


def test_view_publishes_a_done_panes_reply_from_its_transcript(mocker, clean_state):
    # THI-416: the done banner's body. Read only for a done pane, the one
    # state that notifies, so a working pane's growing file isn't re-read.
    from periscope import panes
    from periscope.window_view import build_window_view

    mocker.patch("periscope.window_view.parse_pane", return_value={"agent": "claude", "state": "idle", "spinner": None})
    _stub_subsystems(mocker, pane_content="x")
    mocker.patch("periscope.window_view.session_id_for_pane", return_value="sid-1")
    reply = mocker.patch("periscope.window_view.last_reply_for_session", return_value="Fixed it.")

    view, _ = build_window_view(_window(), now_ts=4000)
    assert (view["state"], view["last_reply"]) == ("idle", None)
    reply.assert_not_called()

    panes._prev_state["abc12345"] = "working"
    view, _ = build_window_view(_window(activity=1), now_ts=5000)
    assert (view["state"], view["last_reply"]) == ("done", "Fixed it.")
    reply.assert_called_once_with("sid-1")


def test_view_stamps_when_a_pane_started_waiting(mocker, clean_state):
    """needs_since holds the needs-input edge across polls and clears when the
    pane stops waiting, so a question's age doesn't reset on a reload."""
    from periscope.window_view import build_window_view

    pid = "abc12345"
    _stub_subsystems(mocker, pane_content="x")
    mocker.patch("periscope.window_view.capture", return_value="x")
    parse = mocker.patch(
        "periscope.window_view.parse_pane",
        return_value={"agent": "claude", "state": "needs-input", "spinner": None},
    )
    assert build_window_view(_window(pid=pid), now_ts=1000)[0]["needs_since"] == 1000
    assert build_window_view(_window(pid=pid), now_ts=1600)[0]["needs_since"] == 1000
    parse.return_value = {"agent": "claude", "state": "working", "spinner": "✻"}
    assert build_window_view(_window(pid=pid), now_ts=1700)[0]["needs_since"] == 0


def test_view_persists_needs_since_so_a_restart_continues_the_episode(mocker, clean_state):
    """A restart forgets _needs_since; a pane still waiting keeps the persisted
    stamp (the notification feed keys a needs-input banner on it), while a
    pane this process watched start waiting again gets a fresh one."""
    from periscope import panes
    from periscope.window_view import build_window_view

    pid = "abc12345"
    _stub_subsystems(mocker, pane_content="x")
    mocker.patch("periscope.window_view.capture", return_value="x")
    parse = mocker.patch(
        "periscope.window_view.parse_pane",
        return_value={"agent": "claude", "state": "needs-input", "spinner": None},
    )
    view, stamp = build_window_view(_window(pid=pid), now_ts=1000)
    assert stamp == (pid, 0, 0, 1000)
    clean_state["windows"][pid] = {"completed_at": 0, "acked_at": 0, "needs_since": 1000}
    # The restart: in-memory stamps are gone, the persisted one isn't.
    panes._needs_since.clear()
    panes._prev_state.clear()
    view, stamp = build_window_view(_window(pid=pid), now_ts=2000)
    assert view["needs_since"] == 1000
    assert stamp is None
    # Stops waiting: cleared, and the clear is persisted.
    parse.return_value = {"agent": "claude", "state": "working", "spinner": "✻"}
    view, stamp = build_window_view(_window(pid=pid), now_ts=2100)
    assert view["needs_since"] == 0
    assert stamp == (pid, 0, 0, 0)
    clean_state["windows"][pid]["needs_since"] = 0
    # A new episode in the same process is stamped now, not from state.json.
    parse.return_value = {"agent": "claude", "state": "needs-input", "spinner": None}
    assert build_window_view(_window(pid=pid), now_ts=2200)[0]["needs_since"] == 2200


# --- account attribution ---------------------------------------------------

_ACCOUNT_B_DIR = "/Users/x/.claude-b"  # conftest clean_state registry


def test_view_reports_account_for_second_subscription_pane(
    mocker,
    clean_state,
    monkeypatch,
):
    from periscope import session_status, window_view

    _stub_subsystems(mocker)
    monkeypatch.setattr(
        session_status,
        "pane_config_dirs",
        lambda: {"%5": _ACCOUNT_B_DIR},
    )
    view, _ = window_view.build_window_view(_window(), now_ts=1000)
    assert view["account"] == "b"


def test_view_default_account_pane_reports_default(mocker, clean_state):
    """No entry in the scan == default account == no chip."""
    from periscope import window_view

    _stub_subsystems(mocker)
    view, _ = window_view.build_window_view(_window(), now_ts=1000)
    assert view["account"] == "default"


def test_view_unregistered_config_dir_is_unknown_not_default(
    mocker,
    clean_state,
    monkeypatch,
):
    """A config dir no account claims must NOT read as the default account:
    the pane demonstrably isn't on it (default's config_dir is ""), and a
    missing chip would assert the opposite."""
    from periscope import session_status, window_view

    _stub_subsystems(mocker)
    monkeypatch.setattr(
        session_status,
        "pane_config_dirs",
        lambda: {"%5": "/Users/tom/.claude-zzz"},
    )
    view, _ = window_view.build_window_view(_window(), now_ts=1000)
    assert view["account"] == "unknown"


def test_account_scan_runs_once_per_poll_not_once_per_window(
    mocker,
    clean_state,
    monkeypatch,
):
    """`pane_config_dirs` forks ps once for the table plus once per candidate
    claude; ~20 windows a poll would be hundreds of forks every 3s."""
    from periscope import session_status, window_view

    _stub_subsystems(mocker)
    calls = []

    def _scan():
        calls.append(1)
        return {"%5": _ACCOUNT_B_DIR}

    monkeypatch.setattr(session_status, "pane_config_dirs", _scan)
    for i in range(5):
        view, _ = window_view.build_window_view(
            _window(index=i, pid=f"pid{i}"),
            now_ts=1000,
        )
        assert view["account"] == "b"
    assert len(calls) == 1


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

    view, stamp = window_view.build_window_view(_window(pane_pid="42"), now_ts=1000)

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
    assert stamp == ("abc12345", 1001, 0, 0)


def test_codex_unknown_is_no_opinion_then_same_turn_idle_completes(mocker, clean_state):
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


def test_view_linked_pr_carries_its_own_checks_and_base(mocker, clean_state):
    """The linked PR's checks and base replace the branch PR's, as `ci` does:
    the CI-failed banner must name this PR's checks, not another query's."""
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
        pr={"pr": "999", "ci": "✓", "ci_checks": [{"name": "other", "state": "success"}], "pr_base": "dev"},
        linked_pr={
            "pr_state": "open",
            "ci": "✗",
            "ci_checks": [{"name": "test", "state": "failure"}],
            "pr_base": "main",
        },
    )

    view, _ = build_window_view(_window(pid=pid), now_ts=1000)
    assert view["ci_checks"] == [{"name": "test", "state": "failure"}]
    assert view["pr_base"] == "main"


def test_view_linked_pr_cold_drops_the_branch_prs_checks(mocker, clean_state):
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
        pr={"pr": "999", "ci": "✓", "ci_checks": [{"name": "x", "state": "success"}], "pr_base": "dev"},
    )

    view, _ = build_window_view(_window(pid=pid), now_ts=1000)
    assert "ci_checks" not in view
    assert "pr_base" not in view


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


def test_view_derives_a_linear_ticket_when_none_is_linked(mocker, clean_state):
    # THI-441: link_linear was the only way to a ticket chip, so a pane on
    # `tdody/fdy-8562-…` with its PR open showed the PR and no ticket.
    from periscope.window_view import build_window_view

    mocker.patch(
        "periscope.window_view.parse_pane",
        return_value={"agent": "claude", "state": "idle", "spinner": None},
    )
    _stub_subsystems(
        mocker,
        pane_content="x",
        git={"branch": "tdody/fdy-8562-checkpoint-tooling"},
        pr={"pr": 8818, "pr_title": "feat(outcomes): restack"},
    )

    view, _ = build_window_view(_window(), now_ts=1000)
    assert view["linked_linear"] is None
    assert view["linear_ticket"] == "FDY-8562"
    assert view["linear_ticket_source"] == "branch"


def test_view_derives_only_the_team_keys_set(mocker, clean_state):
    from periscope.window_view import build_window_view

    clean_state["settings"] = {"linear_team_keys": ["THI"]}
    mocker.patch(
        "periscope.window_view.parse_pane",
        return_value={"agent": "claude", "state": "idle", "spinner": None},
    )
    _stub_subsystems(mocker, pane_content="x", git={"branch": "tdody/fdy-8562-checkpoint-tooling"})

    view, _ = build_window_view(_window(), now_ts=1000)
    assert view["linear_ticket"] is None


def test_view_derives_no_linear_ticket_over_a_linked_one(mocker, clean_state):
    from periscope.window_view import build_window_view

    pid = "abc12345"
    clean_state["windows"][pid] = {"linked_linear": "FAR-456"}
    mocker.patch(
        "periscope.window_view.parse_pane",
        return_value={"agent": "claude", "state": "idle", "spinner": None},
    )
    _stub_subsystems(mocker, pane_content="x", git={"branch": "tdody/fdy-8562-checkpoint-tooling"})

    view, _ = build_window_view(_window(pid=pid), now_ts=1000)
    assert view["linked_linear"] == "FAR-456"
    assert view["linear_ticket"] is None
    assert view["linear_ticket_source"] is None


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

    view, _ = build_window_view(_window(), now_ts=1000)
    assert view["state"] == "shell"  # error → agent=None → shell


def test_view_includes_track_id(mocker, clean_state, fresh_activity_db):
    """The view ships a resolved track_id (explicit pane_tracks tag wins)."""
    from periscope import activity
    from periscope.window_view import build_window_view

    activity.insert_track({"id": "tk_x", "name": "X", "repo": None, "created_at": 1, "archived_at": None})
    # pane_tracks keys on the @periscope_id (the window's `pid`), not %N.
    activity.set_pane_track("abc12345", "tk_x")
    _stub_subsystems(mocker)
    view, _ = build_window_view(_window(pid="abc12345"), now_ts=1000)
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
            "some output\n⠋ Thinking…\n  fdy | master | clean\n  24% | ↑235k ↓479 | $17.04 | Opus 4.7 (1M context)"
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
    assert mem_signal({"pid": 1, "mem_mb": 600, "age_s": 3600}) is None


def test_mem_signal_warn_on_footprint():
    from periscope.window_view import mem_signal

    out = mem_signal({"pid": 1, "mem_mb": 2048, "age_s": 60})
    assert out == {"tier": "warn", "trigger": "mem", "gb": 2.0, "age_s": 60}


def test_mem_signal_warn_on_age_alone():
    from periscope.window_view import mem_signal

    out = mem_signal({"pid": 1, "mem_mb": 500, "age_s": 3 * 86400})
    assert out == {"tier": "warn", "trigger": "age", "gb": 0.5, "age_s": 3 * 86400}


def test_mem_signal_bad_overrides_warn():
    from periscope.window_view import mem_signal

    out = mem_signal({"pid": 1, "mem_mb": 4300, "age_s": 60})
    assert out["tier"] == "bad"
    assert out["gb"] == 4.2


def test_mem_signal_unreadable_footprint_trips_on_age_only():
    from periscope.window_view import mem_signal

    assert mem_signal({"pid": 1, "mem_mb": None, "age_s": 60}) is None
    out = mem_signal({"pid": 1, "mem_mb": None, "age_s": 3 * 86400})
    assert out == {"tier": "warn", "trigger": "age", "gb": None, "age_s": 3 * 86400}


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


def test_view_reports_the_launch_effort_from_argv(mocker, clean_state, monkeypatch):
    from periscope import session_status, window_view

    _stub_subsystems(mocker)
    monkeypatch.setattr(session_status, "pane_efforts", lambda: {"%5": "high"})
    view, _ = window_view.build_window_view(_window(), now_ts=1000)
    assert view["launch_effort"] == "high"
    assert view["session_effort"] is None


def test_view_shows_a_set_effort_only_while_the_same_claude_runs(
    mocker,
    clean_state,
    monkeypatch,
):
    # The per-pane control's value is remembered because Claude never reports
    # effort back. A new claude in the same pane starts fresh, so the label
    # must not follow it.
    from periscope import session_status, window_view
    from periscope.store import set_window_fields

    _stub_subsystems(mocker)
    set_window_fields(
        "abc12345",
        session_effort={"value": "low", "claude_pid": 4242},
        session_model={"value": "sonnet", "claude_pid": 4242},
    )
    monkeypatch.setattr(session_status, "pane_claude_pids", lambda: {"%5": 4242})
    view, _ = window_view.build_window_view(_window(), now_ts=1000)
    assert (view["session_effort"], view["session_model"]) == ("low", "sonnet")

    monkeypatch.setattr(session_status, "pane_claude_pids", lambda: {"%5": 9999})
    view, _ = window_view.build_window_view(_window(), now_ts=1000)
    assert (view["session_effort"], view["session_model"]) == (None, None)


# --- pending_permission: the request a banner's Allow / Deny answers ----------

# What the hook stores for `touch probe.txt` (pane_permission_hook.match_key):
# a digest the server never reads, only hands to panes.dialog_matches.
_BASH_KEY = "sha256:" + hashlib.sha256(b"touchprobe.txt").hexdigest()


def _permission_row(
    act,
    pane_id: str,
    request_id: str,
    created_at: float,
    summary: str = "touch probe.txt",
    tool: str = "Bash",
    match_key: str | None = _BASH_KEY,
) -> None:
    """A row as pane_permission_hook.py writes it."""
    with act._LOCK:
        c = act._conn()
        c.execute(
            "INSERT INTO pane_permissions "
            "(pane_id, session_id, agent_id, request_id, tool, summary, match_key, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (pane_id, "sid-1", None, request_id, tool, summary, match_key, created_at),
        )
        c.commit()


def _waiting_on(mocker, waiting_for: str | None, state: str = "needs-input"):
    _stub_subsystems(mocker, pane_content="x")
    mocker.patch(
        "periscope.window_view.parse_pane",
        return_value={"agent": "claude", "state": state, "spinner": None, "waiting_for": waiting_for},
    )


def test_view_publishes_the_one_permission_request_of_this_wait(mocker, clean_state, fresh_activity_db):
    from periscope.window_view import build_window_view

    _waiting_on(mocker, "permission prompt")
    # Answered before this wait began: not the dialog on screen.
    _permission_row(fresh_activity_db, "%5", "0ld0000000000000", 990.0, summary="ls")
    # Stamped by the hook just before the dialog rendered, a poll before needs_since.
    _permission_row(fresh_activity_db, "%5", "a1b2c3d4e5f60718", 999.4)
    # Another pane's prompt is its own.
    _permission_row(fresh_activity_db, "%6", "ffff000000000000", 1000.0)
    view, _ = build_window_view(_window(), now_ts=1000)
    assert view["needs_since"] == 1000
    # match_key stays server-side: only the endpoint's screen check reads it.
    assert view["pending_permission"] == {
        "request_id": "a1b2c3d4e5f60718",
        "tool": "Bash",
        "summary": "touch probe.txt",
    }


def test_view_has_no_pending_permission_without_a_row(mocker, clean_state, fresh_activity_db):
    from periscope.window_view import build_window_view

    _waiting_on(mocker, "permission prompt")
    view, _ = build_window_view(_window(), now_ts=1000)
    assert view["pending_permission"] is None


def test_view_has_no_pending_permission_when_two_requests_fit(mocker, clean_state, fresh_activity_db):
    # Parallel tool calls each asking (or two prompts inside one poll): which
    # one the dialog shows can't be told, so the banner offers Open only.
    from periscope.window_view import build_window_view

    _waiting_on(mocker, "Permission Prompt")
    _permission_row(fresh_activity_db, "%5", "a1b2c3d4e5f60718", 999.4)
    _permission_row(fresh_activity_db, "%5", "b1b2c3d4e5f60718", 999.8)
    view, _ = build_window_view(_window(), now_ts=1000)
    assert view["pending_permission"] is None


def test_view_has_no_pending_permission_outside_a_permission_wait(mocker, clean_state, fresh_activity_db):
    from periscope.window_view import build_window_view

    _permission_row(fresh_activity_db, "%5", "a1b2c3d4e5f60718", 999.4)
    _waiting_on(mocker, "approve AskUserQuestion")
    assert build_window_view(_window(), now_ts=1000)[0]["pending_permission"] is None
    _waiting_on(mocker, None, state="working")
    assert build_window_view(_window(), now_ts=1000)[0]["pending_permission"] is None


def test_pending_permission_matches_waiting_for_case_insensitively(fresh_activity_db):
    from periscope.window_view import pending_permission_for

    _permission_row(fresh_activity_db, "%5", "a1b2c3d4e5f60718", 999.4)
    assert pending_permission_for("%5", "needs-input", "Permission Prompt", 1000)["request_id"] == "a1b2c3d4e5f60718"
    # needs_since 0 means "not waiting": no episode to scope the rows to.
    assert pending_permission_for("%5", "needs-input", "permission prompt", 0) is None


def test_pending_permission_row_carries_the_match_key_the_published_one_drops(fresh_activity_db):
    from periscope.window_view import pending_permission_for, pending_permission_row

    _permission_row(fresh_activity_db, "%5", "a1b2c3d4e5f60718", 999.4)
    assert pending_permission_row("%5", "needs-input", "permission prompt", 1000) == {
        "request_id": "a1b2c3d4e5f60718",
        "tool": "Bash",
        "summary": "touch probe.txt",
        "match_key": _BASH_KEY,
    }
    assert "match_key" not in pending_permission_for("%5", "needs-input", "permission prompt", 1000)


@pytest.mark.parametrize("match_key", [None, ""])
def test_a_request_without_a_match_key_gets_no_allow(fresh_activity_db, match_key):
    # A tool whose dialog can't be checked for an exact match (WebFetch, an
    # MCP tool, NotebookEdit — the hook stores no key): the banner is Open-only.
    from periscope.window_view import pending_permission_for, pending_permission_row

    _permission_row(
        fresh_activity_db, "%5", "a1b2c3d4e5f60718", 999.4, tool="WebFetch", summary="https://x", match_key=match_key
    )
    assert pending_permission_row("%5", "needs-input", "permission prompt", 1000) is None
    assert pending_permission_for("%5", "needs-input", "permission prompt", 1000) is None


def test_an_unkeyed_row_still_counts_against_the_one_row_rule(fresh_activity_db):
    # Two requests in one wait, only one of them verifiable: the dialog on
    # screen may be the other one, so neither is named.
    from periscope.window_view import pending_permission_row

    _permission_row(fresh_activity_db, "%5", "a1b2c3d4e5f60718", 999.4)
    _permission_row(fresh_activity_db, "%5", "b1b2c3d4e5f60718", 999.8, tool="WebFetch", match_key=None)
    assert pending_permission_row("%5", "needs-input", "permission prompt", 1000) is None


def test_the_window_reaches_exactly_five_seconds_before_needs_since(fresh_activity_db):
    from periscope.window_view import pending_permission_row

    _permission_row(fresh_activity_db, "%5", "a1b2c3d4e5f60718", 995.0)
    assert pending_permission_row("%5", "needs-input", "permission prompt", 1000)["request_id"] == "a1b2c3d4e5f60718"
    _permission_row(fresh_activity_db, "%6", "b1b2c3d4e5f60718", 994.99)
    assert pending_permission_row("%6", "needs-input", "permission prompt", 1000) is None


def test_a_row_stamped_after_needs_since_still_counts(fresh_activity_db):
    # needs_since is the build's floored now_ts, taken before its fan-out reads
    # the session file: the hook can stamp mid-build, after it.
    from periscope.window_view import pending_permission_row

    _permission_row(fresh_activity_db, "%5", "a1b2c3d4e5f60718", 1000.6)
    assert pending_permission_row("%5", "needs-input", "permission prompt", 1000)["request_id"] == "a1b2c3d4e5f60718"


def test_view_publishes_a_permission_request_from_the_session_file(mocker, clean_state, fresh_activity_db):
    # The real path: a mapped pane's state and waiting_for come from Claude's
    # session file, not the scrape (a permission dialog's footer is outside
    # parse_pane's family).
    from periscope.window_view import build_window_view

    _stub_subsystems(mocker, pane_content="x")
    mocker.patch(
        "periscope.window_view.session_state_for",
        return_value={"state": "needs-input", "status": "waiting", "waiting_for": "permission prompt"},
    )
    _permission_row(fresh_activity_db, "%5", "a1b2c3d4e5f60718", 997.0)
    view, _ = build_window_view(_window(), now_ts=1000)
    assert (view["state"], view["needs_since"]) == ("needs-input", 1000)
    assert view["pending_permission"] == {
        "request_id": "a1b2c3d4e5f60718",
        "tool": "Bash",
        "summary": "touch probe.txt",
    }


def test_a_request_just_outside_the_window_is_logged_once_per_wait(fresh_activity_db, caplog):
    import logging

    from periscope.window_view import pending_permission_row

    caplog.set_level(logging.INFO, logger="periscope")  # prod's level
    _permission_row(fresh_activity_db, "%5", "a1b2c3d4e5f60718", 990.0)
    for _ in range(3):  # every tick of the same wait
        assert pending_permission_row("%5", "needs-input", "permission prompt", 1000) is None
    misses = [r for r in caplog.records if "too narrow" in r.getMessage()]
    assert len(misses) == 1
    assert misses[0].levelno == logging.INFO and "%5" in misses[0].getMessage()


def test_a_wait_with_no_request_anywhere_logs_nothing(fresh_activity_db, caplog):
    # A pane whose Claude hasn't loaded the hook: Open only, and not a miss.
    import logging

    from periscope.window_view import pending_permission_row

    caplog.set_level(logging.INFO, logger="periscope")  # prod's level
    _permission_row(fresh_activity_db, "%5", "a1b2c3d4e5f60718", 980.0)  # an older wait's
    assert pending_permission_row("%5", "needs-input", "permission prompt", 1000) is None
    assert not [r for r in caplog.records if "too narrow" in r.getMessage()]


def test_a_db_error_leaves_the_banner_open_only_not_the_card_an_error(
    mocker, clean_state, fresh_activity_db, monkeypatch, caplog
):
    import sqlite3

    from periscope import window_view

    monkeypatch.setattr(window_view, "_permission_error_logged_at", 0.0)
    mocker.patch.object(
        window_view.activity, "pane_permissions_since", side_effect=sqlite3.OperationalError("database is locked")
    )
    _waiting_on(mocker, "permission prompt")
    view, _ = window_view.build_window_view(_window(), now_ts=1000)
    assert view["state"] == "needs-input"
    assert view["pending_permission"] is None
    # Logged, but not again on the next tick.
    assert window_view.pending_permission_row("%5", "needs-input", "permission prompt", 1000) is None
    assert len([r for r in caplog.records if "Open only" in r.getMessage()]) == 1
