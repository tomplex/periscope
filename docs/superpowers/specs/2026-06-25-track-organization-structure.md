# Track-based organization — code-structure proposal

**For spec:** `2026-06-25-track-organization-design.md`
**Status:** structure proposal, pending Tom's review (precedes the implementation plan)

This proposes the *shape* of the code: which modules hold the `tracks`/`pane_tracks`
layer, where resolution lives, how the migration is structured, the WS re-key target
type, the frontend rail restructure, and per-module test strategy. No implementation
code — just structure and rationale, against real file paths.

---

## 1. Spec pushback

Two structural assumptions in the spec I'd reconsider before the plan is written.

### 1.1 The `tracks` entity does NOT belong in `state.json` — it belongs in `periscope.db`

The spec (§Data model) calls `tracks` "a new SQLite store, replacing two of today's
ad-hoc stores" and says it resolves "the JSON/SQLite split the 2026-06-24 audit
flagged." Good. But the spec also folds in `projects`, which today is a `state.json`
dict (`store.py`), and CLAUDE.md plus `config.py:72-76` both say `periscope.db` is
"the destination for other persistent state (prefs, projects) that may migrate out of
state.json later." **Commit to that here: the `tracks` entity table lives in
`periscope.db` alongside `pane_tracks`, NOT in `state.json`.** Half-migrating (membership
in SQLite, entity in JSON, the exact split the audit flagged) would reproduce the
problem the spec says it's resolving. This is the right moment to land the entity in
SQLite because we're already rewriting every reader.

Keep in `state.json`: only the `ui` ordering prefs (`repo_order` → renamed track order,
`panes_by_worktree`), which the spec correctly says stay JSON (config-shaped,
user-action-mutated).

### 1.2 Don't put behavior on a `Track` class — keep it value-data + functions

The spec's vocabulary ("the one organizational primitive", lifecycle actions) reads
object-oriented and could tempt a `Track` class owning create/rename/dissolve/teardown.
Resist that. A track is an immutable-ish row with no coupled mutable in-process state and
no polymorphism — the existing `projects.py`/`workspaces.py` precedent is a `TypedDict` +
module-level functions, and tear-down/dissolve are *orchestration over tmux + tables*,
not methods on a row. **Rung 2 (value-data + pure/IO functions), not rung 3 (class).**
A `Track` class would be a class with only `__init__`-plus-helpers — the smell the taste
rules call out. (Detail in §4.)

---

## 2. Assumptions (filling spec gaps)

- **Single-session name** is a new `config.py` constant (e.g. `MANAGED_SESSION = "periscope"`),
  mirroring `USAGE_SESSION_PREFIX` / `MCP_SOCKET_PATH` placement. The spec names "one
  periscope-owned tmux session" but not the name.
- **`loose` track** is a reserved sentinel track id (a named constant `LOOSE_KEY = "loose"`
  in `tracks.py`, mirroring `projects.MAIN_KEY`), auto-created on first non-git tab.
  Non-archivable, non-killable (the `placement_kill_set` refusal that `MAIN_KEY` has today).
- **Repo-default track id** is derived deterministically from the repo path (a track row
  with `repo=<path>`, `name=<basename>`), so re-deriving on a fresh boot finds the same
  row. `resolve_track_for_window` get-or-creates it.
- **`tracks.id`** is a slug like today's `ws_<slug>` / pinned-dir keys — stable, human-ish,
  used directly as the `pane_tracks.track_id` FK and as the rail's top-level key (replacing
  `repo_order` entries). Repo-default tracks key on the repo path (byte-identical-to-projects
  requirement); goal tracks key on a `tk_<slug>` id.
- **Migration ordering**: window-move (physical) runs *before* the `tracks`/`pane_tracks`
  seed (metadata), so the seed reads windows already in the managed session.
- **Branch sub-cluster** is computed entirely frontend-side from `w.branch` (already in the
  payload per spec §Risks); no backend field, no table.

---

## 3. File layout

