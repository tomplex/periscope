"""/api/reviews* — the queue, a walkthrough, comment/prepare/merge/clear.
Windows come from a patched state_hub._last_blob (the route never computes
the state blob — docs/testing.md's no-thread rule); delivery and spawn are
AsyncMocks."""

import json
from unittest.mock import AsyncMock

import pytest

from periscope import reviews, state_hub

WT = "# T\n\n> branch: feat/x · head: abc\n\nProblem.\n\n## Stop A\n> kind: reuse · refs: a.py:1-3\n\nbody\n"


def _win(**over):
    base = {
        "pid": "p1",
        "pane_id": "%1",
        "session": "s",
        "index": 1,
        "name": "work",
        "cwd": "/dev/fdy/wt",
        "repo_key": "/dev/fdy",
        "repo_label": "fdy",
        "branch": "feat/x",
        "agent": "claude",
        "channel_attached": True,
        "activity": 1,
        "track_id": "/dev/fdy",
        "pr": None,
        "pr_state": None,
        "ci": None,
    }
    return {**base, **over}


@pytest.fixture
def place(mocker):
    # Never the real rail write or a live tmux listing.
    mocker.patch("periscope.routes.reviews.list_windows", return_value=[])
    return mocker.patch("periscope.routes.reviews.open_ops.place_in_rail")


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch, mocker, place):
    monkeypatch.setattr(reviews, "REVIEWS_DIR", tmp_path)
    monkeypatch.setattr(state_hub, "_last_blob", json.dumps({"windows": [_win()]}))
    mocker.patch("periscope.worktrees._cached_worktrees", return_value=[])
    mocker.patch("periscope.worktrees.for_branch", return_value=None)
    mocker.patch("periscope.reviews.git_pr.cached_linked_pr_state", return_value=None)
    # Never the real thing in tests: it starts a _bg thread (docs/testing.md).
    mocker.patch("periscope.reviews.cached_open_prs", return_value=[])
    mocker.patch("periscope.routes.reviews.open_ops.discover_repos", return_value=["/dev/fdy"])
    # A spawn in one test must not make the next test's prepare refuse to spawn.
    from periscope.routes import reviews as route

    monkeypatch.setattr(route, "_recent_spawn", {})
    yield


def _write_wt():
    p = reviews.walkthrough_path("fdy", "feat/x")
    p.parent.mkdir(parents=True)
    p.write_text(WT)


def test_list(client):
    _write_wt()
    r = client.get("/api/reviews")
    assert r.status_code == 200
    rows = r.json()["rows"]
    assert len(rows) == 1 and rows[0]["branch"] == "feat/x" and rows[0]["walkthrough"]["stops"] == 1


def test_list_derives_tickets_with_the_settings_team_keys(client, clean_state, monkeypatch):
    clean_state["settings"] = {"linear_team_keys": ["FDY"]}
    wins = [_win(pid="a", branch="tdody/fdy-8562-x"), _win(pid="b", branch="tdody/thi-442-y")]
    monkeypatch.setattr(state_hub, "_last_blob", json.dumps({"windows": wins}))
    rows = {r["branch"]: r for r in client.get("/api/reviews").json()["rows"]}
    assert rows["tdody/fdy-8562-x"]["linear_ticket"] == "FDY-8562"
    assert rows["tdody/thi-442-y"]["linear_ticket"] is None


def test_walkthrough_404_without_file_and_no_diff_without_worktree(client):
    assert client.get("/api/reviews/walkthrough", params={"repo": "fdy", "branch": "feat/x"}).status_code == 404
    _write_wt()
    r = client.get("/api/reviews/walkthrough", params={"repo": "fdy", "branch": "feat/x"})
    assert r.status_code == 200
    body = r.json()
    assert body["text"] == WT and body["comments"] == [] and body["diff"] is None and body["stale_by"] is None
    assert body["reviewed"] == {}


