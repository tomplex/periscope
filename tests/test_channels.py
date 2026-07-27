"""Channels: tool implementations + alert log + GC.

The live MCP listener (binds /tmp/periscope-mcp.sock, runs an MCP Server
per connection) is exercised by `tests/test_channel_smoke.py` against the
sibling `channel_server.py` and by per-peel manual smoke. These pytest
tests cover the pure-logic surface that the listener dispatches into.
"""

import pytest

from periscope.channels import (
    _CHANNEL_ALERTS,
    _CHANNEL_UNREAD,
    _CHANNELS_LOCK,
    _MCP_SESSIONS,
    _channel_gc,
    _do_link_linear_tool,
    _do_link_pr_tool,
    _do_notify_tool,
    _do_open_document_tool,
    recent_alerts,
    rehydrate_alerts_from_events,
)

_WINDOW = {
    "pane_id": "%5", "session": "tc/x", "index": "0",
    "name": "win", "cwd": "/tmp",
}


@pytest.fixture(autouse=True)
def reset_channel_state():
    """Clear the in-memory channel dicts between tests so each one starts
    from a clean slate."""
    with _CHANNELS_LOCK:
        _CHANNEL_ALERTS.clear()
        _CHANNEL_UNREAD.clear()
        _MCP_SESSIONS.clear()
    yield
    with _CHANNELS_LOCK:
        _CHANNEL_ALERTS.clear()
        _CHANNEL_UNREAD.clear()
        _MCP_SESSIONS.clear()


def test_recent_alerts_drops_panes_missing_from_windows():
    """The feed joins against live windows, so a closed pane's alerts fall out
    on their own — the same invariant _channel_gc enforces on the store."""
    _do_notify_tool("%5", {"message": "live", "kind": "info"})
    _do_notify_tool("%99", {"message": "pane is gone", "kind": "need_human"})
    assert [it["message"] for it in recent_alerts([_WINDOW])] == ["live"]


def test_recent_alerts_sorts_newest_first_and_caps_at_limit():
    with _CHANNELS_LOCK:
        _CHANNEL_ALERTS["%5"] = [
            {"id": f"a{i}", "ts": i, "kind": "info",
             "severity": "info", "message": f"m{i}"}
            for i in range(5)
        ]
    items = recent_alerts([_WINDOW], limit=3)
    assert [it["message"] for it in items] == ["m4", "m3", "m2"]


def test_notify_kicks_state_hub_so_the_alert_pushes_immediately(mocker):
    """An alert's whole value is immediacy: notify() must wake the broadcast
    loop instead of letting the alert wait out the steady tick."""
    kick = mocker.patch("periscope.state_hub.kick")
    _do_notify_tool("%5", {"message": "blocked", "kind": "need_human"})
    kick.assert_called_once()


def test_notify_tool_appends_alert_and_bumps_unread():
    _do_notify_tool("%5", {"message": "done", "kind": "done"})
    assert len(_CHANNEL_ALERTS["%5"]) == 1
    entry = _CHANNEL_ALERTS["%5"][0]
    assert entry["message"] == "done"
    assert entry["kind"] == "done"
    assert "ts" in entry
    assert _CHANNEL_UNREAD["%5"] == 1


def test_notify_tool_multiple_alerts_accumulate():
    _do_notify_tool("%5", {"message": "first", "kind": "info"})
    _do_notify_tool("%5", {"message": "second", "kind": "done"})
    assert len(_CHANNEL_ALERTS["%5"]) == 2
    assert _CHANNEL_UNREAD["%5"] == 2


def test_channel_gc_drops_unknown_panes():
    _CHANNEL_ALERTS["%5"] = [{"message": "x", "kind": "info", "ts": 0}]
    _CHANNEL_UNREAD["%5"] = 1
    _CHANNEL_ALERTS["%99"] = [{"message": "y", "kind": "info", "ts": 0}]
    _CHANNEL_UNREAD["%99"] = 1

    _channel_gc({"%5"})

    assert "%5" in _CHANNEL_ALERTS
    assert "%5" in _CHANNEL_UNREAD
    assert "%99" not in _CHANNEL_ALERTS
    assert "%99" not in _CHANNEL_UNREAD


def test_rehydrate_alerts_from_events(fresh_activity_db):
    """After a restart the in-memory cache is empty but the events log survives;
    rehydrate reloads every recent alert per pane (not just one)."""
    import time as _t

    from periscope import activity
    base = int(_t.time()) - 100
    activity.record("pane", "%1", "alert", "tests pass", detail="done", at=base)
    activity.record("pane", "%1", "alert", "need a nudge", detail="need_human", at=base + 10)
    activity.record("pane", "%2", "alert", "fyi", detail="info", at=base + 5)
    # A non-alert event must not be rehydrated as an alert.
    activity.record("pane", "%1", "channel", "hi", detail="message", at=base + 20)

    with _CHANNELS_LOCK:
        _CHANNEL_ALERTS.clear()
        _CHANNEL_UNREAD.clear()

    n = rehydrate_alerts_from_events()
    assert n == 3
    assert len(_CHANNEL_ALERTS["%1"]) == 2          # BOTH of %1's alerts, not one
    assert _CHANNEL_UNREAD["%1"] == 2
    assert {a["kind"] for a in _CHANNEL_ALERTS["%1"]} == {"done", "need_human"}
    assert _CHANNEL_ALERTS["%2"][0]["message"] == "fyi"


