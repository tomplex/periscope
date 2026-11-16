"""POST /api/linear/start — worktree, Claude with a prompt, the Linear link, rail placement.
Windows come from a patched state_hub.current_windows and the spawn is an AsyncMock (docs/testing.md)."""

import os
import time
from unittest.mock import AsyncMock

import pytest

from periscope import channels, store

BODY = {
    "repo": "/repos/api",
    "branch": "me/eng-1-fix-it",
    "ticket": "ENG-1",
    "title": "Fix it",
    "prompt": "Read Linear ticket ENG-1.",
}
SPAWNED = {"target": "api-wt:2", "session": "api-wt", "index": 2, "pid": "k", "pane_id": "%5"}


def _win(**over):
    base = {
        "pid": "p1",
        "pane_id": "%1",
        "session": "s",
        "index": 1,
        "repo_label": "api",
        "branch": "me/eng-1-fix-it",
        "agent": "claude",
        "channel_attached": True,
        "activity": 1,
    }
    return {**base, **over}


@pytest.fixture(autouse=True)
def _env(monkeypatch, mocker, windows):
    from periscope.routes import linear as route

    # A spawn in one test must not make the next test's start refuse to spawn.
    monkeypatch.setattr(route, "_recent_spawn", {})
    # Never a live tmux listing.
    mocker.patch("periscope.routes.linear.list_windows", return_value=[])
    mocker.patch("periscope.tracks.repo_default_track", return_value="/repos/api")
    mocker.patch("periscope.open_ops.resolve_worktree_session", return_value=("api-wt", {"repo": "/repos/api"}))


@pytest.fixture
def windows(mocker):
    """The windows the route reads; set .return_value in a test."""
    return mocker.patch("periscope.state_hub.current_windows", new=AsyncMock(return_value=[]))


@pytest.fixture
def worktree(mocker):
    return mocker.patch(
        "periscope.worktree_spawn.spawn_worktree",
        return_value={"path": "/repos/api-wt", "base_branch": "main", "branch": BODY["branch"], "reused": False},
    )


@pytest.fixture
def spawn(mocker):
    return mocker.patch("periscope.channels.spawn_claude_with_prompt", new=AsyncMock(return_value=dict(SPAWNED)))


@pytest.fixture
def place(mocker):
    return mocker.patch("periscope.open_ops.place_in_rail")


def test_start_spawns_claude_in_the_worktree(client, worktree, spawn, place):
    r = client.post("/api/linear/start", json=BODY)
    assert r.status_code == 200
    assert r.json() == {"mode": "spawned", "pid": "k", "path": "/repos/api-wt", "reused": False}
    worktree.assert_called_once_with("/repos/api", "me/eng-1-fix-it")
    args, kwargs = spawn.call_args
    assert args == ("/repos/api-wt", "/repos/api", "Read Linear ticket ENG-1.") and kwargs == {"session": "api-wt"}
    assert store.get_window("k") == {"linked_linear": "ENG-1", "linked_linear_title": "Fix it"}
    place.assert_called_once_with("api-wt", {"repo": "/repos/api"}, ["k"])


def test_a_claude_on_the_branch_is_reported_not_prompted(client, windows, worktree, spawn, place):
    from periscope.routes import linear as route

    windows.return_value = [_win()]
    route._recent_spawn[(os.path.realpath(BODY["repo"]), BODY["branch"])] = time.monotonic()
    r = client.post("/api/linear/start", json=BODY)
    assert r.status_code == 200 and r.json() == {"mode": "focused", "pid": "p1"}
    assert route._recent_spawn == {}
    worktree.assert_not_called()
    spawn.assert_not_called()
    place.assert_not_called()


def test_a_shell_or_an_unattached_pane_on_the_branch_does_not_count(client, windows, worktree, spawn, place):
    windows.return_value = [_win(agent=None), _win(pid="p2", channel_attached=False)]
    assert client.post("/api/linear/start", json=BODY).json()["mode"] == "spawned"


def test_a_start_before_the_spawned_claude_attaches_is_refused(client, worktree, spawn, place):
    from periscope.routes import linear as route

    assert client.post("/api/linear/start", json=BODY).status_code == 200
    r = client.post("/api/linear/start", json=BODY)
    assert r.status_code == 409 and "attached" in r.json()["detail"] and spawn.call_count == 1
    route._recent_spawn[(os.path.realpath(BODY["repo"]), BODY["branch"])] = time.monotonic() - route.RECENT_SPAWN_S
    assert client.post("/api/linear/start", json=BODY).status_code == 200 and spawn.call_count == 2


def test_concurrent_starts_spawn_once(mocker, worktree, place):
    import asyncio

    import httpx

    from periscope import app as app_mod
    from periscope.routes import linear as route

    async def run():
        gate = asyncio.Event()

        async def slow_spawn(*a, **k):
            await gate.wait()
            return dict(SPAWNED)

        spawn = mocker.patch("periscope.channels.spawn_claude_with_prompt", new=AsyncMock(side_effect=slow_spawn))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_mod.app), base_url="http://t") as c:
            first = asyncio.create_task(c.post("/api/linear/start", json=BODY))
            while not spawn.call_count and not first.done():
                await asyncio.sleep(0.01)
            second = await asyncio.wait_for(c.post("/api/linear/start", json=BODY), 5)
            gate.set()
            return (await first).status_code, second.status_code, spawn.call_count

    assert asyncio.run(run()) == (200, 409, 1)
    assert route._starting == set()


def test_a_bad_branch_is_400(client, worktree, spawn):
    worktree.side_effect = ValueError("branch name cannot start with '-': '-x'")
    r = client.post("/api/linear/start", json={**BODY, "branch": "-x"})
    assert r.status_code == 400 and "cannot start" in r.json()["detail"]
    spawn.assert_not_called()


def test_a_worktree_git_cannot_read_is_502(client, mocker, worktree, spawn):
    mocker.patch("periscope.open_ops.resolve_worktree_session", return_value=None)
    r = client.post("/api/linear/start", json=BODY)
    assert r.status_code == 502 and "not inside a git repo" in r.json()["detail"]
    spawn.assert_not_called()


def test_a_failed_spawn_is_502(client, worktree, spawn, place):
    spawn.side_effect = channels.SpawnFailed("no server")
    r = client.post("/api/linear/start", json=BODY)
    assert r.status_code == 502 and r.json()["detail"] == "spawn failed: no server"
    place.assert_not_called()


def test_a_rail_placement_failure_still_answers_spawned(client, worktree, spawn, place):
    place.side_effect = RuntimeError("store busy")
    r = client.post("/api/linear/start", json=BODY)
    assert r.status_code == 200 and r.json()["pid"] == "k"


def test_no_title_stamps_only_the_ticket(client, worktree, spawn, place):
    assert client.post("/api/linear/start", json={**BODY, "title": ""}).status_code == 200
    assert store.get_window("k") == {"linked_linear": "ENG-1"}
