# Codebase Health Check — 2026-05-21

**Scope:** Full repository (`periscope/` backend package + routes, `server.py`, `channel_shim.py`, `static/` frontend, `history/` package, `tests/`, `src-tauri/` Rust shell, build scripts). Excluded: `node_modules/`, `.venv/`, `static/vendor/`.
**Context:** Periscope is a browser dashboard over the host machine's tmux sessions — a FastAPI server plus a vanilla-JS ES-module frontend (deliberately no bundler) that watches every Claude Code pane. Bolted on: a conversation-history indexer (`history/`), an in-process MCP server ("channels"), and an optional Tauri native macOS shell. Active areas: everything. No specific pain points called out.

## Executive Summary

The codebase is in good overall health for a single-user tool: it is unusually well-documented (most non-obvious code already carries a "why" comment), has a clean module split, and shows almost no dead code. The 41 findings cluster around three themes: (1) a handful of functions have grown into monoliths — `parse_pane` most acutely — that are hard to modify safely; (2) several pieces of git/worktree logic and frontend plumbing are copy-pasted across files and have started to drift; and (3) the FastAPI routes use two incompatible error-response conventions, which the frontend then handles inconsistently. None of the findings indicate broken functionality — they are maintainability and consistency debt that will compound as the project grows.

## Finding Counts

| Severity  | Count |
|-----------|-------|
| Critical  | 3     |
| Important | 13    |
| Minor     | 25    |
| **Total** | **41** |

## Findings

### Critical

#### 1. `parse_pane` is a ~235-line god function doing eight independent parsing tasks
- **Category:** Complexity / Confusing Code / Extensibility
- **Location:** `periscope/panes.py:261-496` (notably the question-mark block at `panes.py:409-453`)
- **Details:** `parse_pane` is the single most regression-prone function in the codebase — CLAUDE.md flags it explicitly and it has a dedicated test file. In one ~235-line body it sequentially performs status-line detection, spinner/active-op detection, needs-input footer detection, pending-input + ghost-text filtering, recap extraction, last-meaningful-line extraction, question-mark needs-input detection, and API-error detection, then resolves a state-priority ladder. The question-mark block alone is a ~44-line nested loop-within-loop at 3-4 levels of indentation with subtle control flow (an unconditional `break` after a chrome-skip `continue`, easy to misread as a bug). The chrome-skip predicate (`startswith(("─","❯","⏵"))`, `STATUS_RE.match`, `"github.com/" in line`, `SPINNER_RE.match`) is duplicated verbatim in two places. Recent signals (`api_error`, `asked_question`) were bolted on as more inline blocks. Any change requires full path-tracing.
- **Suggestion:** Extract each detection concern into its own small named helper (`_detect_status`, `_detect_spinner`, `_detect_needs_input`, `_detect_pending_input`, `_extract_recap`, `_last_meaningful_line`, `_detect_asked_question`) operating on the already-split line buffers. Factor the duplicated chrome-skip predicate into one shared helper. `parse_pane` becomes a short orchestrator that assembles the result dict and applies the state-priority ladder. The dedicated test file makes this refactor safe.

#### 2. Git repo/branch resolution sequence copy-pasted across four sites
- **Category:** DRY / Extensibility
- **Location:** `periscope/routes/projects.py:108-118`, `periscope/routes/projects.py:424-441`, `periscope/store.py:227-242` (and the `--abbrev-ref HEAD` branch resolution accompanying each; `projects.py:69-75`)
- **Details:** The exact sequence "resolve repo via `git rev-parse --git-common-dir` (absolutize, `dirname`, realpath), then resolve `base_branch` via `git rev-parse --abbrev-ref HEAD` with the `HEAD`→empty fallback" is copy-pasted verbatim in the v2 migration, `projects_adopt`, and `projects_promote`. The code comments themselves admit the duplication ("same algorithm as the v2 migration", "matches Task 1's migration + adopt endpoints"). This is business logic — how periscope derives a project's repo identity. Project creation is an actively-extended area; each new verb re-copies the block, and a change to the resolution rule (submodules, a new git layout) must be applied to every copy or they silently diverge.
- **Suggestion:** Extract a single helper (e.g. `resolve_repo_for_dir(path) -> (pinned_dir, repo, base_branch)` in `periscope/projects.py` or a small git-helpers module) and call it from the migration and every project-creation route.