def test_rehydrate_skips_panes_with_live_alerts(fresh_activity_db):
    """A notify() racing startup already populated %1; rehydrate must leave it
    untouched wholesale rather than double-count the badge."""
    import time as _t

    from periscope import activity
    base = int(_t.time()) - 100
    activity.record("pane", "%1", "alert", "old", detail="info", at=base)
    activity.record("pane", "%1", "alert", "older", detail="info", at=base - 50)

    with _CHANNELS_LOCK:
        _CHANNEL_ALERTS.clear()
        _CHANNEL_UNREAD.clear()
        _CHANNEL_ALERTS["%1"] = [{"id": "live", "message": "fresh",
                                  "kind": "info", "severity": "info", "ts": 999}]
        _CHANNEL_UNREAD["%1"] = 1

    n = rehydrate_alerts_from_events()
    assert n == 0
    assert len(_CHANNEL_ALERTS["%1"]) == 1
    assert _CHANNEL_ALERTS["%1"][0]["id"] == "live"
    assert _CHANNEL_UNREAD["%1"] == 1


def test_rehydrate_respects_max_age(fresh_activity_db):
    import time as _t

    from periscope import activity
    activity.record("pane", "%1", "alert", "ancient", detail="info",
                    at=int(_t.time()) - 200000)
    activity.record("pane", "%1", "alert", "recent", detail="info",
                    at=int(_t.time()) - 10)

    with _CHANNELS_LOCK:
        _CHANNEL_ALERTS.clear()
        _CHANNEL_UNREAD.clear()

    n = rehydrate_alerts_from_events(max_age_s=3600)
    assert n == 1
    assert _CHANNEL_ALERTS["%1"][0]["message"] == "recent"


def test_link_pr_tool_writes_to_state(clean_state, mocker):
    mocker.patch("periscope.channels._resolve_pid_for_pane", return_value="abc123")
    mocker.patch("periscope.store._write_state")  # don't actually write disk

    _do_link_pr_tool("%5", {"number": 1234})

    assert clean_state["windows"]["abc123"]["linked_pr"] == 1234


def test_link_linear_tool_writes_to_state(clean_state, mocker):
    mocker.patch("periscope.channels._resolve_pid_for_pane", return_value="abc123")
    mocker.patch("periscope.store._write_state")

    _do_link_linear_tool("%5", {"id": "FAR-456"})

    assert clean_state["windows"]["abc123"]["linked_linear"] == "FAR-456"


def test_link_linear_tool_persists_title_and_status(clean_state, mocker):
    mocker.patch("periscope.channels._resolve_pid_for_pane", return_value="abc123")
    mocker.patch("periscope.store._write_state")

    _do_link_linear_tool(
        "%5",
        {"id": "FAR-456", "title": "Fix the thing", "status": "In Progress"},
    )

    entry = clean_state["windows"]["abc123"]
    assert entry["linked_linear"] == "FAR-456"
    assert entry["linked_linear_title"] == "Fix the thing"
    assert entry["linked_linear_status"] == "In Progress"


def test_link_linear_tool_clears_stale_metadata_on_relink(clean_state, mocker):
    """Each call fully describes the link — re-linking with just an id must
    not leave title/status pointing at the previous ticket."""
    mocker.patch("periscope.channels._resolve_pid_for_pane", return_value="abc123")
    mocker.patch("periscope.store._write_state")

    _do_link_linear_tool(
        "%5", {"id": "FAR-1", "title": "First", "status": "Done"}
    )
    _do_link_linear_tool("%5", {"id": "FAR-2"})

    entry = clean_state["windows"]["abc123"]
    assert entry["linked_linear"] == "FAR-2"
    assert "linked_linear_title" not in entry
    assert "linked_linear_status" not in entry


def test_open_document_tool_opens_persisted_tab(clean_state, mocker, tmp_path):
    mocker.patch("periscope.channels._resolve_pid_for_pane", return_value="abc123")
    mocker.patch("periscope.store._write_state")
    f = tmp_path / "spec.md"
    f.write_text("hi")

    _do_open_document_tool("%5", {"path": str(f), "line": 12})

    entry = clean_state["windows"]["abc123"]
    assert entry["open_tabs"] == [{"path": str(f), "line": 12}]
    assert entry["active_tab"] == f"file:{f}"


def test_open_document_tool_resolves_relative_against_pane_cwd(
    clean_state, mocker, tmp_path
):
    mocker.patch("periscope.channels._resolve_pid_for_pane", return_value="abc123")
    mocker.patch("periscope.store._write_state")
    (tmp_path / "doc.md").write_text("hi")
    mocker.patch("periscope.channels.tmux", return_value=f"{tmp_path}\n")

    _do_open_document_tool("%5", {"path": "doc.md"})

    entry = clean_state["windows"]["abc123"]
    assert entry["open_tabs"][0]["path"] == str(tmp_path / "doc.md")


def test_open_document_tool_rejects_missing_file(clean_state, tmp_path):
    import json

    result = _do_open_document_tool("%5", {"path": str(tmp_path / "nope.md")})

    body = json.loads(result[0].text)
    assert body["ok"] is False
    assert "no such file" in body["error"]
    assert clean_state["windows"] == {}


def test_open_document_tool_errors_when_pane_unresolvable(
    clean_state, mocker, tmp_path
):
    import json

    mocker.patch("periscope.channels._resolve_pid_for_pane", return_value="")
    f = tmp_path / "spec.md"
    f.write_text("hi")

    result = _do_open_document_tool("%5", {"path": str(f)})

    body = json.loads(result[0].text)
    assert body["ok"] is False
    assert "could not resolve pid" in body["error"]
    assert clean_state["windows"] == {}


def test_notify_tool_stamps_unique_id():
    # Real signature: _do_notify_tool(pane: str, arguments: dict).
    # Both alerts append under the same pane key in _CHANNEL_ALERTS (a dict
    # pane→list), so read the list under that pane, not the dict itself.
    _do_notify_tool("%5", {"message": "first", "kind": "done"})
    _do_notify_tool("%5", {"message": "second", "kind": "done"})
    entries = _CHANNEL_ALERTS["%5"]
    ids = [a["id"] for a in entries]
    assert all(isinstance(i, str) and i for i in ids)
    assert len(set(ids)) == len(ids)  # unique


