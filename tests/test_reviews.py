"""periscope.reviews — the on-disk walkthrough/comments convention and the
queue assembly. Files live outside the repo (~/.claude/reviews); tests point
REVIEWS_DIR at tmp_path."""

import json
import os
import subprocess

import pytest

from periscope import reviews

WALKTHROUGH = """# Cost pressure gauge

> branch: feat/x · head: 3f2a1c9 · pr: 1234
> repo: fdy

Panes with a bloated context cost more per turn and nothing said so.

## Adds a per-pane cost-pressure gauge
> kind: new-subsystem · refs: periscope/cost_pressure.py, static/src/split/ctxBand.js:1-40

Prose.

```
## not a stop (fenced)
```

## Reuses the PR cache
> kind: reuse · refs: periscope/git_pr.py:223-276

More prose.
"""

COMMENTS = """## 2026-10-26T09:14 · Adds a per-pane cost-pressure gauge
> resolved: no

Why does this bypass the signals store?
## looks like a heading but is body text

## 2026-10-26T09:20 · Reuses the PR cache
> resolved: yes

Fine.
"""


@pytest.fixture(autouse=True)
def _dir(tmp_path, monkeypatch):
    monkeypatch.setattr(reviews, "REVIEWS_DIR", tmp_path)
    monkeypatch.setattr(reviews, "_DEFAULT_BRANCH", {})
    monkeypatch.setattr(reviews, "_GH_SLUG", {})
    yield tmp_path


def test_slug_and_paths(tmp_path):
    assert reviews.slug_for("feat/a/b") == "feat--a--b"
    assert reviews.review_dir("fdy", "feat/a") == tmp_path / "fdy" / "feat--a"
    assert reviews.walkthrough_path("fdy", "feat/a").name == "walkthrough.md"
    assert reviews.comments_path("fdy", "feat/a").name == "comments.md"


def test_meta_pairs_splits_on_dot_and_newline():
    assert reviews.meta_pairs(["branch: feat/x · head: abc", "repo: fdy"]) == {
        "branch": "feat/x",
        "head": "abc",
        "repo": "fdy",
    }


def test_meta_pairs_rejects_markup():
    assert reviews.meta_pairs(["**For agentic workers:** REQUIRED"]) is None
    assert reviews.meta_pairs(["just a quote"]) is None


def test_parse_header():
    assert reviews.parse_header(WALKTHROUGH) == {"branch": "feat/x", "head": "3f2a1c9", "pr": 1234, "repo": "fdy"}
    assert reviews.parse_header("# T\n\n> head: abc\n") is None  # branch required
    assert reviews.parse_header("# T\n\n> **bold**: x\n") is None


def test_parse_header_non_ascii_digit_pr_is_none():
    # str.isdigit() accepts "²" but int("²") raises, which 500'd /api/reviews.
    h = reviews.parse_header("# T\n\n> branch: feat/x · head: abc · pr: ²\n")
    assert h is not None and h["pr"] is None


def test_count_stops_ignores_fenced_headings():
    assert reviews.count_stops(WALKTHROUGH) == 2


def test_parse_comments():
    got = reviews.parse_comments(COMMENTS)
    assert [c["stop"] for c in got] == ["Adds a per-pane cost-pressure gauge", "Reuses the PR cache"]
    assert got[0]["resolved"] is False and got[1]["resolved"] is True
    assert got[0]["ts"] == "2026-10-26T09:14"
    assert "## looks like a heading" in got[0]["body"]
    assert reviews.parse_comments("") == []


def test_append_comment_creates_then_appends(tmp_path):
    reviews.append_comment("fdy", "feat/x", "Stop A", "first", "2026-10-26T09:14")
    reviews.append_comment("fdy", "feat/x", "Stop B", "second\nline", "2026-10-26T09:15")
    text = (tmp_path / "fdy" / "feat--x" / "comments.md").read_text()
    got = reviews.parse_comments(text)
    assert [(c["stop"], c["body"], c["resolved"]) for c in got] == [
        ("Stop A", "first", False),
        ("Stop B", "second\nline", False),
    ]