```
NEW
  periscope/tracks.py              entity + resolution: Track TypedDict, CRUD over the
                                   `tracks` table, resolve_track_for_window, LOOSE_KEY,
                                   placement_kill_set (moved here), seed_tracks (migration helper)
  periscope/migrate_single_session.py
                                   the one-shot physical window-move + flag gate; called
                                   from app.py lifespan. Isolated because it's a one-way
                                   destructive mutation that must be independently testable
                                   under @needs_tmux.
  periscope/routes/tracks.py       APIRouter: create/rename/move-tab/dissolve/tear-down
  tests/test_tracks.py             unit: TypedDict CRUD, resolution ladder (real periscope.db)
  tests/test_migrate_single_session.py  @needs_tmux: real move-window + re-key assertions
  tests/routes/test_tracks.py      route tests for the lifecycle endpoints

CHANGED — backend
  periscope/activity.py            +tracks/pane_tracks DDL in _SCHEMA; +pane_tracks tag
                                   accessors (set/get/map/prune); +tracks-table row accessors
                                   (the SQLite read/write the entity layer in tracks.py calls)
  periscope/config.py              +MANAGED_SESSION constant
  periscope/store.py               delete `projects` dict + accessors; ui prefs key rename
                                   (worktrees_by_repo → drop; track order replaces repo_order)
  periscope/projects.py            DELETE resolve_project_for_window session-fallback;
                                   most callers move to tracks.resolve_track_for_window;
                                   placement_kill_set moves to tracks.py; backfill replaced
                                   by tracks.seed_tracks
  periscope/open_ops.py            ensure_session → one shared session (window, not session);
                                   place_in_rail keys track id not tmux_session; :212 caller
  periscope/worktree_spawn.py      _layout_two_window: new-window in MANAGED_SESSION, not new-session
  periscope/cleanup.py             liveness off session-equality → pane_tracks membership
  periscope/channels.py            spawn_claude three placement modes → pane_tracks tagging
  periscope/routes/ws.py           /ws/pane keyed on pane_id (target type change)
  periscope/routes/sessions.py     bare-session callers (:80,217,319) → resolve by track
  periscope/routes/cleanup.py      kill-set off w["session"]== → placement_kill_set
  periscope/routes/open.py         track tagging on create
  periscope/tmux.py                deliver_input/capture target = pane_id (already %id-capable)
  periscope/app.py                 lifespan: prune_pane_tracks; prod+flag-gated migration call

CHANGED — frontend
  static/src/split/railTree.js     mergeLiveAndPrefs: top-level=track, mid-tier=derived branch
  static/src/split/Rail.jsx        track groups + branch sub-clusters + filter chips
  static/src/split/RailRows.jsx    TrackRow (was RepoRow), BranchRow (was WorktreeRow), dissolve/teardown actions
  static/src/split/Detail.jsx      selection keys off pane_id; review/liveSessions set → pane_id set
  static/src/terminal/terminalCore.js  /ws/pane URL built from pane_id
  static/src/util.js               targetQuery → paneIdQuery (target type change)
  static/src/prefs.js              ui key rename (track order; drop worktrees_by_repo)
  static/src/overlays/OpenOmnibox.jsx + static/src/open/classify.js  "new track" card replaces "new workspace"
```

---

## 4. Per-module structure

### `periscope/activity.py` — tables (rung 1: functions)

Add to `_SCHEMA` (mirroring `pane_workspaces` / `pane_projects` verbatim):

```
CREATE TABLE IF NOT EXISTS tracks (
  id          TEXT PRIMARY KEY,   -- 'loose' | repo path (repo-default) | 'tk_<slug>'
  name        TEXT NOT NULL,
  repo        TEXT,               -- nullable affiliation
  created_at  INTEGER NOT NULL,
  archived_at INTEGER
);
CREATE TABLE IF NOT EXISTS pane_tracks (
  pane_id    TEXT PRIMARY KEY,    -- tmux %id
  track_id   TEXT NOT NULL,
  updated_at INTEGER NOT NULL
);
```

Functions (exact analogues of the existing tag-table set, all `with _LOCK: _conn()`):
- `set_pane_track(pane_id: str, track_id: str | None) -> None`
- `get_pane_track(pane_id: str) -> str | None`
- `pane_track_map() -> dict[str, str]`
- `prune_pane_tracks(alive_pane_ids: set[str]) -> int`
- tracks-table row IO: `insert_track(row) / get_track(id) / all_tracks() / update_track(id, **fields) / archive_track(id) / delete_track(id)`.

**Rationale:** zero new abstraction — these are the same stateless DB functions as
`pane_workspaces`/`pane_projects`, sharing the module's `_CONN`/`_LOCK` singleton. A "store
class" here would be a class with only the connection as state that `_conn()` already owns.

### `periscope/tracks.py` — entity + resolution (rung 2: value-data + functions)

