"""Git + GitHub PR state, plus the activity-timeline cache."""

import os
import re
import subprocess
import time
from types import SimpleNamespace

import pytest

from periscope import git_pr, gitutil
from periscope.git_pr import (
    _ci_checks,
    _ci_glyph,
    _gh_run_state,
    _linked_pr_cache,
    _pr_cache,
    _signal_cache,
    git_signal_for,
    git_state_for,
    linear_ticket_fields,
    linear_ticket_for,
    linked_pr_state_for,
    pr_state_for,
)


@pytest.fixture(autouse=True)
def _reset_caches():
    _pr_cache.clear()
    _linked_pr_cache.clear()
    _signal_cache.clear()
    yield
    _pr_cache.clear()
    _linked_pr_cache.clear()
    _signal_cache.clear()


def test_git_state_for_returns_none_for_non_git_path(tmp_path):
    """A path with no .git directory yields None."""
    assert git_state_for(str(tmp_path)) is None


def test_git_state_for_returns_none_for_missing_path(tmp_path):
    """A path that doesn't exist on disk also yields None (no _run call)."""
    assert git_state_for(str(tmp_path / "nope")) is None
    assert git_state_for("") is None


_HEAD = "# branch.oid 0123456789abcdef0123456789abcdef01234567\n# branch.head main\n"


def _mock_git(mocker, *, status=_HEAD, diff="", code=0):
    """Patch _run so git_state_for sees one `status --porcelain=v2 --branch`
    answer and one `diff-index --shortstat` answer; repo identity falls back."""

    def fake_run(cmd, cwd=None, timeout=3.0, env=None):
        if "status" in cmd:
            return (code, status)
        if "diff-index" in cmd:
            return (0, diff)
        return (1, "")  # rev-parse --git-common-dir / remote get-url

    mocker.patch("periscope.git_pr._run", side_effect=fake_run)
    mocker.patch("periscope.gitutil._run", side_effect=fake_run)


def test_git_state_counts_untracked_files(tmp_path, mocker):
    """`git diff HEAD` ignores untracked files, so a worktree whose only change
    was brand-new files used to report "clean" — and isDirty() then suppressed
    the chip entirely, hiding the work."""
    _mock_git(mocker, status=_HEAD + "? brand_new.py")
    assert git_state_for(str(tmp_path))["git"] == "?1"


def test_git_state_combines_tracked_and_untracked(tmp_path, mocker):
    _mock_git(
        mocker,
        status=_HEAD + "1 .M N... 100644 100644 100644 aaa bbb a.py\n? new.py\n? newdir/",
        diff=" 1 file changed, 12 insertions(+), 3 deletions(-)",
    )
    assert git_state_for(str(tmp_path))["git"] == "+12 -3 ?2"


def test_git_state_untracked_with_unpushed_commits(tmp_path, mocker):
    """The `*` suffix means unpushed commits and composes with the ? count."""
    _mock_git(mocker, status=_HEAD + "# branch.upstream origin/main\n# branch.ab +2 -0\n? a.py")
    assert git_state_for(str(tmp_path))["git"] == "?1 *"


def test_git_state_clean_stays_clean(tmp_path, mocker):
    """No tracked diff, no untracked, no unpushed -> the literal isDirty() checks."""
    _mock_git(mocker)
    assert git_state_for(str(tmp_path))["git"] == "clean"
    _mock_git(mocker, status=_HEAD + "# branch.upstream origin/main\n# branch.ab +3 -1")
    assert git_state_for(str(tmp_path))["git"] == "clean *"


def test_git_state_detached_head_is_the_short_sha(tmp_path, mocker):
    _mock_git(mocker, status="# branch.oid 0123456789abcdef0123456789abcdef01234567\n# branch.head (detached)")
    assert git_state_for(str(tmp_path))["branch"] == "@0123456"


def test_git_state_none_when_status_fails(tmp_path, mocker):
    _mock_git(mocker, status="fatal: not a git repository (or any of the parent directories): .git", code=128)
    assert git_state_for(str(tmp_path)) is None


