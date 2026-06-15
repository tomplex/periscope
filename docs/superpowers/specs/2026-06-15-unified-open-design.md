# Unified "open" — one omnibox, one server endpoint

## Problem

Periscope's `+ new` dropdown exposes four conceptually-identical actions as
separate UI surfaces, and a fifth case has no surface at all:

| Menu item | Endpoint | Source spec |
|---|---|---|
| `+ session` | `POST /api/session/new` | a name (+ cwd) |
| `+ project` | `POST /api/projects` | repo + branch → new worktree |
| `review PR` | `POST /api/projects/pr-review` | repo + PR number → worktree |
| *(none)* | — | open an existing directory |

All of these are the same verb — **materialize a session into the rail** —
differing only by how the target is specified. The split into multiple modals
forces the user to pre-classify their intent into a UI screen before acting.

The missing case is the motivating bug: a directory periscope already knows
about (a registered project whose tmux session has since died — e.g. `splash`
on `main`) is **invisible and un-reopenable**. The rail is session-anchored on
*live* windows (`railTree.js` builds groups only from live windows), so a
dormant project renders nothing; and every creation path 409s with "project
already exists" the moment a project is registered at that directory
(`routes/projects.py` create/adopt/promote). There is no UI path to revive it.

## Goal

One entry point — an **omnibox** — where what you type/pick determines the
action, and a **single server endpoint** that owns all dispatch and side
effects. The UI speaks in terms of "the user picked this target"; the server
does the right thing.

### Principles

- **The server abstracts; the UI speaks UI terms.** The frontend never calls
  `session/new` / `projects` / `pr-review` directly. It assembles a *target
  descriptor* and POSTs it to one endpoint. The server owns resolve →
  register → create-or-focus → pin → rail-placement.
- **Idempotent.** Opening a target that is already live focuses it; opening a
  dormant/unregistered target creates it. No 409 on "already a project."
- **No client-side timing hacks.** Rail placement moves server-side, killing
  the ~3500ms `deferRailAdd` poll-wait race.

## Non-goals (v1)

- **Arbitrary non-git directories.** The picker is scoped to git repos and
  their worktrees (the user chose fuzzy repo/dir search). `/api/open` 400s a
  non-git path for now. Revisit if the need appears.
- **`+ session` as a distinct concept.** Dropped. A bare named session in `~`
  is just "open a directory and get a shell," which open already does
  (claude+shell layout). A scratch session unattached to any repo = "open
  `~`" — not worth a first-class affordance.
- **Sophisticated fuzzy ranking.** Start with substring match + a sensible
  ordering. No fuzzy scorer in v1.
- **Command-palette keyboard shortcut.** The entry point is the `+ new`
  button; a global summon key is a possible follow-up.

## Architecture

```
                         ┌──────────────────────────────────────┐
  + new (button)  ─────► │ OpenOmnibox (modal)                   │
                         │  • GET /api/open/catalog (once, on open)
                         │  • client-side filter/rank per keystroke
                         │  • card types → target descriptor      │
                         └───────────────┬──────────────────────┘
                                         │ POST /api/open  { descriptor }
                                         ▼
                         ┌──────────────────────────────────────┐
                         │ routes/open.py                         │
                         │  resolve → register-if-absent →        │
                         │  create-or-focus session →             │
                         │  pin to project → write rail pref →    │
                         │  return { tmux_session, repo, ... }     │
                         └──────────────────────────────────────┘
                              calls shared helpers extracted from
                              projects.py / sessions.py (no UI-facing
                              duplication; legacy endpoints become thin
                              or are retired)
```

### Target descriptor

The descriptor carries **intent**, not mechanism. Three variants — these are
genuinely different operations the server cannot infer from an opaque token
(you cannot guess "new worktree off `feat-x`" from a bare path):

```jsonc
{ "path": "/Users/tom/dev/splash" }              // open an existing dir
{ "repo": "/Users/tom/dev/splash", "branch": "feat-x" }  // worktree: open if exists, else create
{ "repo": "/Users/tom/dev/splash", "pr": 1234 }  // fetch + worktree + open
```

The omnibox card type maps 1:1 to a descriptor variant. The UI's
responsibility ends at "user picked this card → POST its descriptor." One
endpoint, one response contract — no per-action endpoints, no per-action
success ritual.

