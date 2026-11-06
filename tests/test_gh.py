"""gh — every `gh` periscope runs carries a token read every 10 minutes, not a
keychain read per call, and runs with telemetry and the update check off."""

import os
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from periscope import gh


@pytest.fixture(autouse=True)
def _no_gh_token(monkeypatch):
    """Overrides conftest's seeded "no token" entry: these tests read it."""
    monkeypatch.setattr(gh, "_token_cache", None)
    monkeypatch.setattr(gh, "_tokenless_cwds", set())


@pytest.fixture
def clock(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(gh, "time", SimpleNamespace(monotonic=lambda: now[0]))
    return now


def _fake(monkeypatch, *, token="tok-1", answers=None):
    calls = []
    answers = list(answers or [])

    def run(cmd, cwd=None, timeout=3.0, env_overrides=None):
        calls.append((cmd, {**os.environ, **(env_overrides or {})}))
        if cmd[1:3] == ["auth", "token"]:
            return (0, token) if token else (1, "You are not logged into any GitHub hosts.")
        return answers.pop(0) if answers else (0, "[]")

    monkeypatch.setattr(gh, "_run", run)
    return calls


def _token_reads(calls):
    return sum(1 for cmd, _ in calls if cmd == ["gh", "auth", "token"])


def test_run_forks_nothing_when_gh_is_not_available(monkeypatch):
    calls = _fake(monkeypatch)
    monkeypatch.setattr(gh, "AVAILABLE", False)
    assert gh.run("pr", "list") == (127, "gh not installed")
    assert calls == []


def test_a_call_carries_the_token_and_runs_quiet(monkeypatch):
    calls = _fake(monkeypatch)
    assert gh.run("pr", "list", "--limit", "1", cwd="/r", timeout=8.0) == (0, "[]")
    cmd, env = calls[-1]
    assert cmd == ["gh", "pr", "list", "--limit", "1"]
    assert env["GH_TOKEN"] == "tok-1"
    assert env["GH_TELEMETRY"] == "false"
    assert env["GH_NO_UPDATE_NOTIFIER"] == "1"
    assert env["PATH"] == os.environ["PATH"]  # the rest of the environment rides along


def test_the_token_is_read_every_ten_minutes(monkeypatch, clock):
    calls = _fake(monkeypatch)
    gh.run("pr", "list")
    clock[0] += 599
    gh.run("pr", "view", "1")
    assert _token_reads(calls) == 1
    clock[0] += 2
    gh.run("pr", "list")
    assert _token_reads(calls) == 2


def test_a_failed_token_read_is_retried_after_a_minute(monkeypatch, clock):
    monkeypatch.delenv("GH_TOKEN", raising=False)
    calls = _fake(monkeypatch, token=None)
    assert gh.run("pr", "list") == (0, "[]")
    assert "GH_TOKEN" not in calls[-1][1]  # no token: the call still runs
    clock[0] += 59
    gh.run("pr", "list")
    assert _token_reads(calls) == 1
    clock[0] += 2
    gh.run("pr", "list")
    assert _token_reads(calls) == 2


@pytest.mark.parametrize(
    "answer",
    [
        "HTTP 401: Bad credentials (https://api.github.com/graphql)",
        "failed to get runs: HTTP 401: Bad credentials (https://api.github.com/repos/tom/x/actions/runs)",
        'non-200 OK status code: 401 Unauthorized body: "{\\"message\\":\\"Bad credentials\\"}"',
    ],
)
def test_a_401_rereads_the_token_at_most_once_a_minute(monkeypatch, clock, answer):
    # Two _run calls per gh.run() now (the call, then its tokenless retry);
    # both 401 here so the cwd (None) is never remembered and every call
    # still hits the token path first.
    calls = _fake(monkeypatch, answers=[(1, answer)] * 6)
    gh.run("pr", "list")  # read 1, then 401
    clock[0] += 30
    gh.run("pr", "list")  # inside the minute: the same token, still 401
    assert _token_reads(calls) == 1
    clock[0] += 31
    gh.run("pr", "list")
    assert _token_reads(calls) == 2


def test_a_401_then_tokenless_retry_success_is_remembered_for_that_cwd(monkeypatch, clock):
    """A *.ghe.com repo whose token is for github.com (or vice versa): the
    token 401s, the tokenless retry succeeds on gh's own credentials, and
    that result — not the 401 — is what gh.run returns."""
    calls = _fake(
        monkeypatch,
        answers=[(1, "HTTP 401: Bad credentials"), (0, "ok-1"), (0, "ok-2")],
    )
    assert gh.run("pr", "list", cwd="/ghe/repo") == (0, "ok-1")
    pr_calls = [env for cmd, env in calls if cmd[:2] == ["gh", "pr"]]
    assert len(pr_calls) == 2
    assert "GH_TOKEN" in pr_calls[0]
    assert "GH_TOKEN" not in pr_calls[1]

    # The next call from the same cwd skips the token outright: one _run
    # call, no 401 first.
    assert gh.run("pr", "list", cwd="/ghe/repo") == (0, "ok-2")
    pr_calls = [env for cmd, env in calls if cmd[:2] == ["gh", "pr"]]
    assert len(pr_calls) == 3
    assert "GH_TOKEN" not in pr_calls[-1]


def test_a_remembered_cwd_carries_the_token_again_once_it_is_reread(monkeypatch, clock):
    """A revoked-then-replaced token, or a stray 401: the old token's
    tokenless fallback must not stick to the cwd forever."""
    calls = _fake(
        monkeypatch,
        token="tok-1",
        answers=[(1, "HTTP 401: Bad credentials"), (0, "ok-1"), (0, "ok-2")],
    )
    assert gh.run("pr", "list", cwd="/ghe/repo") == (0, "ok-1")

    clock[0] += gh._TOKEN_TTL_S + 1
    assert gh.run("pr", "list", cwd="/ghe/repo") == (0, "ok-2")
    pr_calls = [env for cmd, env in calls if cmd[:2] == ["gh", "pr"]]
    assert len(pr_calls) == 3  # 2 from the first gh.run (401, then tokenless retry) + 1 here
    assert pr_calls[-1]["GH_TOKEN"] == "tok-1"
    assert _token_reads(calls) == 2  # the initial read, then the TTL re-read


def test_a_401_whose_tokenless_retry_also_fails_returns_the_401_result(monkeypatch, clock):
    answer = "HTTP 401: Bad credentials"
    calls = _fake(monkeypatch, answers=[(1, answer)] * 4)
    assert gh.run("pr", "list", cwd="/r") == (1, answer)
    # Not remembered: the next call from the same cwd carries the token
    # again and 401s first, same as before.
    assert gh.run("pr", "list", cwd="/r") == (1, answer)
    pr_calls = [env for cmd, env in calls if cmd[:2] == ["gh", "pr"]]
    assert len(pr_calls) == 4
    assert all("GH_TOKEN" in env for env in pr_calls[::2])  # the first try of each call


def test_a_remembered_cwd_doesnt_affect_other_cwds(monkeypatch, clock):
    calls = _fake(monkeypatch, answers=[(1, "HTTP 401: Bad credentials"), (0, "ok")])
    gh.run("pr", "list", cwd="/ghe/repo")
    calls.clear()
    assert gh.run("pr", "list", cwd="/other/repo") == (0, "[]")
    cmd, env = calls[-1]
    assert "GH_TOKEN" in env


@pytest.mark.parametrize(
    "answer",
    [
        'no pull requests found for branch "x"',
        "GraphQL: Could not resolve to a PullRequest with the number of 401. (repository.pullRequest)",
    ],
)
def test_other_failures_keep_the_token(monkeypatch, clock, answer):
    calls = _fake(monkeypatch, answers=[(1, answer)])
    gh.run("pr", "view", "9")
    clock[0] += 61
    gh.run("pr", "list")
    assert _token_reads(calls) == 1


def test_every_gh_call_goes_through_gh_run():
    pkg = Path(__file__).resolve().parents[1] / "periscope"
    offenders = [p.name for p in pkg.rglob("*.py") if p.name != "gh.py" and re.search(r'\[\s*"gh"\s*,', p.read_text())]
    assert offenders == []