def test_clear_review_removes_only_that_dir(tmp_path):
    reviews.append_comment("fdy", "feat/x", "S", "b", "2026-10-26T09:14")
    reviews.append_comment("fdy", "feat/y", "S", "b", "2026-10-26T09:14")
    reviews.clear_review("fdy", "feat/x")
    assert not (tmp_path / "fdy" / "feat--x").exists()
    assert (tmp_path / "fdy" / "feat--y").exists()
    reviews.clear_review("fdy", "nope")  # missing dir is a no-op


def test_parse_comments_tolerates_hand_edited_resolved_line():
    text = "## 2026-10-26T09:14 · A\n> resolved: no\n\nfirst\n\n## 2026-10-26T09:20 · B  \n> resolved: Yes \n\nsecond\n"
    got = reviews.parse_comments(text)
    assert [(c["stop"], c["resolved"], c["body"]) for c in got] == [("A", False, "first"), ("B", True, "second")]


def test_clear_review_refuses_paths_outside_reviews_dir(tmp_path):
    reviews.append_comment("fdy", "feat/x", "S", "b", "2026-10-26T09:14")
    for repo, branch in (("", ""), ("..", "x")):
        with pytest.raises(ValueError):
            reviews.clear_review(repo, branch)
    assert (tmp_path / "fdy" / "feat--x" / "comments.md").exists()


def _win(**over):
    base = {
        "pid": "p1",
        "pane_id": "%1",
        "session": "s",
        "index": 1,
        "name": "work",
        "cwd": "/dev/fdy/.claude/worktrees/x",
        "repo_key": "/dev/fdy",
        "repo_label": "fdy",
        "branch": "feat/x",
        "agent": "claude",
        "channel_attached": True,
        "activity": 100,
        "track_id": "/dev/fdy",
        "pr": None,
        "pr_state": None,
        "ci": None,
    }
    return {**base, **over}


def test_owner_for_picks_attached_claude_on_the_branch_most_recent():
    wins = [
        _win(pid="shell", agent=None),
        _win(pid="other", branch="feat/y"),  # same track, other branch
        _win(pid="detached", channel_attached=False),
        _win(pid="old", activity=50),
        _win(pid="new", activity=200),
    ]
    assert reviews.owner_for(wins, "fdy", "feat/x")["pid"] == "new"
    assert reviews.owner_for(wins, "fdy", "feat/z") is None
    assert reviews.owner_for(wins, "other-repo", "feat/x") is None


def test_worktree_for_cached_and_fresh(mocker):
    cached = mocker.patch("periscope.worktrees._cached_worktrees", return_value=[("/wt/x", "feat/x")])
    fresh = mocker.patch("periscope.worktrees.for_branch", return_value="/wt/fresh")
    wins = [_win()]
    assert reviews.worktree_for(wins, "fdy", "feat/x") == "/wt/x"
    cached.assert_called_once_with("/dev/fdy")
    assert reviews.worktree_for(wins, "fdy", "feat/x", fresh=True) == "/wt/fresh"
    fresh.assert_called_once_with("/dev/fdy", "feat/x")
    assert reviews.worktree_for(wins, "nope", "feat/x") is None  # no window knows the repo path


def _git_repo(path):
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    for n in ("a", "b", "c"):
        (path / n).write_text(n)
        subprocess.run(["git", "-C", str(path), "add", n], check=True)
        subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", n], check=True, env={**os.environ, **env})
    return subprocess.run(
        ["git", "-C", str(path), "rev-list", "--all"], capture_output=True, text=True, check=True
    ).stdout.split()


def test_stale_by_counts_commits_since_head(tmp_path):
    shas = _git_repo(tmp_path / "r")  # newest first
    assert reviews.stale_by(str(tmp_path / "r"), shas[0]) == 0
    assert reviews.stale_by(str(tmp_path / "r"), shas[2]) == 2
    assert reviews.stale_by(str(tmp_path / "r"), "0000000") is None
    assert reviews.stale_by(None, shas[0]) is None


MINE = '[{"number": 1234, "title": "Gauge", "headRefName": "feat/x", "isDraft": false, "statusCheckRollup": [], "author": {"login": "tom"}}]'
REQUESTED = (
    '[{"number": 1234, "title": "Gauge", "headRefName": "feat/x", "isDraft": false, "statusCheckRollup": [], "author": {"login": "tom"}},'
    ' {"number": 99, "title": "Teammate PR", "headRefName": "feat/theirs", "isDraft": true, "statusCheckRollup": [], "author": {"login": "sam"}}]'
)


