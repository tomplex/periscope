# Session-anchored rail — code structure

Structure proposal for `2026-06-12-session-anchored-rail-design.md`. No new
source modules: every change lands in an existing file, plus two new test
files. The pure-merge / render split the rail already has is exactly the
right shape for this feature; the work is re-keying the merge and adding one
pure chip helper.

## Assumptions

1. **Dev group renders only when it has live panes.** Membership stays
   live-driven (windows ARE the membership, per the existing merge contract).
   With zero dev windows there is no dev group and no dev "+ New tab" row.
   The spec doesn't say "always render dev"; if Tom wants an always-present
   dev group, that's a one-line change in the merge (unconditionally append
   `MAIN_KEY`) — but it would also need an always-on new-tab target, which
   the projects payload provides, so it's cheap to flip later.
2. **Null-repo project group**: group key = the project's `pinned_dir`,
   group label = `project.name` (falls back to basename of the key). The
   spec says such a project "is its own top-level group" without naming the
   key; pinned_dir is the only stable unique value available.
3. **`__main__` is always in the `/api/state` projects payload** —
   `projects_view` filters on `archived_at` and `__main__` is never
   archivable (`archive_project` raises), and `migrate_v2` always seeds the
   sentinel. The dev new-tab target (`mainProject.tmux_session`) is therefore
   always resolvable client-side.
4. **`POST /api/projects` result carries `repo`** — `NewProjectModal` /
   `ReviewPrModal` already read `result.repo` as a fallback; they just flip
   preference order (see File layout).
5. **The Rail empty state becomes near-unreachable** (any live window now
   produces at least the dev group). The existing empty-state JSX stays as a
   guard for the genuinely-zero-windows case; its copy is updated not to
   claim "worktree-backed sessions" are required.

## File layout

```
periscope/
  projects.py                          ~5 LOC   resolve_project_for_window folds no-match → MAIN_KEY
  routes/sessions.py                   ~35 LOC  _window_new_plain: MAIN_KEY → ~/dev cwd + auto-create
                                                session; window_new_worktree: fold the two 400 checks
  window_view.py                       0 LOC    unchanged (pinned_for_aff guard already MAIN_KEY-aware)
static/src/
  split/railTree.js                    rewrite  merge re-keyed to projects; + groupKeyForWindow,
                                                paneChip, projectLabel, groupLabel; MAIN_KEY replaces
                                                OTHER_REPO_KEY (~220 LOC after)
  split/Rail.jsx                       ~60 LOC  consume projects signal; dev flat-list branch;
                                                syncRailPrefs keeps panes_by_worktree["__main__"];
                                                label sources; MAIN_KEY at the 4 enforcement points
  split/RailRows.jsx                   ~25 LOC  PaneRow chip slot; RepoRow isOther→isDev ("dev" glyph);
                                                WorktreeRow drops isOther (dev has no worktree rows)
  overlays/NewProjectModal.jsx         1 LOC    repoKey: result.repo || wins[0].repo_key (flip order)
  overlays/ReviewPrModal.jsx           1 LOC    same flip
  split/__tests__/railTree.test.js     NEW      vitest for merge + chip + label helpers
tests/
  test_projects.py                     NEW      direct tests for resolve_project_for_window (fold)
  routes/test_sessions.py              update   None-resolver case retired; ~/dev cwd; auto-create;
                                                folded worktree-400 message
  test_window_view.py                  update   any "unmanaged → project_pinned_dir None" assertions
                                                become "__main__"
```

`static/dist/app.js` rebuilt + committed per repo convention. No changes to
`prefs.js` (getters/patchUI already shape-agnostic; `addWorktreeToRail` works
unchanged with project-repo keys). `OpenPickerModal` untouched (spec: known
drift, follow-up).

## Per-module structure

### `static/src/split/railTree.js` — pure functions (rung 1)

Stays a single pure module (no signals, no DOM), consumed by `Rail.jsx` for
render AND `currentMergedOrder()` for drag splices — that dual consumption is
the existing reason for purity and it must hold for the new shape too.

Key signatures:

```js
export const MAIN_KEY = "__main__";          // replaces OTHER_REPO_KEY (retired)

// window → top-level group key. Folds to MAIN_KEY when: no project_pinned_dir,
// pinned_dir === MAIN_KEY, or no row in projectsByPin (archived / delete race).
// Otherwise row.repo, or the pinned_dir itself for null-repo projects.
export function groupKeyForWindow(w, projectsByPin)        // → string

// Same return shape as today — repoOrder ends with MAIN_KEY when dev is live;
// worktreesByRepo[MAIN_KEY] is always [] (dev has no sub-rows);
// panesByWorktree[MAIN_KEY] is the unified dev pid order (no "review" sentinel).
export function mergeLiveAndPrefs(windows, projects, prefRepoOrder, prefWtByRepo, prefPanesByWt)
  // → { repoOrder, worktreesByRepo, panesByWorktree }

// Project-row label: project.name || base_branch || session. Replaces the
// cwd-derived first-pane branch (label churn on cd).
export function projectLabel(project, session)             // → string

// Top-level group label: "dev" for MAIN_KEY; project.name for a null-repo
// project's own group; else basename(groupKey).
export function groupLabel(groupKey, projects)             // → string

// Chip text per the spec's rendering rules, or null (at-pin / nothing to say).
// isDev: window is in the dev group; sessionPrefix: folded ad-hoc session
// name to prefix, or null. Reads w.worktree_affiliation, w.repo_label,
// w.branch, w.cwd.
export function paneChip(w, { isDev = false, sessionPrefix = null } = {})  // → string | null

export function maxSeverity(states)                        // unchanged
export function indexWindowsByWorktree(windows)            // unchanged
```

