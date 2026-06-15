# Unified "open" — code structure proposal

Structures the feature in `docs/superpowers/specs/2026-06-15-unified-open-design.md`.
Inputs verified against the live tree (line numbers cited are as-read on 2026-06-15).

## Spec pushback

Two structural assumptions in the spec I'd change before they reach the plan.

### 1. The convergence-on-path dispatch must not live in the route handler

The spec's pseudocode puts the whole `if descriptor.path / elif branch / elif pr`
recursion inside `def open(descriptor)` and draws it inside the `routes/open.py`
box. If that recursion lands in the FastAPI handler it is (a) untestable without
spinning the route and (b) the one piece of genuinely branchy logic in the
feature buried behind HTTP plumbing.

**Alternative:** the dispatch is a plain function `open_target(descriptor) ->
OpenResult` in a new non-route module `periscope/open_ops.py`. The route handler
in `routes/open.py` is a ~10-line shim: parse body → call `open_target` →
catch domain errors → map to HTTP → return. The recursion (`branch` and `pr`
both reduce to the `path` case by re-calling `open_target`) lives in the
testable function. This matches periscope's existing split exactly — every
subsystem is a non-route module and `routes/*.py` is a thin APIRouter over it
(`projects.py` ↔ `routes/projects.py`, `worktrees.py` has no route at all).

### 2. Helpers should raise domain errors; the route maps to HTTP — but the spec's own evidence says `_layout_two_window` resists this

The spec's open question (lines 162-165) asks whether helpers raise domain
errors or `HTTPException`. Recommendation: **`open_ops.py` raises `ValueError`
(non-git, bad input) and a single new `SessionNameCollision` only where the
caller branches on it; `routes/open.py` maps to status codes.** This is already
the house convention — `spawn_worktree` raises `ValueError`, routes catch and
map (projects.py:132-133, 266-267); `create_project` raises `ValueError`.

The wrinkle the plan must own: `_layout_two_window` (worktree_spawn.py:208)
*deliberately* raises `HTTPException(500)` internally and its docstring says so
on purpose. `open_ops.py` calling it would import `HTTPException` into a
non-route module — tolerable (it's already imported across `worktree_spawn.py`
and `gitutil` callers), but it muddies the "domain errors only" line. I am
**not** proposing to de-couple `_layout_two_window` from `HTTPException` in this
feature — that's a yak-shave touching `projects_create`/`projects_pr_review`
that are being retired anyway. Instead: `open_ops.py` lets `_layout_two_window`'s
`HTTPException(500)` propagate untouched (a spawn failure *is* a 500), and uses
`ValueError`/`SessionNameCollision` for the cases it owns. The route catches
`ValueError → 400`, `SessionNameCollision` is handled internally (dedupe, never
surfaces), and `HTTPException` passes through. One mixed convention, documented,
rather than a refactor that isn't this feature's job.

### 3. `SessionNameCollision` is the one custom exception — justify it explicitly

Per the taste rule, a custom exception needs a named caller that branches on its
*type*. Here it is: `ensure_session` must distinguish "recorded session name is
dead" (spawn under that name) from "recorded name is live but belongs to
something unrelated" (dedupe the name, update the project row) — spec lines
138-142. That's a control-flow decision keyed on a specific failure, not a
generic bad-input. Everything else uses built-in `ValueError`. If during
implementation `ensure_session` can make that decision without raising (it
probably can — it's an internal `has-session` check + ownership compare, not an
exception path), **drop `SessionNameCollision` entirely** and use a return-value
discriminant. Flagged in Decisions to sanity-check.

## Assumptions

- **`OpenResult` is a frozen dataclass**, not a dict, despite the spec writing
  the return as `{ tmux_session, repo, claude_pid, ui }`. It crosses the
  `open_ops` → route boundary and the route serializes it; a typed value-object
  there beats a free dict. The route does `asdict(result)` (or builds the
  response model from it) at the HTTP boundary.
- **The descriptor is parsed into a discriminated union at the route boundary**
  (Pydantic), then `open_ops.open_target` receives a typed descriptor — not a
  raw dict. See Per-module structure.