def test_open_prs_merges_both_lists_and_assigns_roles(mocker):
    mocker.patch("periscope.reviews._GH_AVAILABLE", True)
    mocker.patch("periscope.reviews.gitutil.github_slug", return_value="tom/fdy")
    run = mocker.patch("periscope.reviews._run", side_effect=[(0, MINE), (0, REQUESTED)])
    got = reviews.open_prs("/dev/fdy")
    assert [(p["number"], p["role"], p["branch"], p["author"], p["draft"]) for p in got] == [
        (1234, "mine", "feat/x", "tom", False),
        (99, "review-requested", "feat/theirs", "sam", True),
    ]
    first, second = run.call_args_list
    assert "--author" in first.args[0] and "@me" in first.args[0] and first.kwargs["cwd"] == "/dev/fdy"
    assert "--search" in second.args[0] and "review-requested:@me" in second.args[0]


def test_open_prs_without_gh_is_empty(mocker):
    mocker.patch("periscope.reviews._GH_AVAILABLE", False)
    assert reviews.open_prs("/dev/fdy") == []


def test_list_reviews_assembles_prs_then_local_then_orphans(tmp_path, mocker, monkeypatch):
    monkeypatch.setattr(reviews.gitutil, "detect_default_branch", lambda root: "main")
    mocker.patch("periscope.reviews.gitutil.github_slug", return_value="tom/fdy")
    mocker.patch("periscope.worktrees._cached_worktrees", return_value=[("/dev/fdy/.claude/worktrees/x", "feat/x")])
    mocker.patch(
        "periscope.reviews.cached_open_prs",
        return_value=[
            {
                "number": 1234,
                "title": "Gauge",
                "branch": "feat/x",
                "draft": False,
                "ci": "✓",
                "role": "mine",
                "author": "tom",
            },
            {
                "number": 99,
                "title": "Theirs",
                "branch": "feat/theirs",
                "draft": True,
                "ci": None,
                "role": "review-requested",
                "author": "sam",
            },
        ],
    )
    linked = mocker.patch(
        "periscope.reviews.git_pr.cached_linked_pr_state", return_value={"pr_state": "merged", "ci": "✓"}
    )
    mocker.patch("periscope.reviews.stale_by", return_value=None)
    # my PR's branch has a walkthrough + one unresolved comment
    wt = reviews.walkthrough_path("fdy", "feat/x")
    wt.parent.mkdir(parents=True)
    wt.write_text(WALKTHROUGH)
    reviews.append_comment("fdy", "feat/x", "Stop", "hm", "2026-10-26T09:14")
    # orphan dir: no PR, no live window; its header names a closed PR
    o = reviews.walkthrough_path("fdy", "old/branch")
    o.parent.mkdir(parents=True)
    o.write_text("# T\n\n> branch: old/branch · head: abc · pr: 500\n")
    wins = [_win(), _win(pid="p2", branch="feat/local")]

    rows = reviews.list_reviews(wins, ["/dev/fdy"])
    assert [(r["repo"], r["branch"], r["role"]) for r in rows] == [
        ("fdy", "feat/x", "mine"),
        ("fdy", "feat/theirs", "review-requested"),
        ("fdy", "feat/local", "local"),
        ("fdy", "old/branch", "local"),
    ]
    x = rows[0]
    assert x["pr"] == {
        "number": 1234,
        "title": "Gauge",
        "draft": False,
        "state": "open",
        "ci": "✓",
        "author": "tom",
        "url": "https://github.com/tom/fdy/pull/1234",
    }
    assert x["walkthrough"] == {"stops": 2, "head": "3f2a1c9", "stale_by": None}
    assert x["unresolved"] == 1
    assert x["owner"] == {"pid": "p1", "pane_id": "%1", "session": "s", "index": 1, "name": "work"}
    assert x["worktree"] == "/dev/fdy/.claude/worktrees/x"
    theirs = rows[1]
    assert theirs["owner"] is None and theirs["worktree"] is None and theirs["walkthrough"] is None
    local = rows[2]
    assert local["pr"] is None and local["owner"]["pid"] == "p2"
    ob = rows[3]
    # An orphan row links too: the URL comes from the origin remote, not from gh.
    assert ob["pr"] == {
        "number": 500,
        "title": "",
        "draft": False,
        "state": "merged",
        "ci": "✓",
        "author": None,
        "url": "https://github.com/tom/fdy/pull/500",
    }
    linked.assert_called_with("/dev/fdy", 500)
    assert ob["owner"] is None and ob["slug"] == "old--branch"