### `POST /api/open`

Single entry for all materialization. The response carries the updated `ui`
prefs blob (see "Server-owned rail placement") so the omnibox can refresh the
client's prefs cache synchronously. Pseudocode:

```
def open(descriptor) -> { tmux_session, repo, claude_pid, ui }:
    if descriptor.path:
        toplevel = git_toplevel(descriptor.path)      # 400 if not a git repo
        repo = resolve_repo(toplevel)                  # --git-common-dir → parent repo
        project = ensure_project(toplevel, repo)       # register if absent (NO 409)
        session, claude_pid = ensure_session(project)  # focus-or-spawn; see below
        ui = place_in_rail(session, project)           # server-side rail pref write
        return { tmux_session: session, repo: repo, claude_pid, ui }

    if descriptor.branch:
        wt = worktree_for_branch(descriptor.repo, descriptor.branch)  # via git worktree list
        if wt is None:
            wt = create_worktree(descriptor.repo, descriptor.branch)
        return open({ path: wt })                      # converges on the path case

    if descriptor.pr:
        wt = fetch_pr_into_worktree(descriptor.repo, descriptor.pr)   # incl. rollback
        result = open({ path: wt })                    # converges on the path case
        set_window_fields(result.claude_pid, linked_pr=descriptor.pr, is_fork=...)  # see note
        return result
```

**`ensure_session` is the idempotent create-or-focus core.** Match liveness by
the project's recorded `tmux_session` name via `tmux has-session` — NOT by a
cwd scan. `projects_adopt` matches by `os.path.realpath(w["cwd"]) == pinned_dir`
(projects.py:95), which is the exact cwd collision CLAUDE.md warns about
(multiple panes share a cwd); the project row already persists `tmux_session`,
so name-based liveness is collision-free and survives the session's death (the
dormant-splash case). Three outcomes:
  - session name live AND belongs to this project → focus it, resolve its
    claude pid, return.
  - session name dead → spawn the claude+shell layout under that name.
  - session name live but belongs to something unrelated (the recorded name
    was reused) → spawn under a deduped name and update `project.tmux_session`.
    `projects_create` already does a `has-session` pre-check + 409 here
    (projects.py:252-256); `/api/open` dedupes instead of erroring.

**PR linkage is not free in the convergence.** The path case has no knowledge
of the originating PR, so the PR variant writes `set_window_fields(claude_pid,
linked_pr=pr, is_fork=...)` *after* convergence (mirrors projects.py:637). This
requires `open()` to return the claude pid, hence `claude_pid` in the response.

**Reuse — partly done, partly a real refactor.** The claude+shell layout is
*already* a shared helper: `_layout_two_window` (worktree_spawn.py:208), reused
by `projects_create` and `projects_pr_review` — call it directly. `resolve_repo`
/ `resolve_repo_and_branch` / `detect_default_branch` (gitutil.py),
`create_project` / `all_projects` (projects.py), `spawn_worktree`
(worktree_spawn.py) are clean callables. BUT two pieces need genuine extraction,
which the plan must budget for:
  - **PR fetch-into-worktree** is ~90 lines of inline orchestration in
    `projects_pr_review` (collision pre-checks, `gh` call, fetch under repo
    lock, `_discard_pr_worktree` rollback on every failure step;
    projects.py:504-637). Lifting it into `fetch_pr_into_worktree(repo, pr) ->
    wt_path` must preserve the rollback semantics.
  - **HTTPException coupling.** `_layout_two_window` and the PR helpers
    `raise HTTPException` internally; `spawn_worktree` raises `ValueError` that
    each caller maps to a status. `/api/open` must map these consistently
    (non-git → 400, collision → 409, etc.). Decide whether helpers raise
    domain errors (and the route maps) or keep raising HTTPException.

### `GET /api/open/catalog`

Read-only enumeration the omnibox loads once on open. Builds on the existing
`/api/projects/discoverable` scan (known project repos + git repos one level
under `~/dev`), extended to enumerate worktrees:

