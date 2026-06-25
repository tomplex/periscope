# Track-based organization — design spec

**Date:** 2026-06-25
**Status:** Design approved (brainstorming), pending spec review
**Supersedes the model in:** `2026-06-24-metadata-anchored-rail-design.md` (phase 1 groundwork — this is the "collapse follow-on" that doc's code comments anticipate)

## Problem

Periscope's rail grew its organization out of how Tom happened to manage tmux
before periscope existed. The result is several *different mechanisms* pretending
to be one organizational scheme:

- **project → worktree** is a *structural* tier derived from git + tmux. A worktree
  literally **is** a tmux session (`open_ops.ensure_session` → `_layout_two_window`
  spawns one session per worktree; `railTree.js:91-95` keys the mid-tier on
  `w.session`).
- **workspace** is a *curated* tier — a manual per-pane tag (`pane_workspaces`),
  flat, cross-repo, with totally different mechanics.
- **dev / `MAIN_KEY`** is a *third* model: the flat catchall for unmanaged sessions.
- Vocabulary is overloaded: project vs repo, worktree vs session, workspace, tab vs
  pane vs window.

Periscope is now Tom's primary interface for Claude — he thinks about his work in
terms of how periscope displays it, not how tmux organizes it. The grouping UX is
inconsistent and tmux-coupled, and that coupling is the source of a recurring bug
class (e.g. the 2026-06-24 renumber-windows incident where session-index kill
targets drifted onto the wrong pane).

## Goal

**Periscope stops using tmux as an organizational tool and uses it purely as a
terminal backend.** Organization is driven entirely by periscope's own metadata,
through its own UI, with one consistent vocabulary.

This is a model change, not a re-skin. It is allowed to change how Tom works — much
of the current workflow is an artifact of periscope's current constraints.

## The model

### Two concepts, total

**Track** — the one organizational primitive. A named container of tabs. It scales
across every flavor of work Tom does, with **no "type" to choose**:

- **Big & sprawling** — weeks long, many branches, deep shared context (e.g.
  `attribute-config`).
- **Small & self-contained** — one task, usually one branch, sometimes a couple of
  tabs splitting implement/review, sometimes no branch at all (research).
- **Home base** — a project poked at continuously (`periscope`, `splash`); a rolling
  stream of small tasks under one roof. A track that never closes.

A track adapts to what it holds; the flavors above are emergent, not declared.

**Tab** — one Claude (or shell). It carries **affiliations** — repo, branch, PR,
Linear ticket — that *decorate* it as chips but **never decide which track it
belongs to**. A tab belongs to exactly one track at a time.

Everything else (repo, branch, PR, worktree) is an attribute, not a tier.

### Rail structure

Tracks are the top level of the rail. A track renders as a **flat list of tabs**,
*except*:

- When a track spans **more than one git branch**, same-branch tabs tuck into a
  **branch sub-cluster** (one level of nesting). The sub-cluster **auto-appears at
  the 2nd branch and auto-collapses to flat at 1 branch**. Branch membership is
  **derived live from each tab's git state (its pane cwd)** — never a manual tag.
  The only thing curated is the *track*; sub-clusters form and dissolve on their own.

The existing **attention sort** at the top of the rail stays as-is. New **filter
chips** let the rail (and that attention sort) be scoped to a single track.

### Membership & lifecycle

**Membership rule: repo-default + promote. Every tab always has a home.**

- Opening work in repo *X* lands the tab in a track named *X* (created if absent) —
  this is the home-base behavior.
- The user creates **named goal-tracks** ("attribute-config") via the omnibox and
  **moves/drags tabs** into them (re-tags `pane_tracks`).
- A branchless / non-git tab lands in a **`loose`** track.
- Reorganization is by *moving* tabs, never by filing from an empty state.

**Lifecycle actions:**

- **Create** — implicit (open repo *X* → track *X*) or explicit (omnibox "new track").
- **Rename** — free; a track name is just metadata.
- **Move tab** — row action / drag; re-tags `pane_tracks`.
- **Close — two distinct actions** (the incident-sensitive part, kept explicit):
  - **Dissolve** — remove the track; its tabs fall back to their repo-default / loose
    track. **Nothing is killed.** The safe reorg path.
  - **Close & tear down** — kill the track's tabs (by **stable `pane_id`**, never
    session:index) and optionally remove their git worktrees, behind a confirm that
    shows exactly what dies. The destructive path.