#### 3. FastAPI routes split between two incompatible error-response conventions
- **Category:** Inconsistent Patterns
- **Location:** `HTTPException` style — `periscope/routes/projects.py` (~40 sites), `periscope/routes/settings.py:40`, parts of `periscope/routes/sessions.py:221`. `{"ok": False, "error": ...}` style — `periscope/routes/send.py:39`, `periscope/routes/pane.py:121`, `periscope/routes/prefs.py:45`, `periscope/routes/lgtm.py:34`, `periscope/routes/channel.py:18`, `periscope/routes/paste_image.py:54`, `periscope/routes/auto_rename.py:28`, `periscope/routes/cleanup.py`.
- **Details:** The 16 route modules handle client-facing errors two fundamentally incompatible ways: one raises `HTTPException` (HTTP 4xx/5xx + `{"detail": ...}`), the other returns HTTP 200 with `{"ok": False, "error": ...}`. `sessions.py` is internally split — `window_new_worktree` raises `HTTPException` while `window_new`/`session_new`/etc. return `{"ok": False, ...}`. `static/util.js` `apiCall` has to defensively normalize both shapes (`data.error || data.detail`), and several frontend callers handle only one. A developer adding a route has no rule to follow.
- **Suggestion:** Pick one convention, document it in CLAUDE.md, and migrate the minority. Standardizing all error returns on `{"ok": False, "error": ...}` is the smaller migration (nearly all routes already return `{"ok": True, ...}` on success); standardizing on `HTTPException` is the alternative. Either way, eliminate the per-module and intra-`sessions.py` divergence.

### Important

#### 4. `projects_pr_review` is a ~200-line route handler with six rollback paths
- **Category:** Complexity
- **Location:** `periscope/routes/projects.py:530-726`
- **Details:** A single route handler (~197 lines) does repo/PR validation, two separate tmux-collision pre-checks (before and after the `gh` call), a `gh pr view` subprocess + JSON parse, fork/state metadata extraction, name resolution, a `git fetch` with bespoke error-string matching, worktree path resolution, locked `git worktree add` with orphan-branch cleanup, a race re-check, a two-window layout call with rollback, and project-row creation with rollback. It has at least six distinct rollback/cleanup paths with subtly different combinations of `worktree remove` / `branch -D` / `kill-session`.
- **Suggestion:** Extract cohesive steps into helpers (`_resolve_pr_metadata`, `_fetch_pr_branch`, `_create_pr_worktree`) so the handler reads as a linear sequence of validated steps. Centralize the "roll back worktree + branch + session" cleanup in one helper or context manager so the six paths don't each re-spell it.

#### 5. `compute_candidates` carries a deeply nested, fixed-signal per-worktree loop
- **Category:** Complexity / Extensibility
- **Location:** `periscope/cleanup.py:187-317` (signal blocks at `cleanup.py:252-301`)
- **Details:** `compute_candidates` is ~130 lines; its core is a nested `for repo` → `for wt_path, branch` loop whose ~85-line body reaches 4+ levels of indentation. Four staleness signals (PR state, branch-merged, remote-gone, idle) are inlined as `if` blocks with ad-hoc cross-signal coupling (`if not any(s["kind"].startswith("pr_") ...)`). Tracing why a worktree did or didn't become a candidate requires reading the whole loop body, and adding a fifth signal means editing the loop, the `Signal["kind"]` literal-union comment, and any frontend switch.
- **Suggestion:** Extract per-worktree evaluation into a `_evaluate_worktree(...) -> Candidate | None` helper so the outer function is just two loops and an append. If more signals are anticipated, model each as a small predicate function returning `Signal | None` and iterate a list of them.

#### 6. `window_new` mixes mode-dispatch, resume orchestration, and shared spawn logic
- **Category:** Complexity / Extensibility
- **Location:** `periscope/routes/sessions.py:68-194`
- **Details:** `window_new` is ~127 lines handling four cases — legacy `mode` mapping, `mode=resume` with a brand-new session (its own complete early-return path at lines 115-145), `mode=resume` falling through to an existing session, and the plain non-resume spawn. Resume bookkeeping is written in two non-adjacent places. The `mode` parameter is an inline string dispatch (`claude`/`vim`/`shell`/`resume`) that has grown with each window-creation feature. Sibling endpoints (`window_new_worktree`, `_layout_two_window`) re-implement the same "new window + 100ms sleep + send-keys + note_focus/note_action" sequence.
- **Suggestion:** Split into a thin `window_new` dispatcher plus `_spawn_resume_window(...)` and `_spawn_plain_window(...)`. Extract the shared "create window, optionally send a command, stamp focus/action" sequence into one helper used by all spawn endpoints.

