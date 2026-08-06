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
    r = client.patch(f"/api/tracks?track_id={tid}", json={"name": "W2"})
    assert r.status_code == 200
    assert r.json()["name"] == "W2"


def test_rename_missing_404():
    r = client.patch("/api/tracks?track_id=tk_nope", json={"name": "X"})
    assert r.status_code == 404


def test_move_tab_retags():
    tid = client.post("/api/tracks", json={"name": "T"}).json()["id"]
    r = client.post(f"/api/tracks/move-tab?track_id={tid}", json={"pane_id": "%7"})
    assert r.status_code == 200
    from periscope import activity
    assert activity.get_pane_track("%7") == tid


def test_move_tab_missing_track_404():
    r = client.post("/api/tracks/move-tab?track_id=tk_nope", json={"pane_id": "%1"})
    assert r.status_code == 404


def test_dissolve_kills_nothing(mocker):
    tid = client.post("/api/tracks", json={"name": "T"}).json()["id"]
    mutate = mocker.patch("periscope.routes.tracks._tmux_mutate")
    r = client.post(f"/api/tracks/dissolve?track_id={tid}")
    assert r.status_code == 200
    mutate.assert_not_called()
    # track archived, but the pane tag survives so the tab itself lives on
    from periscope import activity
    assert activity.get_track(tid)["archived_at"] is not None


def test_dissolve_missing_404():
    r = client.post("/api/tracks/dissolve?track_id=tk_nope")
    assert r.status_code == 404


def test_teardown_returns_kill_list(mocker):
    tid = client.post("/api/tracks", json={"name": "T"}).json()["id"]
    from periscope import activity
    # Tags key on @periscope_id (pid_raw on the raw rows); the kill list still
    # reports stable %N pane ids.
    activity.set_pane_track("aaaa0001", tid)
    activity.set_pane_track("aaaa0002", tid)
    mocker.patch(
        "periscope.routes.tracks.list_windows",
        return_value=[
            {"session": "s", "index": 0, "pane_id": "%1", "pid_raw": "aaaa0001", "cwd": "/x"},
            {"session": "s", "index": 1, "pane_id": "%2", "pid_raw": "aaaa0002", "cwd": "/x"},
        ],
    )
    mutate = mocker.patch("periscope.routes.tracks._tmux_mutate")
    mocker.patch("periscope.routes.tracks.drop_target_focus")
    r = client.post(f"/api/tracks/teardown?track_id={tid}", json={"delete_worktrees": False})
    assert r.status_code == 200
    killed = r.json()["killed"]
    assert sorted(p for _, p in killed) == ["%1", "%2"]
    assert mutate.call_count == 2


def test_teardown_refuses_loose_409():
    r = client.post("/api/tracks/teardown?track_id=loose", json={"delete_worktrees": False})
    assert r.status_code == 409


# A repo-default track's id IS its repo path. These use a REAL slash-bearing
# path: as a path segment the %2F 405'd before reaching any handler, so every
# write against a project row failed — dragging a tab into it, renaming it,
# dissolving it. Query param, per CLAUDE.md invariant #6.
REPO = "/Users/tom/dev/fdy"


def test_slashed_track_id_reaches_the_handler():
    """The regression: a repo path as the id must ROUTE, not 405."""
    from periscope import activity, tracks
    tracks.repo_default_track(REPO)
    r = client.post(f"/api/tracks/move-tab?track_id={REPO}", json={"pane_id": "%9"})
    assert r.status_code == 200, r.text
    assert activity.get_pane_track("%9") == REPO


def test_rename_slashed_track_id():
    from periscope import tracks
    tracks.repo_default_track(REPO)
    r = client.patch(f"/api/tracks?track_id={REPO}", json={"name": "the project"})
    assert r.status_code == 200
    assert r.json()["name"] == "the project"


def test_teardown_refuses_repo_default_409():
    from periscope import tracks
    tracks.repo_default_track(REPO)
    r = client.post(f"/api/tracks/teardown?track_id={REPO}", json={"delete_worktrees": False})
    assert r.status_code == 409


def test_dissolve_refuses_repo_default_409():
    # Dissolving a catchall is a silent no-op (its tabs fall back to it), and
    # the archived row keeps resolving — 409 instead, matching teardown.
    from periscope import tracks
    tracks.repo_default_track(REPO)
    assert client.post(f"/api/tracks/dissolve?track_id={REPO}").status_code == 409


def test_teardown_missing_404():
    r = client.post("/api/tracks/teardown?track_id=tk_nope", json={"delete_worktrees": False})
    assert r.status_code == 404
