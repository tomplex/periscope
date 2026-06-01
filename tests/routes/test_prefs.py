"""Tests for /api/prefs/*."""


def test_get_prefs_returns_state(client, clean_state):
    clean_state["ui"]["view"] = "grid"
    r = client.get("/api/prefs")
    assert r.status_code == 200
    body = r.json()
    assert body["ui"]["view"] == "grid"


def test_patch_prefs_ui_valid_view(client, clean_state):
    r = client.patch("/api/prefs/ui", json={"view": "stream"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["ui"]["view"] == "stream"


def test_patch_prefs_ui_rejects_bogus_view(client, clean_state):
    r = client.patch("/api/prefs/ui", json={"view": "weird"})
    assert r.status_code == 400
    assert "invalid view" in r.json()["detail"]


def test_put_window_annotation(client, clean_state):
    r = client.put("/api/prefs/windows/abc123", json={"notes": "hi", "tags": ["a", "b", "a"]})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["annotation"]["notes"] == "hi"
    assert body["annotation"]["tags"] == ["a", "b"]


def test_put_window_annotation_invalid_pid(client, clean_state):
    r = client.put("/api/prefs/windows/abc-123", json={"notes": "hi"})
    assert r.status_code == 400
    assert "invalid pid" in r.json()["detail"]


def test_delete_window_annotation(client, clean_state):
    clean_state["windows"]["abc123"] = {"notes": "x", "tags": ["t"]}
    r = client.delete("/api/prefs/windows/abc123")
    assert r.json()["ok"] is True
    assert "notes" not in clean_state["windows"]["abc123"]
    assert "tags" not in clean_state["windows"]["abc123"]


def test_add_and_delete_command(client, clean_state):
    r = client.post("/api/prefs/commands", json={"label": "deploy", "exec": "make deploy"})
    assert r.json()["ok"] is True
    # Duplicate rejected.
    r2 = client.post("/api/prefs/commands", json={"label": "deploy"})
    assert r2.status_code == 409
    assert "duplicate" in r2.json()["detail"]
    # Delete.
    r3 = client.delete("/api/prefs/commands/deploy")
    assert r3.json()["ok"] is True


def test_update_command(client, clean_state):
    clean_state["commands"].append({"label": "old", "exec": "cmd"})
    r = client.put("/api/prefs/commands/old", json={"label": "new"})
    body = r.json()
    assert body["ok"] is True
    assert body["commands"][0]["label"] == "new"


def test_reorder_commands(client, clean_state):
    clean_state["commands"].extend([
        {"label": "a", "exec": ""},
        {"label": "b", "exec": ""},
        {"label": "c", "exec": ""},
    ])
    r = client.put("/api/prefs/commands", json={"labels": ["c", "a", "b"]})
    body = r.json()
    assert body["ok"] is True
    assert [c["label"] for c in body["commands"]] == ["c", "a", "b"]


def test_ui_patch_accepts_split_view(client, clean_state):
    r = client.patch("/api/prefs/ui", json={"view": "split"})
    assert r.status_code == 200, r.text
    assert r.json()["ui"]["view"] == "split"


def test_ui_patch_rejects_unknown_view(client, clean_state):
    r = client.patch("/api/prefs/ui", json={"view": "kanban"})
    assert r.status_code == 400


def test_ui_patch_accepts_rail_state_keys(client, clean_state):
    body = {
        "repo_order": ["/home/tom/dev/foo"],
        "worktrees_by_repo": {"/home/tom/dev/foo": ["session-a"]},
        "panes_by_worktree": {"session-a": ["pane:abc", "review"]},
        "rail_collapsed": {"repo:/home/tom/dev/foo": False},
        "last_selected": {"kind": "pane", "pid": "abc"},
    }
    r = client.patch("/api/prefs/ui", json=body)
    assert r.status_code == 200, r.text
    ui = r.json()["ui"]
    for key in body:
        assert ui[key] == body[key]
