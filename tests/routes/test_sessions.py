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


def _fake_window_tmux(index="3", pane_id="%new"):
    """tmux() side_effect for the plain-window path: window_id → index/pane_id
    via display-message, everything else (send-keys etc.) → ''."""
    def fake(*a):
        if a and a[0] == "display-message":
            if "#{window_index}" in a:
                return index
            if "#{pane_id}" in a:
                return pane_id
        return ""
    return fake


def test_window_new_simple_shell(client, mocker, fresh_activity_db):
    # `session` is now a track id. An unknown track → cwd ~/dev; the window is
    # created in MANAGED_SESSION; the new pane is tagged into the track param.
    from periscope import config
    _patch(mocker, "tmux", side_effect=_fake_window_tmux(index="3", pane_id="%new"))
    _patch(mocker, "_tmux_mutate", return_value=(True, "@9"))  # new-window → window_id
    _patch(mocker, "_run", return_value=(0, ""))  # has-session: exists
    r = client.post("/api/window/new?session=untracked&mode=shell")
    body = r.json()
    assert body["ok"] is True
    assert body["session"] == config.MANAGED_SESSION
    assert body["index"] == 3
    assert body["target"] == f"{config.MANAGED_SESSION}:3"
    assert fresh_activity_db.get_pane_track("%new") == "untracked"


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


# === track-model "+ New tab" ==============================================
# /api/window/new's `session` param is a TRACK ID. cwd comes from the track's
# repo (repo-default track id == repo path; loose/None → ~/dev). The window is
# always created in MANAGED_SESSION; the new pane is tagged into the track.

def test_window_new_repo_track_uses_repo_cwd(client, mocker, fresh_activity_db):
    # A repo-default track (id == repo path, repo set) → cwd is the repo.
    from periscope import config, tracks
    tid = tracks.repo_default_track("/Users/foo/dev/myproj")
    _patch(mocker, "tmux", side_effect=_fake_window_tmux(index="7", pane_id="%p7"))
    _patch(mocker, "_run", return_value=(0, ""))  # MANAGED_SESSION exists
    mutate = _patch(mocker, "_tmux_mutate", return_value=(True, "@7"))
    r = client.post(f"/api/window/new?session={tid}&mode=shell")
    assert r.status_code == 200, r.text
    call = next(c for c in mutate.call_args_list if c.args[0] == "new-window")
    cwd_idx = list(call.args).index("-c") + 1
    assert call.args[cwd_idx] == "/Users/foo/dev/myproj"
    # Target MANAGED_SESSION exactly (=prefix), and tag the new pane.
    assert call.args[2] == f"={config.MANAGED_SESSION}:"
    assert fresh_activity_db.get_pane_track("%p7") == tid


def test_window_new_loose_track_defaults_to_dev_dir(client, mocker, fresh_activity_db):
    # A goal track with no repo (or an unknown track id) → cwd ~/dev.
    import os

    from periscope import tracks
    tk = tracks.create_track(name="my goal")  # repo=None
    _patch(mocker, "tmux", side_effect=_fake_window_tmux(index="2", pane_id="%pg"))
    _patch(mocker, "_run", return_value=(0, ""))  # MANAGED_SESSION exists
    mutate = _patch(mocker, "_tmux_mutate", return_value=(True, "@2"))
    r = client.post(f"/api/window/new?session={tk['id']}&mode=shell")
    assert r.status_code == 200, r.text
    call = next(c for c in mutate.call_args_list if c.args[0] == "new-window")
    cwd_idx = list(call.args).index("-c") + 1
    assert call.args[cwd_idx] == os.path.expanduser("~/dev")
    assert fresh_activity_db.get_pane_track("%pg") == tk["id"]


def test_window_new_creates_managed_session_when_absent(client, mocker, fresh_activity_db):
    # If MANAGED_SESSION doesn't exist yet, new-session creates it (the new
    # window IS the tab — no follow-on new-window).
    import os
    _patch(mocker, "tmux", side_effect=_fake_window_tmux(index="1", pane_id="%p1"))
    _patch(mocker, "_run", return_value=(1, ""))  # has-session: missing
    mutate = _patch(mocker, "_tmux_mutate", return_value=(True, "@1"))
    r = client.post("/api/window/new?session=untracked&mode=shell")
    assert r.status_code == 200
    new_session = next(c for c in mutate.call_args_list if c.args[0] == "new-session")
    cwd_idx = list(new_session.args).index("-c") + 1
    assert new_session.args[cwd_idx] == os.path.expanduser("~/dev")
    assert not any(c.args[0] == "new-window" for c in mutate.call_args_list)
    assert fresh_activity_db.get_pane_track("%p1") == "untracked"


def test_window_new_existing_branch_cwd_override(client, mocker, fresh_activity_db):
    # An existing-branch pick: the client passes `cwd` (that branch's worktree
    # path). It overrides the track's repo and the new pane is tagged into the
    # track. Use a real dir so the `os.path.isdir` guard passes.
    import tempfile

    from periscope import config, tracks
    tid = tracks.repo_default_track("/Users/foo/dev/myproj")
    _patch(mocker, "tmux", side_effect=_fake_window_tmux(index="4", pane_id="%pb"))
    _patch(mocker, "_run", return_value=(0, ""))  # MANAGED_SESSION exists
    mutate = _patch(mocker, "_tmux_mutate", return_value=(True, "@4"))
    with tempfile.TemporaryDirectory() as wt:
        r = client.post(f"/api/window/new?session={tid}&mode=shell&cwd={wt}")
        assert r.status_code == 200, r.text
        call = next(c for c in mutate.call_args_list if c.args[0] == "new-window")
        cwd_idx = list(call.args).index("-c") + 1
        assert call.args[cwd_idx] == wt
        assert call.args[2] == f"={config.MANAGED_SESSION}:"
        assert fresh_activity_db.get_pane_track("%pb") == tid