def test_comment_appends_and_delivers(client, mocker):
    deliver = mocker.patch("periscope.channels._deliver", new=AsyncMock(return_value={"ok": True}))
    r = client.post("/api/reviews/comment", json={"repo": "fdy", "branch": "feat/x", "stop": "Stop A", "body": "why?"})
    assert r.status_code == 200 and r.json() == {"ok": True}
    pane_id, msg, sender = deliver.call_args.args
    assert pane_id == "%1" and sender == "periscope"
    assert 'Review comment on "Stop A"' in msg and msg.endswith("why?")
    got = reviews.parse_comments(reviews.comments_path("fdy", "feat/x").read_text())
    assert got[0]["stop"] == "Stop A" and got[0]["body"] == "why?"


def test_comment_without_owner_still_appends_409(client, monkeypatch):
    monkeypatch.setattr(state_hub, "_last_blob", json.dumps({"windows": []}))
    r = client.post("/api/reviews/comment", json={"repo": "fdy", "branch": "feat/x", "stop": "Stop A", "body": "why?"})
    assert r.status_code == 409 and "no owning pane" in r.json()["detail"]
    saved = reviews.parse_comments(reviews.comments_path("fdy", "feat/x").read_text())
    assert [(c["stop"], c["resolved"]) for c in saved] == [("Stop A", False)]


def test_comment_deliver_failure_is_409(client, mocker):
    mocker.patch(
        "periscope.channels._deliver", new=AsyncMock(return_value={"ok": False, "error": "target not attached"})
    )
    r = client.post("/api/reviews/comment", json={"repo": "fdy", "branch": "feat/x", "stop": "Stop A", "body": "why?"})
    assert r.status_code == 409 and "not attached" in r.json()["detail"]


def test_prepare_with_owner_delivers(client, mocker, place):
    deliver = mocker.patch("periscope.channels._deliver", new=AsyncMock(return_value={"ok": True}))
    r = client.post("/api/reviews/prepare", json={"repo": "fdy", "branch": "feat/x"})
    assert r.status_code == 200 and r.json() == {"ok": True, "mode": "delivered"}
    msg = deliver.call_args.args[1]
    assert "walkthrough" in msg and str(reviews.walkthrough_path("fdy", "feat/x")) in msg
    place.assert_not_called()


def test_prepare_without_owner_spawns_in_worktree(client, mocker, monkeypatch, place):
    monkeypatch.setattr(state_hub, "_last_blob", json.dumps({"windows": [_win(agent=None)]}))
    mocker.patch("periscope.worktrees.for_branch", return_value="/dev/fdy/wt")
    mocker.patch("periscope.routes.reviews.tracks.repo_default_track", return_value="/dev/fdy")
    mocker.patch(
        "periscope.routes.reviews.open_ops.resolve_worktree_session", return_value=("fdy-wt", {"repo": "/dev/fdy"})
    )
    spawn = mocker.patch(
        "periscope.channels.spawn_claude_with_prompt",
        new=AsyncMock(
            return_value={"target": "fdy-wt:2", "session": "fdy-wt", "index": 2, "pid": "k", "pane_id": "%5"}
        ),
    )
    r = client.post("/api/reviews/prepare", json={"repo": "fdy", "branch": "feat/x"})
    assert r.status_code == 200 and r.json()["mode"] == "spawned"
    args, kwargs = spawn.call_args
    assert args[0] == "/dev/fdy/wt" and args[1] == "/dev/fdy" and kwargs["session"] == "fdy-wt"
    assert "walkthrough" in args[2] and str(reviews.walkthrough_path("fdy", "feat/x")) in args[2]
    place.assert_called_once_with("fdy-wt", {"repo": "/dev/fdy"}, ["k"])


def test_prepare_with_no_windows_at_all_still_finds_the_worktree(client, mocker, monkeypatch):
    # The queue found the repo via discover_repos; the route must too, or the
    # queue enables prepare on a row whose click 409s.
    monkeypatch.setattr(state_hub, "_last_blob", json.dumps({"windows": []}))
    mocker.patch("periscope.worktrees.for_branch", return_value="/dev/fdy/wt")
    mocker.patch("periscope.routes.reviews.tracks.repo_default_track", return_value="/dev/fdy")
    mocker.patch(
        "periscope.routes.reviews.open_ops.resolve_worktree_session", return_value=("fdy-wt", {"repo": "/dev/fdy"})
    )
    spawn = mocker.patch(
        "periscope.channels.spawn_claude_with_prompt",
        new=AsyncMock(
            return_value={"target": "fdy-wt:2", "session": "fdy-wt", "index": 2, "pid": "k", "pane_id": "%5"}
        ),
    )
    r = client.post("/api/reviews/prepare", json={"repo": "fdy", "branch": "feat/x"})
    assert r.status_code == 200 and spawn.call_args.args[0] == "/dev/fdy/wt"


