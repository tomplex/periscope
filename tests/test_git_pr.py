"""Git + GitHub PR state, plus the activity-timeline cache."""

import subprocess

import pytest

from periscope.git_pr import (
    _GIT_TTL,
    _gh_run_state,
    _git_cache,
    _pr_cache,
    cached_git_state,
    git_state_for,
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


def _mock_git(mocker, *, diff="", untracked="", ahead="0"):
    """Patch _run so git_state_for sees a controlled repo shape."""
    def fake_run(cmd, cwd=None, timeout=3.0):
        if "rev-parse" in cmd and "--git-dir" in cmd:
            return (0, ".git")
        if "rev-parse" in cmd and "--abbrev-ref" in cmd:
            return (0, "main")
        if "ls-files" in cmd:
            return (0, untracked)
        if "diff" in cmd:
            return (0, diff)
        if "rev-list" in cmd:
            return (0, ahead)
        return (0, "")
    mocker.patch("periscope.git_pr._run", side_effect=fake_run)


def test_git_state_counts_untracked_files(tmp_path, mocker):
    """`git diff HEAD` ignores untracked files, so a worktree whose only change
    was brand-new files used to report "clean" — and isDirty() then suppressed
    the chip entirely, hiding the work."""
    _mock_git(mocker, untracked="brand_new.py\n")
    assert git_state_for(str(tmp_path))["git"] == "?1"


def test_git_state_combines_tracked_and_untracked(tmp_path, mocker):
    _mock_git(
        mocker,
        diff=" 1 file changed, 12 insertions(+), 3 deletions(-)",
        untracked="a.py\nnewdir/\n",
    )
    assert git_state_for(str(tmp_path))["git"] == "+12 -3 ?2"


def test_git_state_untracked_with_unpushed_commits(tmp_path, mocker):
    """The `*` suffix means unpushed commits and composes with the ? count."""
    _mock_git(mocker, untracked="a.py\n", ahead="2")
    assert git_state_for(str(tmp_path))["git"] == "?1 *"


def test_git_state_clean_stays_clean(tmp_path, mocker):
    """No tracked diff, no untracked, no unpushed -> the literal isDirty() checks."""
    _mock_git(mocker)
    assert git_state_for(str(tmp_path))["git"] == "clean"
    _mock_git(mocker, ahead="3")
    assert git_state_for(str(tmp_path))["git"] == "clean *"


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


from periscope.gitutil import github_slug


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True)


def test_github_slug_ssh_form(tmp_path):
    _git(tmp_path, "init")
    _git(tmp_path, "remote", "add", "origin",
         "git@github.com:faradayio/periscope.git")
    assert github_slug(str(tmp_path)) == "faradayio/periscope"


def test_github_slug_https_form(tmp_path):
    _git(tmp_path, "init")
    _git(tmp_path, "remote", "add", "origin",
         "https://github.com/faradayio/periscope.git")
    assert github_slug(str(tmp_path)) == "faradayio/periscope"


def test_github_slug_none_for_non_github(tmp_path):
    _git(tmp_path, "init")
    _git(tmp_path, "remote", "add", "origin",
         "https://gitlab.com/x/y.git")
    assert github_slug(str(tmp_path)) is None


def test_github_slug_none_when_no_remote(tmp_path):
    _git(tmp_path, "init")
    assert github_slug(str(tmp_path)) is None


def test_git_state_includes_repo_key_and_label(tmp_path, mocker):
    """git_state_for() returns repo_key (full path) + repo_label (basename)."""
    import periscope.git_pr as gp
    # git_state_for short-circuits when the path is not a directory, so
    # build a real worktree-like directory tree under tmp_path.
    repo_dir = tmp_path / "foo"
    worktree_dir = repo_dir / "branch-a"
    worktree_dir.mkdir(parents=True)
    common_dir = repo_dir / ".git"
    common_dir.mkdir()

    def fake_run(args, cwd=None, timeout=None):
        # git_state_for first probes --git-dir; return ".git" to satisfy it.
        if "rev-parse" in args and "--git-dir" in args:
            return (0, ".git")
        # diff shortstat (unstaged + staged) → clean
        if "diff" in args and "--shortstat" in args:
            return (0, "")
        if "rev-parse" in args and "--abbrev-ref" in args:
            return (0, "main")
        if "rev-list" in args:
            return (0, "0")
        if "remote" in args and "get-url" in args:
            return (1, "")  # no github slug
        if "rev-parse" in args and "--git-common-dir" in args:
            return (0, str(common_dir))
        return (0, "")

    # Both modules import _run from periscope.tmux at module load — patch both.
    mocker.patch("periscope.git_pr._run", side_effect=fake_run)
    mocker.patch("periscope.gitutil._run", side_effect=fake_run)
    out = gp.git_state_for(str(worktree_dir))
    assert out["repo_key"] == str(repo_dir)
    assert out["repo_label"] == "foo"