def test_pr_url_caches_a_found_slug_but_retries_after_none(mocker):
    slug = mocker.patch("periscope.reviews.gitutil.github_slug", side_effect=[None, "tom/fdy"])
    assert reviews._pr_url("/dev/fdy", 7) is None
    assert reviews._pr_url("/dev/fdy", 7) == "https://github.com/tom/fdy/pull/7"
    assert reviews._pr_url("/dev/fdy", 8) == "https://github.com/tom/fdy/pull/8"
    assert slug.call_count == 2
    assert reviews._pr_url(None, 7) is None


def test_open_prs_skips_repos_without_github_and_returns_none_on_failure(mocker):
    mocker.patch("periscope.reviews._GH_AVAILABLE", True)
    mocker.patch("periscope.reviews.gitutil.github_slug", return_value=None)
    assert reviews.open_prs("/dev/local-only") == []
    mocker.patch("periscope.reviews.gitutil.github_slug", return_value="tom/fdy")
    mocker.patch("periscope.reviews._run", return_value=(1, "auth error"))
    assert reviews.open_prs("/dev/fdy") is None


def test_list_reviews_skips_default_branch_and_detached_windows(mocker):
    mocker.patch("periscope.reviews.cached_open_prs", return_value=[])
    mocker.patch("periscope.reviews.gitutil.detect_default_branch", return_value="main")
    mocker.patch("periscope.worktrees._cached_worktrees", return_value=[])
    reviews._DEFAULT_BRANCH.clear()
    wins = [_win(pid="a", branch="main"), _win(pid="b", branch="@abc1234"), _win(pid="c", branch="?")]
    assert reviews.list_reviews(wins, ["/dev/fdy"]) == []
    rows = reviews.list_reviews([_win(branch="feat/x")], ["/dev/fdy"])
    assert [(r["branch"], r["role"]) for r in rows] == [("feat/x", "local")]


def test_cached_open_prs_serves_stale_rows_and_refreshes_once(mocker):
    root = "/dev/stale-test"
    rows = [{"number": 1}]
    reviews._prs_cache[root] = (0, rows)
    bg = mocker.patch("periscope.reviews._bg")  # no real thread (docs/testing.md)
    try:
        assert reviews.cached_open_prs(root) == rows
        assert reviews.cached_open_prs(root) == rows
        bg.assert_called_once()
    finally:
        reviews._prs_cache.pop(root, None)
        reviews._prs_fetching.discard(root)


def test_fetch_failure_keeps_last_good_rows_and_clears_flag(mocker):
    root = "/dev/fail-test"
    rows = [{"number": 1}]
    reviews._prs_cache[root] = (0, rows)
    reviews._prs_fetching.add(root)
    mocker.patch("periscope.reviews.open_prs", return_value=None)
    try:
        reviews._fetch_prs_into_cache(root)
        assert reviews._prs_cache[root][1] == rows
        assert reviews._prs_cache[root][0] > 0
        assert root not in reviews._prs_fetching
    finally:
        reviews._prs_cache.pop(root, None)


def test_stale_by_does_not_cache_unknown_head(tmp_path):
    shas = _git_repo(tmp_path / "r")
    assert reviews.stale_by(str(tmp_path / "r"), "0000000") is None
    assert not any(k[1] == "0000000" for k in reviews._STALE_CACHE)
    assert reviews.stale_by(str(tmp_path / "r"), shas[1]) == 1


