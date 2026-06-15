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

Single entry for all materialization. Pseudocode:

```
def open(descriptor):
    if descriptor.path:
        toplevel = git_toplevel(descriptor.path)      # 400 if not a git repo
        repo = resolve_repo(toplevel)                  # --git-common-dir → parent repo
        project = ensure_project(toplevel, repo)       # register if absent (NO 409)
        session = find_live_session_for(toplevel, project)
        if session is None:
            session = spawn_claude_shell(toplevel, project)   # 2-window layout
        place_in_rail(session, repo, project)          # server-side rail pref write
        return { tmux_session: session, repo: repo, ... }

    if descriptor.branch:
        # worktree path may or may not exist yet
        wt = worktree_path_for(descriptor.repo, descriptor.branch)
        if not exists(wt):
            wt = create_worktree(descriptor.repo, descriptor.branch)
        return open({ path: wt })                      # converges on the path case

    if descriptor.pr:
        wt = fetch_pr_into_worktree(descriptor.repo, descriptor.pr)
        return open({ path: wt })                      # converges on the path case
```

Branch and PR variants converge on the path case after producing a worktree,
so create-or-focus + register + rail-placement live in one place.

**Reuse, do not duplicate.** The logic already exists, scattered across
`routes/projects.py` (`projects_adopt` toplevel resolution + `matched_session`
scan; `projects_create` worktree spawn + claude/shell layout;
`projects_pr_review` fetch-into-worktree), `routes/sessions.py`
(`session_new`, `window/new-worktree`), `periscope/projects.py`
(`create_project`, `all_projects`), and `periscope/gitutil.py`
(`resolve_repo`, `resolve_repo_and_branch`, `detect_default_branch`). The plan
must extract these into shared helpers that both `/api/open` and any remaining
legacy callers use. The exact extraction boundaries are a plan-phase decision.

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
worktrees enumerate uniformly).

### Server-owned rail placement

`/api/open` writes the rail pref itself instead of the client doing
`addWorktreeToRail` after a poll-wait. At creation time the panes exist in
tmux and periscope can stamp/resolve their `@periscope_id` pids immediately
(`periscope/pids.py`), so the endpoint has everything `addWorktreeToRail`
needs: `repo_order`, `worktrees_by_repo`, `panes_by_worktree`. It persists the
same UI-pref shape that `PATCH /api/prefs/ui` writes today. The UI re-polls
and the new session is already placed — no `deferRailAdd`, no 3500ms timer, no
race for the open path.

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
result. On success the modal closes; the next poll shows the placed session.

The input→descriptor decision is a **pure classifier function** (string →
candidate descriptors), unit-tested independently of the DOM.

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
- **PR variant**: `{repo, pr}` fetches into a worktree and opens (mock `gh`).
- **Non-git path** → 400.
- **Rail pref written**: after open, the UI-pref shape reflects the new
  session's placement.

Catalog test (`tests/routes/test_open.py` or alongside): enumerates worktrees
including the main checkout; merges known-project repos with discovered repos.

Frontend: the input→descriptor classifier gets a focused unit test (the
interesting invariant). The omnibox rendering/drill-in flow is browser-verified
per the project's UI-testing norm, not unit-tested.

## Open questions for the plan phase

- Exact extraction boundaries for the shared helpers (what moves out of
  `routes/projects.py` / `routes/sessions.py` into reusable functions).
- Whether the legacy endpoints (`/api/projects`, `/api/projects/pr-review`,
  `/api/session/new`) are retired outright or kept as thin internal callers
  during the transition. (`pr-review` is also used by `ReviewPrModal`, which
  this replaces; `/api/projects/promote` and the cleanup flows are out of
  scope and must keep working.)
- Branch drill-in interaction details (existing-branch list vs. new-branch
  text entry in one field).
