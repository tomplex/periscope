"""Tests for /api/projects/*."""

import json

import pytest


@pytest.fixture(autouse=True)
def _stub_worktree_path(mocker, tmp_path):
    """projects_pr_review resolves the worktree path via worktree_path(),
    which calls _resolve_layout (settings I/O + a git-worktree-list). Stub
    it to a tmp path so these tests stay hermetic — none of them assert on
    the worktree path itself.
    """
    mocker.patch(
        "periscope.routes.projects.worktree_path",
        side_effect=lambda repo, slug: str(tmp_path / "wt" / slug.replace("/", "-")),
    )


# === phase 4: PR review =====================================================

def _gh_view_json(pr_number, head_ref="feature-foo", is_cross=False, state="OPEN", base="main"):
    """Build a fake `gh pr view --json ...` output."""
    return json.dumps({
        "headRefName": head_ref,
        "isCrossRepository": is_cross,
        "headRepository": {"name": "fakerepo"},
        "baseRefName": base,
        "state": state,
    })


def _pr_review_run_sequence(repo_path, gh_output):
    """Ordered return values for `_run` covering the PR review flow when
    no explicit name is given (the common case — name auto-resolves from
    the PR's head branch, so the tmux collision check runs AFTER gh):
      1. git rev-parse --show-toplevel        → (0, repo)
      2. gh pr view --json ...                → (0, gh_output)
      3. tmux has-session                     → (1, "") meaning "not found"
      4. git fetch origin pull/N/head:pr-N    → (0, "")
      5. git worktree add <path> pr-N         → (0, "")
    The order MUST match the endpoint's actual call sequence.
    """
    return [
        (0, str(repo_path)),
        (0, gh_output),
        (1, ""),
        (0, ""),
        (0, ""),
    ]