- **`ensure_project` and `place_in_rail` are new functions**, not methods. The
  spec names them as steps; nothing in them owns mutable state (they read/write
  the `store.py` singletons through existing typed accessors).
- **Catalog enumeration reuses `worktrees._cached_worktrees`** but needs a
  branch list too; `_cached_worktrees` returns `[(path, branch)]` per repo
  (worktrees.py:62) — enough for the `worktrees` array, but the `repos` array's
  `branches` field still needs the `git branch` call that `projects_discoverable`
  already does (projects.py:424-432). Assumed both feed the catalog.
- **`OpenPickerModal` is retired** (see Decisions). Its sole job — add an
  already-live session to the rail — is subsumed by "open dir" cards, which the
  server now focuses-not-spawns and rail-places server-side.
- **No new DB tables.** Everything writes through `store.py` (`state.json`:
  projects, windows, ui). The catalog is a read; `activity.db` is untouched.

## File layout

```
periscope/
  open_ops.py                 NEW  — open_target() dispatch + ensure_session, ensure_project,
                                      place_in_rail, worktree_for_branch, build_catalog. The
                                      whole non-HTTP core of the feature.
  routes/open.py              NEW  — APIRouter: POST /api/open, GET /api/open/catalog.
                                      Thin: parse → open_ops → map errors → serialize.
  worktree_spawn.py           EDIT — _layout_two_window stamps BOTH windows (claude + shell),
                                      returns both pids (or a small struct). +~6 lines.
  routes/projects.py          EDIT — extract fetch_pr_into_worktree() (the ~90 inline lines
                                      504-637) into open_ops (or projects.py); delete the
                                      retired POST /api/projects + /api/projects/pr-review
                                      route bodies. adopt/patch/archive/discoverable stay.
  routes/sessions.py          EDIT — delete POST /api/session/new. (Check the file for other
                                      live routes before deleting the module.)
  projects.py                 maybe — fetch_pr_into_worktree may land here instead of open_ops
                                      (it's project-domain orchestration). See Decisions.

static/src/
  overlays/OpenOmnibox.jsx    NEW  — the omnibox modal. Replaces the +new dropdown's targets.
  open/classify.js            NEW  — pure string → candidate-descriptors classifier. No DOM,
                                      no signals. Unit-tested.
  open/__tests__/classify.test.js  NEW — vitest unit test for the classifier.
  chrome/Header.jsx           EDIT — +new becomes a single button that opens the omnibox; the
                                      three <Dropdown> menu items deleted.
  overlays/Overlays.jsx       EDIT — drop the +session imperative handler (lines 27-37); mount
                                      <OpenOmnibox/>; remove NewProjectModal/ReviewPrModal/
                                      OpenPickerModal from the composition.
  overlays/NewProjectModal.jsx     DELETE
  overlays/ReviewPrModal.jsx       DELETE
  overlays/OpenPickerModal.jsx     DELETE
  prefs.js                    reuse — patchUI() is the write path for the returned ui blob.
                                      addWorktreeToRail() + its deferRailAdd callers die with
                                      the modals (verify no other caller first).

tests/
  test_open_ops.py            NEW  — unit/integration for open_target, ensure_session,
                                      worktree_for_branch, build_catalog, fetch_pr_into_worktree.
  routes/test_open.py         NEW  — the route-level scenarios from the spec's Testing section.
  test_worktree_spawn.py      EDIT — add the both-windows-stamped assertion.
  routes/test_projects.py     EDIT — drop pr-review route cases (migrate rollback coverage to
                                      test_open_ops.py); keep adopt/patch/archive/discoverable.
  routes/test_sessions.py     DELETE or trim — session/new gone.
```

Naming note: the spec writes `periscope/open_ops.py`. `open` is a Python
builtin; the module name `open_ops` and the function name `open_target` both
avoid shadowing it. Do **not** name the function `open()`.

## Per-module structure

### `periscope/open_ops.py` — plain functions (rung 1)

No mutable state, no polymorphism, three descriptor variants dispatched by a
match. This is functions, full stop. A class here would be a bag of methods over
the `store.py` singletons.

