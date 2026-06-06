# Codebase Health Check — 2026-06-05

**Scope:** Full codebase — `periscope/` (FastAPI backend, ~8.8k LOC) + `static/src/` (Preact frontend, ~7.6k LOC). Tests excluded from findings except where test organization itself drifts.
**Context:** Periscope is a single-user FastAPI + Preact dashboard over every tmux pane on the host, used exclusively for all of the owner's dev work. Bolted-on subsystems: conversation history indexer, in-process MCP "channels" server, LGTM review mirror, Tauri shell. Active areas: session resurrection/resume, `pane_sessions` DB migration, pane→session transcript mapping, frontend split view. Known pain points going in: untested worktree code, CLAUDE.md drift, unreviewed frontend.

## Executive Summary

The codebase is **healthy and well-factored** — no Critical findings. Large files are decomposed into focused helpers, the intricate parts (parse_pane, the terminal/WebSocket bridge, the MCP shim) are essential complexity with motivating "why" comments, and the documented conventions (HTTPException errors, `_bg`/`_task` wrappers, no `from server import`, `_STATE` discipline) are consistently followed. The 20 findings cluster into three real themes: (1) **CLAUDE.md has drifted substantially from the actual tree** — it's the load-bearing map and it now documents roughly half the code; (2) **a handful of genuine duplications** worth consolidating (GitHub-slug parsing, CI state/glyph decoding, image-paste handlers, repo-toplevel validation); and (3) **partial adoption of `apiCall`** on the frontend, which is worse than none because failure behavior is now unpredictable per call site. Everything else is minor cleanup. No restructuring is warranted — these are targeted fixes, not an architecture problem.

## Finding Counts

| Severity | Count |
|----------|-------|
| Critical | 0     |
| Important| 9     |
| Minor    | 11    |
| **Total**| **20**|

## Findings

### Critical

None.

### Important

