# Resolution Plan

**Verification command:** backend batches → `uv run pytest -q`; frontend batches → `npm run build && npx vitest run` (commit the rebuilt `static/dist/app.js`). Renames in Batch 1 touch both → run both.
**Date:** 2026-06-05

## Settled UI lexicon (use in code, docs, conversation)

- **Header** — top bar (Header.jsx): `UsagePill`s, `FilterBar` (search + `all▾`), `+ new` menu, `···` overflow, **fleet summary** (`N windows · N working · N done · N idle · updated …`).
- **Rail** (left, Rail.jsx/RailRows/railTree) — nav tree: **Repo** (◆) → **Worktree** (⌇) → rows. Row kinds: **Claude window** (`*`), **shell window** (`$`), **review row** (`○ review start→`, virtual LGTM), **new-tab action** (`+`). Each row: **status dot** (green=working/yellow=idle/gray=done) + **chips** (PR / Linear / git-stat / count badge). Bottom: **Activity feed**.
- **Detail** (center, Detail.jsx) — focused pane content; **PaneHeader** chip row + two modes: **Transcript view** / **Terminal view**.
- **Inspector** (right, was Sidebar.jsx → Inspector.jsx) — edits focused-pane metadata: **Linked** (PR/Linear), **Notes** (+ tags), **Files**.
- **Split** (Split.jsx) — the 3-column container; whole surface = "the dashboard."
- Domain ids: **`pane_id`** = tmux pane (`%N`); **`pscope_id`** = periscope per-window id (the `@periscope_id` option, was `pid`); **`session_id`** = Claude session (JSONL stem). Always qualify *tmux session* vs *Claude session* — never bare "session."

## Won't Fix

### #14: PaneHeader chip-row refactor
Cosmetic decomposition of a working ~120-line UI component; refactoring without browser verification carries regression risk that outweighs the readability gain.

### #15: Rail render nesting
The nesting is largely essential to the tree shape; restructuring a 260-line render path risks visual regressions for a cosmetic win.

### #18: Single-window pid-resolution shim
Only 2 occurrences; the project's own extract-on-third-use policy says wait for a third call site.

## Batches

(Doc regen is intentionally LAST so it documents the final tree in one pass — Batches 1 and 4 change filenames it references.)

### Batch 1: Terminology renames (mechanical, foundation)
Done first so later batches reference the settled names. Use refactor-mcp / LSP for safe propagation.
- #20: rename periscope per-window id `pid` → `pscope_id` across store.py accessors (`get_window`/`set_window_fields`/…), pids.py, routes, and frontend `w.pid` / `transcriptSeen[pid]` etc. Leave the OS process id in `pidfile.py` as `pid` (that one is correct).
- Rename `src/sidebar/Sidebar.jsx` → `src/inspector/Inspector.jsx` (component + dir + all imports, incl. Detail.jsx).

### Batch 2: Dead code removal (mechanical)
- #9: remove 8 unused prefs.js exports + any now-orphaned prefs keys (collapsed_sessions, session_order)
- #10: remove dead `setTerminalUrlCallback`, its `urlLinkCallback` variable, and the dead branch in terminalCore.js
- #11: remove unused `alertDialog` export from Dialog.jsx
- #12: remove `_RESUME_RE` from resurrect.py and fix the misleading comment
- #13: drop the unnecessary `export` keyword from self-wiring overlay open/close helpers

### Batch 3: Backend dedup & consolidation (mechanical)
- #2: consolidate GitHub-slug parsing to one helper in gitutil.py; route git_pr.py's `github_origin` callers through it
- #3: extract one CI-conclusion → canonical-state classifier in git_pr.py; map to glyph/word at the two call sites
- #6: extract `resolve_repo_toplevel_or_400(path)` validator; call from the project-route handlers
- #7: rename the channel-route `pane` query param → `pane_id` to match the rest of the codebase
- #17: extract a `pane_meta(target)` display-message helper; use in pane.py and auto_rename.py (keep ws.py's size query separate)
- #19: expose the config base dir as a public constant from config.py; import it in log.py, store.py, pidfile.py, activity.py

### Batch 4: Delete dead Modal UI (architectural)
Mounted but unreachable — `modalTarget` is never set to a pane by any live code path (grid/card-era artifact, superseded by inline Detail).
- #21: delete `modal/Modal.jsx`, the `openModal` bridge in poll.js, the `modalTarget` signal in store.js, and the `<Modal/>` mount in main.jsx; browser-verify the split view still opens Transcript / Terminal / review inline after removal

### Batch 5: Frontend dedup + apiCall (mechanical)
Scoped to live files only (Modal.jsx removed in Batch 4; Inspector.jsx is the renamed Sidebar).
- #4: centralize CI glyph → {state, class, label} decode in util.js; consume in Detail.jsx, RailRows.jsx, Inspector.jsx, filter.js
- #5: extract `pasteImageFromClipboard(event, target, {deliver})` helper; use in Detail.jsx and Transcript.jsx
- #8: route the unhandled/ad-hoc raw `fetch` calls through `apiCall` (fixing the paste "undefined" error-shape bug); leave intentional bypasses (poll.js, prefs.js GET bootstrap, modalRequest.js, terminal-reporting paste paths) with a one-line "why" comment

### Batch 6: `_window_new_resume` dedup (architectural)
- #16: share the common tail (index parse, target build, stamp, `_resuming` set, result dict) across the two arms; verify whether the create-path `exec` omission is intentional or an incidental bug before collapsing

### Batch 7: CLAUDE.md regen + lexicon (mechanical)
Last — documents the final tree (post-rename, post-Modal-deletion) in one pass.
- #1: regenerate backend module table (26 modules), route list (17 files / 51 routes), test-mirror coverage-gap list (uncovered: gitutil, repo_locks, worktree_spawn, worktrees; route-only: cleanup, projects), frontend area table (drop nonexistent Alerts.jsx; add inspector/, preview/, split/ additions, terminal/theme.js, overlays/modalRequest.js + LauncherModal); drop hard-coded counts in favor of "run `uv run pytest -q`". Add the **UI Lexicon** section from above.