### tmux as pure backend

**One periscope-owned tmux session.** Every tab is a window in it. The rail **never**
reads the tmux session for grouping — it reads `pane_tracks`.

- Periscope is Tom's only interface; raw-tmux navigation (`prefix+n`, `prefix+1..9`)
  is not a concern, so collapsing every window into one session has no usability cost.
- Window names become free-form labels periscope controls (the narrator already
  renames windows).
- **The terminal bridge re-keys on `pane_id`.** Today `/ws/pane` is addressed by
  `session:index` (`routes/ws.py:34`, `static/src/util.js`, `terminalCore.js`), and the
  keystroke/resize paths use that `session:index` target. One session with
  `renumber-windows on` makes the index drift constantly (every kill/new renumbers),
  which would stale every open terminal. The bridge moves to addressing the pane by its
  stable `pane_id` (the mirror already subscribes by `pane_id`, `ws.py:78`, and tmux
  accepts `-t %id` for `display-message`/`capture-pane`/`send-keys`/`resize`). This kills
  the index-drift fragility the single session amplifies and matches the stable-id
  invariant.
- **Mirror load concentrates on one control client.** `tmux_mirror.py` is one
  `tmux -C attach` process *per session*; collapsing to one session routes every pane's
  `%output` through a single reader task + reply-callback queue, and every layout change
  reconciles all open terminals. This is a real change, **not** a simplification —
  acceptable for a single-user tool, but the spec does not claim it as a simplification.
- This deletes the session-per-worktree machinery, the `-2`/`-3` name-dedupe dance,
  the worktree-is-session coupling, and the renumber-windows kill-drift bug class.

**Foreign panes (tmux panes periscope did not create) are ignored entirely.** They do
not appear in the dashboard. The old `dev` / `MAIN_KEY` unmanaged-sessions catchall is
**removed**. Everything periscope shows is a tab it created, in its session, in a track.

**No periscope pane ever vanishes for lack of a tag.** `MAIN_KEY` today rescues more than
foreign panes — it also catches archived-project panes, delete-race panes, and no-row
pins (`railTree.js:55-58`). Track resolution must replace that rescue: a periscope pane
with no explicit `pane_tracks` row resolves to its **repo-default track** (derived from
its repo), or the **`loose`** track if non-git. Archiving a track does not hide its tabs;
they fall back to repo-default/loose (the *dissolve* semantics). So removing `MAIN_KEY`
loses only genuinely-foreign panes, never a periscope-created one.

## Data model

A new SQLite store, replacing two of today's ad-hoc stores (and resolving the
JSON/SQLite split the 2026-06-24 architecture audit flagged):

- **`tracks` table** — `id`, `name`, `repo` (nullable affiliation), `created_at`,
  `archived_at`. One table replaces **both** today's `projects` registry (currently
  raw dicts in `state.json`) **and** the `workspaces` entity (currently a JSON entity
  whose membership already lives in SQLite). These were always the same concept.
- **`pane_tracks` table** — `pane_id → track_id`. The single membership tag. Replaces
  `pane_workspaces` **and** the session-name match in `resolve_project_for_window`.
  Pruned by the existing dead-pane reaper (same pattern as `pane_sessions` /
  `pane_status` / `pane_projects`).
- **Branch sub-cluster** — no table; derived live from each tab's git branch.
- **Ordering** — track order in the rail and tab order within a track stay in the
  `ui` prefs blob (config-shaped, user-action-mutated; leave as JSON).