def test_review_requested_pr_matches_its_pr_n_checkout(mocker):
    mocker.patch(
        "periscope.reviews.cached_open_prs",
        return_value=[
            {
                "number": 123,
                "title": "T",
                "branch": "feat/theirs",
                "draft": False,
                "ci": None,
                "role": "review-requested",
                "author": "sam",
            },
        ],
    )
    mocker.patch("periscope.worktrees._cached_worktrees", return_value=[("/wt/pr-123", "pr-123")])
    mocker.patch("periscope.reviews.stale_by", return_value=None)
    mocker.patch("periscope.reviews.gitutil.detect_default_branch", return_value="main")
    wins = [_win(pid="rv", branch="pr-123", cwd="/wt/pr-123")]
    rows = reviews.list_reviews(wins, ["/dev/fdy"])
    assert [(r["branch"], r["role"]) for r in rows] == [("feat/theirs", "review-requested")]
    assert rows[0]["worktree"] == "/wt/pr-123"
    assert rows[0]["owner"]["pid"] == "rv"


def test_stale_by_rejects_option_like_head(mocker):
    run = mocker.patch("periscope.reviews._run", side_effect=AssertionError("must not run git"))
    assert reviews.stale_by("/wt", "-x") is None
    run.assert_not_called()


def test_aliases_for_cache_header_or_neither(tmp_path, monkeypatch):
    import time

    monkeypatch.setattr(reviews, "REVIEWS_DIR", tmp_path)
    monkeypatch.setitem(reviews._prs_cache, "/dev/fdy", (time.time(), [{"number": 7, "branch": "feat/x"}]))
    assert reviews.aliases_for("/dev/fdy", "fdy", "feat/x") == {"pr-7"}
    assert reviews.aliases_for("/dev/fdy", "fdy", "feat/y") == set()
    p = reviews.walkthrough_path("fdy", "feat/y")
    p.parent.mkdir(parents=True)
    p.write_text("# T\n\n> branch: feat/y · head: abc1234 · pr: 9\n\nbody\n")
    assert reviews.aliases_for("/dev/other", "fdy", "feat/y") == {"pr-9"}
    assert reviews.aliases_for(None, "fdy", "feat/y") == {"pr-9"}


@pytest.mark.parametrize(
    ("text", "n"),
    [
        ("##\n## A\n##\tB\n### not\n#not\n", 3),  # bare and tab-separated count; ### and #not don't
        ("````\n```\n## inner\n```\n````\n## B\n", 1),  # a 4-backtick fence swallows an inner ```
        ("```foo```\n## A\n", 1),  # inline code, not a fence opener
        ("~~~\n## in tilde\n```\n~~~\n## B\n", 1),  # a tilde fence ignores a backtick line
        ("    ```\n## A\n", 1),  # indented code is not a fence
        ("```\n## in\n ```  \n## B\n", 1),  # a closer may be indented and trail spaces
    ],
)
def test_count_stops_follows_commonmark(text, n):
    assert reviews.count_stops(text) == n


def test_orphan_with_open_pr_is_review_requested_and_rows_count_comments(tmp_path, mocker, monkeypatch):
    """A submitted review drops the PR from `review-requested:@me`; its review dir
    must still surface as that role while the PR is open, and every row carries
    the total comment count (the view refetches on it)."""
    monkeypatch.setattr(reviews, "REVIEWS_DIR", tmp_path)
    monkeypatch.setattr(reviews.gitutil, "detect_default_branch", lambda root: "main")
    mocker.patch("periscope.worktrees._cached_worktrees", return_value=[])
    mocker.patch("periscope.reviews.cached_open_prs", return_value=[])
    mocker.patch("periscope.reviews.stale_by", return_value=None)
    states = {77: {"pr_state": "open", "ci": "✓"}, 78: {"pr_state": "merged", "ci": None}}
    mocker.patch("periscope.reviews.git_pr.cached_linked_pr_state", side_effect=lambda cwd, n: states[n])
    for branch, pr in (("theirs/open", 77), ("theirs/done", 78)):
        p = reviews.walkthrough_path("fdy", branch)
        p.parent.mkdir(parents=True)
        p.write_text(f"# T\n\n> branch: {branch} · head: abc1234 · pr: {pr}\n\nbody\n\n## S\n")
    reviews.append_comment("fdy", "theirs/open", "S", "a", "2026-10-26T09:14")
    reviews.append_comment("fdy", "theirs/open", "S", "b", "2026-10-26T09:15")
    c = reviews.comments_path("fdy", "theirs/open")
    c.write_text(c.read_text().replace("> resolved: no", "> resolved: yes", 1))
    rows = {r["branch"]: r for r in reviews.list_reviews([], ["/dev/fdy"])}
    assert (rows["theirs/open"]["role"], rows["theirs/open"]["pr"]["number"]) == ("review-requested", 77)
    assert (rows["theirs/open"]["unresolved"], rows["theirs/open"]["comments"]) == (1, 2)
    assert (rows["theirs/done"]["role"], rows["theirs/done"]["comments"]) == ("local", 0)