def test_prepare_without_owner_or_worktree_409(client, monkeypatch):
    monkeypatch.setattr(state_hub, "_last_blob", json.dumps({"windows": []}))
    r = client.post("/api/reviews/prepare", json={"repo": "fdy", "branch": "feat/x"})
    assert r.status_code == 409


def test_bad_repo_or_branch_is_400(client):
    assert (
        client.post("/api/reviews/comment", json={"repo": "../x", "branch": "b", "stop": "s", "body": "t"}).status_code
        == 400
    )
    assert client.post("/api/reviews/clear", json={"repo": "fdy", "branch": "../../etc"}).status_code == 400


def test_merge_without_owner_409(client, monkeypatch):
    monkeypatch.setattr(state_hub, "_last_blob", json.dumps({"windows": []}))
    assert client.post("/api/reviews/merge", json={"repo": "fdy", "branch": "feat/x"}).status_code == 409


def test_clear_removes_dir(client):
    _write_wt()
    r = client.post("/api/reviews/clear", json={"repo": "fdy", "branch": "feat/x"})
    assert r.status_code == 200
    assert not reviews.review_dir("fdy", "feat/x").exists()


@pytest.mark.parametrize(
    "repo,branch",
    [
        ("", "b"),
        ("fdy", ""),
        ("fdy", ".."),
        ("fdy", "a/../b"),
        ("fdy", "/abs"),
        ("fdy", "."),
        (".", "b"),
        ("..", "b"),
        ("a/b", "b"),
    ],
)
def test_check_rejects_traversal(client, repo, branch):
    assert client.post("/api/reviews/clear", json={"repo": repo, "branch": branch}).status_code == 400
    assert client.get("/api/reviews/walkthrough", params={"repo": repo, "branch": branch}).status_code == 400


def test_clear_dot_branch_never_touches_repo_dir(client):
    _write_wt()
    assert client.post("/api/reviews/clear", json={"repo": "fdy", "branch": "."}).status_code == 400
    assert reviews.walkthrough_path("fdy", "feat/x").exists()


def test_comment_timestamp_is_minute_precision(client, mocker):
    mocker.patch("periscope.channels._deliver", new=AsyncMock(return_value={"ok": True}))
    client.post("/api/reviews/comment", json={"repo": "fdy", "branch": "feat/x", "stop": "Stop A", "body": "why?"})
    got = reviews.parse_comments(reviews.comments_path("fdy", "feat/x").read_text())
    assert len(got) == 1 and len(got[0]["ts"]) == 16


def test_prepare_spawn_without_pid_still_succeeds(client, mocker, monkeypatch, place):
    monkeypatch.setattr(state_hub, "_last_blob", json.dumps({"windows": []}))
    mocker.patch("periscope.worktrees.for_branch", return_value="/dev/fdy/wt")
    mocker.patch("periscope.routes.reviews.tracks.repo_default_track", return_value="/dev/fdy")
    mocker.patch(
        "periscope.routes.reviews.open_ops.resolve_worktree_session", return_value=("fdy-wt", {"repo": "/dev/fdy"})
    )
    spawn = mocker.patch(
        "periscope.channels.spawn_claude_with_prompt",
        new=AsyncMock(return_value={"target": "fdy-wt:2", "session": "fdy-wt", "index": 2, "pid": "", "pane_id": "%5"}),
    )
    r = client.post("/api/reviews/prepare", json={"repo": "fdy", "branch": "feat/x"})
    assert r.status_code == 200 and r.json()["pane_id"] is None and spawn.call_count == 1
    place.assert_not_called()