def test_window_new_nonexistent_cwd_falls_back_to_repo(client, mocker, fresh_activity_db):
    # A `cwd` that isn't a real dir is ignored — fall back to the track's repo.
    from periscope import tracks
    tid = tracks.repo_default_track("/Users/foo/dev/myproj")
    _patch(mocker, "tmux", side_effect=_fake_window_tmux(index="4", pane_id="%pn"))
    _patch(mocker, "_run", return_value=(0, ""))
    mutate = _patch(mocker, "_tmux_mutate", return_value=(True, "@4"))
    r = client.post(f"/api/window/new?session={tid}&mode=shell&cwd=/no/such/dir")
    assert r.status_code == 200, r.text
    call = next(c for c in mutate.call_args_list if c.args[0] == "new-window")
    cwd_idx = list(call.args).index("-c") + 1
    assert call.args[cwd_idx] == "/Users/foo/dev/myproj"


def test_window_new_branch_spawns_worktree_when_none_exists(client, mocker, fresh_activity_db):
    # branch set + the track has a repo + no existing worktree → spawn_worktree
    # is called with the repo + branch, and its path becomes the window's cwd.
    from periscope import config, tracks
    tid = tracks.repo_default_track("/Users/foo/dev/myproj")
    spawn = _patch(mocker, "spawn_worktree", return_value={
        "path": "/Users/foo/dev/worktrees/myproj/tc-feat",
        "base_branch": "main", "branch": "tc/feat",
    })
    _patch(mocker, "tmux", side_effect=_fake_window_tmux(index="6", pane_id="%pw"))
    _patch(mocker, "_run", return_value=(0, ""))
    mutate = _patch(mocker, "_tmux_mutate", return_value=(True, "@6"))
    r = client.post(f"/api/window/new?session={tid}&mode=shell&branch=tc/feat")
    assert r.status_code == 200, r.text
    spawn.assert_called_once_with("/Users/foo/dev/myproj", "tc/feat")
    call = next(c for c in mutate.call_args_list if c.args[0] == "new-window")
    cwd_idx = list(call.args).index("-c") + 1
    assert call.args[cwd_idx] == "/Users/foo/dev/worktrees/myproj/tc-feat"
    assert call.args[2] == f"={config.MANAGED_SESSION}:"
    assert fresh_activity_db.get_pane_track("%pw") == tid


def test_window_new_branch_409_on_existing(client, mocker, fresh_activity_db):
    # spawn_worktree raising "already exists" maps to 409.
    from periscope import tracks
    tid = tracks.repo_default_track("/Users/foo/dev/myproj")
    _patch(mocker, "spawn_worktree",
           side_effect=ValueError("worktree path already exists: /x"))
    _patch(mocker, "_run", return_value=(0, ""))
    _patch(mocker, "_tmux_mutate", return_value=(True, "@6"))
    r = client.post(f"/api/window/new?session={tid}&mode=shell&branch=tc/dup")
    assert r.status_code == 409


def test_window_new_branch_ignored_for_loose_track(client, mocker, fresh_activity_db):
    # branch on a repo-less (loose) track has no repo to resolve against → it's
    # ignored and the tab opens at ~/dev (no spawn_worktree call).
    import os

    from periscope import tracks
    tk = tracks.create_track(name="my goal")  # repo=None
    spawn = _patch(mocker, "spawn_worktree", return_value={})
    _patch(mocker, "tmux", side_effect=_fake_window_tmux(index="2", pane_id="%pl"))
    _patch(mocker, "_run", return_value=(0, ""))
    mutate = _patch(mocker, "_tmux_mutate", return_value=(True, "@2"))
    r = client.post(f"/api/window/new?session={tk['id']}&mode=shell&branch=tc/x")
    assert r.status_code == 200, r.text
    spawn.assert_not_called()
    call = next(c for c in mutate.call_args_list if c.args[0] == "new-window")
    cwd_idx = list(call.args).index("-c") + 1
    assert call.args[cwd_idx] == os.path.expanduser("~/dev")


def test_window_new_branch_reuses_an_existing_worktree(client, mocker, fresh_activity_db):
    """A branch that already has a worktree but no live pane must land in that
    worktree — NOT spawn a second one, and NOT fall back to the repo root.

    This is the "open something that isn't currently open" case: the launcher
    offers catalog branches, most of which have a worktree on disk already.
    """
    from periscope import open_ops, tracks
    tid = tracks.repo_default_track("/Users/foo/dev/myproj")
    mocker.patch.object(open_ops, "worktree_for_branch",
                        return_value="/Users/foo/dev/worktrees/myproj/tc-old")
    spawn = _patch(mocker, "spawn_worktree", return_value={})
    _patch(mocker, "tmux", side_effect=_fake_window_tmux(index="7", pane_id="%pr"))
    _patch(mocker, "_run", return_value=(0, ""))
    mutate = _patch(mocker, "_tmux_mutate", return_value=(True, "@7"))

    r = client.post(f"/api/window/new?session={tid}&mode=shell&branch=tc/old")

    assert r.status_code == 200, r.text
    spawn.assert_not_called()
    call = next(c for c in mutate.call_args_list if c.args[0] == "new-window")
    cwd_idx = list(call.args).index("-c") + 1
    assert call.args[cwd_idx] == "/Users/foo/dev/worktrees/myproj/tc-old"