def test_git_reads_never_write_the_index(tmp_path, git_identity):
    # A dashboard refresh must not touch .git/index from under the user's own
    # git commands. `git diff HEAD` rewrites it when files are stat-dirty
    # (new mtime, same content) even with --no-optional-locks, which is why
    # line counts come from diff-index instead.
    repo = _committed(tmp_path)
    (repo / "b.txt").write_text("b\n")
    (repo / "c.txt").write_text("c\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "more files")
    now = time.time()
    for name in ("a.txt", "b.txt", "c.txt"):
        os.utime(repo / name, (now, now + 86400))
    (repo / "a.txt").write_text("one\nTWO\n")  # a real content change too
    index = repo / ".git" / "index"
    before = (index.stat().st_mtime_ns, index.read_bytes())
    assert git_state_for(str(repo))["git"] == "+1 -1"
    after = (index.stat().st_mtime_ns, index.read_bytes())
    assert after == before


def test_a_staged_rename_and_edit_counts_as_one_line_changed(tmp_path, git_identity):
    # Without -M, diff-index would count a rename as a full delete+add
    # (101 insertions/100 deletions here) instead of the +1/-1 the edit is.
    repo = _committed(tmp_path)
    _git(repo, "mv", "a.txt", "a2.txt")
    (repo / "a2.txt").write_text("one\nTWO\n")
    _git(repo, "add", "a2.txt")
    assert git_state_for(str(repo))["git"] == "+1 -1"


def test_git_state_passes_its_timeout_to_git(tmp_path, mocker):
    _mock_git(mocker)
    git_state_for(str(tmp_path), timeout=10.0)
    assert {c.kwargs["timeout"] for c in git_pr._run.call_args_list} == {10.0}


# --- the two-command read against real repos -------------------------------


@pytest.fixture
def git_identity(monkeypatch):
    """Commit in fixture repos without the developer's git config or signing."""
    for k, v in {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
    }.items():
        monkeypatch.setenv(k, v)


def _seven_command_state(path):
    """The pre-THI-450 seven-command read of branch + git, kept as the oracle."""

    def run(*args):
        r = subprocess.run(["git", "-C", path, *args], capture_output=True, text=True)
        return r.returncode, r.stdout.strip()

    if run("rev-parse", "--git-dir")[0] != 0:
        return None
    _, branch = run("rev-parse", "--abbrev-ref", "HEAD")
    if not branch or branch == "HEAD":
        _, sha = run("rev-parse", "--short", "HEAD")
        branch = f"@{sha}" if sha else "?"
    _, diff = run("diff", "HEAD", "--shortstat")
    adds_m = re.search(r"(\d+) insertion", diff)
    dels_m = re.search(r"(\d+) deletion", diff)
    adds = int(adds_m.group(1)) if adds_m else 0
    dels = int(dels_m.group(1)) if dels_m else 0
    _, untracked = run("ls-files", "-o", "--exclude-standard", "--directory", "--no-empty-directory")
    new = sum(1 for line in untracked.splitlines() if line.strip())
    code, ahead_s = run("rev-list", "--count", "@{u}..HEAD")
    ahead = int(ahead_s) if code == 0 and ahead_s.isdigit() else 0
    bits = []
    if adds or dels:
        bits.append(f"+{adds} -{dels}")
    if new:
        bits.append(f"?{new}")
    state = " ".join(bits) if bits else "clean"
    if ahead > 0:
        state += " *"
    return {"branch": branch, "git": state}


def _committed(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "a.txt").write_text("one\ntwo\n")
    (repo / ".gitignore").write_text("*.log\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "init")
    return repo


def _clean(tmp_path):
    return _committed(tmp_path)


def _dirty(tmp_path):
    repo = _committed(tmp_path)
    (repo / "a.txt").write_text("one\nTWO\nthree\n")
    (repo / "b.txt").write_text("staged\n")
    _git(repo, "add", "b.txt")
    return repo


def _untracked_only(tmp_path):
    # A file, a wholly-untracked tree, a dir holding only ignored files, a dir
    # mixing ignored and untracked, and an empty dir.
    repo = _committed(tmp_path)
    (repo / "new.py").write_text("x\n")
    (repo / "newdir" / "deep").mkdir(parents=True)
    (repo / "newdir" / "deep" / "f.txt").write_text("x\n")
    (repo / "logs").mkdir()
    (repo / "logs" / "run.log").write_text("x\n")
    (repo / "mixed").mkdir()
    (repo / "mixed" / "keep.txt").write_text("x\n")
    (repo / "mixed" / "skip.log").write_text("x\n")
    (repo / "empty").mkdir()
    return repo


def _detached(tmp_path):
    repo = _committed(tmp_path)
    _git(repo, "checkout", "-q", "--detach")
    return repo


def _ahead(tmp_path):
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "-q", "--bare", str(remote))
    repo = _committed(tmp_path)
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "-q", "-u", "origin", "main")
    for n in (1, 2):
        (repo / "a.txt").write_text(f"v{n}\n")
        _git(repo, "commit", "-qam", f"c{n}")
    return repo


