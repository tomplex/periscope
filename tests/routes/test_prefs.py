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


def test_patch_prefs_ui_detail_mode_by_pid(client, clean_state):
    r = client.patch(
        "/api/prefs/ui",
        json={"detail_mode_by_pid": {"abc123": "transcript", "def456": "terminal"}},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ui"]["detail_mode_by_pid"] == {
        "abc123": "transcript",
        "def456": "terminal",
    }


def test_patch_prefs_ui_pinned_pids(client, clean_state):
    # Rail conversation pinning: the star toggle PATCHes pinned_pids; the
    # field must round-trip or the client's optimistic pin reverts on the
    # server response.
    r = client.patch("/api/prefs/ui", json={"pinned_pids": ["abc123", "def456"]})
    assert r.status_code == 200
    assert r.json()["ui"]["pinned_pids"] == ["abc123", "def456"]
    # unpin (empty list) round-trips too — must not be dropped as falsy
    r = client.patch("/api/prefs/ui", json={"pinned_pids": []})
    assert r.status_code == 200
    assert r.json()["ui"]["pinned_pids"] == []


def test_patch_prefs_ui_rejects_bogus_detail_mode(client, clean_state):
    r = client.patch(
        "/api/prefs/ui",
        json={"detail_mode_by_pid": {"abc123": "weird"}},
    )
    assert r.status_code == 400
    assert "invalid detail_mode" in r.json()["detail"]


def test_put_window_annotation(client, clean_state):
    r = client.put("/api/prefs/windows/abc123", json={"notes": "hi", "tags": ["a", "b", "a"]})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["annotation"]["notes"] == "hi"
    assert body["annotation"]["tags"] == ["a", "b"]
    assert body["annotation"]["pinned_files"] == []


def test_put_window_annotation_pinned_files(client, clean_state):
    # Dedupe + preserve insertion order; trim blanks.
    r = client.put("/api/prefs/windows/abc123", json={
        "pinned_files": ["/a/b.md", "  ", "/a/b.md", "/c/d.html"],
    })
    assert r.status_code == 200
    assert r.json()["annotation"]["pinned_files"] == ["/a/b.md", "/c/d.html"]
    # Empty list clears the field.
    r2 = client.put("/api/prefs/windows/abc123", json={"pinned_files": []})
    assert r2.json()["annotation"]["pinned_files"] == []
    assert "pinned_files" not in clean_state["windows"]["abc123"]


def test_put_window_annotation_invalid_pid(client, clean_state):
    r = client.put("/api/prefs/windows/abc-123", json={"notes": "hi"})
    assert r.status_code == 400
    assert "invalid pid" in r.json()["detail"]


def test_delete_window_annotation(client, clean_state):
    clean_state["windows"]["abc123"] = {"notes": "x", "tags": ["t"], "pinned_files": ["/x"]}
    r = client.delete("/api/prefs/windows/abc123")
    assert r.json()["ok"] is True
    assert "notes" not in clean_state["windows"]["abc123"]
    assert "tags" not in clean_state["windows"]["abc123"]
    assert "pinned_files" not in clean_state["windows"]["abc123"]


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