#### 7. `_detect_default_branch` reimplemented independently in two modules
- **Category:** DRY / Extensibility
- **Location:** `periscope/worktree_spawn.py:89-106`, `periscope/cleanup.py:52-74`
- **Details:** Both modules define a private `_detect_default_branch(repo)` with the identical algorithm (`git symbolic-ref refs/remotes/origin/HEAD`, fall back to probing for `main`/`master`). The `cleanup.py` copy adds a cache; the `worktree_spawn.py` copy does not. Both are actively-developed areas, so the two copies can drift — a fix in one is invisible to the other.
- **Suggestion:** Keep one canonical `detect_default_branch(repo)` in a shared git-helpers location (or `worktrees.py`); the caching layer can wrap the shared uncached primitive.

#### 8. `projects_pr_review` re-implements worktree creation instead of using `spawn_worktree`
- **Category:** DRY
- **Location:** `periscope/routes/projects.py:647-670` vs `periscope/worktree_spawn.py:149-209`
- **Details:** `projects_pr_review` re-implements worktree-path resolution and `git worktree add` inline (computing `WORKTREES_DIR / repo_name / _slug_for_path(name)`, the `repo_lock` block, `mkdir`, the add, `worktrees_invalidate`) instead of going through `spawn_worktree` — it even imports `WORKTREES_DIR` and `_slug_for_path` as private symbols to do so. The path-layout knowledge now lives in two places; a layout change (e.g. the `inline` layout option `spawn_worktree` already supports) silently won't reach PR-review worktrees.
- **Suggestion:** Extend `spawn_worktree` to accept the "fetch a PR refspec into a pre-named local branch" case (or factor out its path-resolution + locked-add core) so the PR-review endpoint reuses it.

#### 9. Frontend modal lifecycle duplicated across four modal modules
- **Category:** DRY
- **Location:** `static/new-project-modal.js`, `static/review-pr-modal.js`, `static/settings-modal.js:19-80`, `static/cleanup-modal.js:18-92`
- **Details:** All four modal modules contain near-identical copies of `showError`/`clearError`, the `isOpen` guard, and `openX`/`closeX` functions doing the same five operations (`classList.remove("hidden")`, add a body class, `pushEscape`, and the inverse on close). `new-project-modal.js` and `review-pr-modal.js` are almost a line-for-line clone of each other. A change to modal behavior (focus-trap, animation) requires editing four files.
- **Suggestion:** Extract a shared modal-shell helper (open/close lifecycle + body class + escape registration + error-element wiring) that each modal module composes. The two repo-picker modals could also share their discoverable-repos fetch/render logic.

#### 10. Frontend API calls split between the shared `apiCall` wrapper and hand-rolled `fetch`
- **Category:** DRY / Inconsistent Patterns
- **Location:** `static/grid.js:803,822,955,993` (+ `808-810`, `827-829`), `static/new-project-modal.js:97-106`, `static/review-pr-modal.js:85-95`, `static/settings-modal.js:107-116`, `static/cleanup-modal.js:111-120`, `static/modal.js:409`
- **Details:** `util.js` exports `apiCall`, a deliberately-built wrapper that normalizes the `{ok,error}` vs `{detail}` error shapes and surfaces failures via `showToast`. Many call sites instead hand-roll the same `fetch` + `if (!res.ok) { const err = await res.json().catch(() => ({})); ... }` block, inconsistently — `grid.js` `handlePromote`/`handleAdopt` read only `err.detail` and silently drop the `data.error` path. Error-reporting quality depends on which file made the call; a route returning `{ok:false,error}` shows "undefined" in some call sites.
- **Suggestion:** Route all JSON API calls through `apiCall` unless there is a concrete reason (binary upload, streaming). Remove the hand-rolled error blocks; decide whether modal-local `showError` should defer to `showToast` for uniform surfacing.

#### 11. `_migrate_v1_to_v2` runs live tmux/git subprocess I/O at module import time
- **Category:** Confusing Code
- **Location:** `periscope/store.py:134-279`
- **Details:** Importing `periscope.store` triggers `_STATE = _load_state()` → `_migrate_v1_to_v2`, which on a fresh/v1 state file shells out to `tmux list-windows` and multiple `git rev-parse` invocations per session, then writes `state.json`. The module docstring warns import mutates `state.json`, but does not mention import can spawn an unbounded number of `git`/`tmux` subprocesses and block — surprising for what looks like a plain data-layer import, and a hazard for tests or tooling that import the module expecting it to be cheap.
- **Suggestion:** Expand the module/`_migrate_v1_to_v2` docstring to explicitly state that first-load migration shells out to tmux + git and can block. Consider gating the tmux/git walk behind an explicit call rather than an import side effect.