```python
class Track(TypedDict, total=False):
    id: str
    name: str
    repo: str | None          # nullable affiliation
    created_at: int
    archived_at: int | None

LOOSE_KEY = "loose"

def resolve_track_for_window(window: dict) -> str: ...           # the ladder, §5 below
def repo_default_track(repo: str | None) -> str: ...             # get-or-create by repo
def create_track(*, name: str, repo: str | None = None) -> Track: ...
def rename_track(track_id: str, name: str) -> bool: ...
def move_pane(pane_id: str, track_id: str) -> None: ...          # thin: activity.set_pane_track
def dissolve_track(track_id: str) -> None: ...                   # archive/delete row; tabs fall back
def teardown_targets(track_id: str, windows: list[dict]) -> list[tuple[str, str]]: ...  # kill set
def seed_tracks(windows: list[dict]) -> int: ...                 # migration seed (idempotent)
```

All keyword-only past one positional, comprehensive types, no `Any`. `Track` is value-data;
the functions are the behavior — `dissolve`/`teardown` orchestrate `activity` + tmux, they
are not row methods.

**`placement_kill_set` → `teardown_targets`:** today's `projects.placement_kill_set`
(`projects.py:184-211`) is already `pane_id`-based and refuses `MAIN_KEY`. Move it here,
rename, retarget membership to `pane_track_map()` instead of `resolve_project_for_window`,
and refuse `LOOSE_KEY` + repo-default tracks (the new "never mass-kill the catchall"
guard). **Reuse, don't reinvent — it's the spec-blessed tear-down primitive.**

**Rationale:** mirrors exactly how `workspaces.py` relates to `activity.py`'s
`pane_workspaces` table, and how `projects.py` relates to `pane_projects`. One file =
the track concern. ~150-250 LOC, one tightly-coupled concern.

### `periscope/migrate_single_session.py` — one-shot migration (rung 1: functions)

```python
def run_if_needed() -> None:
    """Prod+flag-gated one-shot. No-op unless config.is_prod() and the
    `migrations.single_session_done` flag is unset."""

def _move_managed_windows() -> int: ...   # tmux move-window on #{window_id}, idempotent
def _mark_done() -> None: ...             # set state.json flag after a successful pass
```

**Why its own module, not a function in `tracks.py`:** it's a one-way *physical* tmux
mutation with no natural idempotency key (spec §migration), gated differently from
everything else (prod + persisted flag), and needs an isolated `@needs_tmux` test that
spawns real tmux. Bundling it into `tracks.py` would couple the pure resolution logic
(unit-testable) to a tmux-only integration surface. The window-move loop reuses the
`move-window -s <#{window_id}> -t <session>:` pattern proven in `routes/projects.py:257-278`
(the adopt flow). The `tracks`/`pane_tracks` seed it triggers is `tracks.seed_tracks`,
idempotent the way `backfill_pane_projects` is (skip already-tagged panes).

**Flag home:** `state.json` `settings`-style marker `migrations.single_session_done`,
read/written via `store` accessors — same pattern as `_channels_migration_v1`'s
`channels_migration_v1_done` gate (`store.py`). NOT a new SQLite row; it's a one-bit
config flag and `state.json` already carries migration flags.

### `periscope/routes/tracks.py` — lifecycle endpoints (rung 1: functions + APIRouter)

One `APIRouter`, Pydantic request bodies (typed, no dict-soup), `raise HTTPException` per
the route convention. Endpoints: `POST /api/tracks` (create), `PATCH /api/tracks/{id}`
(rename), `POST /api/tracks/{id}/move-tab` (re-tag), `POST /api/tracks/{id}/dissolve`,
`POST /api/tracks/{id}/teardown` (the destructive confirm path; body carries
`delete_worktrees: bool`, returns the exact kill list so the UI confirm shows what dies).
Mirrors `routes/workspaces.py` + the destructive bits of `routes/cleanup.py`.

### WS re-key target — a single string convention, no new type

The clean structural move is to make `pane_id` (`%id`) the *only* pane address on the
bridge, dropping the `session:index` composite entirely:
- `/ws/pane?pane_id=%56` (drop `session` + `index` query params).
- `tmux.deliver_input` / `capture` / the resize path pass `-t %56` — tmux already accepts
  `%id` for `send-keys`/`capture-pane`/`display-message`/`resize`, and `tmux_mirror.py`
  *already* subscribes and reconciles by `pane_id` (`ws.py:78`, `tmux_mirror.py:416-418`).