# --- history tools ---

def _body(result):
    import json
    return json.loads(result[0].text)


def _row(**over):
    row = {
        "session_id": "abc123", "jsonl_path": "/x.jsonl",
        "project_path": "/Users/tom/dev/periscope", "branch": "main",
        "started_at": 1700000000, "ended_at": 1700003600, "duration_s": 3600,
        "user_msg_count": 10, "asst_msg_count": 20, "tool_use_count": 30,
        "was_interrupted": False, "ended_cleanly": True,
        "summary": "Fixed the spinner", "summary_model": "haiku",
        "tags": ["bugfix"], "first_user_msg": "spinner flickers",
        "last_user_msg": "thanks", "files_touched": [f"/f{i}.py" for i in range(25)],
        "notable_cmds": ["pytest -q"], "tool_use_counts": {"Bash": 30},
        "trivial": False, "is_live": False, "rank": 1,
        "rerank_reason": None, "rerank_score": None,
    }
    row.update(over)
    return row


def test_search_history_tool_trims_fields_and_caps_files(mocker):
    from periscope.channels import _do_search_history_tool
    search = mocker.patch("history.search", return_value=[_row()])

    body = _body(_do_search_history_tool("%5", {"query": "spinner", "limit": 5}))

    search.assert_called_once_with(
        "spinner", project=None, since=None, limit=5)
    assert body["ok"] is True
    r = body["results"][0]
    assert set(r) == {"session_id", "project_path", "branch", "started_at",
                      "duration_s", "summary", "tags", "files_touched",
                      "first_user_msg"}
    assert len(r["files_touched"]) == 10  # capped from 25


def test_search_history_tool_requires_query():
    from periscope.channels import _do_search_history_tool
    body = _body(_do_search_history_tool("%5", {"query": "   "}))
    assert body["ok"] is False


def test_get_history_session_tool_unknown_id(mocker):
    from periscope.channels import _do_get_history_session_tool
    mocker.patch("history.search.get_session", return_value=None)
    body = _body(_do_get_history_session_tool("%5", {"session_id": "nope"}))
    assert body["ok"] is False
    assert "unknown" in body["error"]


def _session_data(n_msgs):
    return {
        "session_id": "abc123", "jsonl_path": "/x.jsonl",
        "project_path": "/p", "branch": "main",
        "started_at": 1, "ended_at": 2, "duration_s": 1,
        "user_msg_count": n_msgs, "asst_msg_count": 0, "tool_use_count": 0,
        "was_interrupted": False, "ended_cleanly": True,
        "summary": "s", "tags": [], "first_user_msg": "f",
        "last_user_msg": "l", "final_assistant_msg": "fa",
        "files_touched": [], "notable_cmds": [], "tool_use_counts": {},
        "jsonl_missing": False,
        "messages": [
            {"role": "user", "uuid": f"u{i}", "ts_ms": i, "text": f"msg {i}"}
            for i in range(n_msgs)
        ],
    }


def test_get_history_session_tool_default_tail_slice(mocker):
    from periscope.channels import _do_get_history_session_tool
    mocker.patch("history.search.get_session",
                 return_value=_session_data(100))

    body = _body(_do_get_history_session_tool("%5", {"session_id": "abc123"}))

    assert body["ok"] is True
    assert body["total_messages"] == 100
    assert len(body["messages"]) == 30
    assert body["messages"][0]["text"] == "msg 70"   # tail-30 by default
    assert body["messages"][-1]["text"] == "msg 99"
    assert body.get("final_assistant_msg") != "messages"  # metadata present
    assert body["summary"] == "s"


def test_get_history_session_tool_explicit_offset_and_clamp(mocker):
    from periscope.channels import _do_get_history_session_tool
    mocker.patch("history.search.get_session",
                 return_value=_session_data(10))

    body = _body(_do_get_history_session_tool(
        "%5", {"session_id": "abc123", "offset": 2, "limit": 3}))
    assert [m["text"] for m in body["messages"]] == ["msg 2", "msg 3", "msg 4"]

    # negative offset beyond start clamps to 0
    body = _body(_do_get_history_session_tool(
        "%5", {"session_id": "abc123", "offset": -50, "limit": 5}))
    assert body["messages"][0]["text"] == "msg 0"


def test_get_history_session_tool_caps_notable_cmds(mocker):
    from periscope.channels import _do_get_history_session_tool
    data = _session_data(5)
    data["notable_cmds"] = ["x" * 500] * 25
    mocker.patch("history.search.get_session", return_value=data)

    body = _body(_do_get_history_session_tool("%5", {"session_id": "abc123"}))

    assert len(body["notable_cmds"]) == 10
    assert all(len(c) < 200 and c.endswith("…[truncated]")
               for c in body["notable_cmds"])


def test_resume_session_tool_wraps_window_new_resume(mocker):
    from periscope.channels import _do_resume_session_tool
    resume = mocker.patch(
        "periscope.routes.sessions._window_new_resume",
        return_value={"ok": True, "target": "resumes:3", "session": "resumes",
                      "index": 3, "mode": "resume",
                      "resumed_session_id": "abc123"})

    body = _body(_do_resume_session_tool("%5", {"session_id": "abc123"}))

    assert body["ok"] is True
    assert body["target"] == "resumes:3"
    args = resume.call_args[0]
    assert args[0] == "resumes"                  # default sentinel session
    assert "--resume abc123" in args[1]
    assert args[2] == "abc123"


def test_resume_session_tool_maps_http_errors(mocker):
    from fastapi import HTTPException

    from periscope.channels import _do_resume_session_tool
    mocker.patch(
        "periscope.routes.sessions._window_new_resume",
        side_effect=HTTPException(409, "session looks live; wait a minute"))

    body = _body(_do_resume_session_tool("%5", {"session_id": "abc123"}))

    assert body["ok"] is False
    assert "looks live" in body["error"]


