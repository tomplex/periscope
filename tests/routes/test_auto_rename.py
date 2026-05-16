"""Tests for /api/auto-rename-session and /api/auto-rename-window."""

import json

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    from periscope.app import app
    return TestClient(app)


def _patch(mocker, base_path, name, **kwargs):
    """Patch a name across both possible locations during the move."""
    for prefix in (f"periscope.routes.auto_rename.{name}", f"{base_path}.{name}"):
        try:
            return mocker.patch(prefix, **kwargs)
        except (AttributeError, ModuleNotFoundError):
            continue


def test_auto_rename_session_applies_new_names(client, mocker):
    fake_windows = [
        {"session": "main", "index": 0, "name": "old0", "active": True, "cwd": "/tmp", "pid_raw": ""},
        {"session": "main", "index": 1, "name": "old1", "active": False, "cwd": "/tmp", "pid_raw": ""},
    ]

    _patch(mocker, "server", "list_windows", return_value=fake_windows)
    _patch(mocker, "server", "_attach_git_then_resolve_pids")
    _patch(mocker, "server", "capture", return_value="some output")
    _patch(mocker, "server", "parse_pane", return_value={"recap": "", "pending_input": ""})
    _patch(mocker, "server", "cached_git_state", return_value={"branch": "main"})
    _patch(mocker, "server", "cached_pr_state", return_value={"pr": None})
    _patch(mocker, "server", "build_rename_prompt", return_value="prompt")
    _patch(
        mocker, "server", "claude_complete",
        return_value=json.dumps({"0": "fresh0", "1": "fresh1"}),
    )
    tmux_mock = _patch(mocker, "server", "tmux", return_value="")

    r = client.post("/api/auto-rename-session?session=main")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert {a["new"] for a in body["applied"]} == {"fresh0", "fresh1"}
    # tmux rename-window must have been invoked.
    rename_calls = [c for c in tmux_mock.call_args_list if c.args and c.args[0] == "rename-window"]
    assert len(rename_calls) == 2


def test_auto_rename_session_unknown_session_returns_error(client, mocker):
    _patch(mocker, "server", "list_windows", return_value=[])
    _patch(mocker, "server", "_attach_git_then_resolve_pids")
    r = client.post("/api/auto-rename-session?session=nope")
    body = r.json()
    assert body["ok"] is False
    assert "not found" in body["error"]


def test_auto_rename_window_applies(client, mocker):
    # display-message returns "name\tcwd".
    tmux_mock = _patch(mocker, "server", "tmux", return_value="oldname\t/tmp")
    _patch(mocker, "server", "_attach_git_then_resolve_pids")
    _patch(mocker, "server", "capture", return_value="some output")
    _patch(mocker, "server", "parse_pane", return_value={"recap": "", "pending_input": ""})
    _patch(mocker, "server", "cached_git_state", return_value={"branch": "main"})
    _patch(mocker, "server", "cached_pr_state", return_value={"pr": None})
    _patch(mocker, "server", "build_rename_prompt", return_value="prompt")
    _patch(mocker, "server", "claude_complete", return_value=json.dumps({"0": "newname"}))

    r = client.post("/api/auto-rename-window?session=main&index=0")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["applied"] is True
    assert body["new"] == "newname"
