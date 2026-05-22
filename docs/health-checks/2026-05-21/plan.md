# Resolution Plan

**Verification command:** `uv run pytest -q`
**Date:** 2026-05-21

## Won't Fix

### #24: TTL-cache pattern reimplemented across 6 sites
The six caches genuinely differ in refresh strategy (synchronous recompute vs. background refresh; bool vs. set in-flight guards), so a unifying helper would need enough configuration knobs that it would not be clearly simpler than the current explicit per-module code — and the project explicitly favors explicit code over premature abstraction.

### #23: modal.js is a 1150-line module
modal.js is internally well-organized with reasonably-sized, well-commented functions; carving out modal-review.js is a meaningful refactor for a Minor finding with no correctness, testability, or extensibility payoff.

## Batches

### Batch 1: Dead code removal (mechanical)
- #17: Unused import `get_window` in cleanup.py
- #18: Unused import `Any` in git_pr.py
- #19: Unused import `list_windows` in pids.py
- #20: Unused exported function `isLoaded` in prefs.js
- #21: Unused `lastLoadError` accessor + write-only `lastError` in prefs.js

### Batch 2: Comments & doc fixes (mechanical)
- #11: Expand store.py docstring re import-time tmux/git subprocess I/O
- #15: Document the test-mirror exception in CLAUDE.md (5 modules without test files)
- #30: Docstring note on `_attach_git_then_resolve_pids` transitive writes
- #31: Comment on `git_state_for` regex/substring-guard coupling
- #32: Docstring on `smooth_spinner`/`smooth_is_claude` side effects
- #41: Document `.empty-mcp.json` purpose and consumer

### Batch 3: Small correctness & clarity fixes (mechanical)
- #14: Convert prefs.py handlers from `async def` to `def` (event-loop blocking)
- #29: `_send_to_target` silent tmux failure (dead `CalledProcessError` branch)
- #33: `cached_scraped_usage` re-spawns scrape every poll after a failure
- #34: `renderStream` re-sorts `opened` in place inside an argument expression
- #36: `build_window_view` `pr` field type is branch-dependent (int vs str)
- #37: `_bg` and `_task` take the `name` argument in opposite positions

### Batch 4: Mechanical helper extractions (mechanical)
- #22: Extract `renderCardMeta`/`renderCardActivity`/`renderCardFooter` from `renderCard`
- #25: Extract `_gc_windows`/`_gc_projects`/`_resolve_one` from `resolve_pids`
- #26: Extract one pid-resolution helper from the two copies in channels.py
- #27: Add `_tool_result` helper for the 11 repeated TextContent returns in channels.py
- #28: Move the `client` TestClient fixture into a shared conftest

### Batch 5: Relocations & rename (mechanical)
- #35: Move `_layout_two_window` out of routes/projects.py into a non-route module
- #40: Rename `periscope/views.py` to name its actual responsibility

### Batch 6: parse_pane decomposition (architectural)
- #1: Decompose `parse_pane` into per-signal detection helpers + shared chrome-skip predicate

### Batch 7: Git helper consolidation (architectural)
- #2: Extract one repo/branch resolution helper used by the migration + project routes
- #7: Consolidate the two `_detect_default_branch` implementations into one shared helper

### Batch 8: projects_pr_review decomposition & spawn_worktree reuse (architectural)
- #4: Decompose the ~200-line `projects_pr_review` handler, centralize its rollback paths
- #8: Make `projects_pr_review` reuse `spawn_worktree` instead of re-implementing worktree creation

### Batch 9: compute_candidates decomposition (architectural)
- #5: Extract per-worktree evaluation from `compute_candidates`' deeply nested loop

### Batch 10: window_new decomposition (architectural)
- #6: Split `window_new` into a dispatcher + resume/plain spawn helpers; share the spawn sequence

### Batch 11: MCP tool registry (architectural)
- #13: Replace the three-place inline tool dispatch in channels.py with a per-tool registry

### Batch 12: PR URL repo-slug derivation (architectural)
- #12: Derive the GitHub owner/repo slug server-side; remove the hardcoded `faradayio/fdy` URL

### Batch 13: FastAPI error-response convention (architectural)
- #3: Standardize routes on one error-response convention; fix the intra-sessions.py split
- #38: Resolve the overloaded `ok` key in `cleanup_archive`'s batch response

### Batch 14: Frontend modal-shell & apiCall consistency (architectural)
- #9: Extract a shared modal-shell helper (open/close lifecycle, error wiring) for the 4 modal modules
- #10: Route frontend API calls through the shared `apiCall` wrapper

### Batch 15: grid.js module split (architectural)
- #16: Extract the stream-view renderer and usage-pill into their own ES modules

### Batch 16: views.py accessor API (architectural)
- #39: Give channels.py and panes.py accessor functions so views.py stops importing private state
