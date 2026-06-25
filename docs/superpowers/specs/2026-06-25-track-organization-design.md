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
- The control-mode mirror (`tmux_mirror.py`) is per-session — one session is *simpler*
  for it, not harder.
- This deletes the session-per-worktree machinery, the `-2`/`-3` name-dedupe dance,
  the worktree-is-session coupling, and the renumber-windows kill-drift bug class.

**Foreign panes (tmux panes periscope did not create) are ignored entirely.** They do
not appear in the dashboard. The old `dev` / `MAIN_KEY` unmanaged-sessions catchall is
**removed**. Everything periscope shows is a tab it created, in its session, in a track.

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
the session-keyed `worktrees_by_repo` rail pref are **deleted**.

## The migration (one-shot, at deploy)

This is the second half of the work Tom originally asked about — physically
consolidating every wrapped tab into one session:

1. Create the single periscope-owned session.
2. For each currently-managed pane, `tmux move-window` it into that session, acting on
   **stable pane/window ids, never indices** (the renumber-windows lesson).
3. Seed `tracks` + `pane_tracks` from today's grouping — each project → a track (its
   panes tagged in), each existing workspace → a track, branches collapse into auto
   sub-clusters. This is the `backfill_pane_projects` pattern that already runs
   synchronously at boot, so **the rail is byte-identical at cutover**.
4. Foreign panes are left where they are and simply drop off the dashboard.

The migration must be idempotent and safe to run against the live prod session (it
runs once, in the lifespan, gated so it does not re-run after cutover).

## Affected code (cross-reference for the plan)

- `periscope/projects.py` — `resolve_project_for_window` (154-181, delete the
  session-match fallback), `backfill_pane_projects` (214), `placement_kill_set`
  (184-211, already pane_id-based — reuse for "close & tear down").
- `periscope/open_ops.py` — `ensure_session` / `worktree_for_branch` / `place_in_rail`
  (151+); `_layout_two_window` in `worktree_spawn.py` (208-289) — spawn into the shared
  session instead of `new-session`.
- `static/src/split/railTree.js` — `mergeLiveAndPrefs` (72-156); the mid-tier is
  session-keyed at 91-95. **This is the core frontend change**: the worktree tier must
  key on derived branch, top-level on track.
- `static/src/split/{Rail,RailRows,Detail}.jsx`, `railTree.js` — track rendering,
  branch sub-clusters, filter chips, dissolve vs close & tear-down actions.
- `periscope/store.py` / new `periscope/tracks.py` — the `tracks` + `pane_tracks` layer
  (model on `pane_workspaces` / `pane_projects` in `activity.py`).
- `periscope/channels.py` (`spawn_claude`), `routes/open.py`, `routes/sessions.py`,
  `routes/cleanup.py` — track tagging on create; dissolve/tear-down endpoints.
- `periscope/app.py` lifespan — the one-shot migration step.

## Non-goals (v1)

- No drag-multi-select in the rail (single-tab move/drag only).
- No per-track shared-state / roster persistence (deliberate follow-on, as in the
  workspace spec).
- No foreign-pane *adoption* (we chose ignore, not pull-in).
- No cross-repo auto-magic beyond the repo-default rule (cross-repo tracks are built by
  explicitly moving tabs in).

## Risks & things to verify

- **The migration moving live windows.** `tmux move-window` across sessions on the live
  prod session, with the mirror attached. Must verify the mirror reconnects and the
  WS bridge survives the move (real-tmux test, not mocked — the 2026-06-24 lesson).
- **`tracks` replacing `projects` is a broad change.** `projects.py` is read in many
  places (window_view, open_ops, channels, routes). The rename/replace must be
  exhaustive; a missed `tmux_session` read silently reintroduces session coupling.
- **Branch derivation cost.** Deriving each tab's branch live (per poll) must not add a
  git call per pane on the hot path — reuse whatever git state `git_pr.py` / parse_pane
  already surfaces.
- **Byte-identical cutover.** The backfill must reproduce today's grouping exactly, or
  Tom's rail visibly reshuffles on deploy. Verify against a snapshot of current state.