def _no_upstream(tmp_path):
    repo = _committed(tmp_path)
    _git(repo, "checkout", "-q", "-b", "feature")
    (repo / "a.txt").write_text("feature\n")
    _git(repo, "commit", "-qam", "f")
    return repo


def _non_repo(tmp_path):
    d = tmp_path / "plain"
    d.mkdir()
    return d


@pytest.mark.parametrize(
    ("build", "want"),
    [
        (_clean, ("main", "clean")),
        (_dirty, ("main", "+3 -1")),
        (_untracked_only, ("main", "?3")),
        (_detached, (None, "clean")),  # branch is "@<sha7>", checked against the oracle
        (_ahead, ("main", "clean *")),
        (_no_upstream, ("feature", "clean")),
        (_non_repo, None),
    ],
)
def test_two_command_read_matches_the_seven_command_read(build, want, tmp_path, git_identity):
    path = str(build(tmp_path))
    new = git_state_for(path)
    got = None if new is None else {"branch": new["branch"], "git": new["git"]}
    assert got == _seven_command_state(path)
    if want is None:
        assert got is None
    else:
        assert got["git"] == want[1]
        assert got["branch"] == want[0] or (want[0] is None and got["branch"].startswith("@"))


def test_a_subdirectory_counts_the_whole_repos_untracked_entries(tmp_path, git_identity):
    # The one place the two reads differ on purpose: ls-files -o was scoped to
    # the cwd, status is whole-repo — as diff HEAD's line counts already were.
    repo = _committed(tmp_path)
    (repo / "pkg" / "deep").mkdir(parents=True)
    (repo / "pkg" / "mod.py").write_text("x\n")
    (repo / "pkg" / "deep" / "tracked.py").write_text("x\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "pkg")
    (repo / "pkg" / "new.py").write_text("x\n")
    (repo / "pkg" / "scratch").mkdir()
    (repo / "pkg" / "scratch" / "x.txt").write_text("x\n")
    (repo / "root-new.txt").write_text("x\n")
    for sub, old in (("pkg", "?2"), ("pkg/deep", "clean")):
        path = str(repo / sub)
        assert _seven_command_state(path)["git"] == old
        assert git_state_for(path)["git"] == "?3"


def test_unborn_branch_reads_its_name(tmp_path, git_identity):
    # `rev-parse --abbrev-ref HEAD` fails before the first commit, and _run
    # handed its stderr back as the branch; porcelain v2 names the branch.
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "trunk")
    (repo / "new.py").write_text("x\n")
    assert git_state_for(str(repo)) == {
        "branch": "trunk",
        "git": "?1",
        "repo_slug": None,
        "repo_key": os.path.realpath(repo),
        "repo_label": "repo",
    }


def test_a_bare_container_reads_its_branch_and_repo(tmp_path, git_identity):
    # The layout whose .git file says `gitdir: ./.bare` has no work tree at the
    # container; it still has a branch and a repo, and its pane stays in its track.
    src = _committed(tmp_path)
    box = tmp_path / "box"
    box.mkdir()
    _git(tmp_path, "clone", "-q", "--bare", str(src), str(box / ".bare"))
    _git(box / ".bare", "remote", "set-url", "origin", "git@github.com:tom/box.git")
    (box / ".git").write_text("gitdir: ./.bare\n")
    assert git_state_for(str(box)) == {
        "branch": "main",
        "git": "clean",
        "repo_slug": "tom/box",
        "repo_key": os.path.realpath(box),
        "repo_label": "box",
    }