- Frontend: `util.targetQuery` (last-colon split) → `paneIdQuery(paneId)`; `terminalCore`
  and `Detail` selection keys carry `w.pane_id` (already in the `/api/state` payload via
  `panes.py:285`) instead of `session:index`.

No new class/type — `pane_id` is a `str` everywhere already. This *removes* the
`session:index` parsing surface rather than adding a type. The one subtlety to flag: the
frontend currently keys selection and pin prefs on `w.pid` (`@periscope_id`), a *different*
identifier from `w.pane_id` (tmux `%id`). The WS re-key touches only the *terminal address*
(`pane_id`); the rail's selection/pin/membership keys stay on `pid`. Keep those two
distinct — conflating them is the trap.

---

## 5. Track resolution — the ladder

`resolve_track_for_window(window) -> str` (always returns a track id — "every tab always
has a home", spec §Membership). Replaces `projects.resolve_project_for_window`:

1. **Explicit tag.** `activity.get_pane_track(window["pane_id"])` → return it if the track
   row exists and isn't archived (mirror `resolve_workspace_for_window`'s stale-tag guard).
2. **Repo-default.** Else derive repo from the window's git state (`w.branch`/cwd → repo
   path, already cached via `git_pr.cached_git_state`); `repo_default_track(repo)`
   get-or-creates the track keyed on the repo path and returns its id.
3. **Loose.** Non-git / branchless → `LOOSE_KEY`.

