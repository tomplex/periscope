"""Channels: tool implementations + alert log + GC.

The live MCP listener (binds /tmp/periscope-mcp.sock, runs an MCP Server
per connection) is exercised by `tests/test_channel_smoke.py` against the
sibling `channel_server.py` and by per-peel manual smoke. These pytest
tests cover the pure-logic surface that the listener dispatches into.
"""

import pytest

from periscope.channels import (
    _CHANNELS_LOCK, _CHANNEL_ALERTS, _CHANNEL_UNREAD, _MCP_SESSIONS,
    _channel_gc,
    _do_notify_tool, _do_link_pr_tool, _do_link_linear_tool,
    _do_open_document_tool,
)


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
    assert "messages" != body.get("final_assistant_msg")  # metadata present
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
    mocker.patch("periscope.channels._resolve_window", return_value=("child99", "%9"))
    mocker.patch("periscope.channels._resolve_pid_for_pane", return_value="parent11")
    set_fields = mocker.patch("periscope.channels.set_window_fields")

    asyncio.run(channels._do_spawn_claude_tool("%1", {"prompt": "go"}))

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
    mocker.patch("periscope.channels._resolve_window", return_value=("child99", "%9"))
    mocker.patch("periscope.channels._resolve_pid_for_pane", return_value="")  # vanished caller
    set_fields = mocker.patch("periscope.channels.set_window_fields")

    result = asyncio.run(channels._do_spawn_claude_tool("%1", {"prompt": "go"}))

    # no spawned_by write when parent pid can't be resolved, and no crash
    for call in set_fields.call_args_list:
        assert "spawned_by" not in call.kwargs
    assert json.loads(result[0].text)["ok"] is True


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