def test_a_bare_container_reads_its_branch_under_a_translated_locale(tmp_path, monkeypatch, git_identity):
    # The no-work-tree fatal is matched against its English text; under a
    # translated locale git prints it translated, so git_state_for must force
    # LC_ALL=C rather than trust the caller's environment.
    monkeypatch.setenv("LC_ALL", "fr_FR.UTF-8")
    src = _committed(tmp_path)
    box = tmp_path / "box"
    box.mkdir()
    _git(tmp_path, "clone", "-q", "--bare", str(src), str(box / ".bare"))
    _git(box / ".bare", "remote", "set-url", "origin", "git@github.com:tom/box.git")
    (box / ".git").write_text("gitdir: ./.bare\n")
    state = git_state_for(str(box))
    assert state is not None
    assert state["branch"] == "main"
    assert state["repo_slug"] == "tom/box"


def test_a_git_dir_without_a_work_tree_reads_its_branch(tmp_path, git_identity):
    # A cwd inside .git, and a plain bare repo: no work tree, still a branch.
    repo = _committed(tmp_path)
    inside = git_state_for(str(repo / ".git"))
    assert (inside["branch"], inside["git"]) == ("main", "clean")
    bare = tmp_path / "plain.git"
    _git(tmp_path, "clone", "-q", "--bare", str(repo), str(bare))
    plain = git_state_for(str(bare))
    assert (plain["branch"], plain["git"]) == ("main", "clean")


def test_git_state_forks_two_commands_once_identity_is_cached(tmp_path, monkeypatch, git_identity):
    repo = str(_committed(tmp_path))
    clock = [1000.0]
    monkeypatch.setattr(git_pr, "time", SimpleNamespace(time=lambda: clock[0]))
    real = git_pr._run
    calls = []

    def rec(cmd, **kw):
        calls.append(cmd)
        return real(cmd, **kw)

    monkeypatch.setattr(git_pr, "_run", rec)
    monkeypatch.setattr(gitutil, "_run", rec)

    def subcommands():
        out = [c[c.index("-C") + 2] for c in calls]
        calls.clear()
        return out

    git_state_for(repo)
    assert subcommands() == ["status", "diff-index", "rev-parse", "remote"]
    git_state_for(repo)
    assert subcommands() == ["status", "diff-index"]
    clock[0] += 601
    git_state_for(repo)
    assert subcommands() == ["status", "diff-index", "rev-parse", "remote"]


@pytest.fixture
def swr(monkeypatch, mocker):
    """A fixed clock, no jitter, a fake git read, and no real refresh thread
    (docs/testing.md): _bg is recorded, and a test runs the refresh itself."""
    clock = [1000.0]
    monkeypatch.setattr(git_pr, "time", SimpleNamespace(time=lambda: clock[0]))
    monkeypatch.setattr(git_pr, "random", SimpleNamespace(uniform=lambda a, b: 0.0))
    inner = mocker.patch("periscope.git_pr.git_state_for", return_value={"branch": "main", "git": "clean"})
    bg = mocker.patch("periscope.git_pr._bg")
    return SimpleNamespace(clock=clock, inner=inner, bg=bg)


def test_first_sight_reads_synchronously(swr):
    # The pid resolver matches panes on (branch, cwd) after a tmux restart, so
    # a cwd's first read can't come back without its branch.
    assert git_pr.cached_git_state("/foo") == {"branch": "main", "git": "clean"}
    swr.inner.assert_called_once_with("/foo")
    swr.bg.assert_not_called()


def test_fresh_entry_is_served_without_git(swr):
    git_pr.cached_git_state("/foo")
    swr.clock[0] += 14
    git_pr.cached_git_state("/foo")
    assert swr.inner.call_count == 1
    swr.bg.assert_not_called()


def test_stale_entry_is_served_and_refreshed_once_in_the_background(swr):
    git_pr.cached_git_state("/foo")
    swr.clock[0] += 16
    swr.inner.return_value = {"branch": "feat", "git": "+1 -0"}
    assert git_pr.cached_git_state("/foo") == {"branch": "main", "git": "clean"}
    assert git_pr.cached_git_state("/foo") == {"branch": "main", "git": "clean"}
    swr.bg.assert_called_once_with("git-state", git_pr._refresh_git_state, "/foo")
    assert swr.inner.call_count == 1  # nothing ran on the request path


