"""Git-backed diff core (periscope.gitdiff).

Uses real git in tmp_path — git is always available and mocking `git diff`
output would only test the mock. The invariant these exist to protect is the
session baseline: a snapshot taken while the tree is dirty must NOT attribute
that pre-existing work to the session (the /clear case).
"""
import subprocess

import pytest

from periscope import gitdiff


def _git(repo, *args) -> str:
    r = subprocess.run(["git", "-C", str(repo), *args],
                       capture_output=True, text=True)
    return r.stdout.strip()


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "a.py").write_text("one\ntwo\nthree\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "base")
    return tmp_path


# --- session baseline -------------------------------------------------

def test_snapshot_excludes_preexisting_dirty_work(repo):
    """THE invariant. /clear mints a new session id mid-work; a plain-HEAD
    baseline would blame all the user's in-flight edits on the new session."""
    (repo / "a.py").write_text("one\nUSER-EDIT\nthree\n")

    base = gitdiff.snapshot_base(str(repo))
    assert gitdiff.diff_for(str(repo), base)["files"] == []

    # Work done *after* the baseline is attributed; the pre-existing edit isn't.
    (repo / "a.py").write_text("one\nUSER-EDIT\nSESSION-EDIT\n")
    files = gitdiff.diff_for(str(repo), base)["files"]
    assert len(files) == 1
    kinds = {(line["kind"], line["text"]) for line in files[0]["hunks"][0]["lines"]}
    assert ("ctx", "USER-EDIT") in kinds       # pre-existing → context
    assert ("add", "SESSION-EDIT") in kinds    # in-session → addition


def test_snapshot_is_non_destructive(repo):
    """stash create must not touch the worktree or the stash ref."""
    (repo / "a.py").write_text("dirty\n")
    before = _git(repo, "status", "--porcelain")
    gitdiff.snapshot_base(str(repo))
    assert _git(repo, "status", "--porcelain") == before
    assert _git(repo, "stash", "list") == ""


def test_snapshot_falls_back_to_head_when_clean(repo):
    """`git stash create` prints nothing on a clean tree."""
    assert gitdiff.snapshot_base(str(repo)) == _git(repo, "rev-parse", "HEAD")


# --- branch scope -----------------------------------------------------

def test_branch_base_is_fork_point_and_diff_spans_commits(repo):
    fork = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-qb", "feature")
    (repo / "a.py").write_text("one\nTWO\nthree\n")
    _git(repo, "commit", "-qam", "committed on branch")
    (repo / "b.py").write_text("new\n")          # uncommitted + untracked-then-added
    _git(repo, "add", "b.py")

    assert gitdiff.branch_base(str(repo)) == fork
    out = gitdiff.diff_for(str(repo), fork)
    by_path = {f["path"]: f for f in out["files"]}
    # Spans BOTH the commit and the uncommitted work — the tab shows reality.
    assert by_path["a.py"]["status"] == "modified"
    assert by_path["b.py"]["status"] == "added"


def test_repo_root_none_outside_git(tmp_path):
    assert gitdiff.repo_root(str(tmp_path / "nope")) is None
    plain = tmp_path / "plain"
    plain.mkdir()
    assert gitdiff.repo_root(str(plain)) is None


# --- unified-diff parsing --------------------------------------------

def test_parse_unified_line_kinds_and_status():
    text = (
        "diff --git a/x.py b/x.py\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/x.py\n"
        "@@ -0,0 +1,2 @@ def enclosing():\n"
        "+added\n"
        " context\n"
        "-removed\n"
    )
    files = gitdiff.parse_unified(text)
    assert len(files) == 1
    f = files[0]
    assert f["path"] == "x.py"
    assert f["status"] == "added"
    assert f["hunks"][0]["header"] == "def enclosing():"
    assert f["hunks"][0]["new_start"] == 1
    assert [line["kind"] for line in f["hunks"][0]["lines"]] == ["add", "ctx", "del"]


def test_parse_unified_context_line_starting_with_plus():
    """A context line whose text begins with '+' must not read as an addition —
    the kind comes from the diff column, not from the text."""
    text = (
        "diff --git a/x.md b/x.md\n"
        "@@ -1,2 +1,2 @@\n"
        " +not an addition\n"
        "+real addition\n"
    )
    lines = gitdiff.parse_unified(text)[0]["hunks"][0]["lines"]
    assert lines[0] == {"kind": "ctx", "text": "+not an addition"}
    assert lines[1] == {"kind": "add", "text": "real addition"}


def test_parse_unified_marks_binary_and_deleted():
    text = (
        "diff --git a/img.png b/img.png\n"
        "Binary files a/img.png and b/img.png differ\n"
        "diff --git a/gone.py b/gone.py\n"
        "deleted file mode 100644\n"
    )
    files = gitdiff.parse_unified(text)
    assert [f["status"] for f in files] == ["binary", "deleted"]


def test_parse_unified_truncates_huge_files(monkeypatch):
    monkeypatch.setattr(gitdiff, "MAX_LINES_PER_FILE", 3)
    body = "".join(f"+line{i}\n" for i in range(50))
    files = gitdiff.parse_unified(
        "diff --git a/big.py b/big.py\n@@ -0,0 +1,50 @@\n" + body)
    assert files[0]["truncated"] is True
    assert len(files[0]["hunks"][0]["lines"]) == 3
