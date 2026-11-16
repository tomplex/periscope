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
- **A fresh pane's cwd lies.** `pane_current_path` is the FOREGROUND process's
  cwd, and while a new pane's shell runs its rc that can be a subshell
  elsewhere (oh-my-zsh's `cd $ZSH` reads as `~/.oh-my-zsh` ~60ms in, sometimes
  as `""`). Two readers got bitten. The open's tag loop left the new shell (and
  under load the agent pane) untagged, so `ensure_session` returns the pids it
  created and `_open_path` tags those by pid. And create-or-focus missed the
  pair it had just made, so a repeat open in that window (opening the same
  dir again a moment later) spawned a second pair; `_layout_two_window`
  now stamps `@periscope_opened` (`"<epoch> <dir>"`) next to `@periscope_id`,
  and `_sits_at` counts it as the window's dir for `_OPENED_TRUST_S` (60s).
  Only that long: the agent window outlives its agent (quit claude, `cd`
  elsewhere, start another), and a record trusted forever would focus that
  pane on every later open of the first dir. `_sits_at` is the open's one
  test of "at this dir" (the tag loop's cwd claim uses it too), and it reads
  an empty cwd as unknown: `realpath("")` is the server's own cwd, and prod
  runs from the periscope checkout, so opening that checkout focused or
  claimed any project's fresh pane reading `""`.
- **Create-or-focus is check-then-create, so it runs under a per-dir lock.**
  The record only helps a repeat that starts after the first open returned.
  A double submit is two concurrent POSTs (`open_endpoint` is a sync `def`, so
  each runs on its own threadpool thread), and the MCP `open` tool races ⌘K
  the same way. The agent window gets its `@periscope_id` only as
  `_layout_two_window`'s last step, so a second check landing mid-layout saw
  no agent at the dir and built a second pair. `_open_path` holds
  `repo_locks.open_lock(toplevel)` across `ensure_project` + `ensure_session`.
  `resolve_worktree_session` (MCP `spawn_claude` with `workspace="new"`, and
  every commander spawn) takes it too, around its own `ensure_project` and
  session-name dedupe: it inserts the same project row, so a spawn racing a
  threadpool open (⌘K, `/api/workspaces/spawn`) of a dir neither had
  registered died "project already exists". Those MCP handlers run one at a
  time on the event loop (so two spawns never race each other), and can wait
  out a ⌘K open's layout; nothing done under the lock waits on the loop, so
  they can't deadlock. It is its own namespace, not `repo_lock(toplevel)`: a main checkout's
  toplevel IS its repo, and the open must not queue behind (or, being
  non-reentrant, deadlock with) the repo's git lock. `OpenOmnibox.post()`
  also drops a pick while one is in flight.
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
  `place_session_in_rail` places every stamped pane of a session: the open,
  the `spawn_claude` tool and `branch_spawn` (prepare, Linear start) use it.
- **Placement is ordering, not visibility.** `mergeLiveAndPrefs` already
  appends live-new tracks and tabs on the next poll, so a genuinely-created
  window shows up regardless. Placement is what makes the order the user chose
  survive. Don't diagnose a missing window by looking at prefs first.
- **`spawn_worktree` checks out an existing branch.** `git worktree add -b`
  fails outright on a branch git already knows, so a branch that exists with no
  worktree used to be unreachable from BOTH surfaces. `_branch_exists` picks
  `worktree add <path> <branch>` (checkout) vs `-b` (fork) accordingly.
- **`spawn_worktree` is idempotent, and that check is the only trustworthy
  one.** git allows a branch in exactly one worktree, so a spawn for a branch
  already checked out returns THAT worktree (`reused: True`) instead of adding.
  It decides under the repo lock off a fresh `git worktree list`, because the
  route's `worktree_for_branch` pre-check runs outside the lock and two clicks
  race straight past it. Before this, the loser of that race died
  `fatal: 'master' is already checked out at …` — a message with no "already
  exists" in it, so it surfaced as a bare 400 — and whenever the branch WAS
  free for that instant, `free_worktree_path` quietly minted the next slug.
  That is where `.worktrees/master-2` … `-5` came from.
- **Nothing answers "where is branch X checked out?" from the cache.**
  `worktrees.for_branch` reads fresh. The 60s cache only learns the branch
  moves periscope itself made; a plain `git switch` inside a worktree moves one
  silently, which made the cached answer wrong in both directions — naming a
  worktree that had drifted off the branch, and missing one that had drifted
  onto it. The cache stays for the 3s poll's affiliation chips.
- **`_layout_two_window` stamps BOTH windows** (claude + shell) so the full
  pane list is known synchronously — `place_in_rail` needs it without waiting
  for the next poll's `resolve_pids` — and the open tags them by those pids.
- **The catalog is repo/worktree-scoped (v1).** `discover_repos` scans each
  `settings.repo_roots` entry one level deep (`~/dev` when unset) and unions
  the registered projects, so a repo opened once by path stays listed
  wherever it lives. Arbitrary non-git dirs 400; ad-hoc live sessions
  outside the discoverable roots are not rail-addable (the old
  `OpenPickerModal` was retired). Widen the catalog if that need returns.
- **Real-tmux tests need a clean `.venv`.** `tests/test_open_ops.py` +
  `tests/test_worktree_spawn.py` spawn real tmux on an isolated `-L` socket
  (`PERISCOPE_TMUX_SOCKET`) with a stub exec (`PERISCOPE_CLAUDE_EXEC`); both
  seams live in `periscope/tmux.py` + `config.py` and are inert in prod.
