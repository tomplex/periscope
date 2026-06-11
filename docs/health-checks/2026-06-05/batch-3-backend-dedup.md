# Batch 3: Backend dedup & consolidation

**Date:** 2026-06-05
**Classification:** mechanical
**Commit:** `7e1c863`

## Findings Resolved
- #2: removed `git_pr.github_origin`; its callers (git_pr `shared_activity_for`, activity.py, and the 4 tests) now use the single `gitutil.github_slug`. activity.py imports it from the gitutil leaf.
- #3: extracted `_CI_FAILED_CONCLUSIONS` frozenset in git_pr.py, shared by `pr_state_for` and `_gh_run_state`. **Scoped narrow on purpose** — only the failed-conclusion bucket is byte-identical between the two; their "running" and "success/neutral" buckets already differ (PENDING handling; NEUTRAL/SKIPPED → ✓ vs None), so merging those would change behavior. Left un-merged and documented in a comment.
- #6: extracted `resolve_repo_toplevel_or_400()` in routes/projects.py; the two byte-identical validation blocks (projects_create, the PR-project route) now call it. The two *partial* copies (adopt route, lines ~70/94) have different messages/shape and were intentionally left alone.
- #7: renamed channel-route query param `pane` → `pane_id` (both endpoints + error messages); updated frontend callers (Inspector.jsx ×2, Modal.jsx ×1) and the 6 test calls. Error-message assertions still pass ("pane_id" contains "pane").
- #17: extracted `tmux.pane_meta(target) -> (window_name, cwd)`; pane.py and auto_rename.py call it while keeping their distinct error handling (pane swallows → "", auto_rename raises 500). turns.py left alone (queries pane_id, not window_name).
- #19: added `config.config_dir()` — a per-call function (NOT a constant) because tests redirect state by monkeypatching XDG_CONFIG_HOME at runtime (conftest, test_store, test_turns, test_pidfile). log/store/pidfile/activity now call it; removed the now-orphaned `import os` from log.py and activity.py's local `import os`. `ACTIVITY_DB` stays import-time-frozen (value/timing unchanged).

## Verification
```
$ uv run pytest -q
435 passed in 4.85s

$ npm run build
✓ built in 468ms

$ npx vitest run
Test Files  2 passed (2)
     Tests  31 passed (31)
```

## Notes
- One test broke and was fixed: `test_pane_returns_parsed_payload` mocked `periscope.routes.pane.tmux` for the display-message call, but #17 moved that call into `periscope.tmux.pane_meta`. Updated the test to mock the new seam (`pane_meta`) — legitimate, since pane_meta is now the genuine boundary for `(window_name, cwd)`. (The test had hit *real* tmux and returned a live window name, which is how the break surfaced.)
- Pre-existing ty diagnostics in store.py (TypedDict return invariance) and projects.py (realpath overload, unused annotation-only locals) are unrelated to these edits; the project gates on pytest, not ty.
- `static/dist/app.js` changed this time (the #7 `?pane_id=` query string is embedded in the bundle).
