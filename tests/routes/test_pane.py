"""Tests for /api/pane and /api/rename."""


def _patch(mocker, name, **kwargs):
    for prefix in (f"periscope.routes.pane.{name}", f"server.{name}"):
        try:
            return mocker.patch(prefix, **kwargs)
        except (AttributeError, ModuleNotFoundError):
            continue


def test_pane_returns_parsed_payload(client, mocker):
    def fake_tmux(*args):
        if args and args[0] == "capture-pane":
            return "$ ls\nfoo bar\n"
        if args and args[0] == "display-message":
            return "win-name\t/tmp/proj"
        return ""

    _patch(mocker, "tmux", side_effect=fake_tmux)
    _patch(mocker, "parse_pane", return_value={"is_claude": False, "spinner": None})
    _patch(mocker, "smooth_spinner", side_effect=lambda t, s: s)
    _patch(mocker, "smooth_is_claude", side_effect=lambda t, c: c)
    _patch(mocker, "cached_git_state", return_value={"branch": "main"})
    _patch(mocker, "cached_pr_state", return_value={"pr": None})
    _patch(mocker, "cached_pane_activity", return_value=[])
    _patch(mocker, "_attach_git_then_resolve_pids")
    _patch(mocker, "list_windows", return_value=[
        {"session": "main", "index": 0, "name": "x", "pane_id": "%7", "cwd": "/tmp"}
    ])
    _patch(mocker, "cached_lgtm_state", return_value=None)

    r = client.get("/api/pane?session=main&index=0")
    assert r.status_code == 200
    body = r.json()
    assert body["target"] == "main:0"
    assert body["session"] == "main"
    assert body["name"] == "win-name"
    assert "content" in body


def test_rename_sets_name(client, mocker):
    tmux_mock = mocker.MagicMock(return_value="")
    for path in ("periscope.routes.pane.tmux", "server.tmux"):
        try:
            mocker.patch(path, tmux_mock)
            break
        except (AttributeError, ModuleNotFoundError):
            continue

    r = client.post(
        "/api/rename?session=main&index=0",
        json={"name": "fresh-name"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["name"] == "fresh-name"
    assert any(
        c.args and c.args[0] == "rename-window" and "fresh-name" in c.args
        for c in tmux_mock.call_args_list
    )


def test_rename_rejects_empty(client, mocker):
    for path in ("periscope.routes.pane.tmux", "server.tmux"):
        try:
            mocker.patch(path, return_value="")
            break
        except (AttributeError, ModuleNotFoundError):
            continue
    r = client.post("/api/rename?session=main&index=0", json={"name": "   "})
    body = r.json()
    assert body["ok"] is False
    assert "empty" in body["error"]
