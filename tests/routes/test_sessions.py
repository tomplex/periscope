"""Tests for /api/session/* and /api/window/*.

POST /api/session/new was retired; session creation is now handled by
POST /api/open (open_ops.ensure_session). Tests for session/new have been
removed; the equivalent coverage lives in tests/test_open_ops.py.
"""


def _patch(mocker, name, **kwargs):
    """Patch in either the route module (post-extract) or server (pre)."""
    for prefix in (f"periscope.routes.sessions.{name}", f"server.{name}"):
        try:
            return mocker.patch(prefix, **kwargs)
        except (AttributeError, ModuleNotFoundError):
            continue
    return None


def test_session_delete_unmanaged_session_400(client, mocker):
    # Contract narrowed: a session no project owns is not a closable worktree.
    _patch(mocker, "_tmux_mutate", return_value=(True, ""))
    r = client.delete("/api/session?session=foo")
    assert r.status_code == 400


def test_session_delete_kills_placement_set_sparing_ws_pane(
        client, clean_state, fresh_activity_db, mocker):
    clean_state["projects"]["/repo/a"] = {
        "tmux_session": "sess_a", "repo": "/repo", "archived_at": None}
    clean_state["workspaces"]["ws_goal"] = {"id": "ws_goal", "archived_at": None}
    fresh_activity_db.set_pane_workspace("%claude", "ws_goal")   # dragged out
    mocker.patch("periscope.routes.sessions.list_windows", return_value=[
        {"session": "sess_a", "index": 0, "pane_id": "%claude"},
        {"session": "sess_a", "index": 1, "pane_id": "%shell"},
    ])
    calls = []
    _patch(mocker, "_tmux_mutate",
           side_effect=lambda *a: (calls.append(a), (True, ""))[1])
    r = client.delete("/api/session?session=sess_a")
    assert r.status_code == 200
    # Kill by stable pane_id (renumber-windows-safe), not session:index.
    assert ("kill-pane", "-t", "%shell") in calls      # shell killed by pane_id
    assert not any(a[0] == "kill-session" for a in calls)
    assert not any("sess_a:" in a for a in calls)       # never index-targeted
    assert all("%claude" not in a for a in calls)       # claude (ws) spared


def test_window_new_simple_shell(client, mocker):
    # display-message → cwd
    _patch(mocker, "tmux", return_value="/tmp")
    _patch(mocker, "_tmux_mutate", return_value=(True, "3"))
    _patch(mocker, "_run", return_value=(0, ""))  # has-session: exists (never real tmux)
    r = client.post("/api/window/new?session=main&mode=shell")
    body = r.json()
    assert body["ok"] is True
    assert body["index"] == 3
    assert body["target"] == "main:3"


def test_window_new_resume_unknown_session_id(client, mocker):
    # Patch history.search.get_session to return None.
    mocker.patch("history.search.get_session", return_value=None)
    r = client.post("/api/window/new?session=resumes&mode=resume&resume_id=nope")
    assert r.status_code == 404
    assert "unknown session_id" in r.json()["detail"]


def test_window_move(client, mocker):
    # display-message → window_id "@42"; list-windows → "@42 5"
    def fake_tmux(*args):
        if args and args[0] == "display-message":
            return "@42"
        if args and args[0] == "list-windows":
            return "@42 5\n"
        return ""

    _patch(mocker, "tmux", side_effect=fake_tmux)
    _patch(mocker, "_tmux_mutate", return_value=(True, ""))
    _patch(mocker, "_run", return_value=(0, ""))
    r = client.post("/api/window/move?session=src&index=0&dest=dst")
    body = r.json()
    assert body["ok"] is True
    assert body["index"] == 5
    assert body["target"] == "dst:5"


def test_window_move_rejects_same_session(client, mocker):
    r = client.post("/api/window/move?session=main&index=0&dest=main")
    assert r.status_code == 400
    assert "same as source" in r.json()["detail"]


def test_window_delete(client, mocker):
    _patch(mocker, "_tmux_mutate", return_value=(True, ""))
    r = client.delete("/api/window?session=main&index=0")
    body = r.json()
    assert body["ok"] is True
    assert body["target"] == "main:0"


# === phase 3 ==============================================================
# /api/window/new defaults cwd to project.pinned_dir when the target session
# is owned by a non-archived non-main project.

