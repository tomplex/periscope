"""POST /api/linear/start — worktree, Claude with a prompt, the Linear link, rail placement.
Windows come from a patched state_hub.current_windows and the spawn is an AsyncMock (docs/testing.md).
The tests of a reused PR worktree run real git in tmp_path."""

import json
import os
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from periscope import branch_spawn, channels, state_hub, store

BODY = {
    "repo": "/repos/api",
    "branch": "me/eng-1-fix-it",
    "ticket": "ENG-1",
    "title": "Fix it",
    "prompt": "Read Linear ticket ENG-1.",
}
SID = "3f2a9c1e-5b7d-4e2a-9f10-2c4d6e8a0b1c"
SPAWNED = {
    "target": "api-wt:2",
    "session": "api-wt",
    "index": 2,
    "pid": "k",
    "pane_id": "%5",
    "session_id": SID,
    "account": "b",
}
# The guard's key: prepare knows a repo only by its label, and both routes share the guard.
KEY = ("api", BODY["branch"])


def _win(**over):
    base = {
        "pid": "p1",
        "pane_id": "%1",
        "session": "s",
        "index": 1,
        "repo_key": "/repos/api",
        "repo_label": "api",
        "branch": "me/eng-1-fix-it",
        "agent": "claude",
        "channel_attached": True,
        "activity": 1,
    }
    return {**base, **over}


@pytest.fixture(autouse=True)
def _env(monkeypatch, mocker, windows):
    # A spawn in one test must not make the next test's start refuse to spawn.
    monkeypatch.setattr(branch_spawn, "_recent_spawn", {})
    # Never a live tmux listing.
    mocker.patch("periscope.open_ops.list_windows", return_value=[])
    mocker.patch("periscope.tracks.repo_default_track", return_value="/repos/api")
    mocker.patch("periscope.open_ops.resolve_worktree_session", return_value=("api-wt", {"repo": "/repos/api"}))


@pytest.fixture(autouse=True)
def email(mocker):
    """The spawned Claude's account email; never the real ~/.claude.json."""
    return mocker.patch("periscope.store.account_email", return_value="you@example.com")


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


# A PR start: `pr` in place of `branch`. Periscope checks the PR out as `pr-812`.
PR_BODY = {**{k: v for k, v in BODY.items() if k != "branch"}, "pr": 812}
PR_KEY = ("api", "pr-812")


@pytest.fixture
def no_pr_worktree(mocker):
    """No worktree of the repo is on `pr-812` yet."""
    return mocker.patch("periscope.worktrees.for_branch", return_value=None)


@pytest.fixture
def fetch(mocker):
    from periscope import projects

    return mocker.patch(
        "periscope.projects.fetch_pr_into_worktree",
        return_value=projects.PRWorktree(
            path="/repos/api-pr",
            base_branch="main",
            is_fork=True,
            local_branch="pr-812",
            pr_state="OPEN",
            name="sam/retry-uploads",
        ),
    )


@pytest.fixture
def prepare(client, monkeypatch, mocker, tmp_path):
    """POST /api/reviews/prepare on the same repo label and branch, with `wins` as the last blob."""
    from periscope import reviews

    monkeypatch.setattr(reviews, "REVIEWS_DIR", tmp_path)
    mocker.patch("periscope.open_ops.discover_repos", return_value=["/repos/api"])
    # Not refused, prepare would spawn into the branch's worktree.
    mocker.patch("periscope.worktrees.for_branch", return_value="/repos/api-wt")

    def post(wins):
        monkeypatch.setattr(state_hub, "_last_blob", json.dumps({"windows": wins}))
        return client.post("/api/reviews/prepare", json={"repo": "api", "branch": BODY["branch"]})

    return post


def test_start_spawns_claude_in_the_worktree(client, worktree, spawn, place, email):
    r = client.post("/api/linear/start", json=BODY)
    assert r.status_code == 200
    assert r.json() == {
        "mode": "spawned",
        "pid": "k",
        "path": "/repos/api-wt",
        "reused": False,
        "session_id": SID,
        "email": "you@example.com",
    }
    email.assert_called_once_with("b")
    worktree.assert_called_once_with("/repos/api", "me/eng-1-fix-it")
    args, kwargs = spawn.call_args
    assert args == ("/repos/api-wt", "/repos/api", "Read Linear ticket ENG-1.")
    assert kwargs == {"session": "api-wt", "name": None}
    assert store.get_window("k") == {"linked_linear": "ENG-1", "linked_linear_title": "Fix it"}
    place.assert_called_once_with("api-wt", {"repo": "/repos/api"}, ["k"])