def test_resume_session_tool_requires_session_id():
    from periscope.channels import _do_resume_session_tool
    body = _body(_do_resume_session_tool("%5", {"session_id": ""}))
    assert body["ok"] is False


def test_resume_session_tool_tags_pane_track(fresh_activity_db, mocker):
    # workspace_id on resume is a TRACK id: tag the resumed pane in pane_tracks.
    from periscope import activity, tracks
    from periscope.channels import _do_resume_session_tool
    tk = tracks.create_track(name="Auth")
    mocker.patch(
        "periscope.routes.sessions._window_new_resume",
        return_value={"ok": True, "target": "resumes:3", "session": "resumes",
                      "index": 3, "mode": "resume", "resumed_session_id": "abc"})
    mocker.patch("periscope.channels.tmux", return_value="%88")  # display-message #{pane_id}

    body = _body(_do_resume_session_tool(
        "%5", {"session_id": "abc", "workspace_id": tk["id"]}))

    assert body["ok"] is True and body["workspace_id"] == tk["id"]
    assert activity.get_pane_track("%88") == tk["id"]


# --- inter-claude management tools ---

import asyncio
import json
from unittest.mock import AsyncMock


def test_resolve_window_by_pid_matches_stamped_handle(mocker):
    from periscope import channels
    rows = [
        {"session": "s", "index": 2, "name": "w", "cwd": "/x",
         "pane_id": "%7", "pid_raw": "abcd1234"},
    ]
    mocker.patch("periscope.channels.list_windows", return_value=rows)

    def _attach(ws):
        for w in ws:
            w["pid"] = w.pop("pid_raw")
    mocker.patch("periscope.channels._attach_git_then_resolve_pids", side_effect=_attach)

    pid, pane_id, window = channels._resolve_window_by_pid("abcd1234")
    assert pid == "abcd1234"
    assert pane_id == "%7"
    assert window["session"] == "s" and window["index"] == 2


def test_resolve_window_by_pid_miss_returns_empty(mocker):
    from periscope import channels
    mocker.patch("periscope.channels.list_windows", return_value=[
        {"session": "s", "index": 2, "pane_id": "%7", "pid_raw": "other"},
    ])
    mocker.patch("periscope.channels._attach_git_then_resolve_pids")
    assert channels._resolve_window_by_pid("abcd1234") == ("", "", {})


def test_resolve_window_by_pid_empty_handle_returns_empty(mocker):
    from periscope import channels
    mocker.patch("periscope.channels.list_windows")
    assert channels._resolve_window_by_pid("") == ("", "", {})


def test_spawn_claude_writes_spawned_by(mocker):
    from periscope import channels
    # tmux session lookup for the caller pane → session|cwd
    mocker.patch("periscope.channels.tmux", return_value="sess|/home/tom")
    mocker.patch("periscope.channels._run", return_value=(0, ""))  # has-session ok
    mocker.patch("periscope.channels._tmux_mutate", return_value=(True, "3"))
    mocker.patch("periscope.channels.os.path.isdir", return_value=True)
    mocker.patch("periscope.channels.asyncio.sleep", new=AsyncMock())
    mocker.patch("periscope.channels._plain_pane_snapshot", return_value="auto mode on")
    mocker.patch("periscope.channels.note_focus")
    mocker.patch("periscope.channels.note_action")
    # Brand-new window: mint a fresh unique pid (stamp_new_window), NOT
    # _resolve_window — the latter rebinds and can steal a live pane's pid.
    stamp = mocker.patch("periscope.channels.stamp_new_window", return_value="child99")
    mocker.patch("periscope.channels._resolve_pid_for_pane", return_value="parent11")
    set_fields = mocker.patch("periscope.channels.set_window_fields")
    # "same" mode now tags the spawned pane into the caller's track — isolate
    # from the real DB (this test has no fresh_activity_db).
    mocker.patch("periscope.channels.tracks.resolve_track_for_window", return_value="tk_x")
    mocker.patch("periscope.channels.tracks.move_pane")

    asyncio.run(channels._do_spawn_claude_tool("%1", {"prompt": "go"}))

    stamp.assert_called_once_with("sess:3")  # mint-fresh, no rebind collision
    set_fields.assert_any_call("child99", spawned_by="parent11")


def test_spawn_claude_no_parent_tolerated(mocker):
    from periscope import channels
    mocker.patch("periscope.channels.tmux", return_value="sess|/home/tom")
    mocker.patch("periscope.channels._run", return_value=(0, ""))
    mocker.patch("periscope.channels._tmux_mutate", return_value=(True, "3"))
    mocker.patch("periscope.channels.os.path.isdir", return_value=True)
    mocker.patch("periscope.channels.asyncio.sleep", new=AsyncMock())
    mocker.patch("periscope.channels._plain_pane_snapshot", return_value="auto mode on")
    mocker.patch("periscope.channels.note_focus")
    mocker.patch("periscope.channels.note_action")
    mocker.patch("periscope.channels.stamp_new_window", return_value="child99")
    mocker.patch("periscope.channels._resolve_pid_for_pane", return_value="")  # vanished caller
    set_fields = mocker.patch("periscope.channels.set_window_fields")
    mocker.patch("periscope.channels.tracks.resolve_track_for_window", return_value="tk_x")
    mocker.patch("periscope.channels.tracks.move_pane")

    result = asyncio.run(channels._do_spawn_claude_tool("%1", {"prompt": "go"}))

    # no spawned_by write when parent pid can't be resolved, and no crash
    for call in set_fields.call_args_list:
        assert "spawned_by" not in call.kwargs
    assert json.loads(result[0].text)["ok"] is True