The session-match fallback in `resolve_project_for_window` (projects.py:174-181) and
the session-keyed `worktrees_by_repo` rail pref are **deleted** — but deleting the
fallback has a **blast radius the plan must enumerate, not just the one function**:

- **Bare-session callers.** `resolve_project_for_window` is called with `{"session": …}`
  and **no `pane_id`** at `routes/sessions.py:80,217,319` and `open_ops.py:212`. With the
  fallback gone these return `None`, breaking `/api/window/new` cwd resolution, the
  new-worktree project gating, and close-worktree. Each must be rewritten to resolve by
  track id (from `pane_tracks`), not by session.
- **Session-equality liveness/kill logic.** `cleanup.py:204-256` keys liveness on
  `tmux_session in alive_sessions` / `last.get("session") == tmux_session`, and
  `routes/cleanup.py:66-80` builds its kill set by `w["session"] == tmux_session`. With
  one shared session these match **every** window — a correctness bug in the destructive
  path. Both must move to track-membership (`pane_tracks` / `placement_kill_set`), not
  session identity.
- **`spawn_claude` placement modes.** `channels.py:~458-614` has three placement modes
  (`same`/`new` × `workspace_id`) that all write `pane_workspaces`/`pane_projects` and
  call `place_in_rail` with session-keyed prefs, including the `branch → worktree →
  workspace="new"` recursion. Folding these into `pane_tracks` is real work to budget.

## The migration (one-shot, at deploy)

This is the second half of the work Tom originally asked about — physically
consolidating every wrapped tab into one session. It runs **in the lifespan at boot,
before serving, and gated on `config.is_prod()`** — this timing is load-bearing:

- **No live terminals exist at boot.** Mirrors and `/ws/pane` connections attach lazily
  when a browser opens a pane (`ws.py:78` `subscribe`). Running the window moves before
  serving means no terminal is mid-stream when windows move sessions — this is what makes
  `move-window` safe, and is why the bridge re-key (above) is the ongoing safety net, not
  the migration's.
- **Prod-gated.** Dev (`PERISCOPE_DEV=1`) shares the same tmux server unless
  `PERISCOPE_TMUX_SOCKET` is set; an ungated migration would let a dev boot consolidate
  Tom's real prod sessions. Gate on `config.is_prod()`, same as the worker/MCP.

Steps:

1. Create the single periscope-owned session (idempotent: skip if it already exists).
2. For each currently-managed pane, `tmux move-window` it into that session, acting on
   **stable `#{window_id}`, never indices** (the renumber-windows lesson; the
   `routes/projects.py:257-278` adopt flow already does exactly this and is the model).
3. Seed `tracks` + `pane_tracks` from today's grouping — each project → a track (its
   panes tagged in), each existing workspace → a track, branches collapse into auto
   sub-clusters. This reuses the `backfill_pane_projects` pattern (`projects.py:215`).
4. Foreign panes are left where they are and simply drop off the dashboard.

**Idempotency gate.** The window-move is a one-way physical mutation with no natural
idempotency key, so it must not re-run after cutover. Gate it on a persisted flag (a
`settings`/`state.json` marker, e.g. `migrations.single_session_done`) set after a
successful pass; the `tracks`/`pane_tracks` seed stays idempotent the same way
`backfill_pane_projects` is (skip already-tagged panes).

**Cutover fidelity is NOT byte-identical — only projects are.** `backfill_pane_projects`
reproduces *project* grouping exactly. Workspaces are a different tier today
(`ws:<id>` keys are interleaved top-level groups, `railTree.js:101-108`) and the
bottom-pinned `dev` group disappears, so workspace tracks and any dev panes **reorganize
once, by design**. The verification target is "projects byte-identical; workspaces/dev
reorganize predictably," not "nothing moves."

## Affected code (cross-reference for the plan)