@pytest.mark.parametrize("stop", ["", "  ", "a\nb", "a\rb"])
def test_comment_bad_stop_is_400(client, stop):
    r = client.post("/api/reviews/comment", json={"repo": "fdy", "branch": "feat/x", "stop": stop, "body": "t"})
    assert r.status_code == 400
    assert not reviews.comments_path("fdy", "feat/x").exists()


def test_merge_prompt_gates_on_resolved(client, mocker):
    deliver = mocker.patch("periscope.channels._deliver", new=AsyncMock(return_value={"ok": True}))
    assert client.post("/api/reviews/merge", json={"repo": "fdy", "branch": "feat/x"}).status_code == 200
    msg = deliver.call_args.args[1]
    assert "resolved: yes" in msg and "do not merge" in msg


def test_concurrent_prepare_spawns_once(mocker, monkeypatch, place):
    import asyncio

    import httpx

    from periscope import app as app_mod
    from periscope.routes import reviews as route

    monkeypatch.setattr(state_hub, "_last_blob", json.dumps({"windows": []}))
    mocker.patch("periscope.worktrees.for_branch", return_value="/dev/fdy/wt")
    mocker.patch("periscope.routes.reviews.tracks.repo_default_track", return_value="/dev/fdy")
    mocker.patch(
        "periscope.routes.reviews.open_ops.resolve_worktree_session", return_value=("fdy-wt", {"repo": "/dev/fdy"})
    )

    async def run():
        gate = asyncio.Event()

        async def slow_spawn(*a, **k):
            await gate.wait()
            return {"target": "fdy-wt:2", "session": "fdy-wt", "index": 2, "pid": "k", "pane_id": "%5"}

        spawn = mocker.patch("periscope.channels.spawn_claude_with_prompt", new=AsyncMock(side_effect=slow_spawn))
        body = {"repo": "fdy", "branch": "feat/x"}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_mod.app), base_url="http://t") as c:
            first = asyncio.create_task(c.post("/api/reviews/prepare", json=body))
            while not spawn.await_count and not spawn.call_count:
                await asyncio.sleep(0.01)
            second = await c.post("/api/reviews/prepare", json=body)
            gate.set()
            return (await first).status_code, second.status_code, spawn.call_count

    assert asyncio.run(run()) == (200, 409, 1)
    assert route._preparing == set()


def test_prepare_survives_rail_placement_failure(client, mocker, monkeypatch, place):
    place.side_effect = RuntimeError("store busy")
    monkeypatch.setattr(state_hub, "_last_blob", json.dumps({"windows": []}))
    mocker.patch("periscope.worktrees.for_branch", return_value="/dev/fdy/wt")
    mocker.patch("periscope.routes.reviews.tracks.repo_default_track", return_value="/dev/fdy")
    mocker.patch(
        "periscope.routes.reviews.open_ops.resolve_worktree_session", return_value=("fdy-wt", {"repo": "/dev/fdy"})
    )
    spawn = mocker.patch(
        "periscope.channels.spawn_claude_with_prompt",
        new=AsyncMock(
            return_value={"target": "fdy-wt:2", "session": "fdy-wt", "index": 2, "pid": "k", "pane_id": "%5"}
        ),
    )
    r = client.post("/api/reviews/prepare", json={"repo": "fdy", "branch": "feat/x"})
    assert r.status_code == 200 and r.json()["pane_id"] == "%5" and spawn.call_count == 1


def _pr123(monkeypatch, windows):
    """A review-requested PR (headRefName feat/x) checked out locally as pr-123."""
    import time

    monkeypatch.setitem(reviews._prs_cache, "/dev/fdy", (time.time(), [{"number": 123, "branch": "feat/x"}]))
    monkeypatch.setattr(state_hub, "_last_blob", json.dumps({"windows": windows}))


def test_prepare_delivers_to_owner_on_pr_alias(client, mocker, monkeypatch, place):
    _pr123(monkeypatch, [_win(branch="pr-123")])
    deliver = mocker.patch("periscope.channels._deliver", new=AsyncMock(return_value={"ok": True}))
    r = client.post("/api/reviews/prepare", json={"repo": "fdy", "branch": "feat/x"})
    assert r.status_code == 200 and r.json()["mode"] == "delivered" and deliver.call_args.args[0] == "%1"