def test_spawn_commander_anchors_on_cwd(fresh_activity_db, monkeypatch, mocker):
    from periscope import bg_commander, channels, open_ops
    bg_commander.insert_job(id="c1", text="x", cwd="/tmp", at=1)   # caller IS a live commander
    handle = "cmdr:c1"
    monkeypatch.setattr(open_ops, "resolve_worktree_session",
                        lambda cwd: ("proj-sess", {"repo": "/r"}))  # git cwd resolves
    # Mirror the full mock set from test_spawn_claude_writes_spawned_by: the
    # spawn handler shells out heavily, so all of these must be stubbed or the
    # test hits real tmux.
    tmux_mock = mocker.patch("periscope.channels.tmux", return_value="sess|/home/tom")
    mocker.patch("periscope.channels._run", return_value=(0, ""))
    cap = mocker.patch("periscope.channels._tmux_mutate", return_value=(True, "3"))
    mocker.patch("periscope.channels.os.path.isdir", return_value=True)
    mocker.patch("periscope.channels.asyncio.sleep", new=AsyncMock())
    mocker.patch("periscope.channels._plain_pane_snapshot", return_value="auto mode on")
    mocker.patch("periscope.channels.note_focus")
    mocker.patch("periscope.channels.note_action")
    mocker.patch("periscope.channels.stamp_new_window", return_value="child99")
    mocker.patch("periscope.channels._resolve_pid_for_pane", return_value="parent11")
    mocker.patch("periscope.channels.set_window_fields")
    # The commander path makes `anchored` truthy, so place_in_rail fires:
    # stub list_windows + place_in_rail so it never touches real tmux/state.
    mocker.patch("periscope.channels.list_windows", return_value=[])
    mocker.patch.object(open_ops, "place_in_rail", return_value=None)

    asyncio.run(channels._do_spawn_claude_tool(handle, {"prompt": "x", "cwd": "/r"}))

    # A commander handle is not a tmux target — the caller-context derivation
    # `display-message -t cmdr:c1` must be skipped entirely.
    assert not any(
        c.args and c.args[0] == "display-message" and handle in c.args
        for c in tmux_mock.call_args_list
    )
    # The session the worker landed in must be the RESOLVED "proj-sess",
    # never a derived caller session. Inspect the new-session/new-window
    # _tmux_mutate call args for "proj-sess".
    targets = [c.args for c in cap.call_args_list]
    assert any("proj-sess" in a for args in targets for a in args)


def test_spawn_commander_non_git_cwd_errors(fresh_activity_db, monkeypatch):
    from periscope import bg_commander, channels, open_ops
    bg_commander.insert_job(id="c1", text="x", cwd="/tmp", at=1)
    monkeypatch.setattr(open_ops, "resolve_worktree_session", lambda cwd: None)
    monkeypatch.setattr("os.path.isdir", lambda p: True)
    res = asyncio.run(channels._do_spawn_claude_tool("cmdr:c1", {"prompt": "x", "cwd": "/tmp"}))
    assert _body(res)["ok"] is False and "git" in _body(res)["error"].lower()


def test_spawn_with_branch_creates_worktree(fresh_activity_db, monkeypatch, mocker):
    from periscope import channels, open_ops, worktree_spawn
    created = {}
    monkeypatch.setattr(worktree_spawn, "spawn_worktree",
                        lambda repo, branch: created.update(repo=repo, branch=branch) or {"path": "/wt/foo"})
    seen = {}
    def fake_resolve(cwd):
        seen["cwd"] = cwd
        return ("wt-sess", {"repo": "/r"})
    monkeypatch.setattr(open_ops, "resolve_worktree_session", fake_resolve)
    mocker.patch("periscope.channels.tmux", return_value="sess|/home/tom")
    mocker.patch("periscope.channels._run", return_value=(0, ""))
    cap = mocker.patch("periscope.channels._tmux_mutate", return_value=(True, "3"))
    mocker.patch("periscope.channels.os.path.isdir", return_value=True)
    mocker.patch("periscope.channels.asyncio.sleep", new=AsyncMock())
    mocker.patch("periscope.channels._plain_pane_snapshot", return_value="auto mode on")
    mocker.patch("periscope.channels.note_focus")
    mocker.patch("periscope.channels.note_action")
    mocker.patch("periscope.channels.stamp_new_window", return_value="child99")
    mocker.patch("periscope.channels._resolve_pid_for_pane", return_value="parent11")
    mocker.patch("periscope.channels.set_window_fields")
    mocker.patch("periscope.channels.list_windows", return_value=[])
    mocker.patch.object(open_ops, "place_in_rail", return_value=None)

    asyncio.run(channels._do_spawn_claude_tool(
        "%C", {"prompt": "go", "repo": "/r", "branch": "tc/x"}))

    assert created == {"repo": "/r", "branch": "tc/x"}   # worktree created off repo+branch
    assert seen["cwd"] == "/wt/foo"                      # cwd overridden to the new worktree
    targets = [c.args for c in cap.call_args_list]
    assert any("wt-sess" in a for args in targets for a in args)


def test_spawn_branch_without_repo_errors(fresh_activity_db, monkeypatch, mocker):
    from periscope import channels
    mocker.patch("periscope.channels.tmux", return_value="sess|/home/tom")
    mocker.patch("periscope.channels.os.path.isdir", return_value=True)
    res = asyncio.run(channels._do_spawn_claude_tool("%C", {"prompt": "go", "branch": "tc/x"}))
    assert _body(res)["ok"] is False and "repo" in _body(res)["error"].lower()


