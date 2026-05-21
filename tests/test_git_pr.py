"""Git + GitHub PR state, plus the activity-timeline cache."""

import subprocess

import pytest

from periscope.git_pr import (
    git_state_for, cached_git_state, _gh_run_state,
    _git_cache, _pr_cache, _GIT_TTL,
)


@pytest.fixture(autouse=True)
def _reset_caches():
    _git_cache.clear()
    _pr_cache.clear()
    yield
    _git_cache.clear()
    _pr_cache.clear()


def test_git_state_for_returns_none_for_non_git_path(tmp_path):
    """A path with no .git directory yields None."""
    assert git_state_for(str(tmp_path)) is None


def test_git_state_for_returns_none_for_missing_path(tmp_path):
    """A path that doesn't exist on disk also yields None (no _run call)."""
    assert git_state_for(str(tmp_path / "nope")) is None
    assert git_state_for("") is None


def test_git_state_for_returns_branch_for_mocked_git_repo(tmp_path, mocker):
    """When git rev-parse / diff / rev-list succeed, build the expected dict."""
    def fake_run(cmd, cwd=None, timeout=3.0):
        # cmd is a list like ["git", "-C", path, ...]
        if "rev-parse" in cmd and "--git-dir" in cmd:
            return (0, ".git")
        if "rev-parse" in cmd and "--abbrev-ref" in cmd:
            return (0, "main")
        if "diff" in cmd:
            return (0, "")  # no insertions / deletions
        if "rev-list" in cmd:
            return (0, "0")  # zero ahead
        if "rev-parse" in cmd and "--short" in cmd:
            return (0, "abcd1234")
        return (0, "")
    # git_pr imports _run as a module-level name; patch the live binding.
    mocker.patch("periscope.git_pr._run", side_effect=fake_run)
    out = git_state_for(str(tmp_path))
    assert out is not None
    assert out["branch"] == "main"
    assert out["git"] == "clean"


def test_cached_git_state_uses_ttl(mocker):
    """First call hits git_state_for; second call within TTL serves the cache."""
    mock_inner = mocker.patch(
        "periscope.git_pr.git_state_for",
        return_value={"branch": "main", "git": "clean"},
    )
    cached_git_state("/foo")
    cached_git_state("/foo")
    assert mock_inner.call_count == 1


def test_cached_git_state_returns_none_for_empty_path():
    assert cached_git_state("") is None


def test_cached_git_state_refetches_after_ttl(mocker):
    """Two calls separated by more than _GIT_TTL hit git_state_for twice."""
    mock_inner = mocker.patch(
        "periscope.git_pr.git_state_for",
        return_value={"branch": "main", "git": "clean"},
    )
    times = [1000.0, 1000.0 + _GIT_TTL + 1.0]
    mocker.patch("periscope.git_pr.time.time", side_effect=times)
    cached_git_state("/foo")
    cached_git_state("/foo")
    assert mock_inner.call_count == 2


def test_gh_run_state_maps_success_to_passed():
    assert _gh_run_state({"status": "completed", "conclusion": "success"}) == "passed"


def test_gh_run_state_maps_failure_to_failed():
    assert _gh_run_state({"status": "completed", "conclusion": "failure"}) == "failed"


def test_gh_run_state_maps_in_progress_to_running():
    assert _gh_run_state({"status": "in_progress", "conclusion": None}) == "running"


def test_gh_run_state_returns_none_for_neutral():
    assert _gh_run_state({"status": "completed", "conclusion": "neutral"}) is None
    assert _gh_run_state({"status": "completed", "conclusion": "skipped"}) is None


def test_gh_run_state_returns_none_for_unknown():
    assert _gh_run_state({}) is None


from periscope.git_pr import github_origin


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True)


def test_github_origin_ssh_form(tmp_path):
    _git(tmp_path, "init")
    _git(tmp_path, "remote", "add", "origin",
         "git@github.com:faradayio/periscope.git")
    assert github_origin(str(tmp_path)) == "faradayio/periscope"


def test_github_origin_https_form(tmp_path):
    _git(tmp_path, "init")
    _git(tmp_path, "remote", "add", "origin",
         "https://github.com/faradayio/periscope.git")
    assert github_origin(str(tmp_path)) == "faradayio/periscope"


def test_github_origin_none_for_non_github(tmp_path):
    _git(tmp_path, "init")
    _git(tmp_path, "remote", "add", "origin",
         "https://gitlab.com/x/y.git")
    assert github_origin(str(tmp_path)) is None


def test_github_origin_none_when_no_remote(tmp_path):
    _git(tmp_path, "init")
    assert github_origin(str(tmp_path)) is None