def test_prepare_spawns_in_pr_alias_worktree(client, mocker, monkeypatch, place):
    _pr123(monkeypatch, [])
    mocker.patch("periscope.worktrees.for_branch", side_effect=lambda root, b: "/dev/fdy/pr" if b == "pr-123" else None)
    mocker.patch("periscope.routes.reviews.tracks.repo_default_track", return_value="/dev/fdy")
    mocker.patch(
        "periscope.routes.reviews.open_ops.resolve_worktree_session", return_value=("fdy-pr", {"repo": "/dev/fdy"})
    )
    spawn = mocker.patch(
        "periscope.channels.spawn_claude_with_prompt",
        new=AsyncMock(
            return_value={"target": "fdy-pr:2", "session": "fdy-pr", "index": 2, "pid": "k", "pane_id": "%5"}
        ),
    )
    r = client.post("/api/reviews/prepare", json={"repo": "fdy", "branch": "feat/x"})
    assert r.status_code == 200 and spawn.call_args.args[0] == "/dev/fdy/pr"


def test_comment_and_merge_find_owner_on_pr_alias(client, mocker, monkeypatch):
    _pr123(monkeypatch, [_win(branch="pr-123")])
    mocker.patch("periscope.channels._deliver", new=AsyncMock(return_value={"ok": True}))
    body = {"repo": "fdy", "branch": "feat/x"}
    assert client.post("/api/reviews/comment", json={**body, "stop": "Stop A", "body": "why?"}).status_code == 200
    assert client.post("/api/reviews/merge", json=body).status_code == 200


def test_walkthrough_finds_pr_alias_worktree_for_diff(client, mocker, monkeypatch):
    _pr123(monkeypatch, [])
    _write_wt()
    mocker.patch("periscope.worktrees._cached_worktrees", return_value=[("/dev/fdy/pr", "pr-123")])
    mocker.patch("periscope.routes.reviews.gitdiff.branch_base", return_value="origin/main")
    mocker.patch("periscope.routes.reviews.gitdiff.diff_for", return_value={"files": []})
    mocker.patch("periscope.reviews.head_sha", return_value="abc")
    mocker.patch("periscope.reviews.stale_by", return_value=0)
    r = client.get("/api/reviews/walkthrough", params={"repo": "fdy", "branch": "feat/x"})
    assert r.status_code == 200 and r.json()["diff"] == {"files": []}


def test_prepare_refuses_until_spawned_pane_attaches(client, mocker, monkeypatch, place):
    from periscope.routes import reviews as route

    monkeypatch.setattr(state_hub, "_last_blob", json.dumps({"windows": []}))
    mocker.patch("periscope.worktrees.for_branch", return_value="/dev/fdy/wt")
    mocker.patch("periscope.routes.reviews.tracks.repo_default_track", return_value="/dev/fdy")
    mocker.patch(
        "periscope.routes.reviews.open_ops.resolve_worktree_session", return_value=("fdy-wt", {"repo": "/dev/fdy"})
    )
    spawn = mocker.patch(
        "periscope.channels.spawn_claude_with_prompt",
        new=AsyncMock(
            return_value={"target": "fdy-wt:2", "session": "fdy-wt", "index": 2, "pid": "k", "pane_id": "%5"}
        ),
    )
    body = {"repo": "fdy", "branch": "feat/x"}
    assert client.post("/api/reviews/prepare", json=body).json()["mode"] == "spawned"
    # The pane has not attached (still no owner window): a second click must not spawn a twin.
    r = client.post("/api/reviews/prepare", json=body)
    assert r.status_code == 409 and "attached" in r.json()["detail"] and spawn.call_count == 1
    # Once it attaches, prepare delivers and forgets the spawn.
    monkeypatch.setattr(state_hub, "_last_blob", json.dumps({"windows": [_win(branch="feat/x")]}))
    deliver = mocker.patch("periscope.channels._deliver", new=AsyncMock(return_value={"ok": True}))
    assert client.post("/api/reviews/prepare", json=body).json()["mode"] == "delivered"
    assert deliver.call_count == 1 and route._recent_spawn == {}