def test_spawn_workspace_id_tags_pane_track(fresh_activity_db, mocker):
    # workspace_id is now a TRACK id: the spawned pane must be tagged in
    # pane_tracks (not pane_workspaces).
    from periscope import activity, channels, tracks
    tk = tracks.create_track(name="Auth")
    # tmux() returns the spawned pane id from display-message #{pane_id}; mock it
    # to a stable %N so we can assert the tag landed on it.
    mocker.patch("periscope.channels.tmux", return_value="%77")
    mocker.patch("periscope.channels._run", return_value=(0, ""))
    mocker.patch("periscope.channels._tmux_mutate", return_value=(True, "3"))
    mocker.patch("periscope.channels.os.path.isdir", return_value=True)
    mocker.patch("periscope.channels.asyncio.sleep", new=AsyncMock())
    mocker.patch("periscope.channels._plain_pane_snapshot", return_value="auto mode on")
    mocker.patch("periscope.channels.note_focus")
    mocker.patch("periscope.channels.note_action")
    mocker.patch("periscope.channels.stamp_new_window", return_value="child99")
    mocker.patch("periscope.channels._resolve_pid_for_pane", return_value="parent11")
    mocker.patch("periscope.channels.set_window_fields")

    res = _body(asyncio.run(
        channels._do_spawn_claude_tool("%1", {"prompt": "go", "workspace_id": tk["id"]})))

    assert res["ok"] is True
    assert res["workspace_id"] == tk["id"]
    assert activity.get_pane_track("%77") == tk["id"]


def test_spawn_workspace_id_unknown_track_skips_tag(fresh_activity_db, mocker):
    from periscope import activity, channels
    mocker.patch("periscope.channels.tmux", return_value="%77")
    mocker.patch("periscope.channels._run", return_value=(0, ""))
    mocker.patch("periscope.channels._tmux_mutate", return_value=(True, "3"))
    mocker.patch("periscope.channels.os.path.isdir", return_value=True)
    mocker.patch("periscope.channels.asyncio.sleep", new=AsyncMock())
    mocker.patch("periscope.channels._plain_pane_snapshot", return_value="auto mode on")
    mocker.patch("periscope.channels.note_focus")
    mocker.patch("periscope.channels.note_action")
    mocker.patch("periscope.channels.stamp_new_window", return_value="child99")
    mocker.patch("periscope.channels._resolve_pid_for_pane", return_value="parent11")
    mocker.patch("periscope.channels.set_window_fields")

    res = _body(asyncio.run(
        channels._do_spawn_claude_tool("%1", {"prompt": "go", "workspace_id": "tk_nope"})))

    assert res["ok"] is True and res["workspace_id"] is None
    assert activity.get_pane_track("%77") is None


def test_spawn_anchored_tags_repo_default_track(fresh_activity_db, monkeypatch, mocker):
    # workspace="new" anchors the spawn to its cwd's worktree and tags the
    # spawned pane into the repo-default track for that repo.
    from periscope import activity, channels, open_ops, tracks
    monkeypatch.setattr(open_ops, "resolve_worktree_session",
                        lambda cwd: ("proj-sess", {"repo": "/r"}))
    mocker.patch("periscope.channels.tmux", return_value="%77")
    mocker.patch("periscope.channels._run", return_value=(0, ""))
    mocker.patch("periscope.channels._tmux_mutate", return_value=(True, "3"))
    mocker.patch("periscope.channels.os.path.isdir", return_value=True)
    mocker.patch("periscope.channels.asyncio.sleep", new=AsyncMock())
    mocker.patch("periscope.channels._plain_pane_snapshot", return_value="auto mode on")
    mocker.patch("periscope.channels.note_focus")
    mocker.patch("periscope.channels.note_action")
    mocker.patch("periscope.channels.stamp_new_window", return_value="child99")
    mocker.patch("periscope.channels._resolve_pid_for_pane", return_value="parent11")
    mocker.patch("periscope.channels.set_window_fields")
    mocker.patch("periscope.channels.list_windows", return_value=[])
    mocker.patch.object(open_ops, "place_in_rail", return_value=None)

    res = _body(asyncio.run(channels._do_spawn_claude_tool(
        "%1", {"prompt": "go", "cwd": "/r", "workspace": "new"})))

    assert res["ok"] is True
    # repo_default_track keys the track id on the repo path.
    assert activity.get_pane_track("%77") == tracks.repo_default_track("/r")


def test_send_to_happy(mocker):
    from periscope import channels
    mocker.patch("periscope.channels._resolve_window_by_pid",
                 return_value=("ab12", "%9", {"session": "s", "index": 2}))
    emit = mocker.patch("periscope.channels.emit_channel_event",
                        new=AsyncMock(return_value=True))

    body = _body(asyncio.run(channels._do_send_to_tool("%1", {"handle": "ab12", "message": "hi"})))

    emit.assert_awaited_once_with("%9", "hi")
    assert body == {"ok": True, "handle": "ab12", "pane_id": "%9"}


def test_send_to_no_window(mocker):
    from periscope import channels
    mocker.patch("periscope.channels._resolve_window_by_pid", return_value=("", "", {}))
    body = _body(asyncio.run(channels._do_send_to_tool("%1", {"handle": "ab12", "message": "hi"})))
    assert body["ok"] is False and "no live window" in body["error"]


def test_send_to_not_attached(mocker):
    from periscope import channels
    mocker.patch("periscope.channels._resolve_window_by_pid",
                 return_value=("ab12", "%9", {}))
    mocker.patch("periscope.channels.emit_channel_event", new=AsyncMock(return_value=False))
    body = _body(asyncio.run(channels._do_send_to_tool("%1", {"handle": "ab12", "message": "hi"})))
    assert body["ok"] is False and "not attached" in body["error"]


def test_send_to_self_refused(mocker):
    from periscope import channels
    mocker.patch("periscope.channels._resolve_window_by_pid",
                 return_value=("ab12", "%1", {}))
    emit = mocker.patch("periscope.channels.emit_channel_event", new=AsyncMock(return_value=True))
    body = _body(asyncio.run(channels._do_send_to_tool("%1", {"handle": "ab12", "message": "hi"})))
    assert body["ok"] is False and "own pane" in body["error"]
    emit.assert_not_awaited()