```jsonc
{
  "repos": [
    { "repo": "/Users/tom/dev/splash", "label": "splash",
      "default_branch": "main", "branches": ["main", "feat-x", ...] }
  ],
  "worktrees": [
    { "path": "/Users/tom/dev/splash", "repo": "/Users/tom/dev/splash",
      "branch": "main", "is_main": true },
    { "path": "/Users/tom/dev/worktrees/splash-feat-x",
      "repo": "/Users/tom/dev/splash", "branch": "feat-x", "is_main": false }
  ]
}
```

Worktrees come from `git worktree list --porcelain` per discoverable repo
(the main checkout is git's first worktree entry, so repo-roots and linked
worktrees enumerate uniformly). Reuse the existing `worktrees._cached_worktrees`
helper (worktrees.py:62, 60s TTL) rather than a fresh shell-out. Cost note: the
catalog is ~2N git subprocesses for N discoverable repos (`git branch` +
`git worktree list`, each already capped/timeboxed in `projects_discoverable`,
projects.py:424-432) — acceptable for a single-user tool loaded once per
omnibox open, but it is not a cheap in-memory read.

This `worktrees` list is also the authoritative source for the branch
variant's existence check (see `worktree_for_branch` below) — match an
enumerated worktree by branch, do NOT recompute a path. `worktree_path(repo,
slug)` (worktree_spawn.py:93) slugs the branch (`/`→`-`, so `tc/foo` and
`tc-foo` collide), and `_resolve_layout` *writes* layout settings as a side
effect on first call (worktree_spawn.py:88) — both make a recomputed-path
existence check wrong. A worktree created outside periscope's convention also
won't sit at `worktree_path(...)` but WILL appear in `git worktree list`.

### Server-owned rail placement

`/api/open` writes the rail pref itself instead of the client doing
`addWorktreeToRail` after a poll-wait. Two things this depends on — both
verified against the code, both requiring care the first draft glossed:

**Pid stamping.** `_layout_two_window` today stamps `@periscope_id` on only the
claude window (worktree_spawn.py:280); the shell window gets no pid until the
next poll's `resolve_pids` (state.py / pids.py:217). So the server does NOT
automatically have the full pane list. Fix: **stamp both windows at spawn** so
`place_in_rail` can write the complete `panes_by_worktree` entry. Belt-and-
suspenders: `mergeLiveAndPrefs` appends live-but-unlisted pids
(`[...prefKept, ...live.filter(p => !seen)]`, railTree.js:114), so even a
partial entry self-heals on the next poll — but we stamp both to keep the
write authoritative rather than leaning on backfill.

**Pref keys.** `place_in_rail` must key by the project's `repo` field as the
top-level group key (`groupKeyForWindow` returns `row.repo || pin`,
railTree.js:50) and by the tmux **session name** as the worktree key
(`worktrees_by_repo[repo]` holds session names; railTree.js:76) — NOT the
worktree path the catalog lists. It persists the same `{repo_order,
worktrees_by_repo, panes_by_worktree}` shape `update_ui` writes (prefs.py:54).

**Client cache refresh — the actual timer replacement.** The 3s `/api/state`
poll writes `windows`/`projects`/`usage` but does NOT reload prefs, so a
server-side prefs write leaves the client's `prefsSignal` stale and the rail
unchanged until the next `loadPrefs()`. Therefore `/api/open` **returns the
updated `ui` blob**, and the omnibox writes it straight into `prefsSignal`
(the same in-place update `patchUI` performs after `PATCH /api/prefs/ui`,
prefs.js:115-124). This is what truly replaces `deferRailAdd`: placement is
synchronous on the response, no 3500ms wait, no poll race.

(The legacy `NewProjectModal` / `ReviewPrModal` `deferRailAdd` paths are
removed along with those modals when the omnibox replaces them.)

### Frontend: OpenOmnibox

Replaces the entire `+ new` dropdown. The button keeps the **`+ new`** label
but opens the omnibox directly (no menu). One text input + a ranked result
list, client-filtered over the catalog per keystroke:

- **open dir** — worktrees + repo roots matching the query. Terminal action;
  sends `{ path }`. Cards for already-live dirs are still selectable (server
  focuses rather than spawns).
- **new worktree in `<repo>`…** — per matched repo. In-field drill-in: the
  input switches to filtering that repo's branches / typing a new branch name;
  selection sends `{ repo, branch }`.
