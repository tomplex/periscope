"""Channels: tool implementations + alert log + GC.

The live MCP listener (binds /tmp/periscope-mcp.sock, runs an MCP Server
per connection) is exercised by `tests/test_channel_smoke.py` against the
sibling `channel_server.py` and by per-peel manual smoke. These pytest
tests cover the pure-logic surface that the listener dispatches into.
"""

import pytest

from periscope.channels import (
    _CHANNELS_LOCK, _CHANNEL_ALERTS, _CHANNEL_UNREAD, _MCP_SESSIONS,
    _CHANNEL_OPEN_DOCS, _OPEN_DOC_TTL_S,
    _channel_gc, channel_state_for,
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
        _CHANNEL_OPEN_DOCS.clear()
    yield
    with _CHANNELS_LOCK:
        _CHANNEL_ALERTS.clear()
        _CHANNEL_UNREAD.clear()
        _MCP_SESSIONS.clear()
        _CHANNEL_OPEN_DOCS.clear()


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


def test_open_document_tool_queues_request(tmp_path):
    f = tmp_path / "spec.md"
    f.write_text("hi")

    _do_open_document_tool("%5", {"path": str(f), "line": 12})

    docs = _CHANNEL_OPEN_DOCS["%5"]
    assert len(docs) == 1
    assert docs[0]["path"] == str(f)
    assert docs[0]["line"] == 12
    assert docs[0]["id"]
    # surfaced through channel_state_for (→ window view → /api/state)
    assert channel_state_for("%5")["open_docs"] == docs


def test_open_document_tool_resolves_relative_against_pane_cwd(tmp_path, mocker):
    (tmp_path / "doc.md").write_text("hi")
    mocker.patch("periscope.channels.tmux", return_value=f"{tmp_path}\n")

    _do_open_document_tool("%5", {"path": "doc.md"})

    assert _CHANNEL_OPEN_DOCS["%5"][0]["path"] == str(tmp_path / "doc.md")


def test_open_document_tool_rejects_missing_file(tmp_path):
    import json

    result = _do_open_document_tool("%5", {"path": str(tmp_path / "nope.md")})

    body = json.loads(result[0].text)
    assert body["ok"] is False
    assert "no such file" in body["error"]
    assert "%5" not in _CHANNEL_OPEN_DOCS


def test_open_document_requests_expire_by_ttl(tmp_path):
    f = tmp_path / "spec.md"
    f.write_text("hi")
    _do_open_document_tool("%5", {"path": str(f)})
    _CHANNEL_OPEN_DOCS["%5"][0]["ts"] -= _OPEN_DOC_TTL_S + 1

    assert channel_state_for("%5")["open_docs"] == []
    assert "%5" not in _CHANNEL_OPEN_DOCS


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