def _pr(number, branch, title="T", role="mine"):
    return {
        "number": number,
        "title": title,
        "branch": branch,
        "draft": False,
        "ci": None,
        "role": role,
        "author": "sam",
    }


def _rows_by_branch(mocker, prs, wins, team_keys=frozenset()):
    # Every gh/git call stubbed; cached_open_prs would otherwise start a _bg thread (docs/testing.md).
    mocker.patch("periscope.reviews.cached_open_prs", return_value=prs)
    mocker.patch("periscope.reviews.gitutil.detect_default_branch", return_value="main")
    mocker.patch("periscope.reviews.gitutil.github_slug", return_value="tom/fdy")
    mocker.patch("periscope.worktrees._cached_worktrees", return_value=[])
    mocker.patch("periscope.reviews.stale_by", return_value=None)
    return {r["branch"]: r for r in reviews.list_reviews(wins, ["/dev/fdy"], team_keys)}


_NO_TICKET = {
    "linked_linear": None,
    "linked_linear_title": None,
    "linked_linear_status": None,
    "linear_ticket": None,
    "linear_ticket_source": None,
}


def _ticket(row):
    return {k: row[k] for k in _NO_TICKET}


def test_row_ticket_is_the_owner_panes_link_over_a_derived_one(mocker):
    wins = [
        _win(
            branch="thibaultdody/thi-442-x",
            linked_linear="THI-9",
            linked_linear_title="Other",
            linked_linear_status="In Progress",
        )
    ]
    rows = _rows_by_branch(mocker, [_pr(45, "thibaultdody/thi-442-x", title="THI-442: queue")], wins)
    assert _ticket(rows["thibaultdody/thi-442-x"]) == {
        **_NO_TICKET,
        "linked_linear": "THI-9",
        "linked_linear_title": "Other",
        "linked_linear_status": "In Progress",
    }


def test_row_ticket_from_the_pr_title_then_the_branch(mocker):
    prs = [
        _pr(45, "feat/queue", title="THI-442: queue search"),
        _pr(46, "thibaultdody/thi-443-colors", title="colors"),
        _pr(47, "feat/plain", title="plain"),
    ]
    rows = _rows_by_branch(mocker, prs, [])
    assert _ticket(rows["feat/queue"]) == {**_NO_TICKET, "linear_ticket": "THI-442", "linear_ticket_source": "pr_title"}
    assert _ticket(rows["thibaultdody/thi-443-colors"]) == {
        **_NO_TICKET,
        "linear_ticket": "THI-443",
        "linear_ticket_source": "branch",
    }
    assert _ticket(rows["feat/plain"]) == _NO_TICKET


def test_row_ticket_respects_linear_team_keys(mocker):
    rows = _rows_by_branch(mocker, [_pr(46, "thibaultdody/thi-443-colors", title="colors")], [], frozenset({"FDY"}))
    assert _ticket(rows["thibaultdody/thi-443-colors"]) == _NO_TICKET


def test_row_ticket_reads_the_prs_branch_not_its_pr_n_checkout(mocker):
    # The owner pane sits on `pr-45`, which names no ticket; the row's headRefName does —
    # and the owner payload's own derived ticket never leaks into the row.
    wins = [_win(pid="rv", branch="pr-45", cwd="/wt/pr-45", linear_ticket="ABC-12", linear_ticket_source="branch")]
    prs = [_pr(45, "thibaultdody/thi-442-x", title="queue", role="review-requested")]
    row = _rows_by_branch(mocker, prs, wins)["thibaultdody/thi-442-x"]
    assert row["owner"]["pid"] == "rv"
    assert _ticket(row) == {**_NO_TICKET, "linear_ticket": "THI-442", "linear_ticket_source": "branch"}


