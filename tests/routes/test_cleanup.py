"""Tests for /api/cleanup/*."""


def test_candidates_returns_list(client, mocker):
    mocker.patch(
        "periscope.routes.cleanup.compute_candidates",
        return_value=[
            {
                "pinned_dir": "/Users/x/dev/foo/wt-1",
                "project_name": "foo-feature",
                "tmux_session": "foo-feature",
                "repo": "/Users/x/dev/foo",
                "branch": "feature/x",
                "is_fork": False,
                "signals": [{"kind": "pr_merged", "label": "PR #42 merged"}],
                "dirty": False,
                "untracked": False,
                "idle_days": 5,
            }
        ],
    )
    r = client.get("/api/cleanup/candidates")
    assert r.status_code == 200
    assert len(r.json()["candidates"]) == 1
    assert r.json()["candidates"][0]["project_name"] == "foo-feature"


def test_candidates_repo_filter(client, mocker):
    spy = mocker.patch(
        "periscope.routes.cleanup.compute_candidates", return_value=[]
    )
    r = client.get("/api/cleanup/candidates?repo=/Users/x/dev/foo")
    assert r.status_code == 200
    spy.assert_called_once()
    args = spy.call_args.args
    # repo passed through (post-realpath).
    assert args and "foo" in args[0]


def test_archive_happy_path(client, mocker):
    mocker.patch(
        "periscope.routes.cleanup.all_projects",
        return_value={
            "/wt1": {
                "name": "foo", "tmux_session": "foo", "repo": "/repo",
                "archived_at": None, "base_branch": "main",
            }
        },
    )
    archive_spy = mocker.patch("periscope.routes.cleanup.archive_project", return_value=True)
    mutate_spy = mocker.patch("periscope.routes.cleanup._tmux_mutate")
    mocker.patch("periscope.routes.cleanup.list_windows", return_value=[])
    mocker.patch(
        "periscope.routes.cleanup._run",
        return_value=(0, ""),  # worktree remove succeeds
    )

    r = client.post("/api/cleanup/archive", json={
        "candidates": [{"pinned_dir": "/wt1", "delete_branch": False}],
    })
    assert r.status_code == 200
    body = r.json()
    assert body["archived"] == ["/wt1"]
    assert body["failed"] == []
    archive_spy.assert_called_once_with("/wt1")
    # No window in the session → empty placement set → no kill-session.
    assert all(c.args[0] != "kill-session" for c in mutate_spy.call_args_list)


def test_archive_with_branch_delete(client, mocker):
    mocker.patch(
        "periscope.routes.cleanup.all_projects",
        return_value={
            "/wt1": {
                "name": "foo", "tmux_session": "foo", "repo": "/repo",
                "archived_at": None, "base_branch": "main",
            }
        },
    )
    mocker.patch("periscope.routes.cleanup.archive_project", return_value=True)
    mocker.patch("periscope.routes.cleanup._tmux_mutate")
    mocker.patch("periscope.routes.cleanup.list_windows", return_value=[])
    # _run sequence: rev-parse HEAD → "feature/x", worktree remove → ok,
    # branch -D → ok.
    mocker.patch(
        "periscope.routes.cleanup._run",
        side_effect=[
            (0, "feature/x"),  # rev-parse HEAD (only when delete_branch=True)
            (0, ""),           # worktree remove
            (0, ""),           # branch -D
        ],
    )
    r = client.post("/api/cleanup/archive", json={
        "candidates": [{"pinned_dir": "/wt1", "delete_branch": True}],
    })
    assert r.status_code == 200
    assert r.json()["archived"] == ["/wt1"]


def test_archive_untracked_resolves_repo_from_worktree(client, mocker):
    """Untracked worktree (no project row) — repo derived via
    --git-common-dir."""
    mocker.patch("periscope.routes.cleanup.all_projects", return_value={})
    mocker.patch("periscope.routes.cleanup._tmux_mutate")
    mocker.patch(
        "periscope.routes.cleanup._run",
        side_effect=[
            (0, "/some/repo/.git"),  # git-common-dir
            (0, ""),                  # worktree remove
        ],
    )
    r = client.post("/api/cleanup/archive", json={
        "candidates": [{"pinned_dir": "/wt-orphan", "delete_branch": False}],
    })
    assert r.status_code == 200
    assert r.json()["archived"] == ["/wt-orphan"]