- **review PR** — appears for a PR URL (repo inferred) or `#N` (drill-in to a
  repo picker); sends `{ repo, pr }`.

Drill-ins stay in the same field — never a new modal. Enter picks the top
result. On success the omnibox writes the response's `ui` blob into
`prefsSignal` (so the rail reflects the placement immediately) and closes.

The input→descriptor decision is a **pure classifier function** (string →
candidate descriptors), unit-tested independently of the DOM.

Copy caveat: because non-git paths 400 (see Non-goals), the omnibox's
placeholder/empty-state must not promise "open any directory" — it surfaces
repos and their worktrees, plus PR refs. `~/Downloads` is not openable in v1.

### Grouping — no rail changes

`resolve_repo` already maps a linked worktree and its main checkout to the
same repo root via `git rev-parse --git-common-dir`. So the rail groups the
opened dir under its parent repo node automatically: a repo root becomes the
main/master node, a worktree becomes a branch node. `railTree.js` is
untouched.

## Testing

Route tests (`tests/routes/test_open.py`):
- **Dormant-project open** (the splash bug): open a registered project with no
  live session → spawns the claude+shell layout, does NOT 409, returns the
  session.
- **Already-live open**: open a dir whose session exists → focuses, no
  duplicate session created.
- **Worktree open**: groups under the parent repo (same `repo` key as the main
  checkout).
- **New-worktree variant**: `{repo, branch}` for a non-existent worktree
  creates it; for an existing one opens it.
- **PR variant**: `{repo, pr}` fetches into a worktree and opens (mock `gh`),
  and writes `linked_pr` on the claude pane via `set_window_fields`.
- **Revive with name collision**: registered project, dead session, but the
  recorded `tmux_session` name now belongs to an unrelated live session →
  spawns under a deduped name and updates the project row (no 409).
- **Non-git path** → 400.
- **Rail pref written**: after open, the UI-pref shape reflects the new
  session's placement, keyed by `project.repo` + session name, with both pane
  pids present (claude + shell).

Catalog test (`tests/routes/test_open.py` or alongside): enumerates worktrees
including the main checkout; merges known-project repos with discovered repos.

Helper-level tests: the extracted `fetch_pr_into_worktree` keeps its rollback
coverage (the cases currently in `tests/routes/test_projects.py` for
`pr-review` migrate here). `_layout_two_window`'s two-window stamping gains a
case asserting BOTH windows are stamped (the new requirement).

Frontend: the input→descriptor classifier gets a focused unit test (the
interesting invariant). The omnibox rendering/drill-in flow is browser-verified
per the project's UI-testing norm, not unit-tested.

## Decisions (resolved from review)

- **Legacy routes are retired, not kept as thin wrappers.** `/api/session/new`,
  `/api/projects` (create), and `/api/projects/pr-review` are removed as
  UI-facing endpoints; their logic lives in shared helpers that `/api/open`
  calls. Rationale: keeping dead-to-the-UI routes alive only to satisfy old
  tests is a smell — the tests move down to the helper level (better coverage
  anyway). **Blast radius (verified):** the `spawn_claude` MCP tool
  (`_do_spawn_claude_tool`, channels.py:371) is self-contained and shells out
  to tmux directly — unaffected. `/api/projects/adopt`, `/api/projects/promote`,
  `/api/projects/archive`, and the cleanup flows do not call the retired
  endpoints. What must change with them: the `+ session` handler
  (`Overlays.jsx:31`), `NewProjectModal.jsx`, `ReviewPrModal.jsx`, and
  `OpenPickerModal.jsx` are removed/replaced; `tests/routes/test_sessions.py`
  and the `pr-review` cases in `tests/routes/test_projects.py` migrate to
  helper-level + `/api/open` route tests.

## Open questions for the plan phase

- Exact extraction boundaries for the shared helpers (what moves out of
  `routes/projects.py` / `routes/sessions.py` and whether helpers raise domain
  errors vs. `HTTPException`).
- Branch drill-in interaction details (existing-branch list vs. new-branch
  text entry in one field).
- `OpenPickerModal`'s current job (add an *already-live* session to the rail)
  partially overlaps the omnibox's "open dir" cards. Decide whether it's
  retired too or kept for the live-session-already-running case.
