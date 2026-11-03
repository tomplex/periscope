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


@pytest.fixture
def clock(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(gh, "time", SimpleNamespace(monotonic=lambda: now[0]))
    return now


def _fake(monkeypatch, *, token="tok-1", answers=None):
    calls = []
    answers = list(answers or [])

    def run(cmd, cwd=None, timeout=3.0, env=None):
        calls.append((cmd, env))
        if cmd[1:3] == ["auth", "token"]:
            return (0, token) if token else (1, "You are not logged into any GitHub hosts.")
        return answers.pop(0) if answers else (0, "[]")

    monkeypatch.setattr(gh, "_run", run)
    return calls


def _token_reads(calls):
    return sum(1 for cmd, _ in calls if cmd == ["gh", "auth", "token"])


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
    calls = _fake(monkeypatch, answers=[(1, answer), (1, answer), (1, answer)])
    gh.run("pr", "list")  # read 1, then 401
    clock[0] += 30
    gh.run("pr", "list")  # inside the minute: the same token, still 401
    assert _token_reads(calls) == 1
    clock[0] += 31
    gh.run("pr", "list")
    assert _token_reads(calls) == 2


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