def test_window_new_uses_project_pinned_dir(client, mocker):
    # display-message would return /tmp; project pin should win.
    pin = "/Users/foo/dev/myproj"
    _patch(mocker, "resolve_project_for_window", return_value=pin)
    _patch(mocker, "get_project", return_value={
        "name": "myproj", "tmux_session": "myproj", "archived_at": None,
    })
    _patch(mocker, "tmux", return_value="/tmp")
    _patch(mocker, "_run", return_value=(0, ""))  # has-session: exists
    new_window = _patch(mocker, "_tmux_mutate", return_value=(True, "7"))
    r = client.post("/api/window/new?session=myproj&mode=shell")
    assert r.status_code == 200
    # Verify _tmux_mutate was called with -c <pin>, not -c /tmp.
    call = next(c for c in new_window.call_args_list if c.args[0] == "new-window")
    assert "-c" in call.args
    cwd_idx = list(call.args).index("-c") + 1
    assert call.args[cwd_idx] == pin


def test_window_new_archived_project_falls_through_to_cwd(client, mocker):
    # An archived project shouldn't override; legacy display-message wins.
    _patch(mocker, "resolve_project_for_window", return_value="/Users/foo/dev/myproj")
    _patch(mocker, "get_project", return_value={
        "name": "myproj", "tmux_session": "myproj", "archived_at": 1234567890,
    })
    _patch(mocker, "tmux", return_value="/tmp")
    _patch(mocker, "_run", return_value=(0, ""))  # has-session: exists
    new_window = _patch(mocker, "_tmux_mutate", return_value=(True, "1"))
    r = client.post("/api/window/new?session=myproj&mode=shell")
    assert r.status_code == 200
    call = next(c for c in new_window.call_args_list if c.args[0] == "new-window")
    cwd_idx = list(call.args).index("-c") + 1
    assert call.args[cwd_idx] == "/tmp"


def test_window_new_main_key_defaults_to_dev_dir(client, mocker):
    # MAIN_KEY (dev / folded unmanaged sessions) → ~/dev, not pane-cwd.
    import os
    _patch(mocker, "resolve_project_for_window", return_value="__main__")
    _patch(mocker, "get_project", return_value={"name": "main", "tmux_session": "main"})
    _patch(mocker, "tmux", return_value="/tmp")          # pane cwd must NOT win
    _patch(mocker, "_run", return_value=(0, ""))          # has-session: exists
    new_window = _patch(mocker, "_tmux_mutate", return_value=(True, "3"))
    r = client.post("/api/window/new?session=main&mode=shell")
    assert r.status_code == 200
    call = next(c for c in new_window.call_args_list if c.args[0] == "new-window")
    cwd_idx = list(call.args).index("-c") + 1
    assert call.args[cwd_idx] == os.path.expanduser("~/dev")


def test_window_new_auto_creates_missing_session(client, mocker):
    # Dev's "+ New tab" can target a dead "main" session — auto-create it
    # instead of letting new-window 500.
    import os
    _patch(mocker, "resolve_project_for_window", return_value="__main__")
    _patch(mocker, "get_project", return_value={"name": "main", "tmux_session": "main"})
    _patch(mocker, "tmux", return_value="")
    _patch(mocker, "_run", return_value=(1, ""))          # has-session: missing
    mutate = _patch(mocker, "_tmux_mutate", return_value=(True, "1"))
    r = client.post("/api/window/new?session=main&mode=shell")
    assert r.status_code == 200
    body = r.json()
    assert body["target"] == "main:1"
    new_session = next(c for c in mutate.call_args_list if c.args[0] == "new-session")
    cwd_idx = list(new_session.args).index("-c") + 1
    assert new_session.args[cwd_idx] == os.path.expanduser("~/dev")
    # No new-window after creating the session — new-session's window IS the tab.
    assert not any(c.args[0] == "new-window" for c in mutate.call_args_list)