No `session` read anywhere — this is what deletes the session coupling. **Returns `str`,
never `None`** (unlike today's function): the "always has a home" rule means there's no
null case, which simplifies every caller.

**Bare-session callers** (`routes/sessions.py:80,217,319`, `open_ops.py:212`) call
`resolve_project_for_window({"session": …})` today and rely on the session-match fallback
that's being deleted. Rewrite each to resolve by the **pane's track id** from `pane_tracks`
(they all have a `pane_id` in scope, or can look one up from the window they're acting on),
not by synthesizing a `{"session": …}` dict. Where a caller genuinely needs "the repo this
tab is in" rather than its track, call `git_pr.cached_git_state` directly — that's the real
dependency, and the session-dict was only ever a proxy for it.

---

## 6. Frontend — rail restructure

### `railTree.js` `mergeLiveAndPrefs` (rung 1: pure function, unchanged altitude)

Stays a pure function (it's the textbook case for unit testing, §7). Restructure its
internals:

- **Top-level key = track id** (from a new `w.track_id` field on the payload — the backend
  resolves it once per window in the view build and ships it, so the frontend never
  re-derives the resolution ladder). Replaces `groupKeyForWindow`'s repo/ws/MAIN_KEY
  trichotomy with a single `w.track_id` read. **This collapses three grouping mechanisms
  into one** — the spec's core goal.
- **Mid-tier = derived branch sub-cluster.** Per track, group its tabs by `w.branch`
  (already in the payload). If a track spans ≥2 distinct branches, emit branch sub-clusters;
  at 1 branch, flat. This is pure derivation from live data — no `worktrees_by_repo` pref
  read at all (that pref is **deleted**). Auto-appears/auto-collapses falls straight out of
  "count distinct branches."
- **Ordering prefs:** track order replaces `repo_order`; tab order within a track replaces
  `panes_by_worktree[session]` keyed now by track id. `worktrees_by_repo` is gone.
- **Return shape** becomes `{ trackOrder, branchesByTrack, tabsByBranch }` (or flatten to
  `tabsByTrack` when a track is single-branch). Keep it a plain object — the rail renderer
  maps over it.

The MAIN_KEY rescue (`railTree.js:55-58`) and the `ws:` interleaving (`:101-108`) both
**delete** — there is no catchall and no separate workspace tier; everything is a track,
and the no-tag case is handled *backend-side* by `resolve_track_for_window` always
returning a home.

### Components (rung: presentational functions, unchanged)

- `RepoRow` → **`TrackRow`**: same prop shape, drag descriptor `kind: "track"`. Add
  dissolve vs tear-down to its action menu (two distinct actions per spec §Lifecycle —
  dissolve is the default/safe row action, tear-down is behind a confirm dialog showing the
  kill list returned by `POST /api/tracks/{id}/teardown`).
- `WorktreeRow` → **`BranchRow`**: the mid-tier; now keyed on derived branch, label = branch
  name. Loses `onClose` (a branch sub-cluster is derived, not an entity — you can't close
  it; you tear down the *track* or move/close individual *tabs*).
- **Filter chips**: a new small presentational component above the rail that scopes
  `windows` to a single `track_id` before both the attention sort and `mergeLiveAndPrefs`.
  The attention sort (`attention.js`) stays as-is — it operates on the (now optionally
  filtered) `windows` list; no change to its internals.
- `OpenOmnibox`/`classify.js`: the `workspace` card kind → **`track`** card ("new
  track…"), posting to `POST /api/tracks` instead of `/api/workspaces`. Same drill-in shape.

---

## 7. Test strategy (per module)

| Module | Test | Real vs mocked | Notes |
|---|---|---|---|
| `activity.py` (new tables) | `tests/test_activity.py` (extend) | **real periscope.db** (temp via XDG redirect, existing `conftest`) | set/get/map/prune for `pane_tracks` + tracks-row IO. No mocks — it's SQLite; a mocked DB tests nothing (the Q1-2026 lesson). |
| `tracks.py` | `tests/test_tracks.py` | **real periscope.db**; windows are plain dicts | unit the resolution ladder (tag → repo-default → loose), get-or-create idempotency, `dissolve` fallback, `teardown_targets` refusing LOOSE/repo-default. Directly callable — no tmux needed for resolution. |
| `migrate_single_session.py` | `tests/test_migrate_single_session.py` | **`@needs_tmux`, real tmux** on isolated `-L` socket | **Spec-mandated, must NOT be mocked** (§Risks, the 2026-06-24 lesson). Move a window between sessions, then assert a subsequent subscribe + keystroke + capture all resolve by `pane_id`. Assert the flag gate makes a second `run_if_needed()` a no-op, and `is_prod()`-gating (monkeypatch `config.PORT`). |
| WS re-key (`ws.py`/`tmux.py`) | `tests/routes/test_ws.py` (extend) + a `@needs_tmux` case | **real tmux** for the resolve-by-`%id` path | The migration test above is the integration anchor; add a focused route test that `/ws/pane?pane_id=` resolves. |
| `routes/tracks.py` | `tests/routes/test_tracks.py` | TestClient + real periscope.db | create/rename/move/dissolve/teardown; assert teardown returns the kill list and dissolve kills nothing. |
| `cleanup.py` / `routes/cleanup.py` | `tests/test_*` (extend) + `tests/routes/test_cleanup.py` | real DB; tmux only where already gated | **Regression-critical:** assert the kill set keys on `pane_tracks` membership, NOT `w["session"]==` — with one shared session the old code mass-matches (the destructive-path bug the spec flags). Add a test that two tabs in different tracks but the same (shared) session do NOT co-kill. |
| `channels.py` spawn modes | `tests/test_channels.py` (extend) | existing pattern | assert each placement mode writes `pane_tracks`, not `pane_workspaces`/`pane_projects`. |
| `railTree.js` | new JS unit (alongside `open/classify` unit precedent) | pure, no deps | track grouping, branch sub-cluster appears at 2 branches / collapses at 1, no-tag windows still group (backend supplies `track_id`). The highest-value unit test in the change. |
| `open_ops.py` / `worktree_spawn.py` | `tests/test_open_ops.py` / `tests/test_worktree_spawn.py` (extend) | **`@needs_tmux`** (already gated) | new-window-in-shared-session instead of new-session; `place_in_rail` keys track id. |

**Testability flag (none blocking):** the resolution ladder is a free function taking a
plain `window` dict and reading real SQLite — directly testable, no hard-to-construct
object, no forced mocking. The structure deliberately keeps the tmux-only surface
(`migrate_single_session`, `_layout_two_window`) separated from the pure logic so the bulk
of the change is unit-testable and only the genuinely-physical parts need `@needs_tmux`.

---

## 8. Patterns

**Used:**
- *Functional tag-table* (`pane_tracks`) and *value-data + functions* (`Track` TypedDict +
  `tracks.py`) — direct reuse of the `pane_workspaces`/`workspaces.py` precedent.
- *Frozen-dataclass descriptors* — `open_ops`'s `PathTarget`/`BranchTarget`/`PRTarget` stay
  as-is; the track change doesn't touch the descriptor union.
- *Idempotency-flag-gated one-shot* — `migrate_single_session` reuses the
  `_channels_migration_v1` flag pattern.
- *Reuse of `placement_kill_set`* — the existing pane_id-based, catchall-refusing kill
  primitive becomes `teardown_targets` rather than a new mechanism.

**Considered and rejected:**
- *`Track` class (rung 3)* — rejected: no coupled mutable in-process state; it'd be a class
  with only helpers. Value-data + functions instead.
- *A `TrackStore`/registry class* — rejected: `activity.py`'s `_CONN`/`_LOCK` singleton
  already owns the connection; a store class would re-wrap it. Module functions instead.
- *Migration as a method/function on `tracks.py`* — rejected: couples pure resolution to a
  tmux-only integration surface and a different gate. Own module.
- *A new `PaneAddress` type for the WS re-key* — rejected: `pane_id` is already a `str`;
  the change *removes* the `session:index` composite, it doesn't add a type.
- *An abstract base for "organizational tier"* — rejected: the whole point of the spec is
  that there is now exactly **one** tier (track). No second implementation → no abstraction.

---

## 9. Decisions to sanity-check

1. **`tracks` entity in `periscope.db`, not `state.json`** (§1.1). *Alternative:* keep the
   entity in `state.json` like `projects`/`workspaces` are today. *Close because:* the
   minimal-diff path is JSON (less churn, matches the two stores being folded), but it
   re-creates the JSON/SQLite split the spec says it's resolving. I chose SQLite to land the
   audit fix while we're already touching every reader.

2. **Repo-default track keyed on the repo path** (reusing the project pinned-dir key).
   *Alternative:* a fresh `tk_<slug>` id for repo-default tracks too. *Close because:* the
   spec's "projects byte-identical at cutover" check is far simpler if the repo-default
   track id == today's project key (the rail top-level key doesn't move); a fresh id would
   make every project pane "reorganize," failing the verification target. Chose path-keyed.

3. **Backend resolves `track_id` per window and ships it in `/api/state`**, rather than the
   frontend re-deriving. *Alternative:* frontend reads `pane_tracks` via an endpoint and
   resolves. *Close because:* the frontend already gets `branch`; adding one resolved
   `track_id` field keeps `mergeLiveAndPrefs` a pure function of the payload and avoids a
   second round-trip — but it does mean the resolution ladder runs in the view-build
   fan-out (per-window). Chose backend-resolves; flag if the fan-out cost matters (it's a
   dict lookup against `pane_track_map()`, read once per poll, not per window).

4. **`migrate_single_session.py` as its own module** vs a function in `tracks.py` (§4).
   *Alternative:* `tracks.migrate_single_session()`. *Close because:* it's small and
   track-adjacent, but its gate (prod + physical-flag) and its `@needs_tmux`-only test
   surface differ from the rest of `tracks.py`. Chose separate module for test isolation.

---

## Relevant file paths (absolute)

- Spec: `/Users/tom/dev/periscope-track-org/docs/superpowers/specs/2026-06-25-track-organization-design.md`
- Tag-table precedent: `/Users/tom/dev/periscope-track-org/periscope/activity.py` (`pane_workspaces` ~209-264, `pane_projects` ~267-321, `_SCHEMA` 30-86, `migrate_legacy_pane_sessions` 324-363)
- Entity precedent: `/Users/tom/dev/periscope-track-org/periscope/workspaces.py`, `/Users/tom/dev/periscope-track-org/periscope/projects.py` (`resolve_project_for_window` 153-182, `placement_kill_set` 184-211, `backfill_pane_projects` 215)
- Migration model: `/Users/tom/dev/periscope-track-org/periscope/routes/projects.py` (adopt/promote move-window 213-291)
- WS bridge: `/Users/tom/dev/periscope-track-org/periscope/routes/ws.py` (target 34, subscribe 78), `/Users/tom/dev/periscope-track-org/periscope/tmux.py` (`deliver_input` 77-107, `capture` 51-54), `/Users/tom/dev/periscope-track-org/periscope/tmux_mirror.py` (subscribe 461-477, reconcile-by-%id 416-418)
- Frontend rail: `/Users/tom/dev/periscope-track-org/static/src/split/railTree.js` (`mergeLiveAndPrefs` 72-156), `/Users/tom/dev/periscope-track-org/static/src/split/{Rail,RailRows,Detail}.jsx`, `/Users/tom/dev/periscope-track-org/static/src/util.js` (`targetQuery` 21-28), `/Users/tom/dev/periscope-track-org/static/src/terminal/terminalCore.js` (URL 451-456)
- Gate + lifespan: `/Users/tom/dev/periscope-track-org/periscope/config.py` (`is_prod` 50-59, `ACTIVITY_DB` 76), `/Users/tom/dev/periscope-track-org/periscope/app.py` (lifespan prune + backfill 49-129)
