# Unified open (`periscope/open_ops.py` + `routes/open.py`)

TWO UI surfaces materialize work into the rail, and they answer different
questions. Know which one you're touching:

| Surface | Aims at a track? | Reaches things that aren't running? |
|---|---|---|
| Header `+ new` / ⌘K → `OpenOmnibox` | No — opens into the repo-default track | Yes (whole catalog) |
| Per-track `+ New tab` → `LauncherModal` | Yes (`openLauncher(trackId)`) | Yes (catalog branches for that track's repo) |

The omnibox loads `GET /api/open/catalog` (discoverable repos + their
worktrees) and POSTs a *target descriptor* to `POST /api/open`. The launcher
loads the same catalog and POSTs to `/api/window/new` with the track id. The
server owns all dispatch in both cases.

`open_ops.open_target(descriptor)` takes one of three frozen-dataclass
variants and converges branch/PR onto the path case:
- `PathTarget(path)` — resolve git toplevel (400 if non-git) → `resolve_repo`
  (parent repo) → `ensure_project` (register if absent, **never** 409) →
  `ensure_session` → rebuild pane pids → `place_in_rail` → `OpenResult`.
- `BranchTarget(repo, branch)` — open the branch's worktree, or
  `spawn_worktree` then recurse into the path case.
- `PRTarget(repo, pr)` — `fetch_pr_into_worktree`, recurse, then stamp
  `linked_pr`; rolls back the worktree if the open fails after the fetch.

Invariants worth knowing before touching it:

- **Create-or-focus is cwd-based now, and that IS the old footgun.** Everything
  lives in one `MANAGED_SESSION`, so a per-project session name no longer
  exists to key on: `ensure_session` answers "already open?" by cwd ownership
  *within* the shared session. cwd collides (multiple panes per dir), which the
  pre-tracks design deliberately avoided. Consequence: when a pane already owns
  the target cwd, **no window is created** — the call is a pure focus.
- **A focus that isn't visible reads as a no-op.** Because of the above, the
  client MUST select the returned `claude_pid`; `OpenOmnibox.post()` sets
  `railSelection` + `prefs.setLastSelected`. Without it, opening something
  already open did nothing observable at all — the reported "I tried to open
  fdy master multiple times and nothing happened".
- **Rail placement is server-side, and writes the TRACK keys.** `place_in_rail`
  writes `track_order` / `tabs_by_track` (keyed by **track id**, values are
  `@periscope_id` pids) and the route returns the `ui` blob; the omnibox writes
  it into `prefsSignal` via `prefs.setUI`. This killed the old client-side
  ~3500ms `deferRailAdd` poll-wait race. The pre-tracks trio (`repo_order` /
  `worktrees_by_repo` / `panes_by_worktree`) is NOT read by the rail —
  `prefs.js` drops `worktrees_by_repo`, never reads `panes_by_worktree`, and
  honours `repo_order` only as the fallback when `track_order` is unset.
  Writing them persisted nothing once the rail had saved an order once.
- **Placement is ordering, not visibility.** `mergeLiveAndPrefs` already
  appends live-new tracks and tabs on the next poll, so a genuinely-created
  window shows up regardless. Placement is what makes the order the user chose
  survive. Don't diagnose a missing window by looking at prefs first.
- **`spawn_worktree` checks out an existing branch.** `git worktree add -b`
  fails outright on a branch git already knows, so a branch that exists with no
  worktree used to be unreachable from BOTH surfaces. `_branch_exists` picks
  `worktree add <path> <branch>` (checkout) vs `-b` (fork) accordingly.
- **`_layout_two_window` stamps BOTH windows** (claude + shell) so the full
  pane list is known synchronously — `place_in_rail` needs it without waiting
  for the next poll's `resolve_pids`.
- **The catalog is repo/worktree-scoped (v1).** Arbitrary non-git dirs 400;
  ad-hoc live sessions outside the discoverable roots are not rail-addable
  (the old `OpenPickerModal` was retired). Widen the catalog if that need
  returns.
- **Real-tmux tests need a clean `.venv`.** `tests/test_open_ops.py` +
  `tests/test_worktree_spawn.py` spawn real tmux on an isolated `-L` socket
  (`PERISCOPE_TMUX_SOCKET`) with a stub exec (`PERISCOPE_CLAUDE_EXEC`); both
  seams live in `periscope/tmux.py` + `config.py` and are inert in prod.
