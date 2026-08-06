"""Tests for /api/pane and /api/rename."""


def _patch(mocker, name, **kwargs):
    for prefix in (f"periscope.routes.pane.{name}", f"server.{name}"):
        try:
            return mocker.patch(prefix, **kwargs)
        except (AttributeError, ModuleNotFoundError):
            continue
    return None


def test_pane_returns_parsed_payload(client, mocker):
    def fake_tmux(*args):
        if args and args[0] == "capture-pane":
            return "$ ls\nfoo bar\n"
        return ""

    _patch(mocker, "tmux", side_effect=fake_tmux)
    _patch(mocker, "pane_meta", return_value=("win-name", "/tmp/proj"))
    _patch(mocker, "parse_pane", return_value={"agent": None, "spinner": None})
    _patch(mocker, "smooth_parsed", side_effect=lambda *, pane_id, parsed: parsed)
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


def test_rename_pins_the_name(client, mocker, clean_state):
    """A name Tom typed locks the narrator out permanently, not for
    RENAME_COOLDOWN_S — the cooldown expired unnoticed and the name drifted."""
    from periscope import narrator, store

    _patch(mocker, "tmux", return_value="")
    _patch(mocker, "window_identity", return_value=("p9", "%7", "@3"))

    r = client.post("/api/rename?session=main&index=0", json={"name": "pit-orchestrator"})
    assert r.status_code == 200
    assert r.json()["name_pinned"] is True
    assert store.get_window("p9")["name_pinned"] is True
    assert narrator.is_name_pinned({"pid": "p9", "name": "pit-orchestrator"})


def test_rename_of_an_unstamped_window_reports_no_pin(client, mocker, clean_state):
    """No @periscope_id means nothing to hang the pin on. The rename still
    applies; the response says so rather than implying a lock that isn't there."""
    _patch(mocker, "tmux", return_value="")
    _patch(mocker, "window_identity", return_value=("", "%7", "@3"))

    r = client.post("/api/rename?session=main&index=0", json={"name": "fresh"})
    assert r.status_code == 200
    assert r.json()["name_pinned"] is False


def test_name_pin_toggles(client, clean_state):
    from periscope import store

    assert client.post("/api/name-pin", json={"pid": "p9", "pinned": True}).status_code == 200
    assert store.get_window("p9")["name_pinned"] is True

    r = client.post("/api/name-pin", json={"pid": "p9", "pinned": False})
    assert r.status_code == 200
    assert r.json()["name_pinned"] is False
    # Unpinned is stored as absence, not False — no third state to reason about.
    assert "name_pinned" not in store.get_window("p9")


def test_name_pin_rejects_empty_pid(client, clean_state):
    r = client.post("/api/name-pin", json={"pid": "  ", "pinned": True})
    assert r.status_code == 400


def test_rename_rejects_empty(client, mocker):
    for path in ("periscope.routes.pane.tmux", "server.tmux"):
        try:
            mocker.patch(path, return_value="")
            break
        except (AttributeError, ModuleNotFoundError):
            continue
    r = client.post("/api/rename?session=main&index=0", json={"name": "   "})
    assert r.status_code == 400
    assert "empty" in r.json()["detail"]


def test_pane_turns_returns_messages_end_to_end(client, mocker, tmp_path, monkeypatch):
    # Exercise route -> get_turns_for_pane -> messages_from_jsonl against a real
    # tmp transcript (the Q1-2026 mocked-migration lesson: don't mock the path
    # the bug would live in). Only the tmux boundary and the session lookup are faked.
    import json

    import periscope.activity as activity

    cwd = "/Users/tom/dev/turnsproj"
    enc = tmp_path / activity._encode_cwd(cwd)
    enc.mkdir(parents=True)
    (enc / "sid-9.jsonl").write_text(json.dumps({
        "type": "user", "sessionId": "sid-9", "cwd": cwd,
        "timestamp": "2026-06-01T10:00:00.000Z", "uuid": "u1", "parentUuid": None,
        "message": {"role": "user", "content": "hi there"},
    }) + "\n")
    monkeypatch.setattr(activity, "_PROJECTS_DIR", tmp_path)
    mocker.patch("periscope.turns.tmux", return_value=f"%9\t{cwd}")
    mocker.patch("periscope.turns.session_id_for_pane", return_value="sid-9")

    r = client.get("/api/pane/turns?session=main&index=0")
    assert r.status_code == 200
    body = r.json()
    assert body["session_id"] == "sid-9"
    assert body["messages"][0]["text"] == "hi there"


def test_pane_turns_null_when_no_transcript(client, mocker, tmp_path, monkeypatch):
    import periscope.activity as activity

    monkeypatch.setattr(activity, "_PROJECTS_DIR", tmp_path)  # empty -> no match
    mocker.patch("periscope.turns.tmux", return_value="%9\t/no/such/cwd")
    mocker.patch("periscope.turns.session_id_for_pane", return_value=None)
    r = client.get("/api/pane/turns?session=main&index=0")
    assert r.status_code == 200
    assert r.json() == {"turns": None}


def test_tabs_open_close_activate_roundtrip(client, clean_state, mocker):
    mocker.patch("periscope.store._write_state")

    r = client.post("/api/pane/tabs/open", json={"pid": "p1", "path": "/a/b.md", "line": 3})
    assert r.status_code == 200
    assert r.json() == {
        "ok": True,
        "open_tabs": [{"path": "/a/b.md", "line": 3}],
        "active_tab": "file:/a/b.md",
    }

    r = client.post("/api/pane/tabs/activate", json={"pid": "p1", "tab": "pane"})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "active_tab": "pane"}

    r = client.post("/api/pane/tabs/close", json={"pid": "p1", "path": "/a/b.md"})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "open_tabs": [], "active_tab": "pane"}
    assert clean_state["windows"]["p1"] == {}


def test_tabs_open_rejects_blank_fields(client):
    r = client.post("/api/pane/tabs/open", json={"pid": " ", "path": "/a/b.md"})
    assert r.status_code == 400
    r = client.post("/api/pane/tabs/open", json={"pid": "p1", "path": ""})
    assert r.status_code == 400


def test_rename_stamps_narrator_cooldown(client, mocker):
    def fake_tmux(*args):
        if args[0] == "display-message":
            return "%5\n"
        return ""
    mocker.patch("periscope.routes.pane.tmux", side_effect=fake_tmux)
    stamp = mocker.patch("periscope.routes.pane.stamp_pane_rename")

    r = client.post("/api/rename?session=main&index=0", json={"name": "my-name"})
    assert r.status_code == 200
    stamp.assert_called_once()
    args, kwargs = stamp.call_args
    assert args[0] == "%5"
    assert kwargs["name"] == "my-name"
    assert isinstance(kwargs["at"], int)
