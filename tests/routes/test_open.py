import shutil

import pytest

needs_tmux = pytest.mark.skipif(not shutil.which("tmux"), reason="tmux not installed")


@needs_tmux
def test_post_open_path_returns_contract(client, tmp_git_repo, clean_state, tmux_test_server):
    repo = str(tmp_git_repo)
    r = client.post("/api/open", json={"path": repo})
    assert r.status_code == 200
    body = r.json()
    assert body["repo"] == repo and body["agent_pid"]
    assert body["agent"] == "claude"
    # agent_pid is the contract the client selects on, so create-or-focus is
    # visible in both branches; ui carries the TRACK-era rail keys.
    assert repo in body["ui"]["track_order"]        # repo-default track id == repo
    assert body["agent_pid"] in body["ui"]["tabs_by_track"][repo]

def test_post_open_non_git_400(client, tmp_path, clean_state):
    r = client.post("/api/open", json={"path": str(tmp_path)})
    assert r.status_code == 400

def test_post_open_bad_descriptor_400(client, clean_state):
    assert client.post("/api/open", json={}).status_code == 400
    assert client.post("/api/open", json={"branch": "x"}).status_code == 400  # repo missing

def test_get_catalog(client, clean_state):
    r = client.get("/api/open/catalog")
    assert r.status_code == 200 and "repos" in r.json() and "worktrees" in r.json()
