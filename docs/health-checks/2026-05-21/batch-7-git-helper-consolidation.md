# Batch 7: Git helper consolidation

**Date:** 2026-05-21
**Classification:** architectural
**Commit:** `95ac5fc`

## Findings Resolved
- #2: Created `periscope/gitutil.py` with `resolve_repo(pinned_dir)` and `resolve_repo_and_branch(pinned_dir)`. The `git rev-parse --git-common-dir` → repo + `--abbrev-ref HEAD` → branch sequence — previously copy-pasted verbatim in the v2 migration (`store.py`), `projects_adopt`, and `projects_promote` — is now a single helper those three sites call.
- #7: `gitutil.detect_default_branch(repo)` is the one canonical default-branch resolver. `worktree_spawn.py`'s local `_detect_default_branch` was deleted; `cleanup.py`'s `_detect_default_branch` is now a thin 5-min-TTL cache wrapper over `gitutil.detect_default_branch`. `routes/projects.py` (which imported the `worktree_spawn` copy) now imports from `gitutil`.

## Verification
```
$ uv run pytest -q
........................................................................ [ 23%]
........................................................................ [ 46%]
........................................................................ [ 70%]
........................................................................ [ 93%]
...................                                                      [100%]
307 passed in 3.82s
```

## Notes
- Approach approved by Tom: new `periscope/gitutil.py` module (vs. adding the helpers to `git_pr.py`). `gitutil` imports only `tmux._run` + stdlib, so it is safe for the import-time-sensitive `store.py` migration to use; `store.py` lazy-imports it inside `_migrate_v1_to_v2` alongside the existing lazy `_run`/`list_windows` imports.
- Scope decision: `routes/cleanup.py` has a 4th copy of the `--git-common-dir` block, but it is a conditional fallback with distinct branch-capture semantics and was not in finding #2's listed sites — left untouched (touch only what the finding describes).
- Verified safe to move: the migration / `projects_adopt` / `projects_promote` / `projects_create` repo-resolution paths have **no tests** (all 9 `test_projects.py` tests are `pr_review`, which this batch does not touch), so relocating their `_run` calls into `gitutil` breaks no mock surface. 307/307 confirms.
- Behavior is identical for all realistic inputs. One deliberate normalization: `detect_default_branch` always passes `timeout=3.0` to its `_run` calls (matching `cleanup.py`'s prior choice); `worktree_spawn.py`'s default-branch detection previously used `_run`'s default timeout — for the instant `git symbolic-ref`/`git branch` operations this is immaterial.