def test_background_refresh_lands_with_the_longer_timeout(swr):
    git_pr.cached_git_state("/foo")
    swr.clock[0] += 16
    git_pr.cached_git_state("/foo")
    swr.inner.return_value = {"branch": "feat", "git": "+1 -0"}
    git_pr._refresh_git_state("/foo")
    swr.inner.assert_called_with("/foo", timeout=git_pr._GIT_BG_TIMEOUT_S)
    assert "/foo" not in git_pr._git_refreshing
    assert git_pr.cached_git_state("/foo") == {"branch": "feat", "git": "+1 -0"}
    swr.bg.assert_called_once()  # the landed entry is fresh again


def test_a_failed_background_read_keeps_the_last_state(swr, tmp_path):
    # A status that timed out says nothing new: keep what we had, retry at the
    # next expiry.
    path = str(tmp_path)
    git_pr.cached_git_state(path)
    swr.clock[0] += 16
    git_pr.cached_git_state(path)
    swr.inner.return_value = None
    git_pr._refresh_git_state(path)
    assert git_pr._git_cache[path] == (swr.clock[0] + git_pr._GIT_REFRESH_S, {"branch": "main", "git": "clean"})


def test_a_vanished_cwd_clears(swr):
    git_pr.cached_git_state("/no/such/dir")
    swr.clock[0] += 16
    git_pr.cached_git_state("/no/such/dir")
    swr.inner.return_value = None
    git_pr._refresh_git_state("/no/such/dir")
    assert git_pr.cached_git_state("/no/such/dir") is None


def test_a_refresh_that_raises_releases_the_path(swr):
    git_pr.cached_git_state("/foo")
    swr.clock[0] += 16
    git_pr.cached_git_state("/foo")
    swr.inner.side_effect = OSError("git vanished")
    with pytest.raises(OSError):
        git_pr._refresh_git_state("/foo")
    assert "/foo" not in git_pr._git_refreshing


def test_a_refresh_that_cant_start_releases_the_path(swr):
    git_pr.cached_git_state("/foo")
    swr.clock[0] += 16
    swr.bg.side_effect = RuntimeError("can't start new thread")
    assert git_pr.cached_git_state("/foo") == {"branch": "main", "git": "clean"}
    assert "/foo" not in git_pr._git_refreshing
    git_pr.cached_git_state("/foo")
    assert swr.bg.call_count == 2  # the next read tries again


def test_empty_path_is_none(swr):
    assert git_pr.cached_git_state("") is None
    swr.inner.assert_not_called()


def test_expiry_is_jittered(monkeypatch, mocker):
    # cwds first seen in one refresh must not all expire in the same later one.
    monkeypatch.setattr(git_pr, "time", SimpleNamespace(time=lambda: 1000.0))
    mocker.patch("periscope.git_pr.git_state_for", return_value=None)
    for n in range(50):
        git_pr.cached_git_state(f"/p{n}")
    dues = [due for due, _ in git_pr._git_cache.values()]
    assert all(1000.0 + 10 <= d <= 1000.0 + 20 for d in dues)
    assert len(set(dues)) > 1


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


def test_ci_glyph_rollup():
    assert _ci_glyph([{"conclusion": "FAILURE"}]) == "✗"
    assert _ci_glyph([{"conclusion": "SUCCESS"}, {"status": "IN_PROGRESS"}]) == "⟳"
    assert _ci_glyph([{"conclusion": "SUCCESS"}, {"conclusion": "SKIPPED"}]) == "✓"
    assert _ci_glyph([]) is None
    assert _ci_glyph(None) is None


def test_ci_checks_names_each_check_failing_first():
    rollup = [
        {"name": "lint", "conclusion": "SUCCESS"},
        {"name": "build", "status": "IN_PROGRESS"},
        {"context": "ci/legacy", "state": "PENDING"},
        {"name": "test", "conclusion": "FAILURE"},
        {"name": "unnamed-skip", "conclusion": ""},
    ]
    assert _ci_checks(rollup) == [
        {"name": "test", "state": "failure"},
        {"name": "build", "state": "pending"},
        {"name": "ci/legacy", "state": "pending"},
        {"name": "lint", "state": "success"},
    ]