def test_send_to_missing_args(mocker):
    from periscope import channels
    body = _body(asyncio.run(channels._do_send_to_tool("%1", {"handle": "", "message": "hi"})))
    assert body["ok"] is False and "handle" in body["error"]


def test_report_routes_to_spawner(mocker):
    from periscope import channels
    mocker.patch("periscope.channels._resolve_pid_for_pane", return_value="child99")
    mocker.patch("periscope.channels.get_window", return_value={"spawned_by": "parent11"})
    mocker.patch("periscope.channels._resolve_window_by_pid",
                 return_value=("parent11", "%2", {}))
    emit = mocker.patch("periscope.channels.emit_channel_event", new=AsyncMock(return_value=True))

    body = _body(asyncio.run(channels._do_report_tool("%9", {"message": "done"})))

    emit.assert_awaited_once_with("%2", "done")
    assert body == {"ok": True, "to": "parent11"}


def test_emit_channel_event_records_full_text_into_activity(fresh_activity_db):
    from periscope import activity, channels
    sess = AsyncMock()
    with _CHANNELS_LOCK:
        _MCP_SESSIONS["%5"] = sess
    body = "a long message periscope pushed into the pane " * 3
    sent = asyncio.run(channels.emit_channel_event("%5", body))
    assert sent is True
    sess._write_stream.send.assert_awaited_once()
    out = activity.events_for("%5", None, None)
    assert len(out) == 1
    assert out[0]["src"] == "channel"
    assert out[0]["kind"] == "message"
    assert out[0]["text"] == body   # untruncated


def test_emit_channel_event_unattached_records_nothing(fresh_activity_db):
    from periscope import activity, channels
    sent = asyncio.run(channels.emit_channel_event("%nope", "hi"))
    assert sent is False
    assert activity.events_for("%nope", None, None) == []


def test_report_no_spawner_errors(mocker):
    from periscope import channels
    mocker.patch("periscope.channels._resolve_pid_for_pane", return_value="child99")
    mocker.patch("periscope.channels.get_window", return_value={})  # no spawned_by
    body = _body(asyncio.run(channels._do_report_tool("%9", {"message": "done"})))
    assert body["ok"] is False and "no spawner" in body["error"]


def test_report_spawner_gone(mocker):
    from periscope import channels
    mocker.patch("periscope.channels._resolve_pid_for_pane", return_value="child99")
    mocker.patch("periscope.channels.get_window", return_value={"spawned_by": "parent11"})
    mocker.patch("periscope.channels._resolve_window_by_pid", return_value=("", "", {}))
    body = _body(asyncio.run(channels._do_report_tool("%9", {"message": "done"})))
    assert body["ok"] is False and "no longer live" in body["error"]


def test_list_claudes_filters_and_trims(mocker):
    from periscope import channels
    rows = [
        {"session": "s", "index": 1, "name": "lead", "cwd": "/a", "pane_id": "%2", "pid_raw": "p1"},
        {"session": "s", "index": 2, "name": "shell", "cwd": "/b", "pane_id": "%3", "pid_raw": "p2"},
    ]
    mocker.patch("periscope.channels.list_windows", return_value=rows)

    def _attach(ws):
        for w in ws:
            if "pid_raw" in w:
                w["pid"] = w.pop("pid_raw")
    mocker.patch("periscope.channels._attach_git_then_resolve_pids", side_effect=_attach)

    # capture returns the target so parse_pane can tell windows apart; is_claude
    # true only for the "s:1" pane. This exercises the real per-pane probe path.
    mocker.patch("periscope.tmux.capture", side_effect=lambda target, *a, **k: target)
    mocker.patch("periscope.panes.parse_pane",
                 side_effect=lambda content: {"is_claude": content == "s:1"})
    mocker.patch("periscope.activity.pane_status_lines",
                 return_value={"%2": ("reviewing PR", 123, None)})
    mocker.patch("periscope.channels.channel_state_for", return_value={"attached": True})
    mocker.patch("periscope.channels.get_window", return_value={"spawned_by": "boss0"})

    body = _body(asyncio.run(channels._do_list_claudes_tool("%1", {})))

    assert body["ok"] is True
    assert len(body["claudes"]) == 1
    c = body["claudes"][0]
    assert c == {
        "handle": "p1", "name": "lead", "session": "s", "cwd": "/a",
        "status_line_inferred": "reviewing PR", "attached": True,
        "spawned_by": "boss0",
    }
    assert "pane_id" not in c
    # The `_inferred` suffix is the entire P1 fix — a bare `status_line` reads
    # as the pane's own report and got a correctly-working worker interrupted.
    assert "status_line" not in c


def test_peek_happy(mocker):
    from periscope import channels
    mocker.patch("periscope.channels._resolve_window_by_pid",
                 return_value=("ab12", "%9", {}))
    mocker.patch("periscope.turns.session_id_for_pane", return_value="sess-abc")
    mocker.patch("periscope.turns.jsonl_for_session", return_value="/x/sess-abc.jsonl")
    msgs = [{"role": "user", "text": str(i)} for i in range(30)]
    mocker.patch("periscope.turns.messages_from_jsonl", return_value=msgs)

    body = _body(channels._do_peek_tool("%1", {"handle": "ab12"}))

    assert body["ok"] is True and body["handle"] == "ab12"
    assert len(body["turns"]) == 20
    assert body["turns"][-1]["text"] == "29"


def test_peek_no_session_refuses_without_transcript_read(mocker):
    from periscope import channels
    mocker.patch("periscope.channels._resolve_window_by_pid",
                 return_value=("ab12", "%9", {}))
    mocker.patch("periscope.turns.session_id_for_pane", return_value=None)
    jsonl = mocker.patch("periscope.turns.jsonl_for_session")
    msgs = mocker.patch("periscope.turns.messages_from_jsonl")

    body = _body(channels._do_peek_tool("%1", {"handle": "ab12"}))

    assert body["ok"] is False and "no recorded session" in body["error"]
    jsonl.assert_not_called()   # the cwd-collision footgun is unreachable
    msgs.assert_not_called()


