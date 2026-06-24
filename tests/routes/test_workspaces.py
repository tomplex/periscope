import pytest
from fastapi.testclient import TestClient

from periscope.app import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def _state(clean_state, fresh_activity_db):
    yield


def test_create_workspace():
    r = client.post("/api/workspaces", json={"name": "Auth", "base_repo": "/dev/fdy"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["id"].startswith("ws_auth")
    assert body["name"] == "Auth"


def test_create_promote_tags_panes():
    r = client.post("/api/workspaces", json={"name": "Auth", "tag_panes": ["%1", "%2"]})
    wid = r.json()["id"]
    from periscope import activity
    assert activity.get_pane_workspace("%1") == wid
    assert activity.get_pane_workspace("%2") == wid


def test_tag_and_untag():
    wid = client.post("/api/workspaces", json={"name": "W"}).json()["id"]
    assert client.post("/api/workspaces/tag",
                       json={"workspace_id": wid, "pane_id": "%5"}).status_code == 200
    from periscope import activity
    assert activity.get_pane_workspace("%5") == wid
    assert client.post("/api/workspaces/untag", json={"pane_id": "%5"}).status_code == 200
    assert activity.get_pane_workspace("%5") is None


def test_tag_unknown_workspace_404():
    r = client.post("/api/workspaces/tag", json={"workspace_id": "ws_nope", "pane_id": "%1"})
    assert r.status_code == 404


def test_patch_and_archive():
    wid = client.post("/api/workspaces", json={"name": "W"}).json()["id"]
    assert client.post("/api/workspaces/patch",
                       json={"workspace_id": wid, "name": "W2"}).status_code == 200
    assert client.post("/api/workspaces/archive",
                       json={"workspace_id": wid}).status_code == 200
    r = client.post("/api/workspaces/archive", json={"workspace_id": "ws_nope"})
    assert r.status_code == 404
