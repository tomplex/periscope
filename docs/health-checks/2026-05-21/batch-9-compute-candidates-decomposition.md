# Batch 9: compute_candidates decomposition

**Date:** 2026-05-21
**Classification:** architectural
**Commit:** `d908de2`

## Findings Resolved
- #5: Extracted the ~80-line per-worktree loop body of `compute_candidates` into `_evaluate_worktree(wt_path, branch, repo, default, project_by_pinned, windows_snapshot, alive_sessions, idle_threshold) -> Candidate | None`. `compute_candidates` is now setup + two loops + an append; the deeply-nested signal evaluation (project-row match, 4 staleness signals, Candidate build) lives in the named helper, which returns `None` for the repo's main checkout and for healthy worktrees.

## Verification
```
$ uv run pytest -q
........................................................................ [ 23%]
........................................................................ [ 46%]
........................................................................ [ 70%]
........................................................................ [ 93%]
...................                                                      [100%]
307 passed in 3.94s
```

## Notes
- Approach approved by Tom: extract `_evaluate_worktree` only (vs. also splitting the 4 signals into separate predicate functions). The 4 signals are interdependent — signal 2 fires only when no PR signal is present, signal 3 reads `is_fork` discovered by signal 1 — so they are a cohesive sequence, not independent predicates; per-signal functions would have to thread shared state for no real gain. The Extensibility agent's own note rated that further split Minor / "leave as-is, YAGNI line is close."
- Pure code-movement refactor — `_evaluate_worktree`'s body is a verbatim move of the original loop body (the only change: the no-signals/main-checkout `continue` becomes `return None`). Behavior byte-identical.