def test_peek_no_window(mocker):
    from periscope import channels
    mocker.patch("periscope.channels._resolve_window_by_pid", return_value=("", "", {}))
    body = _body(channels._do_peek_tool("%1", {"handle": "ab12"}))
    assert body["ok"] is False and "no live window" in body["error"]


def test_terminate_happy(mocker):
    from periscope import channels
    mocker.patch("periscope.channels._resolve_window_by_pid",
                 return_value=("ab12", "%9", {"session": "s", "index": 4}))
    mut = mocker.patch("periscope.channels._tmux_mutate", return_value=(True, ""))
    body = _body(channels._do_terminate_tool("%1", {"handle": "ab12"}))
    mut.assert_called_once_with("kill-window", "-t", "s:4")
    assert body == {"ok": True, "terminated": "ab12"}


def test_terminate_self_refused(mocker):
    from periscope import channels
    mocker.patch("periscope.channels._resolve_window_by_pid",
                 return_value=("ab12", "%1", {"session": "s", "index": 4}))
    mut = mocker.patch("periscope.channels._tmux_mutate")
    body = _body(channels._do_terminate_tool("%1", {"handle": "ab12"}))
    assert body["ok"] is False and "own pane" in body["error"]
    mut.assert_not_called()


def test_terminate_no_window(mocker):
    from periscope import channels
    mocker.patch("periscope.channels._resolve_window_by_pid", return_value=("", "", {}))
    body = _body(channels._do_terminate_tool("%1", {"handle": "ab12"}))
    assert body["ok"] is False and "no live window" in body["error"]


def test_terminate_mutate_failure(mocker):
    from periscope import channels
    mocker.patch("periscope.channels._resolve_window_by_pid",
                 return_value=("ab12", "%9", {"session": "s", "index": 4}))
    mocker.patch("periscope.channels._tmux_mutate", return_value=(False, "no such window"))
    body = _body(channels._do_terminate_tool("%1", {"handle": "ab12"}))
    assert body["ok"] is False and body["error"] == "no such window"


# --- commander identity ---

def test_is_commander_checks_the_cmdr_prefix():
    from periscope import channels
    # prefix-only: the cmdr handle is a self-asserted token (owner-only socket);
    # it is NOT claude's session id, so there's no jobs-table cross-check.
    assert channels.is_commander("%3") is False
    assert channels.is_commander("") is False
    assert channels.is_commander("cmdr:anything") is True
    assert channels.is_commander("cmdr:7f3a9b21") is True


def test_list_workspaces_tool_returns_ids_and_live_counts(clean_state, fresh_activity_db, mocker):
    # list_workspaces lists TRACKS now (response key stays `workspaces`).
    from periscope import activity, tracks
    from periscope.channels import _do_list_workspaces_tool
    tk = tracks.create_track(name="Auth", repo="/d/fdy")
    activity.set_pane_track("%1", tk["id"])
    activity.set_pane_track("%99", tk["id"])   # dead pane — excluded from count
    # LOOSE catchall must never surface in the list.
    activity.set_pane_track("%1", tk["id"])
    mocker.patch("periscope.channels.list_windows", return_value=[{"pane_id": "%1"}])
    r = _body(_do_list_workspaces_tool("%1", {}))
    assert r["ok"] is True
    rows = {w["id"]: w for w in r["workspaces"]}
    assert tracks.LOOSE_KEY not in rows
    assert tk["id"] in rows
    assert rows[tk["id"]]["name"] == "Auth"
    assert rows[tk["id"]]["base_repo"] == "/d/fdy"   # track.repo mapped to base_repo
    assert rows[tk["id"]]["tagged_tabs"] == 1        # %99 dead, not counted


def test_list_workspaces_tool_excludes_archived(clean_state, fresh_activity_db, mocker):
    from periscope import activity, tracks
    from periscope.channels import _do_list_workspaces_tool
    tk = tracks.create_track(name="Gone")
    activity.archive_track(tk["id"], ts=1)
    mocker.patch("periscope.channels.list_windows", return_value=[])
    r = _body(_do_list_workspaces_tool("%1", {}))
    assert all(w["id"] != tk["id"] for w in r["workspaces"])


def test_create_workspace_tool(fresh_activity_db):
    # create_workspace now creates a TRACK (response key stays `workspace_id`).
    from periscope import activity, channels
    res = channels._do_create_workspace_tool("%1", {"name": "x", "base_repo": "/r"})
    body = _body(res)
    assert body["ok"] is True and body["name"] == "x"
    row = activity.get_track(body["workspace_id"])
    assert row is not None and row["name"] == "x" and row["repo"] == "/r"


def test_open_tool_dispatches_path(monkeypatch):
    from periscope import channels, open_ops
    seen = {}
    def fake_open(desc):
        seen["desc"] = desc
        class R: tmux_session="s"; repo="/r"; claude_pid="@1"; claude_pane_id="%2"; ui={}
        return R()
    monkeypatch.setattr(open_ops, "open_target", fake_open)
    res = channels._do_open_tool("%1", {"path": "/r"})
    body = _body(res)
    assert body["ok"] is True and isinstance(seen["desc"], open_ops.PathTarget)


def test_open_tool_bad_args(monkeypatch):
    from periscope import channels
    res = channels._do_open_tool("%1", {})   # none of path|repo+branch|repo+pr
    assert _body(res)["ok"] is False


def test_catalog_tool(monkeypatch):
    from periscope import channels, open_ops
    monkeypatch.setattr(open_ops, "build_catalog", lambda: {"repos": [], "worktrees": []})
    res = channels._do_catalog_tool("%1", {})
    assert _body(res)["ok"] is True
