# Resolution Plan

**Verification command:** backend batches → `uv run pytest -q`; frontend batches (2, 4, 5) → `npm run build && npx vitest run` (and commit the rebuilt `static/dist/app.js`)
**Date:** 2026-06-05

## Won't Fix

### #14: PaneHeader chip-row refactor
Cosmetic decomposition of a working 120-line UI component; refactoring without browser verification carries regression risk that outweighs the readability gain.

### #15: Rail render nesting
The nesting is largely essential to the tree shape; restructuring a 260-line render path risks visual regressions for a cosmetic win.

### #18: Single-window pid-resolution shim
Only 2 occurrences; the project's own extract-on-third-use policy says wait for a third call site.

### #20: `pid` name overload
The fix is a cross-cutting rename touching `store.py` accessors and every frontend `w.pid`; that churn isn't worth it for a confusion already bounded to `pidfile.py`.

## Batches

### Batch 1: CLAUDE.md doc regen (mechanical)
- #1: regenerate backend module table (26 modules), route list (17 files), test-mirror coverage-gap list (add gitutil.py), frontend area table (drop Alerts.jsx, add sidebar/preview/split modules); drop hard-coded counts ("353 tests", "11 modules", util.js export list) in favor of "run `uv run pytest -q`"

### Batch 2: Dead code removal (mechanical)
- #9: remove 8 unused prefs.js exports + any now-orphaned prefs keys (collapsed_sessions, session_order)
- #10: remove dead `setTerminalUrlCallback`, its `urlLinkCallback` variable, and the dead branch in terminalCore.js
- #11: remove unused `alertDialog` export from Dialog.jsx
- #12: remove `_RESUME_RE` from resurrect.py and fix the misleading comment to describe the full-rebuild behavior
- #13: drop the unnecessary `export` keyword from self-wiring overlay open/close helpers

### Batch 3: Backend dedup & consolidation (mechanical)
- #2: consolidate GitHub-slug parsing to one helper in gitutil.py; route git_pr.py's `github_origin` callers through it
- #3: extract one CI-conclusion → canonical-state classifier in git_pr.py; map to glyph/word at the two call sites
- #6: extract `resolve_repo_toplevel_or_400(path)` validator; call from the project-route handlers
- #7: rename the channel-route `pane` query param → `pane_id` to match the rest of the codebase
- #17: extract a `pane_meta(target)` display-message helper; use in pane.py and auto_rename.py (keep ws.py's size query separate)
- #19: expose the config base dir as a public constant from config.py; import it in log.py, store.py, pidfile.py, activity.py

### Batch 4: Delete dead Modal UI (architectural)
Mounted but unreachable — `modalTarget` is never set to a pane by any live code path (grid/card-era artifact, superseded by inline `<Detail>`).
- #21: delete `modal/Modal.jsx`, the `openModal` bridge in poll.js, the `modalTarget` signal in store.js, and the `<Modal/>` mount in main.jsx; browser-verify the split view still opens terminals / review / transcript inline after removal

### Batch 5: Frontend dedup + apiCall (mechanical)
Scoped to live files only (Modal.jsx removed in Batch 4).
- #4: centralize CI glyph → {state, class, label} decode in util.js; consume in Detail.jsx, RailRows.jsx, Sidebar.jsx, filter.js
- #5: extract `pasteImageFromClipboard(event, target, {deliver})` helper; use in Detail.jsx and Transcript.jsx
- #8: route the unhandled/ad-hoc raw `fetch` calls through `apiCall` (fixing the paste "undefined" error-shape bug); leave intentional bypasses (poll.js, prefs.js GET bootstrap, modalRequest.js, terminal-reporting paste paths) with a one-line "why" comment

### Batch 6: `_window_new_resume` dedup (architectural)
- #16: share the common tail (index parse, target build, stamp, `_resuming` set, result dict) across the two arms; verify whether the create-path `exec` omission is intentional or an incidental bug before collapsing