def test_a_name_goes_to_the_spawn(client, worktree, spawn, place):
    # The spawn names the window and pins the name against the narrator.
    assert client.post("/api/linear/start", json={**BODY, "name": " eng-1-fix-it "}).status_code == 200
    assert spawn.call_args.kwargs == {"session": "api-wt", "name": "eng-1-fix-it"}


def test_a_claude_on_the_branch_is_reported_not_prompted(client, windows, worktree, spawn, place):
    windows.return_value = [_win()]
    branch_spawn._recent_spawn[KEY] = time.monotonic()
    r = client.post("/api/linear/start", json=BODY)
    assert r.status_code == 200 and r.json() == {"mode": "existing", "pid": "p1"}
    assert branch_spawn._recent_spawn == {}
    worktree.assert_not_called()
    spawn.assert_not_called()
    place.assert_not_called()


def test_a_claude_without_a_channel_on_the_branch_is_reported(client, windows, worktree, spawn):
    # Started by hand, or spawned a moment ago: a second Claude would edit the same worktree.
    windows.return_value = [_win(channel_attached=False)]
    assert client.post("/api/linear/start", json=BODY).json() == {"mode": "existing", "pid": "p1"}
    spawn.assert_not_called()


def test_a_shell_on_the_branch_does_not_count(client, windows, worktree, spawn, place):
    windows.return_value = [_win(agent=None)]
    assert client.post("/api/linear/start", json=BODY).json()["mode"] == "spawned"


def test_a_claude_in_another_repo_with_the_same_folder_name_does_not_count(client, windows, worktree, spawn, place):
    windows.return_value = [_win(repo_key="/oss/api")]
    assert client.post("/api/linear/start", json=BODY).json()["mode"] == "spawned"


def test_a_repo_path_with_a_trailing_slash_finds_the_claude(client, windows, worktree, spawn):
    windows.return_value = [_win()]
    r = client.post("/api/linear/start", json={**BODY, "repo": "/repos/api/"})
    assert r.json() == {"mode": "existing", "pid": "p1"}
    worktree.assert_not_called()


def test_a_start_before_the_spawned_claude_shows_is_refused(client, worktree, spawn, place):
    assert client.post("/api/linear/start", json=BODY).status_code == 200
    r = client.post("/api/linear/start", json=BODY)
    assert r.status_code == 409 and "attached" in r.json()["detail"] and spawn.call_count == 1
    branch_spawn._recent_spawn[KEY] = time.monotonic() - branch_spawn.RECENT_SPAWN_S
    assert client.post("/api/linear/start", json=BODY).status_code == 200 and spawn.call_count == 2


def test_a_prepare_right_after_a_start_on_the_branch_is_refused(client, worktree, spawn, place, prepare):
    assert client.post("/api/linear/start", json=BODY).json()["mode"] == "spawned"
    r = prepare([])
    assert r.status_code == 409 and "attached" in r.json()["detail"] and spawn.call_count == 1


def test_a_start_that_sees_its_claude_before_it_attaches_keeps_the_record(
    client, windows, worktree, spawn, place, prepare
):
    # Clearing it there would let a prepare, which needs an attached Claude, spawn a twin.
    assert client.post("/api/linear/start", json=BODY).json()["mode"] == "spawned"
    windows.return_value = [_win(pid="k", channel_attached=False)]
    assert client.post("/api/linear/start", json=BODY).json() == {"mode": "existing", "pid": "k"}
    r = prepare([_win(pid="k", channel_attached=False)])
    assert r.status_code == 409 and "attached" in r.json()["detail"] and spawn.call_count == 1


def test_concurrent_starts_spawn_once(mocker, worktree, place):
    import asyncio

    import httpx

    from periscope import app as app_mod

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
    assert branch_spawn._in_flight == set()


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


def test_a_retry_right_after_a_failed_spawn_spawns(client, worktree, spawn, place):
    # The failed spawn left no Claude: a record of it would answer every retry 409 for 60 s.
    spawn.side_effect = channels.SpawnFailed("no server")
    assert client.post("/api/linear/start", json=BODY).status_code == 502
    assert branch_spawn._recent_spawn == {} and branch_spawn._in_flight == set()
    spawn.side_effect = None
    r = client.post("/api/linear/start", json=BODY)
    assert r.status_code == 200 and r.json()["mode"] == "spawned" and spawn.call_count == 2