def test_pr_review_success_same_repo(client, mocker, tmp_path):
    """Happy path: same-repo PR (isCrossRepository=False)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    mocker.patch(
        "periscope.routes.projects._run",
        side_effect=_pr_review_run_sequence(repo, _gh_view_json(42, is_cross=False)),
    )
    mocker.patch("periscope.routes.projects._layout_two_window", return_value=("abcd1234", "efgh5678"))
    mocker.patch("periscope.routes.projects.create_project", return_value={
        "name": "pr-42", "tmux_session": "pr-42", "repo": str(repo),
        "base_branch": "main", "archived_at": None,
    })
    # Don't trip on the os.path.exists check for the worktree path —
    # tmp_path/repo doesn't already have ~/dev/worktrees/repo/pr-42.
    mocker.patch("periscope.routes.projects.os.path.exists", return_value=False)
    mocker.patch("periscope.routes.projects.all_projects", return_value={})
    set_fields = mocker.patch("periscope.routes.projects.set_window_fields")

    r = client.post("/api/projects/pr-review", json={
        "repo": str(repo), "pr_number": 42,
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["pr_number"] == 42
    assert body["is_fork"] is False
    assert body["pr_state"] == "OPEN"
    set_fields.assert_called_once_with("abcd1234", linked_pr=42, is_fork=False)


def test_pr_review_fork_pr(client, mocker, tmp_path):
    """Fork PR: is_fork=True flag is written."""
    repo = tmp_path / "repo"
    repo.mkdir()
    mocker.patch(
        "periscope.routes.projects._run",
        side_effect=_pr_review_run_sequence(repo, _gh_view_json(99, is_cross=True)),
    )
    mocker.patch("periscope.routes.projects._layout_two_window", return_value=("abcd1234", "efgh5678"))
    mocker.patch("periscope.routes.projects.create_project", return_value={
        "name": "pr-99", "tmux_session": "pr-99", "repo": str(repo),
        "base_branch": "main", "archived_at": None,
    })
    mocker.patch("periscope.routes.projects.os.path.exists", return_value=False)
    mocker.patch("periscope.routes.projects.all_projects", return_value={})
    set_fields = mocker.patch("periscope.routes.projects.set_window_fields")

    r = client.post("/api/projects/pr-review", json={
        "repo": str(repo), "pr_number": 99,
    })
    assert r.status_code == 200
    assert r.json()["is_fork"] is True
    set_fields.assert_called_once_with("abcd1234", linked_pr=99, is_fork=True)


def test_pr_review_merged_pr_still_creates(client, mocker, tmp_path):
    """A merged PR should still create the project."""
    repo = tmp_path / "repo"
    repo.mkdir()
    mocker.patch(
        "periscope.routes.projects._run",
        side_effect=_pr_review_run_sequence(repo, _gh_view_json(7, state="MERGED")),
    )
    mocker.patch("periscope.routes.projects._layout_two_window", return_value=("abcd1234", "efgh5678"))
    mocker.patch("periscope.routes.projects.create_project", return_value={
        "name": "pr-7", "tmux_session": "pr-7", "repo": str(repo),
        "base_branch": "main", "archived_at": None,
    })
    mocker.patch("periscope.routes.projects.os.path.exists", return_value=False)
    mocker.patch("periscope.routes.projects.all_projects", return_value={})
    mocker.patch("periscope.routes.projects.set_window_fields")

    r = client.post("/api/projects/pr-review", json={
        "repo": str(repo), "pr_number": 7,
    })
    assert r.status_code == 200
    assert r.json()["pr_state"] == "MERGED"


def test_pr_review_not_found(client, mocker, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    mocker.patch(
        "periscope.routes.projects._run",
        side_effect=[
            (0, str(repo)),           # rev-parse
            (1, "no pull requests found for branch"),  # gh pr view
        ],
    )
    r = client.post("/api/projects/pr-review", json={
        "repo": str(repo), "pr_number": 9999,
    })
    assert r.status_code == 404


def test_pr_review_rejects_non_git(client, mocker, tmp_path):
    not_a_repo = tmp_path / "scratch"
    not_a_repo.mkdir()
    mocker.patch(
        "periscope.routes.projects._run",
        return_value=(128, "fatal: not a git repository"),
    )
    r = client.post("/api/projects/pr-review", json={
        "repo": str(not_a_repo), "pr_number": 1,
    })
    assert r.status_code == 400


def test_pr_review_rejects_invalid_pr_number(client, mocker, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    mocker.patch(
        "periscope.routes.projects._run",
        return_value=(0, str(repo)),
    )
    r = client.post("/api/projects/pr-review", json={
        "repo": str(repo), "pr_number": 0,
    })
    assert r.status_code == 400


def test_pr_review_rejects_session_collision_explicit_name(client, mocker, tmp_path):
    """With an explicit name, the `tmux has-session` 409 fires BEFORE gh —
    the cheap pre-check that avoids a wasted ~15s gh call on a known
    collision."""
    repo = tmp_path / "repo"
    repo.mkdir()
    mocker.patch(
        "periscope.routes.projects._run",
        side_effect=[
            (0, str(repo)),  # rev-parse
            (0, ""),          # has-session returns 0 → session exists
        ],
    )
    r = client.post("/api/projects/pr-review", json={
        "repo": str(repo), "pr_number": 42, "name": "my-review",
    })
    assert r.status_code == 409
    assert "already exists" in r.json()["detail"]


def test_pr_review_rejects_session_collision_auto_name(client, mocker, tmp_path):
    """Without an explicit name, the name comes from the PR's head branch,
    so the collision check runs AFTER gh resolves it."""
    repo = tmp_path / "repo"
    repo.mkdir()
    mocker.patch(
        "periscope.routes.projects._run",
        side_effect=[
            (0, str(repo)),                          # rev-parse
            (0, _gh_view_json(42, head_ref="dup")),  # gh pr view
            (0, ""),                                  # has-session: session exists
        ],
    )
    r = client.post("/api/projects/pr-review", json={
        "repo": str(repo), "pr_number": 42,
    })
    assert r.status_code == 409
    assert "already exists" in r.json()["detail"]


def test_pr_review_auto_names_from_head_branch(client, mocker, tmp_path):
    """No explicit name → project name = the PR's head branch (so the user
    doesn't have to type one)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    mocker.patch(
        "periscope.routes.projects._run",
        side_effect=_pr_review_run_sequence(
            repo, _gh_view_json(42, head_ref="tc/lookup-redesign"),
        ),
    )
    mocker.patch("periscope.routes.projects._layout_two_window", return_value=("abcd1234", "efgh5678"))
    create = mocker.patch("periscope.routes.projects.create_project", return_value={
        "name": "tc/lookup-redesign", "tmux_session": "tc/lookup-redesign",
        "repo": str(repo), "base_branch": "main", "archived_at": None,
    })
    mocker.patch("periscope.routes.projects.os.path.exists", return_value=False)
    mocker.patch("periscope.routes.projects.all_projects", return_value={})
    mocker.patch("periscope.routes.projects.set_window_fields")

    r = client.post("/api/projects/pr-review", json={
        "repo": str(repo), "pr_number": 42,
    })
    assert r.status_code == 200, r.text
    # Project name + tmux session both come from headRefName.
    assert create.call_args.kwargs["name"] == "tc/lookup-redesign"
    assert create.call_args.kwargs["tmux_session"] == "tc/lookup-redesign"
