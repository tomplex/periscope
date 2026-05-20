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