```python
# Descriptor: a discriminated union. Defined here as frozen dataclasses; the
# route's Pydantic models parse-and-validate into these (or these ARE the
# pydantic models — see Decisions). Keyword-only, fully typed.
@dataclass(frozen=True)
class PathTarget:    path: str
@dataclass(frozen=True)
class BranchTarget:  repo: str; branch: str
@dataclass(frozen=True)
class PRTarget:      repo: str; pr: int
Descriptor = PathTarget | BranchTarget | PRTarget

@dataclass(frozen=True)
class OpenResult:
    tmux_session: str
    repo: str
    claude_pid: str
    ui: dict          # the prefs ui blob, returned for client-side patchUI

def open_target(descriptor: Descriptor) -> OpenResult:
    """Resolve → register → create-or-focus → place. branch/pr reduce to path."""

def ensure_project(toplevel: str, repo: str) -> Project:
    """Register if absent (NO 409). Returns the project row. Idempotent."""

def ensure_session(project: Project) -> tuple[str, str]:
    """Idempotent create-or-focus core. Returns (tmux_session, claude_pid).
    Liveness by project['tmux_session'] via `tmux has-session` — NOT cwd scan."""

def place_in_rail(tmux_session: str, project: Project, pane_pids: list[str]) -> dict:
    """Write the rail pref server-side. Keys: project['repo'] (group) +
    tmux_session (worktree key). Returns the updated ui blob."""

def worktree_for_branch(repo: str, branch: str) -> str | None:
    """Match an ENUMERATED worktree by branch (via _cached_worktrees). Do NOT
    recompute via worktree_path() — slug collisions + _resolve_layout side
    effects make that wrong (spec lines 199-204)."""

def build_catalog() -> dict:
    """GET /api/open/catalog payload: {repos:[...], worktrees:[...]}."""
```

Rationale: every one of these is a stateless transform or an orchestration over
existing singletons. Reuse, by name:
- `gitutil.resolve_repo` (gitutil.py:18, uses `--git-common-dir`), `git_toplevel`,
  `detect_default_branch`.
- `projects.create_project` / `all_projects` / `get_project` (projects.py:74-106).
- `worktree_spawn._layout_two_window` (the create-the-layout primitive — call directly).
- `worktree_spawn.spawn_worktree` (the create-worktree primitive, raises `ValueError`).
- `worktrees._cached_worktrees` (worktrees.py:62) for liveness/enumeration; `worktrees.invalidate` after a `worktree add`.
- `store.set_window_fields` (store.py:335, NOT projects.py — agent-confirmed) for the PR `linked_pr`/`is_fork` stamp after convergence.
- `store.update_ui` (store.py:401) under the hood of `place_in_rail`; `place_in_rail` must return what `get_ui()` returns so the route can hand it back.
- `pids.stamp_new_window` (pids.py:31) — already used by `_layout_two_window`.

`place_in_rail` must replicate the key discipline `addWorktreeToRail` +
`mergeLiveAndPrefs` use (railTree.js:44-130), verified: group key =
`project['repo']` (matches `groupKeyForWindow` returning `row.repo`); worktree
key = tmux **session name** (matches `worktrees_by_repo[repo]` holding session
names); pane list = the stamped pids, plus the `"review"` sentinel for
repo-backed projects (mergeLiveAndPrefs auto-appends `"review"` when
`hasReview`, railTree.js:~120 — so `place_in_rail` can omit it and let merge add
it, OR write it for authority; pick omit, less duplication of the sentinel rule).

### `fetch_pr_into_worktree(repo, pr) -> str` — plain function

The ~90 lines currently inline in `projects_pr_review` (projects.py:504-637):
`gh pr view` metadata, collision pre-checks, `git worktree add` under `repo_lock`,
`_discard_pr_worktree` rollback on every failure step. Lift verbatim, preserving
the rollback semantics. **Returns the worktree path** (the `is_fork`/`linked_pr`
metadata it computes must also surface — return a small frozen result
`PRWorktree(path, is_fork, base_branch)` rather than a bare str, since
`open_target`'s PR branch needs `is_fork` for the post-convergence
`set_window_fields` stamp, spec lines 144-147). `_discard_pr_worktree`
(projects.py:494) moves with it.