@pytest.mark.parametrize(
    ("branch", "pr_title", "want"),
    [
        # A PR title opening with the ticket wins: "THI-435: …" is how these PRs
        # are titled, and it names the ticket even when the branch doesn't.
        ("feature/permission-dialog-needs-input", "THI-435: parse_pane reads …", ("THI-435", "pr_title")),
        ("tdody/fdy-8562-restack", "[FDY-8600] restack the runbook", ("FDY-8600", "pr_title")),
        # Linear's branch shape, <user>/<key>-<num>-<slug>. fdy's PR titles are
        # conventional commits that never name the ticket (THI-441).
        ("tdody/fdy-8562-checkpoint-tooling", "feat(outcomes): restack", ("FDY-8562", "branch")),
        ("thibaultdody/thi-435-parse-pane", None, ("THI-435", "branch")),
        ("fdy-8562", "", ("FDY-8562", "branch")),
        # Shapes that aren't tickets: a one-digit number (utf-8, vue-3), a date,
        # a slug with no number, a ticket mid-segment, a ticket mid-title.
        ("feat/utf-8-handling", None, None),
        ("ds/2024-06-11_lift_pt_1", None, None),
        ("tdody/fix-rollup-blocklist-by-name", None, None),
        ("worktree-thi-435-permission-dialog", None, None),
        ("main", "feat(outcome-report): parse ISO-8601 dates", None),
        (None, None, None),
        # GitHub-style branches and standards share the shape; their words are
        # never team keys. pr-18 and pr-20-review are live refs (PR #45 review).
        ("fix-123-login-crash", None, None),
        ("tdody/fix-123-login-crash", None, None),
        ("gh-1234-fix-crash", None, None),
        ("issue-42-null", None, None),
        ("feature/bug-12", None, None),
        ("pr-18", None, None),
        ("pr-20-review", None, None),
        ("pep-723-header", None, None),
        ("node-20-upgrade", None, None),
        ("cve-2024-1234-patch", None, None),
        # A title counts only in the ticket's own form, `KEY-123:` or
        # `[KEY-123]`: a title opening with SHA-256 or UTF-8 names no ticket,
        # and mustn't hide the branch's real one.
        ("tdody/fdy-8562-checksums", "SHA-256 checksums for uploads", ("FDY-8562", "branch")),
        ("main", "UTF-8 everywhere", None),
        ("main", "THI-441 show the ticket", None),
        ("main", "PEP-723: inline script metadata", None),
    ],
)
def test_linear_ticket_for(branch, pr_title, want):
    assert linear_ticket_for(branch, pr_title) == want


@pytest.mark.parametrize(
    ("branch", "pr_title", "want"),
    [
        # With team keys set, only they count: ordinary words a word list can't
        # all name (fdy refs: split-7868, magic-404, norm-2025) stop counting.
        ("zf/split-7868-catalog-coverage", None, None),
        ("pd/magic-404-testing", None, None),
        ("norm-2025-06-a", None, None),
        ("tdody/fdy-8562-restack", None, ("FDY-8562", "branch")),
        # A known key needs no guard against one-digit numbers.
        ("tdody/fdy-7-tiny", None, ("FDY-7", "branch")),
        ("main", "THI-441: show the ticket", ("THI-441", "pr_title")),
        ("tdody/fdy-8562-restack", "ABC-12: someone else's key", ("FDY-8562", "branch")),
    ],
)
def test_linear_ticket_for_with_team_keys(branch, pr_title, want):
    assert linear_ticket_for(branch, pr_title, frozenset({"FDY", "THI"})) == want


def test_a_listed_team_key_wins_over_the_word_list():
    assert linear_ticket_for("pr-18", None, frozenset({"PR"})) == ("PR-18", "branch")


_NO_TICKET = {
    "linked_linear": None,
    "linked_linear_title": None,
    "linked_linear_status": None,
    "linear_ticket": None,
    "linear_ticket_source": None,
}


def test_linear_ticket_fields_a_link_replaces_the_derived_ticket():
    linked = {"linked_linear": "THI-9", "linked_linear_title": "Other", "linked_linear_status": "Todo"}
    got = linear_ticket_fields(linked, "tdody/thi-442-x", "THI-442: queue", frozenset())
    assert got == {
        **_NO_TICKET,
        "linked_linear": "THI-9",
        "linked_linear_title": "Other",
        "linked_linear_status": "Todo",
    }


@pytest.mark.parametrize("linked", [None, {}, {"linked_linear": None}])
def test_linear_ticket_fields_derive_without_a_link(linked):
    assert linear_ticket_fields(linked, "feat/x", "THI-442: queue", frozenset()) == {
        **_NO_TICKET,
        "linear_ticket": "THI-442",
        "linear_ticket_source": "pr_title",
    }
    assert linear_ticket_fields(linked, "tdody/thi-442-x", None, frozenset({"FDY"})) == _NO_TICKET