def test_a_rail_placement_failure_still_answers_spawned(client, worktree, spawn, place):
    place.side_effect = RuntimeError("store busy")
    r = client.post("/api/linear/start", json=BODY)
    assert r.status_code == 200 and r.json()["pid"] == "k"
    assert store.get_window("k") == {"linked_linear": "ENG-1", "linked_linear_title": "Fix it"}


def test_no_title_stamps_only_the_ticket(client, worktree, spawn, place):
    assert client.post("/api/linear/start", json={**BODY, "title": ""}).status_code == 200
    assert store.get_window("k") == {"linked_linear": "ENG-1"}


def test_a_pr_start_checks_the_pr_out_and_links_it(client, no_pr_worktree, fetch, worktree, spawn, place):
    r = client.post("/api/linear/start", json=PR_BODY)
    assert r.status_code == 200
    assert r.json() == {
        "mode": "spawned",
        "pid": "k",
        "path": "/repos/api-pr",
        "reused": False,
        "session_id": SID,
        "email": "you@example.com",
    }
    no_pr_worktree.assert_called_once_with("/repos/api", "pr-812")
    fetch.assert_called_once_with("/repos/api", 812)
    worktree.assert_not_called()
    assert spawn.call_args.args == ("/repos/api-pr", "/repos/api", "Read Linear ticket ENG-1.")
    assert store.get_window("k") == {
        "linked_linear": "ENG-1",
        "linked_linear_title": "Fix it",
        "linked_pr": 812,
        "is_fork": True,
    }


def test_a_branch_start_answers_the_worktree_warning(client, worktree, spawn, place):
    worktree.return_value = {**worktree.return_value, "warning": "fetch failed: origin/main may be stale ('offline')"}
    r = client.post("/api/linear/start", json=BODY)
    assert r.status_code == 200 and r.json()["warning"] == "fetch failed: origin/main may be stale ('offline')"


def test_a_pr_start_uses_the_worktree_already_on_the_pr(client, mocker, fetch, spawn, place):
    mocker.patch("periscope.worktrees.for_branch", return_value="/repos/api-pr")
    # The fetch works, and the worktree has the PR's head already.
    git = mocker.patch("periscope.routes.linear._run", return_value=(0, ""))
    r = client.post("/api/linear/start", json=PR_BODY)
    assert r.status_code == 200 and r.json()["path"] == "/repos/api-pr" and r.json()["reused"] is True
    assert "warning" not in r.json()
    assert git.call_args_list[0].args[0] == ["git", "-C", "/repos/api-pr", "fetch", "origin", "pull/812/head"]
    fetch.assert_not_called()
    # Only a fetch asks GitHub whether the PR comes from a fork.
    assert store.get_window("k") == {"linked_linear": "ENG-1", "linked_linear_title": "Fix it", "linked_pr": 812}


def test_a_claude_on_the_pr_branch_is_reported(client, windows, no_pr_worktree, fetch, spawn):
    windows.return_value = [_win(branch="pr-812")]
    assert client.post("/api/linear/start", json=PR_BODY).json() == {"mode": "existing", "pid": "p1"}
    fetch.assert_not_called()
    spawn.assert_not_called()


def test_a_second_pr_start_before_the_claude_shows_is_refused(client, no_pr_worktree, fetch, spawn, place):
    assert client.post("/api/linear/start", json=PR_BODY).status_code == 200
    assert PR_KEY in branch_spawn._recent_spawn
    r = client.post("/api/linear/start", json=PR_BODY)
    assert r.status_code == 409 and spawn.call_count == 1


@pytest.mark.parametrize("target", [{"branch": "me/eng-1-fix-it", "pr": 812}, {}, {"branch": ""}])
def test_a_start_takes_a_branch_or_a_pr(client, worktree, fetch, spawn, target):
    body = {k: v for k, v in BODY.items() if k != "branch"} | target
    r = client.post("/api/linear/start", json=body)
    assert r.status_code == 400 and r.json()["detail"] == "send exactly one of branch and pr"
    worktree.assert_not_called()
    fetch.assert_not_called()
    spawn.assert_not_called()


def test_a_pr_fetch_conflict_is_400_and_a_missing_pr_stays_404(client, no_pr_worktree, fetch, spawn):
    # This route's 409 means only that a start is running.
    from fastapi import HTTPException

    fetch.side_effect = HTTPException(409, "local branch 'pr-812' already in use — remove the existing worktree")
    r = client.post("/api/linear/start", json=PR_BODY)
    assert r.status_code == 400 and "already in use" in r.json()["detail"]
    fetch.side_effect = HTTPException(404, "PR #812 not found in /repos/api: no pull requests found")
    assert client.post("/api/linear/start", json=PR_BODY).status_code == 404
    spawn.assert_not_called()