def test_prepare_spawns_again_once_the_guard_expires(client, mocker, monkeypatch, place):
    """A spawned pane that never attaches (it died) must not block the branch forever."""
    from periscope.routes import reviews as route

    monkeypatch.setattr(state_hub, "_last_blob", json.dumps({"windows": []}))
    mocker.patch("periscope.worktrees.for_branch", return_value="/dev/fdy/wt")
    mocker.patch("periscope.routes.reviews.tracks.repo_default_track", return_value="/dev/fdy")
    mocker.patch(
        "periscope.routes.reviews.open_ops.resolve_worktree_session", return_value=("fdy-wt", {"repo": "/dev/fdy"})
    )
    spawn = mocker.patch(
        "periscope.channels.spawn_claude_with_prompt",
        new=AsyncMock(
            return_value={"target": "fdy-wt:2", "session": "fdy-wt", "index": 2, "pid": "k", "pane_id": "%5"}
        ),
    )
    clock = mocker.patch("periscope.routes.reviews.time.monotonic", return_value=1000.0)
    body = {"repo": "fdy", "branch": "feat/x"}
    assert client.post("/api/reviews/prepare", json=body).status_code == 200
    clock.return_value = 1000.0 + route.RECENT_SPAWN_S - 1
    assert client.post("/api/reviews/prepare", json=body).status_code == 409
    clock.return_value = 1000.0 + route.RECENT_SPAWN_S
    assert client.post("/api/reviews/prepare", json=body).status_code == 200 and spawn.call_count == 2


def test_walkthrough_strips_generated_hunks(client, mocker, monkeypatch):
    _pr123(monkeypatch, [])
    _write_wt()
    mocker.patch("periscope.worktrees._cached_worktrees", return_value=[("/dev/fdy/pr", "pr-123")])
    mocker.patch("periscope.routes.reviews.gitdiff.branch_base", return_value="origin/main")
    hunk = {"header": "", "new_start": 1, "lines": [{"kind": "add", "text": "x"}]}
    mocker.patch(
        "periscope.routes.reviews.gitdiff.diff_for",
        return_value={
            "files": [
                {"path": "static/dist/app.js", "hunks": [hunk]},
                {"path": "package-lock.json", "hunks": [hunk]},
                {"path": "periscope/x.py", "hunks": [hunk]},
            ]
        },
    )
    mocker.patch("periscope.reviews.head_sha", return_value="abc")
    mocker.patch("periscope.reviews.stale_by", return_value=0)
    files = client.get("/api/reviews/walkthrough", params={"repo": "fdy", "branch": "feat/x"}).json()["diff"]["files"]
    assert [(f["path"], f["hunks"], f.get("generated")) for f in files] == [
        ("static/dist/app.js", [], True),
        ("package-lock.json", [], True),
        ("periscope/x.py", [hunk], None),
    ]


def test_prepare_without_worktree_checks_out_the_open_pr_then_spawns(client, mocker, monkeypatch, place):
    monkeypatch.setattr(state_hub, "_last_blob", json.dumps({"windows": []}))
    mocker.patch("periscope.worktrees.for_branch", return_value=None)
    mocker.patch("periscope.routes.reviews._roots", return_value={"fdy": "/dev/fdy"})
    mocker.patch(
        "periscope.routes.reviews.reviews.cached_open_prs",
        return_value=[{"number": 42, "branch": "feat/x", "title": "T", "role": "review-requested"}],
    )
    fetch = mocker.patch(
        "periscope.routes.reviews.projects.fetch_pr_into_worktree",
        return_value=mocker.Mock(path="/dev/fdy/.claude/worktrees/feat-x", is_fork=True),
    )
    mocker.patch("periscope.routes.reviews.tracks.repo_default_track", return_value="/dev/fdy")
    mocker.patch(
        "periscope.routes.reviews.open_ops.resolve_worktree_session", return_value=("fdy-wt", {"repo": "/dev/fdy"})
    )
    spawn = mocker.patch(
        "periscope.channels.spawn_claude_with_prompt",
        new=AsyncMock(
            return_value={"target": "fdy-wt:2", "session": "fdy-wt", "index": 2, "pid": "k", "pane_id": "%5"}
        ),
    )
    stamp = mocker.patch("periscope.routes.reviews.store.set_window_fields")
    r = client.post("/api/reviews/prepare", json={"repo": "fdy", "branch": "feat/x"})
    assert r.status_code == 200 and r.json()["mode"] == "spawned" and r.json()["checked_out"] is True
    fetch.assert_called_once_with("/dev/fdy", 42)
    assert spawn.call_args.args[0] == "/dev/fdy/.claude/worktrees/feat-x"
    # What a ⌘K PR open stamps, so the card shows the PR and cleanup sees merged/closed.
    stamp.assert_called_once_with("k", linked_pr=42, is_fork=True)
    place.assert_called_once()


