# Batch 8: projects_pr_review decomposition & spawn_worktree reuse

**Date:** 2026-05-21
**Classification:** architectural
**Commit:** `4ab1437`

## Findings Resolved
- #4: Decomposed the ~196-line `projects_pr_review` handler into a linear orchestrator over three in-file helpers — `_resolve_pr_metadata` (the `gh pr view` call + parse + 404/400/500 mapping), `_fetch_pr_branch` (the PR-refspec fetch + 409/400 error mapping), and `_discard_pr_worktree` (force-remove worktree + delete orphan branch, the rollback shared by the race-re-check and layout-failure paths).
- #8: Extracted `worktree_path(repo, slug)` into `worktree_spawn.py` — it resolves the inline/sibling layout via `_resolve_layout` and computes the path. Both `spawn_worktree` and `projects_pr_review` now use it. PR-review worktrees previously hardcoded the sibling layout, silently ignoring an `inline`-layout repo; they now honor it.

## Verification
```
$ uv run pytest -q
........................................................................ [ 23%]
........................................................................ [ 46%]
........................................................................ [ 70%]
........................................................................ [ 93%]
...................                                                      [100%]
307 passed in 3.78s
```

## Notes
- Approach approved by Tom: decompose in-file (helpers stay in `routes/projects.py`, so the `gh`/`fetch`/`worktree add` `_run` calls keep their `periscope.routes.projects._run` namespace and the ordered `_pr_review_run_sequence` test mocks are untouched) + share a `worktree_path` helper.
- **Mid-implementation correction surfaced to Tom:** my proposal claimed `test_projects.py` would be untouched. On reading `_resolve_layout` I found it does settings I/O + a `git worktree list`, so routing `worktree_path` through it would add a real settings write into the otherwise-hermetic `pr_review` tests. Tom approved the corrected plan: full fix + one autouse fixture in `test_projects.py` that stubs `worktree_path` to a `tmp_path` — keeps the 9 tests hermetic; none assert on the worktree path itself.
- Behavior preserved exactly, including the two *non*-shared cleanup paths: the `worktree add` failure still does an inline `branch -D` only (no worktree exists yet to remove), and the `create_project` failure still does `kill-session` only (it deliberately leaves the worktree/branch). Only the two identical `worktree remove --force` + `branch -D` rollbacks (race re-check, layout failure) were centralized into `_discard_pr_worktree` — a helper, not a context manager, since a CM spanning `create_project` would have changed that path's behavior.
- The 404 detail message in `_resolve_pr_metadata` now interpolates the realpath'd `repo` rather than the user-typed `body.repo` — an immaterial change to an error string (the not-found test asserts only the status code).