Lives in **`projects.py`** (the non-route module), not `open_ops.py` — it's
project-domain worktree orchestration, peer to `create_project`, and a future
caller other than open is plausible. `open_ops` imports it. (Close call —
flagged.)

### `worktree_spawn._layout_two_window` change — same function, broader stamp

Today: stamps `@periscope_id` only on the claude window (line 280); the shell
window (created line 250-253) is unstamped until the next poll's `resolve_pids`.
Change: after creating the shell window, resolve its index and
`stamp_new_window` it too, then return **both** pids. Smallest viable shape:
return `tuple[str, str]` (claude_pid, shell_pid) — but two callers
(`projects_create`, `projects_pr_review`) are being retired, and the docstring
already says "other callers can ignore the return." After retirement the only
caller is `open_ops`. So: return a 2-tuple `(claude_pid, shell_pid)`; if any
surviving caller wants just claude, it unpacks. Keep it a tuple, not a struct —
two homogeneous values, no field-naming payoff. This stays well under the file's
281 lines.

Why both: `place_in_rail` needs the complete `panes_by_worktree` entry
synchronously. The spec notes `mergeLiveAndPrefs` self-heals a partial entry on
next poll (railTree.js:114) — but stamping both keeps the first write
authoritative and avoids a one-poll flicker of a half-populated worktree.

### `routes/open.py` — thin APIRouter (rung 1, functions)

```python
router = APIRouter()

class OpenBody(BaseModel):           # discriminated by which field is present
    path: str | None = None
    repo: str | None = None
    branch: str | None = None
    pr: int | None = None

@router.post("/api/open")
def open_endpoint(body: OpenBody):
    descriptor = _to_descriptor(body)        # validate exactly-one-variant → 400
    try:
        result = open_ops.open_target(descriptor)
    except ValueError as e:
        raise HTTPException(400, str(e))      # non-git path, bad branch, etc.
    return {"tmux_session": result.tmux_session, "repo": result.repo,
            "claude_pid": result.claude_pid, "ui": result.ui}

@router.get("/api/open/catalog")
def open_catalog():
    return open_ops.build_catalog()
```

`HTTPException(500)` from `_layout_two_window` propagates as-is. Registered in
`app.py`'s `include_router` loop like every other route module. `_to_descriptor`
enforces the exactly-one-of {path, (repo+branch), (repo+pr)} invariant — this is
the boundary validation the taste rules want (request entry), and it produces a
typed descriptor so `open_ops` trusts its input.

### Frontend: `static/src/open/classify.js` — pure functions (rung 1)

The input→descriptor classifier, isolated from the DOM so it's unit-testable
(spec lines 260-261, 305-307). Pure: `string → candidate descriptors`.

```js
// query string (+ catalog for repo resolution) → ranked candidate cards.
// Each candidate carries the descriptor it will POST.
export function classify(query, catalog) -> [{ kind, label, descriptor, ... }]
// helpers, all pure:
export function parsePrRef(query) -> { repo?, pr } | null   // PR URL or #N
export function rankRepos(query, repos) -> [...]            // substring + ordering, no fuzzy
```

No class. It's a transform with no state. The `catalog` and live `windows` are
read by the *component* and passed in as plain data — `classify` never touches
signals. This is the single interesting invariant and the only frontend unit
test.

### Frontend: `static/src/overlays/OpenOmnibox.jsx` — Preact component

Lives in `overlays/` with the other modals (self-gating signal pattern, opened
by the `+new` button via the same `document.getElementById` + `useEffect`
listener the existing modals use). One text input + ranked result list;
drill-ins (new-worktree branch entry, PR repo picker) stay in the same field via
local component state — never a second modal.

Reads: `catalog` once on open (`GET /api/open/catalog` via `apiCall`), and the
live `windows` signal (to mark already-live cards). Writes: on a successful
`POST /api/open`, takes `response.ui` and calls **`patchUI`'s in-place path**
(prefs.js:106) to write `prefsSignal` synchronously — this is the
`deferRailAdd` replacement. Note: `patchUI` *also* PATCHes the server; here the
server already wrote the pref, so the omnibox should set `prefsSignal.value`
directly from `response.ui` (the in-place half of `patchUI`) **without** a
redundant PATCH. Propose a tiny `prefs.setUI(uiBlob)` exported from prefs.js that
does just the `prefsSignal.value = {...P(), ui: uiBlob}` assignment — the
non-network half of `patchUI` — so the omnibox doesn't re-POST what the server
just persisted. (`addWorktreeToRail` is no longer called by anyone after this;
delete it with the modals if grep confirms no other caller.)