def test_ci_checks_caps_the_list():
    rollup = [{"name": f"c{i}", "conclusion": "SUCCESS"} for i in range(10)]
    assert len(_ci_checks(rollup)) == 6
    assert _ci_checks(None) == []


def test_ci_checks_treats_a_commit_status_error_as_failed():
    # A commit status's `state` can be ERROR (distinct from FAILURE) — it
    # must still be named in the CI-failed banner, not dropped silently.
    assert _ci_checks([{"context": "ci/legacy", "state": "ERROR"}]) == [{"name": "ci/legacy", "state": "failure"}]


def test_linked_pr_state_carries_base_and_checks(tmp_path, mocker):
    import json as _json

    payload = _json.dumps(
        {"state": "OPEN", "baseRefName": "main", "statusCheckRollup": [{"name": "test", "conclusion": "FAILURE"}]}
    )
    mocker.patch("periscope.gh.run", return_value=(0, payload))
    mocker.patch("periscope.git_pr._GH_AVAILABLE", True)
    out = linked_pr_state_for(str(tmp_path), 42)
    assert out["pr_base"] == "main"
    assert out["ci_checks"] == [{"name": "test", "state": "failure"}]


def test_pr_state_for_carries_base_and_checks(tmp_path, mocker):
    import json as _json

    payload = _json.dumps(
        [
            {
                "number": 7,
                "title": "t",
                "isDraft": False,
                "additions": 1,
                "deletions": 2,
                "reviewRequests": [],
                "baseRefName": "main",
                "statusCheckRollup": [{"name": "test", "conclusion": "FAILURE"}],
            }
        ]
    )
    mocker.patch("periscope.gh.run", return_value=(0, payload))
    mocker.patch("periscope.git_pr._GH_AVAILABLE", True)
    out = pr_state_for(str(tmp_path), "feature")
    assert out["pr_base"] == "main"
    assert out["ci"] == "✗"
    assert out["ci_checks"] == [{"name": "test", "state": "failure"}]


def _fake_gh(mocker, *, state, rollup=None):
    """Patch gh.run so linked_pr_state_for sees `gh pr view` returning one PR."""
    import json as _json

    payload = _json.dumps({"state": state, "statusCheckRollup": rollup or []})

    mocker.patch("periscope.gh.run", return_value=(0, payload))
    mocker.patch("periscope.git_pr._GH_AVAILABLE", True)


def test_linked_pr_state_merged(tmp_path, mocker):
    _fake_gh(mocker, state="MERGED")
    out = linked_pr_state_for(str(tmp_path), 1234)
    assert out == {"pr_state": "merged", "ci": None, "ci_checks": [], "pr_base": None}


def test_linked_pr_state_open_with_ci(tmp_path, mocker):
    _fake_gh(mocker, state="OPEN", rollup=[{"name": "test", "conclusion": "SUCCESS"}])
    out = linked_pr_state_for(str(tmp_path), 1234)
    assert out == {"pr_state": "open", "ci": "✓", "ci_checks": [{"name": "test", "state": "success"}], "pr_base": None}


def test_linked_pr_state_closed(tmp_path, mocker):
    _fake_gh(mocker, state="CLOSED")
    assert linked_pr_state_for(str(tmp_path), 7)["pr_state"] == "closed"


def test_linked_pr_state_none_without_number(tmp_path, mocker):
    mocker.patch("periscope.git_pr._GH_AVAILABLE", True)
    assert linked_pr_state_for(str(tmp_path), 0) is None
    assert linked_pr_state_for("", 5) is None


from periscope.gitutil import github_slug


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def test_github_slug_ssh_form(tmp_path):
    _git(tmp_path, "init")
    _git(tmp_path, "remote", "add", "origin", "git@github.com:faradayio/periscope.git")
    assert github_slug(str(tmp_path)) == "faradayio/periscope"


def test_github_slug_https_form(tmp_path):
    _git(tmp_path, "init")
    _git(tmp_path, "remote", "add", "origin", "https://github.com/faradayio/periscope.git")
    assert github_slug(str(tmp_path)) == "faradayio/periscope"