def test_local_and_header_rows_derive_from_the_branch_alone(mocker):
    mocker.patch("periscope.reviews.git_pr.cached_linked_pr_state", return_value={"pr_state": "merged", "ci": None})
    p = reviews.walkthrough_path("fdy", "thibaultdody/thi-400-old")
    p.parent.mkdir(parents=True)
    p.write_text("# T\n\n> branch: thibaultdody/thi-400-old · head: abc1234 · pr: 500\n")
    rows = _rows_by_branch(mocker, [], [_win(branch="thibaultdody/thi-441-local")])
    old, local = rows["thibaultdody/thi-400-old"], rows["thibaultdody/thi-441-local"]
    assert old["pr"]["title"] == "" and _ticket(old)["linear_ticket"] == "THI-400"
    assert local["pr"] is None and _ticket(local)["linear_ticket"] == "THI-441"


def test_a_header_row_reads_its_owner_panes_pr_title(mocker):
    # A teammate's PR you reviewed leaves review-requested:@me while still open:
    # its row comes from the walkthrough header, with no title. The owner pane on
    # the branch knows the title — used only when its PR is the row's.
    mocker.patch("periscope.reviews.git_pr.cached_linked_pr_state", return_value={"pr_state": "open", "ci": None})
    for branch, pr in (("feat/reviewed", 500), ("feat/relinked", 501)):
        p = reviews.walkthrough_path("fdy", branch)
        p.parent.mkdir(parents=True)
        p.write_text(f"# T\n\n> branch: {branch} · head: abc1234 · pr: {pr}\n")
    wins = [
        _win(pid="a", branch="feat/reviewed", pr=500, pr_title="THI-442: queue"),
        _win(pid="b", branch="feat/relinked", pr=999, pr_title="THI-9: some other PR"),
    ]
    rows = _rows_by_branch(mocker, [], wins)
    assert _ticket(rows["feat/reviewed"]) == {
        **_NO_TICKET,
        "linear_ticket": "THI-442",
        "linear_ticket_source": "pr_title",
    }
    assert _ticket(rows["feat/relinked"]) == _NO_TICKET


def test_read_reviewed_missing_corrupt_and_mangled_read_as_empty(tmp_path):
    assert reviews.read_reviewed("fdy", "feat/x") == {}
    p = reviews.reviewed_path("fdy", "feat/x")
    p.parent.mkdir(parents=True)
    for bad in ["{not json", "[1, 2]", '"a string"']:
        p.write_text(bad)
        assert reviews.read_reviewed("fdy", "feat/x") == {}
    p.write_text(json.dumps({"A": "0123abcd", "B": 7, "C": None}))
    assert reviews.read_reviewed("fdy", "feat/x") == {"A": "0123abcd"}


def test_set_reviewed_sets_overwrites_unsets_and_removes_the_empty_file(tmp_path):
    reviews.review_dir("fdy", "feat/x").mkdir(parents=True)
    reviews.set_reviewed("fdy", "feat/x", "Stop A", "0123abcd")
    reviews.set_reviewed("fdy", "feat/x", "Stop B", "11111111")
    reviews.set_reviewed("fdy", "feat/x", "Stop A", "ffffffff")
    assert reviews.read_reviewed("fdy", "feat/x") == {"Stop A": "ffffffff", "Stop B": "11111111"}
    reviews.set_reviewed("fdy", "feat/x", "Stop A", None)
    reviews.set_reviewed("fdy", "feat/x", "never marked", None)
    assert reviews.read_reviewed("fdy", "feat/x") == {"Stop B": "11111111"}
    reviews.set_reviewed("fdy", "feat/x", "Stop B", None)
    assert not reviews.reviewed_path("fdy", "feat/x").exists()
    assert not list(reviews.review_dir("fdy", "feat/x").glob("*.tmp"))


def test_set_reviewed_never_creates_the_review_dir():
    with pytest.raises(FileNotFoundError):
        reviews.set_reviewed("fdy", "gone", "Stop A", "0123abcd")
    assert not reviews.review_dir("fdy", "gone").exists()