- `periscope/projects.py` — `resolve_project_for_window` (154-181, delete the
  session-match fallback; rewrite bare-session callers to resolve by track),
  `backfill_pane_projects` (215), `placement_kill_set` (184-211, already pane_id-based —
  reuse for "close & tear down").
- `periscope/open_ops.py` — `ensure_session` / `worktree_for_branch` / `place_in_rail`
  (151+) and the bare-session `resolve_project_for_window` call at `:212`;
  `_layout_two_window` in `worktree_spawn.py` (208-289) — spawn into the shared session
  instead of `new-session`.
- `periscope/routes/sessions.py` — bare-session callers at `:80,217,319` (cwd
  resolution, new-worktree gating, close-worktree) must resolve by track, not session.
- `periscope/cleanup.py` (204-256) + `periscope/routes/cleanup.py` (66-80) — session-
  equality liveness + kill-set logic; move to `pane_tracks` / `placement_kill_set` (one
  shared session makes `w["session"]==…` match everything — a destructive-path bug).
- `static/src/split/railTree.js` — `mergeLiveAndPrefs` (72-156); the mid-tier is
  session-keyed at 91-95. **This is the core frontend change**: the worktree tier must
  key on derived branch (`w.branch`, already in the payload), top-level on track.
- `static/src/split/{Rail,RailRows,Detail}.jsx`, `railTree.js` — track rendering,
  branch sub-clusters, filter chips, dissolve vs close & tear-down actions.
- `periscope/routes/ws.py` + `static/src/util.js` + `static/src/terminal/terminalCore.js`
  + `periscope/tmux.py` (`deliver_input`/`tmux_input`) — re-key `/ws/pane` and the
  keystroke/resize paths from `session:index` to stable `pane_id`.
- `periscope/store.py` / new `periscope/tracks.py` — the `tracks` + `pane_tracks` layer
  (model on `pane_workspaces` / `pane_projects` in `activity.py`).
- `periscope/channels.py` (`spawn_claude`, ~458-614, three placement modes), `routes/open.py`,
  `routes/sessions.py`, `routes/cleanup.py` — track tagging on create; dissolve/tear-down
  endpoints.
- `periscope/app.py` lifespan — the one-shot, prod-gated, flag-gated migration step.

## Non-goals (v1)

- No drag-multi-select in the rail (single-tab move/drag only).
- No per-track shared-state / roster persistence (deliberate follow-on, as in the
  workspace spec).
- No foreign-pane *adoption* (we chose ignore, not pull-in).
- No cross-repo auto-magic beyond the repo-default rule (cross-repo tracks are built by
  explicitly moving tabs in).

## Risks & things to verify

- **The migration moving live windows** *(resolved by design, must still be tested).*
  Moving windows is safe because it runs at boot before any mirror/WS attaches, and the
  bridge re-keys on `pane_id`. Still required: a **real-tmux test** (not mocked — the
  2026-06-24 lesson) that moves a window between sessions and asserts a subsequent
  subscribe + keystroke + capture all resolve by `pane_id`. `move-window` itself is
  proven in-tree (`routes/projects.py:257-278` adopt flow).
- **`tracks` replacing `projects` is a broad change.** The blast radius is enumerated in
  §Data model (bare-session callers, cleanup session-equality, `spawn_claude` modes). A
  missed `tmux_session` / `w["session"]==…` read silently reintroduces session coupling
  or, worse, mass-matches the destructive path. Grep gate before merge:
  `grep -rn 'tmux_session\|\["session"\]\|get("session")' periscope/`.
- **Cutover fidelity.** Projects must be byte-identical; workspaces/dev reorganize once
  (see §migration). Verify against a snapshot of current `/api/state` grouping: assert
  every *project* pane lands in the same group, and that no periscope pane disappears.

*Resolved during spec review (no longer risks):* branch derivation is fully funded
(`git_pr.cached_git_state` → `w.branch`, 60s-cached, already in the view); `move-window`
vs `link-window` (move is correct); `placement_kill_set` reuse for tear-down.