def test_github_slug_none_for_non_github(tmp_path):
    _git(tmp_path, "init")
    _git(tmp_path, "remote", "add", "origin", "https://gitlab.com/x/y.git")
    assert github_slug(str(tmp_path)) is None


def test_github_slug_none_when_no_remote(tmp_path):
    _git(tmp_path, "init")
    assert github_slug(str(tmp_path)) is None


def test_git_state_includes_repo_key_and_label(tmp_path, mocker):
    """git_state_for() returns repo_key (full path) + repo_label (basename)."""
    repo_dir = tmp_path / "foo"
    worktree_dir = repo_dir / "branch-a"
    worktree_dir.mkdir(parents=True)
    common_dir = repo_dir / ".git"
    common_dir.mkdir()

    def fake_run(args, cwd=None, timeout=None, env=None):
        if "status" in args:
            return (0, _HEAD)
        if "--git-common-dir" in args:
            return (0, str(common_dir))
        if "get-url" in args:
            return (1, "")  # no github slug
        return (0, "")

    # Both modules import _run from periscope.tmux at module load — patch both.
    mocker.patch("periscope.git_pr._run", side_effect=fake_run)
    mocker.patch("periscope.gitutil._run", side_effect=fake_run)
    out = git_pr.git_state_for(str(worktree_dir))
    assert out["repo_key"] == str(repo_dir)
    assert out["repo_label"] == "foo"


# --- git_signal_for: the progress signal list_claudes carries ---------------


def _mock_signal(mocker, *, toplevel="/w/tree", head="abc1234\t1700000000\tfix thing", head_code=0, porcelain=""):
    def fake_run(cmd, cwd=None, timeout=3.0):
        if "--show-toplevel" in cmd:
            return (0, toplevel)
        if "log" in cmd:
            return (head_code, head)
        if "status" in cmd:
            return (0, porcelain)
        return (0, "")

    mocker.patch("periscope.git_pr._run", side_effect=fake_run)


def test_git_signal_parses_head_and_counts_dirty(tmp_path, mocker):
    _mock_signal(mocker, porcelain=" M a.rs\n?? b.rs\nR  old.rs -> new.rs\n")
    out = git_signal_for(str(tmp_path))
    assert out["head"] == "abc1234"
    assert out["head_subject"] == "fix thing"
    assert out["head_committed_at"] == 1700000000
    # One entry per line — a rename is a single changed file, not two.
    assert out["dirty"] == 3


def test_git_signal_subject_keeps_embedded_tabs(tmp_path, mocker):
    """%h\\t%ct\\t%s is split twice, not on every tab — a subject containing a
    tab must survive intact rather than truncating at the first one."""
    _mock_signal(mocker, head="abc1234\t1700000000\tfix\tthing")
    assert git_signal_for(str(tmp_path))["head_subject"] == "fix\tthing"


def test_git_signal_clean_tree_is_zero_dirty(tmp_path, mocker):
    _mock_signal(mocker, porcelain="")
    assert git_signal_for(str(tmp_path))["dirty"] == 0


def test_git_signal_empty_repo_has_no_head(tmp_path, mocker):
    """A repo with no commits yet still reports dirty — losing the whole
    signal because HEAD doesn't resolve would blind the caller entirely."""
    _mock_signal(mocker, head="", head_code=128, porcelain="?? a.rs\n")
    out = git_signal_for(str(tmp_path))
    assert out["head"] is None and out["head_committed_at"] is None
    assert out["dirty"] == 1


def test_git_signal_non_git_path_is_none(tmp_path, mocker):
    def fake_run(cmd, cwd=None, timeout=3.0):
        return (128, "")

    mocker.patch("periscope.git_pr._run", side_effect=fake_run)
    assert git_signal_for(str(tmp_path)) is None
    assert git_signal_for(str(tmp_path / "nope")) is None
    assert git_signal_for("") is None


def test_git_signal_toplevel_is_the_worktree_not_the_repo(tmp_path, mocker):
    """Grouping by repo root would flag two linked worktrees as sharing a tree
    when they have separate build dirs and indexes and never contend."""
    wt = tmp_path / "wt"
    wt.mkdir()
    _mock_signal(mocker, toplevel=str(wt))
    assert git_signal_for(str(tmp_path))["toplevel"] == str(wt.resolve())
