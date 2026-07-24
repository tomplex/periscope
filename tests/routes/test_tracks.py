"""Track REST routes: create / rename / move-tab / dissolve / teardown."""
import pytest
from fastapi.testclient import TestClient

from periscope.app import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def _state(clean_state, fresh_activity_db):
    yield


def test_create_returns_row():
    r = client.post("/api/tracks", json={"name": "Auth work"})
    assert r.status_code == 200
    body = r.json()
    assert body["id"].startswith("tk_auth-work")
    assert body["name"] == "Auth work"
    assert body["repo"] is None
    # persisted
    from periscope import activity
    assert activity.get_track(body["id"]) is not None


def test_create_with_repo():
    r = client.post("/api/tracks", json={"name": "fdy", "repo": "/dev/fdy"})
    assert r.status_code == 200
    assert r.json()["repo"] == "/dev/fdy"


def test_rename():
    tid = client.post("/api/tracks", json={"name": "W"}).json()["id"]
    r = client.patch(f"/api/tracks/{tid}", json={"name": "W2"})
    assert r.status_code == 200
    assert r.json()["name"] == "W2"


def test_rename_missing_404():
    r = client.patch("/api/tracks/tk_nope", json={"name": "X"})
    assert r.status_code == 404


def test_move_tab_retags():
    tid = client.post("/api/tracks", json={"name": "T"}).json()["id"]
    r = client.post(f"/api/tracks/{tid}/move-tab", json={"pane_id": "%7"})
    assert r.status_code == 200
    from periscope import activity
    assert activity.get_pane_track("%7") == tid


def test_move_tab_missing_track_404():
    r = client.post("/api/tracks/tk_nope/move-tab", json={"pane_id": "%1"})
    assert r.status_code == 404


def test_dissolve_kills_nothing(mocker):
    tid = client.post("/api/tracks", json={"name": "T"}).json()["id"]
    mutate = mocker.patch("periscope.routes.tracks._tmux_mutate")
    r = client.post(f"/api/tracks/{tid}/dissolve")
    assert r.status_code == 200
    mutate.assert_not_called()
    # track archived, but the pane tag survives so the tab itself lives on
    from periscope import activity
    assert activity.get_track(tid)["archived_at"] is not None


def test_dissolve_missing_404():
    r = client.post("/api/tracks/tk_nope/dissolve")
    assert r.status_code == 404


def test_teardown_returns_kill_list(mocker):
    tid = client.post("/api/tracks", json={"name": "T"}).json()["id"]
    from periscope import activity
    activity.set_pane_track("%1", tid)
    activity.set_pane_track("%2", tid)
    mocker.patch(
        "periscope.routes.tracks.list_windows",
        return_value=[
            {"session": "s", "index": 0, "pane_id": "%1", "cwd": "/x"},
            {"session": "s", "index": 1, "pane_id": "%2", "cwd": "/x"},
        ],
    )
    mutate = mocker.patch("periscope.routes.tracks._tmux_mutate")
    mocker.patch("periscope.routes.tracks.drop_target_focus")
    r = client.post(f"/api/tracks/{tid}/teardown", json={"delete_worktrees": False})
    assert r.status_code == 200
    killed = r.json()["killed"]
    assert sorted(p for _, p in killed) == ["%1", "%2"]
    assert mutate.call_count == 2


def test_teardown_refuses_loose_409():
    r = client.post("/api/tracks/loose/teardown", json={"delete_worktrees": False})
    assert r.status_code == 409


def test_teardown_refuses_repo_default_409():
    # repo-default track has id == repo (see repo_default_track / teardown_targets).
    # Use a slashless repo id so it routes through the {track_id} path param;
    # real repo paths contain slashes (which the UI never tears down anyway).
    from periscope import tracks
    repo = "fdy"
    tracks.repo_default_track(repo)
    r = client.post(f"/api/tracks/{repo}/teardown", json={"delete_worktrees": False})
    assert r.status_code == 409


def test_dissolve_refuses_repo_default_409():
    # Dissolving a catchall is a silent no-op (its tabs fall back to it), and
    # the archived row keeps resolving — 409 instead, matching teardown.
    from periscope import tracks
    repo = "fdy-dissolve"
    tracks.repo_default_track(repo)
    assert client.post(f"/api/tracks/{repo}/dissolve").status_code == 409


def test_teardown_missing_404():
    r = client.post("/api/tracks/tk_nope/teardown", json={"delete_worktrees": False})
    assert r.status_code == 404
