"""Channels: tool implementations + reply log + GC.

The live MCP listener (binds /tmp/periscope-mcp.sock, runs an MCP Server
per connection) is exercised by `tests/test_channel_smoke.py` against the
sibling `channel_server.py` and by per-peel manual smoke. These pytest
tests cover the pure-logic surface that the listener dispatches into.
"""

import pytest

from periscope.channels import (
    _CHANNELS_LOCK, _CHANNEL_REPLIES, _CHANNEL_UNREAD, _MCP_SESSIONS,
    _channel_gc,
    _do_reply_tool, _do_link_pr_tool, _do_link_linear_tool,
)


@pytest.fixture(autouse=True)
def reset_channel_state():
    """Clear the in-memory channel dicts between tests so each one starts
    from a clean slate."""
    with _CHANNELS_LOCK:
        _CHANNEL_REPLIES.clear()
        _CHANNEL_UNREAD.clear()
        _MCP_SESSIONS.clear()
    yield
    with _CHANNELS_LOCK:
        _CHANNEL_REPLIES.clear()
        _CHANNEL_UNREAD.clear()
        _MCP_SESSIONS.clear()


def test_reply_tool_appends_reply_and_bumps_unread():
    _do_reply_tool("%5", {"message": "done", "kind": "done"})
    assert len(_CHANNEL_REPLIES["%5"]) == 1
    entry = _CHANNEL_REPLIES["%5"][0]
    assert entry["message"] == "done"
    assert entry["kind"] == "done"
    assert "ts" in entry
    assert _CHANNEL_UNREAD["%5"] == 1


def test_reply_tool_multiple_replies_accumulate():
    _do_reply_tool("%5", {"message": "first", "kind": "info"})
    _do_reply_tool("%5", {"message": "second", "kind": "done"})
    assert len(_CHANNEL_REPLIES["%5"]) == 2
    assert _CHANNEL_UNREAD["%5"] == 2


def test_channel_gc_drops_unknown_panes():
    _CHANNEL_REPLIES["%5"] = [{"message": "x", "kind": "info", "ts": 0}]
    _CHANNEL_UNREAD["%5"] = 1
    _CHANNEL_REPLIES["%99"] = [{"message": "y", "kind": "info", "ts": 0}]
    _CHANNEL_UNREAD["%99"] = 1

    _channel_gc({"%5"})

    assert "%5" in _CHANNEL_REPLIES
    assert "%5" in _CHANNEL_UNREAD
    assert "%99" not in _CHANNEL_REPLIES
    assert "%99" not in _CHANNEL_UNREAD


def test_link_pr_tool_writes_to_state(monkeypatch, mocker):
    # Seed _STATE directly via monkeypatch (NOT the clean_state fixture).
    # The fixture imports periscope.store which doesn't exist until Peel 4;
    # in Peel 3 we test against server._STATE directly. Peel 4 Task 4.3
    # updates this test to use the clean_state fixture.
    import server
    fresh = {"version": 1, "ui": {}, "windows": {}, "commands": []}
    monkeypatch.setattr(server, "_STATE", fresh)
    mocker.patch("periscope.channels._resolve_pid_for_pane", return_value="abc123")
    mocker.patch("server._write_state")  # don't actually write disk

    _do_link_pr_tool("%5", {"number": 1234})

    assert fresh["windows"]["abc123"]["linked_pr"] == 1234


def test_link_linear_tool_writes_to_state(monkeypatch, mocker):
    import server
    fresh = {"version": 1, "ui": {}, "windows": {}, "commands": []}
    monkeypatch.setattr(server, "_STATE", fresh)
    mocker.patch("periscope.channels._resolve_pid_for_pane", return_value="abc123")
    mocker.patch("server._write_state")

    _do_link_linear_tool("%5", {"id": "FAR-456"})

    assert fresh["windows"]["abc123"]["linked_linear"] == "FAR-456"
