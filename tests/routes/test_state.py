"""Tests for GET /api/state — the big aggregator."""


def _patch(mocker, name, **kwargs):
    for prefix in (f"periscope.routes.state.{name}", f"server.{name}"):
        try:
            return mocker.patch(prefix, **kwargs)
        except (AttributeError, ModuleNotFoundError):
            continue


def test_state_empty(client, mocker, clean_state):
    _patch(mocker, "list_windows", return_value=[])
    _patch(mocker, "update_focus_from_windows")
    _patch(mocker, "_attach_git_then_resolve_pids")
    _patch(mocker, "cached_claude_usage", return_value={})
    _patch(mocker, "cached_scraped_usage", return_value={})

    r = client.get("/api/state")
    assert r.status_code == 200
    body = r.json()
    assert body["windows"] == []
    assert "ts" in body
    assert "usage" in body


def test_state_with_window(client, mocker, clean_state):
    fake_windows = [
        {
            "session": "main", "index": 0, "name": "x",
            "active": True, "cwd": "/tmp", "pid_raw": "",
            "pane_id": "%7", "pid": "abc123",
        },
    ]
    _patch(mocker, "list_windows", return_value=fake_windows)
    _patch(mocker, "update_focus_from_windows")
    _patch(mocker, "_attach_git_then_resolve_pids")
    _patch(mocker, "capture", return_value="$ ls\n")
    _patch(mocker, "parse_pane", return_value={"is_claude": False, "spinner": None, "state": "shell"})
    _patch(mocker, "smooth_spinner", side_effect=lambda t, s: s)
    _patch(mocker, "smooth_is_claude", side_effect=lambda t, c: c)
    _patch(mocker, "cached_git_state", return_value={"branch": "main"})
    _patch(mocker, "cached_pr_state", return_value={"pr": None})
    _patch(mocker, "cached_lgtm_state", return_value=None)
    _patch(mocker, "cached_claude_usage", return_value={})
    _patch(mocker, "cached_scraped_usage", return_value={})
    _patch(mocker, "_channel_gc")

    r = client.get("/api/state")
    assert r.status_code == 200
    body = r.json()
    assert len(body["windows"]) == 1
    w = body["windows"][0]
    assert w["session"] == "main"
    assert w["index"] == 0
    assert w["target"] == "main:0"
    assert w["state"] == "shell"


def test_state_resume_gc_drops_stale(client, mocker, clean_state):
    """_resuming entries pointing at dead targets get cleaned up."""
    from periscope.panes import _resuming
    _resuming.clear()
    _resuming["sid-1"] = {"target": "dead:0", "started_at": 0}  # ancient + missing
    _patch(mocker, "list_windows", return_value=[])
    _patch(mocker, "update_focus_from_windows")
    _patch(mocker, "_attach_git_then_resolve_pids")
    _patch(mocker, "cached_claude_usage", return_value={})
    _patch(mocker, "cached_scraped_usage", return_value={})

    client.get("/api/state")
    assert "sid-1" not in _resuming