def test_prepare_checks_out_a_header_pr_the_open_list_dropped(client, mocker, monkeypatch, place):
    # A submitted review drops the PR from `review-requested:@me`; the queue
    # still shows the row open from its header, so prepare must find it too.
    monkeypatch.setattr(state_hub, "_last_blob", json.dumps({"windows": []}))
    p = reviews.walkthrough_path("fdy", "feat/x")
    p.parent.mkdir(parents=True)
    p.write_text(WT.replace("head: abc", "head: abc · pr: 123"))
    mocker.patch("periscope.worktrees.for_branch", return_value=None)
    mocker.patch("periscope.routes.reviews._roots", return_value={"fdy": "/dev/fdy"})
    mocker.patch("periscope.routes.reviews.reviews.cached_open_prs", return_value=[])
    linked = mocker.patch("periscope.routes.reviews.git_pr.cached_linked_pr_state", return_value={"pr_state": "open"})
    fetch = mocker.patch(
        "periscope.routes.reviews.projects.fetch_pr_into_worktree",
        return_value=mocker.Mock(path="/dev/fdy/pr", is_fork=False),
    )
    mocker.patch("periscope.routes.reviews.tracks.repo_default_track", return_value="/dev/fdy")
    mocker.patch(
        "periscope.routes.reviews.open_ops.resolve_worktree_session", return_value=("fdy-wt", {"repo": "/dev/fdy"})
    )
    mocker.patch(
        "periscope.channels.spawn_claude_with_prompt",
        new=AsyncMock(return_value={"target": "t", "session": "fdy-wt", "index": 2, "pid": "k", "pane_id": "%5"}),
    )
    stamp = mocker.patch("periscope.routes.reviews.store.set_window_fields")
    r = client.post("/api/reviews/prepare", json={"repo": "fdy", "branch": "feat/x"})
    assert r.status_code == 200 and r.json()["checked_out"] is True
    linked.assert_called_once_with("/dev/fdy", 123)
    fetch.assert_called_once_with("/dev/fdy", 123)
    stamp.assert_called_once_with("k", linked_pr=123, is_fork=False)


def test_prepare_without_worktree_or_open_pr_409s_before_touching_git(client, mocker, monkeypatch):
    monkeypatch.setattr(state_hub, "_last_blob", json.dumps({"windows": []}))
    mocker.patch("periscope.worktrees.for_branch", return_value=None)
    mocker.patch("periscope.routes.reviews._roots", return_value={"fdy": "/dev/fdy"})
    mocker.patch("periscope.routes.reviews.reviews.cached_open_prs", return_value=[])
    fetch = mocker.patch("periscope.routes.reviews.projects.fetch_pr_into_worktree")
    r = client.post("/api/reviews/prepare", json={"repo": "fdy", "branch": "feat/x"})
    assert r.status_code == 409 and "no open PR" in r.json()["detail"]
    fetch.assert_not_called()