def test_window_new_no_auto_create_for_project_sessions(client, mocker):
    # Auto-create is gated on MAIN_KEY: a typo'd session= on a project
    # call must error, not silently mint a session.
    _patch(mocker, "resolve_project_for_window", return_value="/Users/foo/dev/myproj")
    _patch(mocker, "get_project", return_value={
        "name": "myproj", "tmux_session": "myproj", "archived_at": None,
    })
    _patch(mocker, "_run", return_value=(1, ""))          # has-session: missing
    mutate = _patch(mocker, "_tmux_mutate", return_value=(False, "no such session"))
    r = client.post("/api/window/new?session=myproj-typo&mode=shell")
    assert r.status_code == 500
    assert not any(c.args[0] == "new-session" for c in mutate.call_args_list)


# /api/window/new-worktree — the new endpoint.

def test_new_worktree_success(client, mocker):
    _patch(mocker, "_run", return_value=(0, ""))  # has-session
    _patch(mocker, "resolve_project_for_window", return_value="/Users/foo/dev/myproj")
    _patch(mocker, "get_project", return_value={
        "name": "myproj",
        "tmux_session": "myproj",
        "repo": "/Users/foo/dev/myproj",
        "base_branch": "tc/feat",
        "archived_at": None,
    })
    _patch(mocker, "spawn_worktree", return_value={
        "path": "/Users/foo/dev/worktrees/myproj/tc-sub",
        "base_branch": "tc/feat",
        "branch": "tc/sub",
    })
    _patch(mocker, "_tmux_mutate", return_value=(True, "5"))
    _patch(mocker, "tmux", return_value="")
    r = client.post("/api/window/new-worktree?session=myproj&branch=tc/sub&exec=")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["worktree_path"] == "/Users/foo/dev/worktrees/myproj/tc-sub"
    assert body["base_branch"] == "tc/feat"
    assert body["index"] == 5
    assert body["target"] == "myproj:5"


def test_new_worktree_rejects_main(client, mocker):
    # Both folds of the same rule: worktree-tab needs a pinned project.
    _patch(mocker, "_run", return_value=(0, ""))
    _patch(mocker, "resolve_project_for_window", return_value="__main__")
    r = client.post("/api/window/new-worktree?session=main&branch=tc/x")
    assert r.status_code == 400
    assert "pinned project" in r.json()["detail"]


def test_new_worktree_rejects_unowned_session(client, mocker):
    _patch(mocker, "_run", return_value=(0, ""))
    _patch(mocker, "resolve_project_for_window", return_value=None)
    r = client.post("/api/window/new-worktree?session=ghost&branch=tc/x")
    assert r.status_code == 400
    assert "pinned project" in r.json()["detail"]


def test_new_worktree_rejects_missing_session(client, mocker):
    # has-session returns non-zero.
    _patch(mocker, "_run", return_value=(1, ""))
    r = client.post("/api/window/new-worktree?session=ghost&branch=tc/x")
    assert r.status_code == 404


def test_new_worktree_rejects_bad_branch(client, mocker):
    r = client.post("/api/window/new-worktree?session=any&branch=-bad")
    assert r.status_code == 400
    r = client.post("/api/window/new-worktree?session=any&branch=")
    assert r.status_code == 400


def test_new_worktree_409_on_existing_path(client, mocker):
    _patch(mocker, "_run", return_value=(0, ""))
    _patch(mocker, "resolve_project_for_window", return_value="/Users/foo/dev/myproj")
    _patch(mocker, "get_project", return_value={
        "name": "myproj", "tmux_session": "myproj",
        "repo": "/Users/foo/dev/myproj", "base_branch": "tc/feat",
        "archived_at": None,
    })
    _patch(
        mocker, "spawn_worktree",
        side_effect=ValueError("worktree path already exists: /x"),
    )
    r = client.post("/api/window/new-worktree?session=myproj&branch=tc/sub")
    assert r.status_code == 409


def test_new_worktree_400_on_other_spawn_error(client, mocker):
    _patch(mocker, "_run", return_value=(0, ""))
    _patch(mocker, "resolve_project_for_window", return_value="/Users/foo/dev/myproj")
    _patch(mocker, "get_project", return_value={
        "name": "myproj", "tmux_session": "myproj",
        "repo": "/Users/foo/dev/myproj", "base_branch": "tc/feat",
        "archived_at": None,
    })
    _patch(mocker, "spawn_worktree", side_effect=ValueError("not a git repo: /x"))
    r = client.post("/api/window/new-worktree?session=myproj&branch=tc/sub")
    assert r.status_code == 400
