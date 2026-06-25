"""Tests for /api/auto-rename-session and /api/auto-rename-window."""

import json


def _patch(mocker, base_path, name, **kwargs):
    """Patch a name across both possible locations during the move."""
    for prefix in (f"periscope.routes.auto_rename.{name}", f"{base_path}.{name}"):
        try:
            return mocker.patch(prefix, **kwargs)
        except (AttributeError, ModuleNotFoundError):
            continue
    return None


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
    assert r.status_code == 404
    assert "not found" in r.json()["detail"]


def test_auto_rename_window_applies(client, mocker):
    # The route reads the window's name/cwd via pane_meta() (its own tmux call
    # is internal to periscope.tmux, so patching auto_rename.tmux can't reach
    # it). Mock pane_meta directly so the test never depends on a real tmux
    # session existing on the dev machine.
    _patch(mocker, "server", "pane_meta", return_value=("oldname", "/tmp"))
    _patch(mocker, "server", "tmux", return_value="oldname\t/tmp")
    _patch(mocker, "server", "stamp_pane_rename")
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


def test_auto_rename_session_stamps_cooldown_per_pane(client, mocker):
    fake_windows = [
        {"session": "main", "index": 0, "name": "old0", "active": True,
         "cwd": "/tmp", "pid_raw": "", "pane_id": "%10"},
        {"session": "main", "index": 1, "name": "old1", "active": False,
         "cwd": "/tmp", "pid_raw": "", "pane_id": "%11"},
    ]
    _patch(mocker, "server", "list_windows", return_value=fake_windows)
    _patch(mocker, "server", "_attach_git_then_resolve_pids")
    _patch(mocker, "server", "capture", return_value="some output")
    _patch(mocker, "server", "parse_pane", return_value={"recap": "", "pending_input": ""})
    _patch(mocker, "server", "cached_git_state", return_value={"branch": "main"})
    _patch(mocker, "server", "cached_pr_state", return_value={"pr": None})
    _patch(mocker, "server", "build_rename_prompt", return_value="prompt")
    _patch(mocker, "server", "claude_complete",
           return_value=json.dumps({"0": "fresh0", "1": "fresh1"}))
    _patch(mocker, "server", "tmux", return_value="")
    stamp = _patch(mocker, "server", "stamp_pane_rename")

    r = client.post("/api/auto-rename-session?session=main")
    assert r.status_code == 200
    stamped = {(c.args[0], c.kwargs["name"]) for c in stamp.call_args_list}
    assert stamped == {("%10", "fresh0"), ("%11", "fresh1")}


def test_auto_rename_window_stamps_cooldown(client, mocker):
    # pane_meta executes in the periscope.tmux namespace, so the route-level
    # tmux patch below does NOT cover it — patch it directly to stay hermetic
    # (the pre-existing window test leans on a real `main` tmux session).
    _patch(mocker, "server", "pane_meta", return_value=("oldname", "/tmp"))

    def fake_tmux(*args):
        if args[0] == "display-message" and "#{pane_id}" in args:
            return "%9\n"
        return ""
    _patch(mocker, "server", "tmux", side_effect=fake_tmux)
    _patch(mocker, "server", "_attach_git_then_resolve_pids")
    _patch(mocker, "server", "capture", return_value="some output")
    _patch(mocker, "server", "parse_pane", return_value={"recap": "", "pending_input": ""})
    _patch(mocker, "server", "cached_git_state", return_value={"branch": "main"})
    _patch(mocker, "server", "cached_pr_state", return_value={"pr": None})
    _patch(mocker, "server", "build_rename_prompt", return_value="prompt")
    _patch(mocker, "server", "claude_complete", return_value=json.dumps({"0": "newname"}))
    stamp = _patch(mocker, "server", "stamp_pane_rename")

    r = client.post("/api/auto-rename-window?session=main&index=0")
    assert r.status_code == 200
    stamp.assert_called_once()
    assert stamp.call_args.args[0] == "%9"
    assert stamp.call_args.kwargs["name"] == "newname"
    assert isinstance(stamp.call_args.kwargs["at"], int)