def test_reviewed_round_trips_through_the_walkthrough(client):
    _write_wt()
    q = {"repo": "fdy", "branch": "feat/x"}
    r = client.post("/api/reviews/reviewed", json={**q, "stop": "Stop A", "sig": "0123abcd", "reviewed": True})
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert client.get("/api/reviews/walkthrough", params=q).json()["reviewed"] == {"Stop A": "0123abcd"}
    r = client.post("/api/reviews/reviewed", json={**q, "stop": "Stop A", "sig": "0123abcd", "reviewed": False})
    assert r.status_code == 200
    assert client.get("/api/reviews/walkthrough", params=q).json()["reviewed"] == {}
    assert not reviews.reviewed_path("fdy", "feat/x").exists()


@pytest.mark.parametrize(
    "stop,sig",
    [
        ("", "0123abcd"),
        ("  ", "0123abcd"),
        ("a\nb", "0123abcd"),
        ("Stop A", "xyz"),
        ("Stop A", "0123ABCD"),
        ("Stop A", "0123abcd0"),
        ("Stop A", "0123abcd\n"),
    ],
)
def test_reviewed_bad_stop_or_sig_is_400(client, stop, sig):
    _write_wt()
    r = client.post(
        "/api/reviews/reviewed", json={"repo": "fdy", "branch": "feat/x", "stop": stop, "sig": sig, "reviewed": True}
    )
    assert r.status_code == 400
    assert not reviews.reviewed_path("fdy", "feat/x").exists()


def test_reviewed_without_a_walkthrough_is_404_and_creates_nothing(client):
    r = client.post(
        "/api/reviews/reviewed",
        json={"repo": "fdy", "branch": "feat/x", "stop": "Stop A", "sig": "0123abcd", "reviewed": True},
    )
    assert r.status_code == 404
    assert not reviews.review_dir("fdy", "feat/x").exists()


def test_reviewed_with_a_dir_holding_only_comments_is_404(client):
    # A dir can hold comments.md (an earlier comment, never a prepare) with no
    # walkthrough.md: _set_reviewed's is_file() guard must still 404 rather
    # than let set_reviewed write reviewed.json into a dir with nothing to mark.
    path = reviews.comments_path("fdy", "feat/x")
    path.parent.mkdir(parents=True)
    path.write_text("## 2026-10-26T09:14 · Stop A\n> resolved: no\n\nhi\n\n")
    r = client.post(
        "/api/reviews/reviewed",
        json={"repo": "fdy", "branch": "feat/x", "stop": "Stop A", "sig": "0123abcd", "reviewed": True},
    )
    assert r.status_code == 404
    assert not reviews.reviewed_path("fdy", "feat/x").exists()


def test_reviewed_rejects_a_stop_with_an_unpaired_surrogate(client):
    # ensure_ascii=False in set_reviewed's json.dumps lets a lone surrogate
    # reach write_text, which raises UnicodeEncodeError; _check_stop must
    # reject it with a 400 before any file is touched. httpx's own `json=`
    # encodes with ensure_ascii=False and fails to utf-8-encode the surrogate
    # before the request is even sent, so the body is built by hand here
    # (plain json.dumps escapes it as ASCII \ud800) to reach the server at all.
    _write_wt()
    body = json.dumps({"repo": "fdy", "branch": "feat/x", "stop": "\ud800", "sig": "0123abcd", "reviewed": True})
    r = client.post("/api/reviews/reviewed", content=body, headers={"Content-Type": "application/json"})
    assert r.status_code == 400
    assert not reviews.reviewed_path("fdy", "feat/x").exists()


def test_reviewed_on_a_review_cleared_mid_request_is_404(client, mocker):
    _write_wt()
    mocker.patch("periscope.routes.reviews.reviews.set_reviewed", side_effect=FileNotFoundError)
    r = client.post(
        "/api/reviews/reviewed",
        json={"repo": "fdy", "branch": "feat/x", "stop": "Stop A", "sig": "0123abcd", "reviewed": True},
    )
    assert r.status_code == 404


def test_reviewed_rejects_traversal(client):
    r = client.post(
        "/api/reviews/reviewed",
        json={"repo": "..", "branch": "feat/x", "stop": "A", "sig": "0123abcd", "reviewed": True},
    )
    assert r.status_code == 400