Rationale: the merge keeps its exact return shape with `MAIN_KEY` woven in
where `OTHER_REPO_KEY` was, so the drag descriptors, `reorderChildren`'s
same-`worktreeKey` drop rule, and `syncRailPrefs` all keep working with key
substitutions instead of new plumbing — the spec's synthetic
`panes_by_worktree["__main__"]` key falls out of the shape for free.
`paneChip` lives here rather than a new file: it is rail-tree display
derivation, pure, ~30 LOC, and shares the one vitest file. `repoLabelFor`
(window-scan based) is deleted, replaced by `groupLabel` (projects-based).

### `static/src/split/Rail.jsx` — component (existing shape, no rung change)

- Reads the `projects` signal (already poll-fed in `store.js`/`poll.js`);
  builds `projectsByPin` and `projectsBySession` maps once per render.
- Passes `projects` into `mergeLiveAndPrefs` (and `currentMergedOrder()` gets
  the same extra arg).
- **Dev branch**: when `repoKey === MAIN_KEY`, render `RepoRow isDev` then a
  flat list of `PaneRow`s from `panesByWorktree[MAIN_KEY]` (windowsByPid map
  built across all windows, since dev spans sessions), each with drag
  descriptor `{ kind: "pane", childKey: pid, worktreeKey: MAIN_KEY }`, then
  one `NewTabRow` whose `worktreeKey` is the `__main__` row's `tmux_session`.
  No `WorktreeRow`, no `ReviewRow`, no `WorktreeMeta` in dev.
- `paneChip` computed per row in the existing render loops:
  `sessionPrefix = (isDev && w.session !== mainProject.tmux_session) ? w.session : null`.
- `wtLabelUniverse` switches to `projectLabel` over the same iteration.
- The five enforcement points swap `OTHER_REPO_KEY` → `MAIN_KEY`; in
  `syncRailPrefs` the stripping changes shape per the spec: `MAIN_KEY`
  excluded from `repo_order` and `worktrees_by_repo`, but
  `nextPanesByWt[MAIN_KEY] = merged.panesByWorktree[MAIN_KEY]` IS persisted.

### `static/src/split/RailRows.jsx` — components (existing shape)

- `PaneRow` gains a `chip` string prop, rendered as
  `<span class="rail-chip">⧉ {chip}</span>` in `pane-row-main` (exact class
  naming free; glyph in the component, text from `paneChip`).
- `RepoRow`: `isOther` renamed `isDev` (still gates draggable + glyph).
- `WorktreeRow` / `ReviewRow`: drop the now-dead `isOther` handling (dev has
  no worktree rows; review sentinel only ever appears under project rows).

### `periscope/projects.py` — functions (rung 1, existing module)

`resolve_project_for_window`: non-empty session with no matching row returns
`MAIN_KEY` instead of `None`. Empty-session early-out keeps returning `None`.
Return type annotation stays `Optional[str]`. ~3 changed lines + docstring.

### `periscope/routes/sessions.py` — functions (rung 1, existing module)

- `_window_new_plain`:
  - `project_key == MAIN_KEY` (now also covers folded unmanaged sessions when
    the caller targets a dead session) → `cwd = os.path.expanduser("~/dev")`.
    Note the resolver is called with `{"session": session}`, so for a live
    unmanaged session the resolver also returns `MAIN_KEY` — pane-cwd
    inheritance for those is gone, per the spec's "acceptable drift".
  - Before `new-window`, `has-session` check; on miss, `new-session -d -s
    session -c cwd -P -F #{window_index}` and use the returned index (the
    same `-P -F` pattern and base-index-1 rationale as `_window_new_resume`).
    Inline in `_window_new_plain`, not shared with the resume path — see
    Decisions.