#### 1. CLAUDE.md architecture docs have substantially drifted from the code
- **Category:** Naming & Organization
- **Location:** `/Users/tom/dev/periscope/CLAUDE.md:99-165`
- **Details:** The doc is the map the owner (and Claude) navigate by, and it's wrong in six places:
  - **Backend module table omits 13 of 26 modules** (CLAUDE.md:99-116) — absent: `activity.py` (575 LOC, the *largest* module, owns periscope.db + has import-discipline rules), `cleanup.py`, `fs.py`, `gitutil.py`, `projects.py`, `repo_locks.py`, `resurrect.py`, `session_status.py`, `tmux_input.py`, `turns.py`, `window_view.py`, `worktree_spawn.py`, `worktrees.py`.
  - **Route count stale** (CLAUDE.md:116) — says "11 modules", actually 17 router files; missing `alerts`, `cleanup`, `fs`, `healthz`, `projects`, `settings`.
  - **Test-mirror exception list incomplete/wrong** (CLAUDE.md:123-129) — claims "exactly five" modules deviate; actually six — `gitutil.py` (load-bearing, imported at import-time by store.py's migration) is also unmirrored and unmentioned.
  - **Frontend table names a deleted component and omits real ones** (CLAUDE.md:159-165) — lists `overlays/Alerts.jsx` which doesn't exist; omits `sidebar/Sidebar.jsx`, `preview/{PreviewTab,PreviewTabInner}.jsx`, and `split/{Transcript,AttentionSections,...}` + `split/{alertFeed,attention,filesTouched,markdown}`.
  - **Test count stale** (CLAUDE.md:120) — says 353, `pytest --collect-only` reports 435.
  - **util.js export list partial** (CLAUDE.md:165) — omits `escapeHtml`, `waitLabel`, `shortestUniqueSuffix`.
- **Suggestion:** Regenerate the module/route tables mechanically from the actual tree (diff `periscope/*.py` and `routes/*.py` stems; diff against `tests/test_*.py` for the coverage-gap list). Drop hard-coded counts ("353 tests", "11 modules") in favor of "run `uv run pytest -q`" to stop future drift.

#### 2. GitHub owner/repo slug extraction duplicated with drifting regexes
- **Category:** DRY / Confusing Code (merged — both agents flagged this)
- **Location:** `/Users/tom/dev/periscope/periscope/gitutil.py:75` (`github_slug`) and `/Users/tom/dev/periscope/periscope/git_pr.py:183` (`github_origin`)
- **Details:** Both shell out to `git remote get-url origin` and parse the slug with near-identical-but-divergent regexes (`github\.com[:/]+([^/]+/[^/]+?)(?:\.git)?/?$` vs `github\.com[:/]([^/\s]+/[^/\s]+?)(?:\.git)?/?\s*$`). They can already disagree on edge-case URLs, and `git_pr.py` *already imports* `github_slug` from gitutil (line 26) — so the second local parser is easy to miss. A fix or new URL form must be applied twice.
- **Suggestion:** Collapse to the single `gitutil.github_slug` (the import-light leaf); have `github_origin`'s callers use it.

#### 3. CI conclusion classification duplicated within git_pr.py
- **Category:** DRY
- **Location:** `/Users/tom/dev/periscope/periscope/git_pr.py:124` and `:171`
- **Details:** The mapping from GitHub `conclusion`/`statusCheckRollup` values into pass/fail/running buckets is written twice — `pr_state_for` (emits glyphs) and `_gh_run_state` (emits words). A new conclusion value GitHub adds must land in both or the card glyph and the activity-timeline state silently disagree.
- **Suggestion:** One conclusion→canonical-state classifier; let each caller map state to its glyph/word vocabulary.

#### 4. CI glyph → class/label decode reimplemented across 5 frontend sites
- **Category:** DRY
- **Location:** `/Users/tom/dev/periscope/static/src/split/Detail.jsx:220`, `static/src/split/RailRows.jsx:30`, `static/src/sidebar/Sidebar.jsx:82`, `static/src/modal/Modal.jsx:360`, `static/src/filter.js:17`
- **Details:** The server's CI glyph contract (`✓`/`✗`/`⟳`) is decoded independently in 5 components, each switching on the literal glyphs. They already diverge on whether a "running" bucket exists (Modal.jsx:360 collapses unknown to `ci-pending`; RailRows returns empty). A backend glyph change breaks all five.
- **Suggestion:** One `glyph→{state,class,label}` decoder in util.js; each component picks the field it needs.

#### 5. Image-paste clipboard handler duplicated across 3 components
- **Category:** DRY
- **Location:** `/Users/tom/dev/periscope/static/src/split/Detail.jsx:115`, `static/src/modal/Modal.jsx:520`, `static/src/split/Transcript.jsx:271`
- **Details:** The ~15-line clipboard-iterate → guard → `fetch('/api/paste-image?...')` → parse block is copy-pasted three times. Detail and Modal are byte-identical; Transcript diverges only by `&deliver=false`. (See also finding #9 — these copies also carry the same error-shape bug.)
- **Suggestion:** Extract `pasteImageFromClipboard(event, target, {deliver})` returning the parsed result; callers decide how to surface it.

#### 6. Repo-toplevel validation duplicated across project routes
- **Category:** DRY
- **Location:** `/Users/tom/dev/periscope/periscope/routes/projects.py:221`, `:513` (partial copies at `:70`, `:94`)
- **Details:** The "realpath → isdir-or-400 → `git rev-parse --show-toplevel` → not-a-repo-400 → realpath(toplevel)" block appears verbatim (same error strings) at two endpoints plus two partial copies. One rule change must be found in every copy.
- **Suggestion:** Extract `resolve_repo_toplevel_or_400(path)` returning the toplevel or raising; call from each handler.

#### 7. Channel routes name the pane id `pane`; everywhere else it's `pane_id`
- **Category:** Inconsistency
- **Location:** `/Users/tom/dev/periscope/periscope/routes/channel.py:16`, `:29`
- **Details:** `channel_clear_unread`/`channel_push` take `pane: str = Query(...)`, but every other backend site and the frontend call this value `pane_id` (the frontend literally reads `data.pane_id` then sends `?pane=`). The silent rename at the channel boundary invites passing the wrong one of the three pane-ish ids (`pane_id`, `pid`/periscope_id, `target`).
- **Suggestion:** Rename the query param to `pane_id`, or document why it diverges.

#### 8. `apiCall` error-handling wrapper bypassed by raw `fetch` (incl. a user-visible bug)
- **Category:** Inconsistency / Confusing Code (merged)
- **Location:** wrapper at `/Users/tom/dev/periscope/static/src/util.js:98`; bypassed at `static/src/modal/Modal.jsx:445,498,530,564`, `static/src/split/Detail.jsx:131,204,281`, `static/src/split/Transcript.jsx:28`, `static/src/split/alertFeed.js:50`
- **Details:** `apiCall` normalizes both `{detail}` (FastAPI) and legacy `{ok:false,error}` shapes and surfaces errors via a Tauri-safe toast. Adoption is partial — files import and use it for some calls but drop to raw `fetch` for others, several with no `res.ok` check at all (Modal.jsx:498 `/api/rename`, :564 `/api/lgtm/items`). Concrete bug: the paste handlers (Detail.jsx:131, Modal.jsx:536) read `d.error`, but `/api/paste-image` raises `HTTPException` → body is `{detail}`, so a real failure shows "image paste failed: **undefined**". Partial adoption is worse than none — you can't tell from a call site whether failures surface.
- **Suggestion:** Route the unhandled/ad-hoc `fetch` calls through `apiCall`; leave the few intentional bypasses (poll.js, prefs.js GET bootstrap, `modalRequest.js`, terminal-reporting paste paths) with a one-line "why" comment.

#### 9. Eight exported functions in prefs.js are never imported (retired-grid leftovers)
- **Category:** Dead Code
- **Location:** `/Users/tom/dev/periscope/static/src/prefs.js:56,60,137,141,158,205,235,384`
- **Details:** `getSessionOrder`, `getCollapsed`, `setSessionOrder`, `setCollapsed`, `hasAnnotation`, `deleteAnnotation`, `isPinnedFile`, `removeWorktreeFromRail` are exported but referenced nowhere (verified against named + namespace imports and tests). `getCollapsed`'s own comment cites "the grid" — a view CLAUDE.md says was retired. Unused public surface on the persistence boundary.
- **Suggestion:** Remove them and any now-orphaned prefs keys they were the sole reader/writer of (e.g. `collapsed_sessions`, `session_order`), or document any kept as intended future API.

### Minor

#### 10. `setTerminalUrlCallback` export and its guarded branch are dead
- **Category:** Dead Code
- **Location:** `/Users/tom/dev/periscope/static/src/terminal/terminalCore.js:77` (branch at `:175`)
- **Details:** Setter never imported/called, so `urlLinkCallback` stays `null` forever and the `:175` branch never executes — URL clicks always fall through to `openExternal`. Its sibling `setTerminalFileCallback` *is* used, so this is a one-off orphan.
- **Suggestion:** Remove the setter, the variable, and the dead branch — or wire it up if per-pane URL handling was intended.

#### 11. `alertDialog` exported from Dialog.jsx but never used
- **Category:** Dead Code
- **Location:** `/Users/tom/dev/periscope/static/src/overlays/Dialog.jsx:62`
- **Details:** `confirmDialog` and `promptDialog` are consumed; `alertDialog` has no call site anywhere.
- **Suggestion:** Remove it, or note if kept for API symmetry.

#### 12. `_RESUME_RE` compiled but unused; its comment is misleading
- **Category:** Dead Code
- **Location:** `/Users/tom/dev/periscope/periscope/resurrect.py:39`
- **Details:** `_RESUME_RE` is never referenced. The comment (`:97-99`) claims the rewrite "drops any pre-existing --resume," but `_rewrite_line` reconstructs the command from scratch (extracting only channel flags), so the strip never happens — the constant is dead and the comment describes behavior that doesn't exist.
- **Suggestion:** Delete `_RESUME_RE`; reword the comment to describe the actual full-rebuild.

#### 13. Several frontend overlay open/close helpers use `export` unnecessarily
- **Category:** Dead Code
- **Location:** `/Users/tom/dev/periscope/static/src/modal/Modal.jsx:640` (and CleanupModal.jsx:20, CommandsModal.jsx:22, NewProjectModal.jsx:23, ReviewPrModal.jsx:20, SettingsModal.jsx:17, OpenPickerModal.jsx:25)
- **Details:** `closeModal`, `openCleanupModal`, etc. are `export`ed but referenced only within their own files (DOM listeners / `window.__periscopeOpenPicker`). Functions are live; only the `export` keyword is dead, signaling an external consumer that no longer exists.
- **Suggestion:** Drop the `export` keyword; keep the functions.

#### 14. `PaneHeader` builds its chip row as a long imperative push-array
- **Category:** Complexity
- **Location:** `/Users/tom/dev/periscope/static/src/split/Detail.jsx:143-262`
- **Details:** ~120-line component: one `parts` array mutated by 9+ sequential `if` blocks, each re-implementing separator logic, with a cwd-tail slug heuristic and an async reveal handler inline. Readable but error-prone to extend (adding a chip means threading separators by hand).
- **Suggestion:** A declarative chip list (predicate + render per entry), or extract the cwd-tail heuristic and row assembly. Not urgent.

#### 15. `Rail` render carries 4-level nested mapping with inline child-row construction
- **Category:** Complexity
- **Location:** `/Users/tom/dev/periscope/static/src/split/Rail.jsx:164-424`
- **Details:** repo-map → worktree-map → child-loop with per-worktree membership indexing, child-row assembly, and rollup computed inline (`:346-417`). Largely essential to the tree shape; top-of-file helpers are already good.
- **Suggestion:** Extract a `<WorktreeBody>`/`renderWorktree` to flatten the deepest nesting and isolate the review-vs-pane branch.

#### 16. `_window_new_resume` duplicates index-parse / target-build / bookkeeping across two arms
- **Category:** Complexity
- **Location:** `/Users/tom/dev/periscope/periscope/routes/sessions.py:116-200`
- **Details:** Two near-parallel arms (create-session vs add-window) each repeat the `-P -F` index parse + error handling, target build, `_send_and_stamp`, `_resuming` set, and a near-identical result dict (create path omits `exec` — looks incidental). The arms must be kept in sync by hand.
- **Suggestion:** Branch only on "created session vs added window"; share the common tail.

#### 17. Pane→(name, cwd) `display-message` resolution repeated across 3 routes
- **Category:** DRY
- **Location:** `/Users/tom/dev/periscope/periscope/routes/pane.py:50`, `routes/auto_rename.py:104`, `turns.py:43`
- **Details:** Three sites build a target, call `tmux display-message -p '#{window_name}\t#{pane_current_path}'`, strip, `partition("\t")`, guard with try/except. pane.py and auto_rename.py are identical. (ws.py:77 is a distinct size/cursor query — correctly separate.)
- **Suggestion:** A small `pane_meta(target)` helper.

#### 18. Single-window pid-resolution shim list duplicated in two routes
- **Category:** DRY
- **Location:** `/Users/tom/dev/periscope/periscope/routes/pane.py:58`, `routes/auto_rename.py:114`
- **Details:** Both build an identical one-element dict list purely to reuse `_attach_git_then_resolve_pids`, then read `one[0]["pid"]`. Only two occurrences — on the YAGNI line.
- **Suggestion:** Extract a single-window wrapper *if/when* a third caller appears (per the codebase's extract-on-third-use policy).

#### 19. Config-dir base path recomputed inline in 5 modules
- **Category:** Extensibility
- **Location:** `/Users/tom/dev/periscope/periscope/config.py:47` (canonical `_XDG`), duplicated at `log.py:23`, `store.py:90`, `pidfile.py:21`, `activity.py:174`
- **Details:** `os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")` appears in 5 modules. config.py already computes this as `_XDG` and is the declared home for cross-cutting paths (log.py already imports config). Changing the convention means editing 5 files; drift would silently split state across two dirs. The activity.py copy is in the recently-written pane_sessions migration path.
- **Suggestion:** Expose the config base as a public constant from config.py; import it in the other four. One-line consolidation, no new abstraction.

#### 20. `pid` names two unrelated identifiers (OS process id vs periscope window id)
- **Category:** Inconsistency / Confusing Code
- **Location:** `/Users/tom/dev/periscope/periscope/pidfile.py:25` (OS pid, `int`) vs `pids.py` / `store.py:328` (periscope window id, `str`)
- **Details:** In pidfile.py `pid` is an OS process id passed to `ps`/`kill`; everywhere else (`get_window(pid)`, `set_window_fields(pid,...)`, frontend `w.pid`) it's the `@periscope_id` window id. Same 3-letter name, two concepts, different types. Local confusion is bounded (OS-pid use is confined to pidfile.py) but a codebase-wide grep conflates them.
- **Suggestion:** Use a distinct name for the window id at the API surface (`wid`/spelled-out `periscope_id`), or keep the OS-pid usage clearly commented as the odd one out.

## Category Summaries

### Dead Code
Small, clean. A cluster of unused exports left over from the retired grid view (prefs.js — Important) plus three minor orphans (`setTerminalUrlCallback` + its dead branch, `alertDialog`, `_RESUME_RE` with a misleading comment) and unnecessary `export` keywords on self-wiring overlays. No orphan files, no commented-out blocks, no unreachable code (ruff clean).

### Complexity
The codebase is exceptionally well-decomposed — the largest files (channels.py, panes.py, terminalCore.js, Modal.jsx) are essential complexity with focused helpers and documented invariants, not flagged. Only three Minor accidental-complexity spots: `PaneHeader`'s imperative chip row, `Rail`'s deep render nesting, and the two-armed `_window_new_resume`.

### DRY Violations
The most actionable theme. Genuine duplications worth consolidating: GitHub-slug parsing (2 copies, drifting), CI conclusion classification (2 copies), CI glyph decode (5 frontend copies), image-paste handler (3 copies), repo-toplevel validation (2+ copies). Two Minor tmux-resolution repeats are on the YAGNI line. The frontend fetch wrappers (`apiCall` vs `modalRequest`) are intentionally distinct and were not flagged as duplication.

### Confusing Code
Very little — the code carries thorough motivating "why" comments per the project's convention. Two real items, both overlapping other categories: the image-paste error-shape bug (shows `undefined`) and the dual GitHub-slug parsers.

### Extensibility
Clean under a YAGNI lens. The MCP tool surface is already a registry, parse_pane delegates to `_detect_*` helpers, prefs is a tidy getter/setter boundary. Only one present-day rigidity: the config-dir path recomputed in 5 modules.

### Inconsistent Patterns
Documented conventions (HTTPException errors, `_bg`/`_task`, no `from server import`, `_STATE` discipline) are followed consistently. Three genuine cross-cutting inconsistencies: the `pane` vs `pane_id` channel-route param, partial `apiCall` adoption, and the `pid` name overload.

### Naming & Organization
Source itself is well-named with accurate docstrings and clear module boundaries. The entire category reduces to one finding: CLAUDE.md has drifted from the tree in six concrete ways and now documents roughly half the code — the highest-value single fix here, since the doc is the navigation map.