Components are not unit-tested (project norm: browser-verify). No class — it's a
function component with hooks like every other modal.

### Deletions — structural cleanup

- `Header.jsx`: the `<Dropdown>` with `+ session` / `+ project` / `review PR`
  (confirmed at the three `<button>` items) collapses to a single `+new` button
  that flips the omnibox's open signal.
- `Overlays.jsx`: delete the `+session` imperative handler (lines 27-37) and
  its `apiCall("/api/session/new")`; drop `<NewProjectModal/>`, `<ReviewPrModal/>`,
  `<OpenPickerModal/>` from the composition (lines 54-64); add `<OpenOmnibox/>`.
- Delete `NewProjectModal.jsx`, `ReviewPrModal.jsx`, `OpenPickerModal.jsx`.

## Patterns

Used:
- **Discriminated union** (descriptor variants) — closed variant set, the exact
  case for it. Frozen dataclasses in Python; the `kind`-tagged candidate in JS.
- **Convergence / reduction** — branch and pr variants reduce to the path case
  by re-calling `open_target`. This is the spec's core idea; it lives in
  `open_ops`, not the route.
- **Constructor-less DI by argument** — `place_in_rail(session, project, pids)`
  takes its inputs; no hidden global reach beyond the `store.py` singletons that
  are the house pattern.
- **Frozen value-objects** — `OpenResult`, `PRWorktree`, the descriptors. Data
  crossing boundaries, pure functions over them (rung 2 for the data, rung 1 for
  the logic).
- **Self-gating modal signal** — `OpenOmnibox` follows the existing per-modal
  `signal(false)` convention rather than inventing modal-router state.

Considered and rejected:
- **`OpenService` / `Opener` class** — no coupled mutable state; it'd be a
  method-bag over `store.py`. Functions.
