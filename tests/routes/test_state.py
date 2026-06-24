"""Tests for GET /api/state — the big aggregator."""


def _patch(mocker, name, **kwargs):
    for prefix in (f"periscope.routes.state.{name}", f"server.{name}"):
        try:
            return mocker.patch(prefix, **kwargs)
        except (AttributeError, ModuleNotFoundError):
            continue
    return None


def test_state_empty(client, mocker, clean_state):
    _patch(mocker, "list_windows", return_value=[])
    _patch(mocker, "update_focus_from_windows")
    _patch(mocker, "_attach_git_then_resolve_pids")
    _patch(mocker, "cached_claude_usage", return_value={})
    _patch(mocker, "cached_plan_usage", return_value={})

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
    _patch(mocker, "cached_plan_usage", return_value={})
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
    _patch(mocker, "cached_plan_usage", return_value={})

    client.get("/api/state")
    assert "sid-1" not in _resuming


def test_state_preserves_window_order_across_fanout(client, mocker, clean_state):
    """The parallel fan-out must return views in the same order as list_windows."""
    windows = [
        {"session": "s", "index": i, "active": i == 0, "activity": 0, "pane_id": f"%{i}", "cwd": ""}
        for i in range(5)
    ]
    _patch(mocker, "list_windows", return_value=windows)
    _patch(mocker, "update_focus_from_windows")
    _patch(mocker, "_attach_git_then_resolve_pids")
    _patch(mocker, "all_projects", return_value={})
    _patch(mocker, "build_window_view",
           side_effect=lambda w, now_ts: ({"index": w["index"]}, None))
    resp = client.get("/api/state")
    assert [v["index"] for v in resp.json()["windows"]] == [0, 1, 2, 3, 4]


def test_state_writes_stamps_once_after_join(client, mocker, clean_state):
    """All _STATE mutation stays single-threaded post-join: set_window_fields_bulk
    is called exactly once with every pane's stamp."""
    windows = [
        {"session": "s", "index": i, "active": False, "activity": 0, "pane_id": f"%{i}", "cwd": ""}
        for i in range(3)
    ]
    _patch(mocker, "list_windows", return_value=windows)
    _patch(mocker, "update_focus_from_windows")
    _patch(mocker, "_attach_git_then_resolve_pids")
    _patch(mocker, "all_projects", return_value={})
    _patch(mocker, "build_window_view",
           side_effect=lambda w, now_ts: ({"index": w["index"]}, (f"pid{w['index']}", 10, 5)))
    bulk = _patch(mocker, "set_window_fields_bulk")
    client.get("/api/state")
    assert bulk.call_count == 1
    written = bulk.call_args[0][0]
    assert set(written.keys()) == {"pid0", "pid1", "pid2"}


def test_state_isolates_one_pane_build_failure(client, mocker, clean_state):
    """A worker RAISING must not sink the response: _safe_build converts it to
    an error view, the other panes build normally, order is preserved."""
    windows = [
        {"session": "s", "index": 0, "active": True, "activity": 0, "pane_id": "%0", "cwd": ""},
        {"session": "s", "index": 1, "active": False, "activity": 0, "pane_id": "%1", "cwd": ""},
    ]
    _patch(mocker, "list_windows", return_value=windows)
    _patch(mocker, "update_focus_from_windows")
    _patch(mocker, "_attach_git_then_resolve_pids")
    _patch(mocker, "all_projects", return_value={})

    def fake_build(w, now_ts):
        if w["index"] == 0:
            raise RuntimeError("git blew up mid-build")  # NOT a capture error
        return ({"index": 1, "state": "idle"}, None)

    _patch(mocker, "build_window_view", side_effect=fake_build)
    resp = client.get("/api/state")
    views = {v["index"]: v for v in resp.json()["windows"]}
    assert views[0]["state"] == "error"   # _safe_build caught the raise
    assert views[1]["state"] == "idle"


def test_state_merges_narrator_status_lines(client, mocker, clean_state,
                                            fresh_activity_db):
    activity = fresh_activity_db
    activity.upsert_pane_status(activity.PaneStatusRow(
        pane_id="%7", session_id="sid", status="fixing flaky reconcile test",
        generated_at=1234, jsonl_size=10, seen_name="claude", renamed_at=None))
    windows = [
        {"session": "s", "index": 0, "active": True, "activity": 0,
         "pane_id": "%7", "cwd": ""},
        {"session": "s", "index": 1, "active": False, "activity": 0,
         "pane_id": "%8", "cwd": ""},
    ]
    _patch(mocker, "list_windows", return_value=windows)
    _patch(mocker, "update_focus_from_windows")
    _patch(mocker, "_attach_git_then_resolve_pids")
    _patch(mocker, "all_projects", return_value={})
    # Hermeticity: left unmocked, the first call walks the real
    # ~/.claude/projects and spawns a real OAuth HTTP thread (same patches
    # as test_state_empty / test_state_with_window).
    _patch(mocker, "cached_claude_usage", return_value={})
    _patch(mocker, "cached_plan_usage", return_value=None)
    _patch(mocker, "build_window_view",
           side_effect=lambda w, now_ts: (
               {"index": w["index"], "pane_id": w["pane_id"]}, None))

    body = client.get("/api/state").json()
    views = {v["index"]: v for v in body["windows"]}
    assert views[0]["status_line"] == "fixing flaky reconcile test"
    assert views[0]["status_at"] == 1234
    assert "status_line" not in views[1]
    assert "status_at" not in views[1]
    assert "status_rail" not in views[0]   # row exists but rail is NULL


def test_state_merges_status_rail_when_present(client, mocker, clean_state,
                                               fresh_activity_db):
    activity = fresh_activity_db
    activity.upsert_pane_status(activity.PaneStatusRow(
        pane_id="%7", session_id="sid", status="comparing figv2 lookup hit rates",
        generated_at=1234, jsonl_size=10, seen_name="claude", renamed_at=None,
        rail="comparing hit rates"))
    windows = [
        {"session": "s", "index": 0, "active": True, "activity": 0,
         "pane_id": "%7", "cwd": ""},
    ]
    _patch(mocker, "list_windows", return_value=windows)
    _patch(mocker, "update_focus_from_windows")
    _patch(mocker, "_attach_git_then_resolve_pids")
    _patch(mocker, "all_projects", return_value={})
    # Hermeticity: same usage patches as test_state_merges_narrator_status_lines.
    _patch(mocker, "cached_claude_usage", return_value={})
    _patch(mocker, "cached_plan_usage", return_value=None)
    _patch(mocker, "build_window_view",
           side_effect=lambda w, now_ts: (
               {"index": w["index"], "pane_id": w["pane_id"]}, None))

    body = client.get("/api/state").json()
    w = body["windows"][0]
    assert w["status_line"] == "comparing figv2 lookup hit rates"
    assert w["status_at"] == 1234
    assert w["status_rail"] == "comparing hit rates"


def test_state_includes_workspaces(client, mocker, clean_state, fresh_activity_db):
    """The /api/state payload carries non-archived workspaces."""
    from periscope.workspaces import create_workspace
    ws = create_workspace(name="WS", base_repo="/dev/fdy")
    _patch(mocker, "list_windows", return_value=[])
    _patch(mocker, "update_focus_from_windows")
    _patch(mocker, "_attach_git_then_resolve_pids")
    _patch(mocker, "cached_claude_usage", return_value={})
    _patch(mocker, "cached_plan_usage", return_value={})

    body = client.get("/api/state").json()
    ids = [w["id"] for w in body["workspaces"]]
    assert ws["id"] in ids