def test_archive_continues_on_individual_failure(client, mocker):
    """One bad row doesn't halt the batch."""
    mocker.patch(
        "periscope.routes.cleanup.all_projects",
        return_value={
            "/wt-good": {
                "name": "good", "tmux_session": "good", "repo": "/repo",
                "archived_at": None, "base_branch": "main",
            },
            "/wt-bad": {
                "name": "bad", "tmux_session": "bad", "repo": "/repo",
                "archived_at": None, "base_branch": "main",
            },
        },
    )
    mocker.patch("periscope.routes.cleanup.archive_project", return_value=True)
    mocker.patch("periscope.routes.cleanup._tmux_mutate")
    mocker.patch("periscope.routes.cleanup.list_windows", return_value=[])
    # First (good): worktree remove returns (0, ""). Second (bad): returns
    # (1, "worktree path not a worktree").
    mocker.patch(
        "periscope.routes.cleanup._run",
        side_effect=[
            (0, ""),                          # wt-good remove
            (1, "not a worktree"),            # wt-bad remove
        ],
    )
    r = client.post("/api/cleanup/archive", json={
        "candidates": [
            {"pinned_dir": "/wt-good", "delete_branch": False},
            {"pinned_dir": "/wt-bad", "delete_branch": False},
        ],
    })
    assert r.status_code == 200
    body = r.json()
    assert "/wt-good" in body["archived"]
    assert any(f["pinned_dir"] == "/wt-bad" for f in body["failed"])


def test_archive_rejects_main(client, mocker):
    mocker.patch("periscope.routes.cleanup.all_projects", return_value={})
    r = client.post("/api/cleanup/archive", json={
        "candidates": [{"pinned_dir": "__main__", "delete_branch": False}],
    })
    assert r.status_code == 200
    body = r.json()
    assert body["archived"] == []
    assert body["failed"][0]["pinned_dir"] == "__main__"
    assert "__main__" in body["failed"][0]["error"]


def test_archive_empty_candidates(client, mocker):
    r = client.post("/api/cleanup/archive", json={"candidates": []})
    assert r.status_code == 200
    assert r.json() == {"archived": [], "failed": []}


def test_archive_spares_ws_pane(client, clean_state, fresh_activity_db, mocker):
    # Archiving a worktree kills its placement set via kill-pane, sparing a
    # pane dragged into a live workspace; never kill-session.
    clean_state["projects"]["/repo/a"] = {
        "name": "a", "tmux_session": "sess_a", "repo": "/repo",
        "archived_at": None, "base_branch": "main",
    }
    clean_state["workspaces"]["ws_goal"] = {"id": "ws_goal", "archived_at": None}
    fresh_activity_db.set_pane_workspace("%claude", "ws_goal")
    mocker.patch("periscope.routes.cleanup.list_windows", return_value=[
        {"session": "sess_a", "index": 0, "pane_id": "%claude"},
        {"session": "sess_a", "index": 1, "pane_id": "%shell"},
    ])
    calls = []
    mocker.patch("periscope.routes.cleanup._tmux_mutate",
                 side_effect=lambda *a: calls.append(a))
    mocker.patch("periscope.routes.cleanup._run", return_value=(0, ""))
    r = client.post("/api/cleanup/archive", json={
        "candidates": [{"pinned_dir": "/repo/a", "delete_branch": False}],
    })
    assert r.status_code == 200
    assert ("kill-pane", "-t", "%shell") in calls      # killed by pane_id
    assert not any(a[0] == "kill-session" for a in calls)
    assert not any("sess_a:" in a for a in calls)       # never index-targeted
    assert all("%claude" not in a for a in calls)       # ws pane spared
