"""Tests for /api/session/* and /api/window/*."""

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    from periscope.app import app
    return TestClient(app)


def _patch(mocker, name, **kwargs):
    """Patch in either the route module (post-extract) or server (pre)."""
    for prefix in (f"periscope.routes.sessions.{name}", f"server.{name}"):
        try:
            return mocker.patch(prefix, **kwargs)
        except (AttributeError, ModuleNotFoundError):
            continue


def test_session_new_creates_session(client, mocker):
    _patch(mocker, "_tmux_mutate", return_value=(True, ""))
    r = client.post("/api/session/new", json={"name": "foo", "cwd": "/tmp"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["session"] == "foo"


def test_session_new_rejects_empty_name(client, mocker):
    _patch(mocker, "_tmux_mutate", return_value=(True, ""))
    r = client.post("/api/session/new", json={"name": "   "})
    body = r.json()
    assert body["ok"] is False
    assert "empty" in body["error"]


def test_session_delete(client, mocker):
    _patch(mocker, "_tmux_mutate", return_value=(True, ""))
    r = client.delete("/api/session?session=foo")
    body = r.json()
    assert body["ok"] is True
    assert body["session"] == "foo"


def test_window_new_simple_shell(client, mocker):
    # display-message → cwd
    _patch(mocker, "tmux", return_value="/tmp")
    _patch(mocker, "_tmux_mutate", return_value=(True, "3"))
    r = client.post("/api/window/new?session=main&mode=shell")
    body = r.json()
    assert body["ok"] is True
    assert body["index"] == 3
    assert body["target"] == "main:3"


def test_window_new_resume_unknown_session_id(client, mocker):
    # Patch history.search.get_session to return None.
    mocker.patch("history.search.get_session", return_value=None)
    r = client.post("/api/window/new?session=resumes&mode=resume&resume_id=nope")
    body = r.json()
    assert body["ok"] is False
    assert "unknown session_id" in body["error"]


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
    body = r.json()
    assert body["ok"] is False
    assert "same as source" in body["error"]


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
    new_window = _patch(mocker, "_tmux_mutate", return_value=(True, "1"))
    r = client.post("/api/window/new?session=myproj&mode=shell")
    assert r.status_code == 200
    call = next(c for c in new_window.call_args_list if c.args[0] == "new-window")
    cwd_idx = list(call.args).index("-c") + 1
    assert call.args[cwd_idx] == "/tmp"


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
    _patch(mocker, "_run", return_value=(0, ""))
    _patch(mocker, "resolve_project_for_window", return_value="__main__")
    r = client.post("/api/window/new-worktree?session=main&branch=tc/x")
    assert r.status_code == 400
    assert "main project" in r.json()["detail"]


def test_new_worktree_rejects_unowned_session(client, mocker):
    _patch(mocker, "_run", return_value=(0, ""))
    _patch(mocker, "resolve_project_for_window", return_value=None)
    r = client.post("/api/window/new-worktree?session=ghost&branch=tc/x")
    assert r.status_code == 400
    assert "not owned by a project" in r.json()["detail"]


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