- `window_new_worktree`: the `if not project_key` and `if project_key ==
  MAIN_KEY` 400s fold into one check with one accurate message
  ("worktree-tab requires a session owned by a pinned project; {session} is
  unmanaged or main").

### `periscope/window_view.py` — no change

`pinned_for_aff` already nulls `MAIN_KEY`; `get_project(MAIN_KEY)` returns
the sentinel row so `project_name` becomes `"main"` for folded windows —
harmless, the frontend labels the group "dev" and never renders main's
project_name.

## Patterns

Used:
- **Pure-core / imperative-shell** (already the rail's shape): all new
  decision logic (`groupKeyForWindow`, `paneChip`, labels, merge) goes in
  `railTree.js`; `Rail.jsx`/`RailRows.jsx` stay wiring + JSX.
- **Sentinel-key substitution**: `MAIN_KEY` inherits `OTHER_REPO_KEY`'s exact
  enforcement points rather than adding a parallel "dev" code path.

Considered and rejected:
- New `chips.js` module — chip derivation is small, pure, and only consumed
  by the rail; a second file would be a one-consumer helper split.
- Separate `dev` field on the merge result (`{ repoOrder, …, dev: {...} }`) —
  would force new drag/sync plumbing; weaving `MAIN_KEY` into the existing
  three maps reuses all of it.
- Server-side group key (emit `rail_group` from `window_view.py`) — the
  frontend already receives `project_pinned_dir` + the full projects list;
  the join is a pure frontend concern and keeping it client-side keeps the
  no-row→dev fallback (poll race) where the race actually lives.
- Shared spawn-window helper between `_window_new_plain` and
  `_window_new_resume` — the resume path interleaves `_resuming` bookkeeping
  and an early return into its create branch; factoring would contort both.

## Test strategy

| Module | Approach |
|---|---|
| `railTree.js` | **vitest unit** (`static/src/split/__tests__/railTree.test.js`, style of `attention.test.js`: tiny `win()`/`proj()` factories). Cases: grouping by project repo (window cd'd away stays put); two projects → one repo group; null-repo project as own group; fold-to-dev for no-row / `__main__` / missing `project_pinned_dir`; dev pinned last; dev flat unified pid order + pref carryover via `panes_by_worktree["__main__"]`; review sentinel only under project rows; `projectLabel` fallback chain; `paneChip` for all four affiliation kinds + dev repo/branch + dev `~`-relative cwd + folded session prefix; old cwd-repo `repo_order` keys silently dropped (prefs self-heal). |
| `Rail.jsx` / `RailRows.jsx` | **Browser-verified** (repo norm — no component test harness). Checklist: chips, dev new-tab spawn into dead main session, drag within dev across folded sessions, dev not draggable, syncRailPrefs persisting dev pane order. |
| `projects.py` | **pytest unit**, new `tests/test_projects.py` against the real store fixtures (`clean_state`): unmatched non-empty session → `MAIN_KEY`; empty session → `None`; matched session → pinned_dir; archived row still matches. (CLAUDE.md flags projects.py as indirectly-covered-only; this starts the direct mirror file while touching it.) |
| `routes/sessions.py` | **pytest route tests**, existing `tests/routes/test_sessions.py` (TestClient + mocked `_tmux_mutate`/`tmux` — the established pattern there; tmux itself is the one boundary this suite has always mocked). Update: the `resolve_project_for_window → None` case becomes a `MAIN_KEY` case asserting `~/dev` cwd; add auto-create-session-on-miss (assert `new-session` issued with `~/dev`); worktree-400 message fold. |
| `window_view.py` | **pytest**, existing `tests/test_window_view.py` — update unmanaged-window assertions (`project_pinned_dir` `None` → `"__main__"`); no new cases needed. |

No structure here forces mocks beyond the suite's existing tmux-subprocess
boundary; all new decision logic is reachable as plain functions.

## Decisions to sanity-check

1. **Dev group only renders when live panes exist** (Assumption 1). The
   alternative — always render dev with its new-tab row — is arguably nicer
   UX and is what "dev pinned to the bottom" might imply. Close because the
   spec is silent and the merge contract ("live windows ARE the membership")
   points the other way. Flipping later is ~3 lines.
2. **Merge return shape unchanged (MAIN_KEY woven in) vs explicit `dev`
   field.** Chose woven-in to reuse all drag/sync plumbing; the cost is that
   `worktreesByRepo[MAIN_KEY] = []` is a slightly odd invariant a reader must
   learn from the module comment.
3. **Inline session-auto-create in `_window_new_plain`** vs extracting a
   helper shared with `_window_new_resume`. Chose inline (~10 LOC dup of the
   `-P -F` pattern) because the resume path's create branch early-returns
   with resume bookkeeping; close because it's the third copy of the
   "tmux returned unexpected index" parse.
4. **`paneChip` in `railTree.js`** vs its own module. Chose colocation (one
   pure rail-derivation module, one test file); close because railTree grows
   to ~220 LOC and chips are conceptually per-row, not per-tree.
5. **New `tests/test_projects.py`** vs adding the fold test to
   `tests/routes/test_projects.py`. Chose the direct mirror file (repo norm:
   `tests/test_<module>.py` per module; CLAUDE.md explicitly asks for direct
   files when touching indirectly-covered modules); close because the route
   file already has all the project fixtures.