@pytest.fixture
def pr_checkout(tmp_path, monkeypatch, fetch):
    """Real git: an origin with PR 812, a clone of it, and a worktree of the clone on `pr-812` at the PR's head.
    `push(text)` adds a commit to the PR on origin and returns its sha. `head()` is the worktree's HEAD."""
    from periscope import worktree_spawn

    # A start that misses the worktree puts its new one here, never under ~/dev/worktrees.
    monkeypatch.setattr(worktree_spawn, "WORKTREES_DIR", tmp_path / "worktrees")
    for var in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        monkeypatch.setenv(var, "t")
    for var in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(var, "t@t")

    def git(*args):
        return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout.strip()

    origin, author, repo, wt = (str(tmp_path / n) for n in ("origin.git", "author", "repo", "wt"))
    git("init", "-q", "--bare", origin)
    git("init", "-q", author)

    def push(text):
        Path(author, "f").write_text(text)
        git("-C", author, "add", "f")
        git("-C", author, "commit", "-qm", text)
        git("-C", author, "push", "-q", origin, "HEAD:refs/heads/main", "HEAD:refs/pull/812/head")
        return git("-C", author, "rev-parse", "HEAD")

    push("one\n")
    git("clone", "-q", origin, repo)
    git("-C", repo, "fetch", "-q", "origin", "pull/812/head:pr-812")
    git("-C", repo, "worktree", "add", "-q", wt, "pr-812")
    return SimpleNamespace(
        origin=origin,
        repo=os.path.realpath(repo),
        wt=os.path.realpath(wt),
        git=git,
        push=push,
        head=lambda: git("-C", wt, "rev-parse", "HEAD"),
    )


def test_a_reused_pr_worktree_moves_to_the_prs_head(client, pr_checkout, fetch, spawn, place):
    new = pr_checkout.push("two\n")
    r = client.post("/api/linear/start", json={**PR_BODY, "repo": pr_checkout.repo})
    assert r.status_code == 200 and r.json()["reused"] is True and "warning" not in r.json()
    assert pr_checkout.head() == new
    assert spawn.call_args.args[0] == pr_checkout.wt
    fetch.assert_not_called()


def test_local_changes_need_no_update_when_the_worktree_has_the_prs_head(client, pr_checkout, spawn, place):
    Path(pr_checkout.wt, "notes.md").write_text("mine\n")
    r = client.post("/api/linear/start", json={**PR_BODY, "repo": pr_checkout.repo})
    assert r.status_code == 200 and "warning" not in r.json()


def test_a_reused_pr_worktree_with_local_changes_stays_as_it_is(client, pr_checkout, spawn, place):
    old = pr_checkout.head()
    pr_checkout.push("two\n")
    Path(pr_checkout.wt, "f").write_text("mine\n")
    r = client.post("/api/linear/start", json={**PR_BODY, "repo": pr_checkout.repo})
    assert r.status_code == 200
    assert r.json()["warning"] == "pr-812 may be behind PR #812: the worktree has local changes"
    assert pr_checkout.head() == old and Path(pr_checkout.wt, "f").read_text() == "mine\n"


def test_a_reused_pr_worktree_that_diverged_stays_as_it_is(client, pr_checkout, spawn, place):
    pr_checkout.push("two\n")
    pr_checkout.git("-C", pr_checkout.wt, "commit", "-q", "--allow-empty", "-m", "local")
    local = pr_checkout.head()
    r = client.post("/api/linear/start", json={**PR_BODY, "repo": pr_checkout.repo})
    assert r.status_code == 200
    assert r.json()["warning"] == "pr-812 may be behind PR #812: it cannot fast-forward"
    assert pr_checkout.head() == local


def test_a_failed_fetch_leaves_the_reused_worktree_as_it_is(client, pr_checkout, spawn, place):
    old = pr_checkout.head()
    pr_checkout.git("-C", pr_checkout.origin, "update-ref", "-d", "refs/pull/812/head")
    r = client.post("/api/linear/start", json={**PR_BODY, "repo": pr_checkout.repo})
    assert r.status_code == 200
    assert r.json()["warning"].startswith("pr-812 may be behind PR #812: fetch failed (")
    assert "pull/812/head" in r.json()["warning"]
    assert pr_checkout.head() == old and r.json()["path"] == pr_checkout.wt