- **Strategy/ABC for the three descriptor variants** — only one impl each, no
  shared behavioral contract worth an interface; a `match` is clearer and the
  set is closed. (If a fourth "arbitrary non-git dir" variant ever lands — a
  non-goal — it's another match arm, not a new subclass.)
- **Custom exception hierarchy** — built-in `ValueError` + at most one
  `SessionNameCollision` (and that one only if `ensure_session` can't decide via
  return value).
- **Frontend modal-router / central open-state machine** — the codebase
  deliberately has none; each modal owns its signal. Don't introduce one for a
  single new modal.
- **Re-POSTing via `addWorktreeToRail`/`patchUI`** from the omnibox — the server
  already persisted the pref; a client PATCH would be a redundant round-trip and
  a write race. Use the in-place-only `setUI`.

## Test strategy

Per the project norm: integration against real tmux/git/state where the value is
in the real dependency; unit tests for pure transforms; components browser-verified.

| Module | Test | Real vs mocked |
|---|---|---|
| `open_ops.open_target` (path case) | `tests/test_open_ops.py` — real tmux session spawn (real `_layout_two_window`), real `state.json` via the `clean_state` fixture | **Real tmux + real store.** Dormant-project, already-live (focus-not-spawn), worktree-groups-under-repo, name-collision-dedupe. Mocking tmux here would reproduce the Q1-2026 mock-passes/prod-fails class. |
| `open_ops.ensure_session` | same file | Real `tmux has-session`. The three outcomes (live+ours / dead / live+foreign) each get a case. This is the collision-prone core CLAUDE.md warns about — must hit real tmux. |
| `open_ops.worktree_for_branch` | same file | Real `git worktree list` (real temp repo + `git worktree add`). Asserts it matches the enumerated worktree, NOT a recomputed `worktree_path` (the slug-collision trap). |
| `fetch_pr_into_worktree` | `tests/test_open_ops.py` (migrated from `test_projects.py` pr-review cases) | **Mock `gh` only** (network); real `git worktree add` + real rollback. The rollback coverage is the load-bearing part — assert `_discard_pr_worktree` fires on each failure step. |
| `build_catalog` | same file | Real git on temp repos; assert main checkout appears as a worktree entry, known-project repos merge with discovered repos. |
| `_layout_two_window` both-stamp | `tests/test_worktree_spawn.py` (new case) | Real tmux; assert BOTH windows carry `@periscope_id`. This is the new requirement and exactly the kind of thing that silently regresses. |
| `routes/open.py` | `tests/routes/test_open.py` | TestClient over the real app; the full spec Testing list incl. non-git→400 and PR-variant `linked_pr` stamp. Route layer is thin, so most logic is already covered at `open_ops` level — these assert the HTTP contract (status codes, response shape incl. `ui` blob with both pane pids). |
| `open/classify.js` | `static/src/open/__tests__/classify.test.js` (vitest) | Pure unit. PR-URL vs `#N` vs bare-path vs repo-name classification; ranking ordering. The single interesting frontend invariant. |
| `OpenOmnibox.jsx` | none (browser-verify) | Per project UI norm. |

Testability flags: none. The structure keeps every branchy/stateful piece
(`open_target`, `ensure_session`, the classifier) reachable directly without
constructing a hard object or driving the DOM — which is the whole reason the
dispatch is in `open_ops` not the route handler (pushback #1).

## Decisions to sanity-check

1. **`fetch_pr_into_worktree` lands in `projects.py`, not `open_ops.py`.**
   Alternative: put it in `open_ops` since open is its only caller today. Close
   because: it's project-worktree-domain orchestration (peer to `create_project`,
   shares `_discard_pr_worktree`/`repo_lock`), and a non-open caller is
   plausible later — but YAGNI cuts the other way and `open_ops` would keep all
   the extracted logic in one file. I chose `projects.py` on domain cohesion.

2. **`SessionNameCollision` custom exception may not be needed.**
   Decision: introduce it only if `ensure_session` genuinely needs to *raise* to
   signal the live-but-foreign case. Alternative (likely better): `ensure_session`
   handles all three outcomes internally via a `has-session` + ownership check
   and never raises — dedupe inline, return normally. Close because I can't see
   the final control flow until it's written; the spec describes it as a branch,
   not an error. Lean: **no custom exception**, decide-by-return.

3. **`place_in_rail` omits the `"review"` sentinel and lets `mergeLiveAndPrefs`
   add it** vs. writing it for authority. Chose omit (don't duplicate the
   sentinel rule across server and client). Alternative: write it server-side so
   the persisted pref is complete without relying on merge. Close because the
   spec leans "authoritative server write" for the pids but the `"review"` rule
   currently lives only in `railTree.js`; duplicating it server-side risks drift.

4. **`_layout_two_window` returns a 2-tuple** `(claude_pid, shell_pid)` vs. a
   named struct. Chose tuple (two homogeneous values, no naming payoff, retiring
   callers). Sanity-check: if `place_in_rail` or a future caller wants to
   distinguish them by role often, a `frozen` 2-field dataclass reads better.

5. **`prefs.setUI(uiBlob)`** as a new tiny export (the non-network half of
   `patchUI`) vs. reusing `patchUI` and eating a redundant server PATCH. Chose
   `setUI` to avoid the re-POST/write-race. Sanity-check: if the team prefers one
   prefs-write path, `patchUI` with a `skipNetwork` flag is the alternative —
   slightly worse (boolean-flag-on-a-function, which the taste rules disfavor),
   so I kept them as two named functions.

6. **`OpenPickerModal` retired.** Spec open question (lines 333-335). Decided:
   retire it — "open dir" cards over the catalog cover the live-session case
   (server focuses), and keeping a second rail-add surface re-fragments the very
   thing this feature unifies. Sanity-check: the picker today adds an
   *already-live unmanaged* session whose dir may not be a discoverable repo; if
   such sessions must remain rail-addable and they don't appear in the catalog,
   the picker (or an omnibox "live sessions" card source fed by the `windows`
   signal) has to stay. Verify the catalog (or a windows-signal card source)
   covers every session the picker can currently add before deleting it.