#### 12. Hardcoded `faradayio/fdy` GitHub repo URL in all PR links
- **Category:** Extensibility
- **Location:** `static/grid.js:105`, `static/modal.js:474`, `static/modal.js:765`
- **Details:** Every PR link in the UI is built from the literal `https://github.com/faradayio/fdy/pull/${pr}`. Periscope is explicitly a multi-repo dashboard — panes live in many repos, and `cleanup.py`/`projects.py` already resolve a per-project `repo` path. Any PR badge for a pane working on a non-`fdy` repo links to the wrong GitHub repository. This is a genuine correctness bug, not a localhost convenience, and the literal is repeated in three places.
- **Suggestion:** Derive the GitHub `owner/repo` slug server-side (parse the `origin` remote URL for the pane's repo) and include it in the `/api/state` window view and pane-detail payload; build PR URLs from that field, with the construction centralized in one helper.

#### 13. MCP tool registry is an inline if/elif dispatch plus a parallel hand-maintained schema list
- **Category:** Extensibility
- **Location:** `periscope/channels.py:425-566`
- **Details:** Adding a channel tool requires editing three disconnected regions of `_run_mcp_for_pane`: the `_list_tools()` return list (~120-line inline `types.Tool(...)` schema block), the `_call_tool()` if/elif chain, and a separate `_do_<tool>` function — plus the `CHANNELS_INSTRUCTIONS` docstring as a fourth description. Schema and dispatch are not co-located, so a tool can be registered without a handler or vice versa with no structural guard. Channels is actively extended (notify/link_pr/link_linear/spawn_claude added incrementally).
- **Suggestion:** Introduce a small per-tool registry (name → {schema, handler}) co-locating each tool's schema and implementation; have `_list_tools` and `_call_tool` iterate it. Keep it a plain dict/list of records — no framework — to stay consistent with the project's vanilla style.

#### 14. Every `routes/prefs.py` handler is `async def` despite doing no `await`
- **Category:** Inconsistent Patterns
- **Location:** `periscope/routes/prefs.py:40,57,88,111,123,141,152`
- **Details:** The dominant convention is: handlers that `await` are `async def`; purely synchronous handlers are plain `def`. All seven `prefs.py` mutation handlers are `async def` but contain no `await` — they only call synchronous `periscope.store` functions that take `_STATE_LOCK` and do blocking file I/O. Beyond the inconsistency, this has a real behavioral consequence: FastAPI runs plain `def` handlers in a threadpool but `async def` handlers directly on the event loop, so `prefs.py`'s synchronous `state.json` writes block the event loop while peer modules' identical store writes do not.
- **Suggestion:** Convert the seven `prefs.py` handlers to plain `def` to match every other synchronous route module. State the rule explicitly: `async def` only when the handler awaits.

#### 15. Five non-route `periscope/` modules have no corresponding test file
- **Category:** Naming & Organization
- **Location:** `tests/` — missing `test_cleanup.py`, `test_projects.py`, `test_repo_locks.py`, `test_worktrees.py`, `test_worktree_spawn.py`
- **Details:** CLAUDE.md states the convention: "one `tests/test_<module>.py` per `periscope/<module>.py`." Five source modules — including the non-trivial worktree/project cluster — have no matching test file. A developer following the documented convention to find or add tests for `periscope/cleanup.py` will look for `tests/test_cleanup.py`, not find it, and either assume the module is untested or create a duplicate. The `cleanup.py` / `routes/cleanup.py` name collision compounds this: only `tests/routes/test_cleanup.py` exists, so a file-jump for "the cleanup test" masks that the 317-line subsystem module is the untested one.
- **Suggestion:** Add the missing `tests/test_<module>.py` files, or — if the logic is genuinely covered only through route tests — document that exception explicitly in CLAUDE.md so the mirror convention is not silently violated.

#### 16. `grid.js` has grown to ~1480 lines spanning three distinct responsibilities
- **Category:** Naming & Organization
- **Location:** `static/grid.js:1-1480`
- **Details:** The file's header comment describes only "Grid rendering, /api/state polling, drag-reorder." In practice it contains ~45 internal functions across three separable concerns: grid-view card/session rendering; an entire alternative stream-view renderer (`renderStream`, `passesStreamQuery`, `ensureStreamScaffold`, stream-query persistence); and the usage-meter pill (`fmtTokens`, `meterBar`, `updateUsagePill`). It has only 3 exports and zero section-divider comments, so a reader scrolling 1480 lines has no map. The frontend deliberately favors small single-purpose modules (as `modal.js`/`terminal.js`/`alerts.js` already are); `grid.js` has drifted from that and its name understates its scope.
- **Suggestion:** Extract the stream-view renderer and the usage-pill rendering into their own ES modules; at minimum add section-divider comments. Reconcile the file name/header with the fact that it owns both dashboard views.

### Minor

#### 17. Unused import `get_window` in `cleanup.py`
- **Category:** Dead Code
- **Location:** `periscope/cleanup.py:29`
- **Details:** `from periscope.store import get_window, get_settings` imports `get_window`, but only `get_settings` is referenced. `get_window` is never used.
- **Suggestion:** Drop `get_window` from the import line.

#### 18. Unused import `Any` in `git_pr.py`
- **Category:** Dead Code
- **Location:** `periscope/git_pr.py:24`
- **Details:** `from typing import Any` is imported but `Any` appears nowhere else in the file.
- **Suggestion:** Remove the `from typing import Any` line.

#### 19. Unused import `list_windows` in `pids.py`
- **Category:** Dead Code
- **Location:** `periscope/pids.py:13`
- **Details:** `from periscope.panes import list_windows` is imported but never called — the module operates on `windows` lists passed in by callers.
- **Suggestion:** Remove the unused `list_windows` import.

#### 20. Unused exported function `isLoaded` in `prefs.js`
- **Category:** Dead Code
- **Location:** `static/prefs.js:18`
- **Details:** `isLoaded()` is an exported accessor for `cache.loaded`, but no module imports or calls it.
- **Suggestion:** Remove the function, or confirm it is intentionally retained as part of the prefs API surface.

#### 21. Unused `lastLoadError` accessor + write-only `lastError` variable in `prefs.js`
- **Category:** Dead Code
- **Location:** `static/prefs.js:22` (function), `:16/:35/:40` (variable)
- **Details:** `lastLoadError()` is exported but never imported. Its only purpose is to read `lastError`, which is otherwise write-only — assigned in `loadPrefs()` but never read except by this dead accessor. Callers actually use `loadPrefs()` returning `null` as the failure signal.
- **Suggestion:** Remove `lastLoadError()`, the `lastError` variable, and its two assignments.

#### 22. `renderCard` builds a card via a long imperative `metaParts` sequence
- **Category:** Complexity
- **Location:** `static/grid.js:67-238`
- **Details:** `renderCard` is ~170 lines. The `metaParts` assembly repeats the `if (metaParts.length) metaParts.push(separator)` idiom seven times before each conditional chip. The meta-row, activity-row, status-label, and footer sections each deserve their own helper.
- **Suggestion:** Extract `renderCardMeta(w)`, `renderCardActivity(w, ...)`, and `renderCardFooter(w)` helpers; replace the repeated separator-push idiom with a single `joinWithDots(parts)` helper.

#### 23. `modal.js` is a ~1150-line module spanning many loosely-related responsibilities
- **Category:** Complexity
- **Location:** `static/modal.js:1-1153`
- **Details:** One module handles modal open/close lifecycle, the LGTM tab-strip model (~380 lines), review-pane iframe mounting, the sidebar (PR/Linear cards, notes editor, activity stream), image paste, and inline rename. Each function is reasonably sized and well-commented, but the breadth makes the file hard to navigate; the LGTM tab machinery is a distinct subsystem entangled with the generic modal.
- **Suggestion:** Consider splitting the LGTM tab-strip + review-pane subsystem into its own `modal-review.js` module, mirroring how `terminal.js` is already factored out.

#### 24. Stale-while-revalidate TTL-cache pattern reimplemented across four modules
- **Category:** Complexity / DRY
- **Location:** `periscope/git_pr.py:64-73,272-287`, `periscope/worktrees.py:62-72`, `periscope/cleanup.py:52-134` (four `_X_cache` helpers), `periscope/usage.py:105-114,259-273`
- **Details:** The same TTL-cache-with-background-refresh shape (`with lock: check ts; if stale and not in_flight: refresh; return cached`) is hand-rolled at least six times. Each re-implements lock acquisition, TTL comparison, and the in-flight guard slightly differently — some use a `set` of in-flight keys, some a bool, some recompute synchronously with no guard. A fix to the caching strategy must touch several places.
- **Suggestion:** Introduce one small TTL-cache helper (decorator or tiny class) encapsulating the lock, TTL, and optional background-refresh-with-in-flight-dedup. A judgment call against the project's "small surgical" ethos — the win is consistency.

#### 25. `resolve_pids` is a long multi-phase function under one lock
- **Category:** Complexity
- **Location:** `periscope/pids.py:81-209`
- **Details:** `resolve_pids` is ~128 lines doing four distinct phases under one `_STATE_LOCK`: per-window pid resolution (mint/rebind/reuse + stamp), per-pid `last_seen` refresh with dirty-tracking, window-entry GC, and archived-project GC. Each phase is individually clear but understanding the function requires reading all four.
- **Suggestion:** Extract `_gc_windows(...)`, `_gc_projects(...)`, and `_resolve_one(...)` helpers called within the existing lock scope; the orchestrator keeps the lock, the phases just become named.

#### 26. Per-pane window/pid resolution loop duplicated within `channels.py`
- **Category:** DRY
- **Location:** `periscope/channels.py:142-147` (`_resolve_pid_for_pane`) and `:296-301` (inside `_do_spawn_claude_tool`)
- **Details:** Two copies of the same loop: iterate `list_windows()`, match a window, call `_attach_git_then_resolve_pids([w])`, read `w.get("pid")`. The only difference is the match key (`pane_id` vs `(session, index)`). A code comment explicitly notes the parallel.
- **Suggestion:** Extract a single resolver accepting a predicate (or both lookup keys) returning `(pid, pane_id)`.

#### 27. Tool-result JSON serialization boilerplate repeated 11 times in `channels.py`
- **Category:** DRY
- **Location:** `periscope/channels.py` — `_do_notify_tool`, `_do_link_pr_tool`, `_do_link_linear_tool`, `_do_spawn_claude_tool`
- **Details:** Every tool implementation, on success and each error branch, repeats `return [types.TextContent(type="text", text=json.dumps(body))]` — 11 occurrences. A change (adding `isError` or a structured-content field) would need all 11 updated.
- **Suggestion:** Add a one-line `_tool_result(body: dict)` helper and have each tool `return _tool_result(...)`.

#### 28. `client` TestClient fixture copy-pasted into 14 route-test files
- **Category:** DRY
- **Location:** `tests/routes/test_*.py` — 14 files (`test_auto_rename.py:10`, `test_channel.py:10`, `test_cleanup.py:8`, `test_history.py:8`, `test_pane.py:8`, `test_prefs.py:8`, `test_lgtm.py:8`, `test_paste_image.py:8`, `test_send.py:8`, `test_settings.py:8`, `test_projects.py:9`, `test_ws.py:17`, `test_sessions.py:8`, `test_state.py:8`)
- **Details:** The identical 3-line `client` fixture is duplicated verbatim in 14 route-test files. `tests/conftest.py` exists and already hosts shared fixtures; this one belongs there.
- **Suggestion:** Move the `client` fixture into `tests/routes/conftest.py` (or the top-level conftest) and delete the 14 copies.

#### 29. `_send_to_target` catches `CalledProcessError`, but `tmux()` never raises it
- **Category:** Confusing Code
- **Location:** `periscope/routes/send.py:55-56`
- **Details:** `_send_to_target` calls `tmux()`, which runs `subprocess.run` without `check=True` and never raises `CalledProcessError`. The `except subprocess.CalledProcessError` branch is dead code. A reader assumes tmux failures surface a structured `e.stderr`; in reality a failing tmux command silently returns empty stdout and `_send_to_target` reports `ok: True`.
- **Suggestion:** Either drop the dead branch, or switch `_send_to_target` to `_tmux_mutate` (which surfaces failure) so genuine send failures are detected.

#### 30. `_attach_git_then_resolve_pids` is a query-named helper that performs writes
- **Category:** Confusing Code
- **Location:** `periscope/pids.py:212-220`
- **Details:** The name reads as a read/resolve operation and it is called from query-style endpoints, but it transitively calls `resolve_pids`, which stamps `@periscope_id` onto tmux windows and may write `state.json`. The thin wrapper that most call sites use does not mention persistence (the underlying `resolve_pids` does have a clear docstring).
- **Suggestion:** Add a one-line docstring note that `_attach_git_then_resolve_pids` transitively stamps tmux and may write `state.json`.

#### 31. `git_state_for` parses diff stats with a regex assuming `.group(1)` always succeeds
- **Category:** Confusing Code
- **Location:** `periscope/git_pr.py:53-54`
- **Details:** `int(re.search(r"(\d+) insertion", diff).group(1)) if "insertion" in diff else 0` guards `re.search` with a substring check on the literal word "insertion", then dereferences `.group(1)`. It works only because the substring guard and the regex are kept in lockstep; the pattern is dense (twice on adjacent lines) with no comment explaining why the `None`-deref is safe. A tweak to either silently introduces an `AttributeError`.
- **Suggestion:** Add a brief comment that the `"insertion" in diff` guard makes the deref safe, or restructure so the regex result is checked directly.

#### 32. `smooth_spinner` / `smooth_is_claude` are named as pure transforms but mutate module globals
- **Category:** Confusing Code
- **Location:** `periscope/panes.py:73-94`
- **Details:** Both read like value-returning transforms but each call mutates module-level dicts (`_spinner_last_seen`, `_claude_last_seen`). The hysteresis depends entirely on this hidden per-call state, so calling twice with the same args yields different results — invisible at call sites in `views.py` and `routes/pane.py`.
- **Suggestion:** Add a one-line docstring to each noting it records/expires per-target state as a side effect.

#### 33. `cached_scraped_usage` re-spawns a scrape on every poll after a failure
- **Category:** Confusing Code
- **Location:** `periscope/usage.py:247-273`
- **Details:** `_refresh_scrape_into_cache` only updates `_scrape_cache` when `result` is truthy, so a failed scrape leaves the old `(ts, data)` tuple — including its old timestamp — untouched. `cached_scraped_usage` keeps comparing against that old `ts`, so after a failure it re-spawns a scrape on *every* poll rather than backing off for `USAGE_SCRAPE_REFRESH_S`. The naming implies the timestamp tracks the last attempt; it actually tracks the last success.
- **Suggestion:** Either stamp the cache timestamp on failed attempts too (so backoff applies), or document that failed scrapes intentionally retry every poll until one succeeds.

#### 34. `renderStream` re-sorts the `opened` array in place inside an argument expression
- **Category:** Confusing Code
- **Location:** `static/grid.js:644`
- **Details:** `updateStreamNewTab(visible[0] || opened.sort((a, b) => b.acted_at - a.acted_at)[0])` mutates `opened` (`Array.prototype.sort` is in-place) purely as a side effect of computing a fallback. `opened` is derived fresh each render so it is currently harmless, but the in-place reorder is buried in a dense one-liner.
- **Suggestion:** Sort a copy (`[...opened].sort(...)`) or hoist the fallback into a named `const`.

#### 35. `_layout_two_window` is a non-route layout primitive living in `routes/projects.py`
- **Category:** Confusing Code
- **Location:** `periscope/routes/projects.py:226-294`
- **Details:** `_layout_two_window` is a ~70-line non-route helper (tmux session creation, send-keys, focus stamping, pid minting) in a file whose docstring declares it "Project CRUD endpoints." It raises `HTTPException` directly — coupling a tmux/layout primitive to FastAPI — and does timing-sensitive `time.sleep` work. A reader looking for layout logic would expect it near `worktree_spawn.py`.
- **Suggestion:** Consider moving `_layout_two_window` into a non-route module, and/or note in its docstring that it raises `HTTPException` so the FastAPI coupling is explicit.

#### 36. `build_window_view` relies on order-dependent dict-merge and changes the `pr` field's type by branch
- **Category:** Confusing Code
- **Location:** `periscope/views.py:99-124,135`
- **Details:** The final `view = {**w, **parsed, **git, **pr, ...}` relies on spread precedence for later keys to override earlier ones. Separately, when `linked_pr` is set, `pr["pr"]` is injected as a `str`, whereas auto-detected PRs from `pr_state_for` carry `pr` as an `int` — so the field's type is branch-dependent. `grid.js` happens to treat it uniformly in a template literal, but a reader cannot tell the field's type from one place.
- **Suggestion:** Normalize `pr["pr"]` to a single consistent type regardless of source.

#### 37. `_bg` and `_task` take the `name` argument in opposite positions
- **Category:** Inconsistent Patterns
- **Location:** `periscope/log.py:42` (`_bg(name, fn, ...)`), `periscope/log.py:54` (`_task(coro, name)`)
- **Details:** The two crash-wrapping helpers are documented and used as a matched pair, with call sites side by side in `app.py`, yet `_bg` takes `name` first and `_task` takes it last. A developer who internalizes one signature will pass arguments wrong to the other; since `name` is only a log label, the mistake produces no error, just mislabeled crash logs.
- **Suggestion:** Make the parameter order consistent across both wrappers; update the single divergent signature plus its call sites.

#### 38. `routes/cleanup.py` overloads the `ok` key to mean "the batch ran", not "the operation succeeded"
- **Category:** Inconsistent Patterns
- **Location:** `periscope/routes/cleanup.py:107,130,132`
- **Details:** `cleanup_archive` always returns `{"ok": True, "archived": [...], "failed": [...]}` even when every item failed — `ok` here means "the batch ran", a different meaning than every other route (where `ok` means the operation succeeded). Per-item failures are `{"pinned_dir": ..., "error": ...}` dicts, a third error shape. A frontend caller checking `data.ok` treats an all-failed batch as success.
- **Suggestion:** Bulk operations are legitimately different, but the overloaded `ok` key is the problem. Consider omitting `ok` for batch endpoints (let `archived`/`failed` lengths speak) or documenting the batch-vs-operation distinction.

#### 39. `periscope/views.py` has high fan-in, importing private state from sibling modules
- **Category:** Extensibility
- **Location:** `periscope/views.py:18-30`
- **Details:** `build_window_view` reaches directly into underscore-prefixed internals of other modules: `channels._CHANNELS_LOCK/_CHANNEL_ALERTS/_CHANNEL_UNREAD/_MCP_SESSIONS` and `panes._acted_at/_completed_at/_focused_at/_prev_state` — and both reads and mutates `panes`' private dicts. Some coupling is expected at the central assembly point, but depending on internals (rather than accessor functions, as `store.py` deliberately provides) means any refactor of channels'/panes' internal state cascades into `views.py` and the test `conftest.py` rebind list.
- **Suggestion:** Give `channels.py` and `panes.py` small read/mutate accessor functions for the state `views.py` needs, mirroring `store.py`'s accessor API.

#### 40. `periscope/views.py` is a one-function module whose generic name does not signal its purpose
- **Category:** Naming & Organization
- **Location:** `periscope/views.py:1-156`
- **Details:** `views.py` exports exactly one function, `build_window_view`. In a FastAPI codebase "views" connotes route handlers; this module contains none — it is the per-pane assembly helper extracted from `routes/state.py`. The generic name invites unrelated assembly helpers, risking junk-drawer drift, and reads oddly next to the real `routes/` directory.
- **Suggestion:** Rename to something naming the actual responsibility (e.g. `window_view.py` or `state_assembly.py`).

#### 41. `.empty-mcp.json` sits at the repo root with no documented consumer
- **Category:** Naming & Organization
- **Location:** `/Users/tom/dev/periscope/.empty-mcp.json`
- **Details:** A dotfile config artifact lives at the repo root with no explanation in CLAUDE.md, README.md, or an obvious code reference. Its name suggests a deliberately-empty MCP config (likely passed to spawned Claude panes), but a reader cannot tell what reads it or whether it is safe to delete.
- **Suggestion:** Document the file's purpose and consumer in CLAUDE.md (or a comment at the spawn site), or move it into a clearly-named location.

## Category Summaries

### Dead Code
Very clean. Only five Minor findings — three unused imports and two unused frontend exports (`isLoaded`, `lastLoadError`). No orphan modules, no unreachable code, no commented-out blocks, no phantom dependencies, no constant feature flags. All `periscope/`, `routes/`, `static/`, `history/`, and `src-tauri/` files are reachable.

### Complexity
The standout category. `parse_pane` (Critical) is a ~235-line monolith doing eight independent parsing jobs and is the most regression-prone code in the project. Three more route/subsystem functions — `projects_pr_review`, `window_new`, `compute_candidates` — have grown into 120-200 line monoliths that mix multiple responsibilities. Several Minor items (`renderCard`, `modal.js`, `resolve_pids`) are long but coherent.

### DRY Violations
The git repo-resolution sequence is copy-pasted across four sites with comments openly admitting the duplication (Critical). Other extractable duplication: a second `_detect_default_branch` implementation, `projects_pr_review` bypassing `spawn_worktree`, four cloned modal modules, hand-rolled `fetch` error handling beside the `apiCall` wrapper, and a TTL-cache pattern reimplemented six times.

### Confusing Code
The codebase is unusually well-commented — most non-obvious behavior already carries a "why" comment matching the project's stated convention, and no finding rises to Critical (no name means the opposite of its behavior, no comment contradicts code). The one Important item is import-time subprocess I/O in `store.py`. The remaining Minor items are genuine but small friction points: query-named functions with hidden writes, dense regex coupling, and a usage-scrape retry-storm after failure.

### Extensibility
The hardcoded `faradayio/fdy` GitHub URL is a real multi-repo correctness bug (Important). The MCP tool registry's three-place inline dispatch is the main rigidity in an actively-extended area. `views.py`'s dependence on sibling-module private state is the notable coupling. YAGNI is otherwise well-respected — no speculative abstractions found.

### Inconsistent Patterns
The FastAPI error-response split (Critical) — `HTTPException` vs `{"ok": False, "error"}`, with `sessions.py` internally inconsistent — is the most impactful finding here, forcing defensive normalization in the frontend. `prefs.py`'s all-`async`-no-`await` handlers are both a convention break and a real event-loop-blocking bug. Two Minor items round it out.

### Naming & Organization
Mostly healthy: `config.py` and `util.js` are coherent leaf modules (not junk drawers), and the `periscope/` flat layout and `routes/<name>.py` pairing are the documented intentional structure. The two real issues: `grid.js` has drifted to ~1480 lines owning three concerns, and five non-route modules break the documented one-test-per-module mirror. `views.py`'s generic name and an undocumented `.empty-mcp.json` are Minor.
