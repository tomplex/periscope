# Metadata-anchored rail — code-structure proposal

Structures the semantic-fix spec (`2026-06-24-metadata-anchored-rail-design.md`).
Scope is the semantic fix only; the physical session collapse + rail
session→project rekey are deferred to the follow-on and are NOT structured here.

The spec is unusually structure-forward — it already names the table, the
accessor set, the resolution shape, and the shared-helper decision. The work
here is to commit the helper's *home*, the backfill's *home + invocation*, and
the per-target state-cleanup factoring, all in Tom's taste, and to flag the one
structural assumption worth a second look before planning.

## Spec pushback

**1. The placement-kill helper does not belong in a new `placement.py`, and it
is not a class.** The spec says "propose where it lives" and floats a new
module. A new module names a concept; this is one pure function plus the
per-target focus-dict cleanup it must not let drift. That is not a subsystem —
it is a function with two callers. Tom's rule: structure only when it names a
real concept, and 3 similar lines beat a premature module.

Put it in **`periscope/projects.py`** as a plain function. Rationale:

- The rule it encodes is *"placement = project ∧ ¬ws override, never `__main__`"* —
  that is project-grouping logic, and `projects.py` already owns
  `resolve_project_for_window`, `MAIN_KEY`, and the `__main__` guards
  (`archive_project` raises on `MAIN_KEY`, `update_project` restricts it). The
  refusal-of-`__main__` invariant lives next to its siblings.
- It reads `activity.get_pane_project` / `pane_workspace_map` — `projects.py`
  does not import `activity` today, but `activity` imports `panes`/`git_pr`, not
  `projects`, so `projects → activity` is acyclic. (Confirm during planning; if
  a cycle surfaces, fall back to a function-level import, the pattern
  `activity._worker_tick` already uses for `narrator`.)
- It must NOT go in `open_ops.py`: that module is the *create* path (no HTTP, no
  destructive ops). It must NOT go in `panes.py`: panes owns parse/focus
  primitives and the `_focused_at`/`_acted_at` dicts, but not project grouping —
  putting group-resolution there inverts the existing dependency (`projects`
  reads from `panes`-derived windows, not vice versa).

The focus-dict cleanup is the wrinkle: `_focused_at` / `_acted_at` /
`_active_per_session` live in `panes.py` and are imported by the routes.
**DECIDED (Tom): extract a shared `drop_target_focus(target)` in `panes.py`**,
called by all four per-target sites — the two new killers (close, cleanup) plus
the existing `window_move` (sessions.py L401-404) and `window_delete`
(sessions.py L414-415) pops. Four call sites is past the rule-of-three; the
extraction kills the drift risk the spec worries about for real, rather than
arguing it away.

`placement_kill_set` stays **pure** (`project_key, windows, maps →
list[(target, pane_id)]`, zero I/O) — the kill plan only. The route iterates the
plan, calls `_tmux_mutate("kill-pane", ...)` then `drop_target_focus(target)`
per killed target; `_active_per_session.pop(session)` only when the session
empties. The rule lives in one pure place, the per-target dict-pop in one impure
place (`panes.py`), the tmux I/O in the routes.

- (rejected) The helper performs the kill *and* the cleanup — pulls
  `_tmux_mutate` + the `panes` dicts into `projects.py`, giving it side-effects
  and a tmux dependency it has never had, and forces route tests to mock tmux to
  test the rule.
- (rejected) Inline the per-target pop in each route — two more copies of the
  same two lines across four total sites; the DRY extraction is cleaner.

**2. "Refuses `__main__`" should be a raised error, not a silent empty set.**
The spec's test table says "a `__main__` group yields no kill set." Returning
`[]` silently is a foot-gun: a future caller that forgets the guard gets a
no-op, not a signal. Raise `ValueError("refusing to kill the __main__/dev
group")`. The two routes guard before calling (close-worktree never targets dev;
cleanup already raises on `MAIN_KEY` at cleanup.py:58), so the raise is
defense-in-depth at the boundary, per Tom's "validate at boundaries" rule and
the route error convention (it maps to a 400/500). No custom exception type —
`ValueError` with context, matching `_canonical_key`/`archive_project`.

## Assumptions

- `closeWorktree(session)` (Rail.jsx:275) passes a **tmux session name** (the
  worktree row key), and `DELETE /api/session?session=<name>` keeps that
  contract — only its *behavior* changes (placement-set kill, not
  `kill-session`). Confirmed: Rail passes `wtKey`; while session==project is 1:1
  (Decision 1 deferred), the route resolves the session to its project group via
  `resolve_project_for_window({"session": session})` and kills that group's
  placement set.
- The placement helper takes the **resolved project_key + the live window list**
  (from `list_windows()`), not a session name, so it is pure and tmux-free. The
  route does the `list_windows()` shell-out and the session→project resolve,
  then hands both to the helper.
- `pane_projects.project` stores a `pinned_dir` — the managed project's key.
  Backfill and tag-on-create only ever write `pinned_dir` values; `MAIN_KEY` is
  never persisted as a tag (unmanaged panes stay untagged → dev), and the
  placement helper refuses a `MAIN_KEY` group regardless.
- Backfill writes a row **only for project-managed panes** (those resolving to a
  `pinned_dir`); unmanaged/external panes stay untagged and fall back to dev.
  Still byte-identical at cutover — untagged → fallback → dev is exactly today's
  answer — with a cleaner row meaning ("belongs to managed project X").

## File layout

```
periscope/activity.py          CHANGED  + pane_projects table in _SCHEMA; + set/get/map/prune accessors (mirror pane_workspaces block)
periscope/projects.py          CHANGED  + resolve tag-first; + placement_kill_set() pure helper; + backfill_pane_projects()
periscope/open_ops.py          CHANGED  tag claude+shell pane_ids in _open_path (next to place_in_rail L203)
periscope/channels.py          CHANGED  tag spawned pane in _do_spawn_claude_tool (the anchored/place_in_rail path ~L617)
periscope/app.py               CHANGED  call backfill SYNCHRONOUSLY before yield; add prune_pane_projects to housekeeping
periscope/panes.py             CHANGED  + drop_target_focus(target) shared per-target focus-dict cleanup
periscope/routes/sessions.py   CHANGED  session_delete → placement-aware; window_move/window_delete adopt drop_target_focus
periscope/routes/cleanup.py    CHANGED  cleanup_archive kill-session → placement-aware (same helper + drop_target_focus)
static/src/split/Rail.jsx      CHANGED  closeWorktree confirm copy reworded (L277)

tests/test_activity.py         CHANGED  + pane_projects round-trip/map/prune (mirror pane_workspaces tests L488-524)
tests/test_projects.py         CHANGED  + resolve tag-first/fallback; + placement_kill_set rule; + backfill idempotence
tests/routes/test_sessions.py  CHANGED  + placement-kill close, ws-pane spared, per-target focus cleanup
tests/routes/test_cleanup.py   CHANGED  + placement-kill teardown, ws-pane spared
```

No new module. Every change lands in a file that already owns its concern.

## Per-module structure

### `activity.py` — pane_projects accessors
**Rung 1 (plain functions).** A fourth per-`pane_id` tenant alongside
`pane_sessions` / `pane_workspaces` / `pane_status`. No new shape — copy the
`pane_workspaces` block (L218-274) verbatim and rename:

- `set_pane_project(pane_id, project: str | None)` — upsert / clear-on-None.
- `get_pane_project(pane_id) -> str | None`.
- `pane_project_map() -> dict[str, str]` — bulk read.
- `prune_pane_projects(alive_pane_ids: set[str]) -> int`.

Plus the `CREATE TABLE IF NOT EXISTS pane_projects` block in `_SCHEMA` (spec's
DDL verbatim). No dataclass — these are `str` values, like `pane_workspaces`;
the dataclass rows (`PaneStatusRow`, `CommanderMarker`) exist only where there
are multiple columns to carry. *Rationale: this is the established sibling
pattern; deviating would be the smell.*

### `projects.py` — resolution + placement helper + backfill
**Rung 1 (plain functions)** throughout.

- `resolve_project_for_window(window)` — prepend the tag-first branch exactly as
  the spec writes it (read `pane_id`, `activity.get_pane_project`, return on
  hit), then the existing session-match body unchanged as fallback. One added
  `if` block. *The two `pane_id`-less callers (`_window_new_plain`,
  `window_new_worktree`) fall through to the fallback automatically — flagged
  fallback-dependent, no code change here.*

- `placement_kill_set(project_key, windows, ws_map) -> list[tuple[str, str]]` —
  pure. Returns `[(target, pane_id)]` for windows whose `pane_projects` ==
  `project_key` AND `pane_id not in ws_map`. Raises `ValueError` on `MAIN_KEY`.
  Takes `ws_map` (from `activity.pane_workspace_map()`) and the project tag per
  window — caller passes the live `windows` (each carrying `pane_id`/`target`)
  so the function does zero I/O. *Rationale: the spec's load-bearing rule
  ("project ∧ ¬ws override, never main") in one pure, directly-testable place.*

  Signature note: it reads each window's *tag*, not `resolve_project_for_window`
  — a window with a tag for project P AND a ws override has tag==P but must be
  excluded by the ws-map check. So the membership predicate is
  `get_pane_project(pane_id) == project_key and pane_id not in ws_map`. Pass the
  tag lookup in (a `proj_map` from a new `pane_project_map()` bulk read) to keep
  it pure and one-DB-read, mirroring how `window_view` already bulk-reads
  `pane_workspace_map`.

- `backfill_pane_projects() -> int` — for every live pane (`list_windows()`)
  with no `pane_projects` row, resolve via the session-match body and write a row
  **only when it resolves to a real project** (a `pinned_dir`). Unmanaged /
  external panes (`MAIN_KEY`) are left untagged and resolve to dev by default — a
  row means "belongs to managed project X", nothing else. Idempotent (skip rows
  that exist). Returns count written. Lives here, not in `activity.py`, because it
  *composes* resolution + the accessor — it is projects-domain orchestration
  that happens to write through an activity accessor, same as how `open_ops`
  orchestrates across `projects`/`store`/`tmux`. *Rationale below in "Backfill
  home."*

### `open_ops.py` / `channels.py` — tag on create
**Rung 1.** Two-line additions at the existing placement sites, no new
structure:

- `_open_path` (L198-203): the claude `pane_id` is already recovered at L198-202;
  the shell pane's `pane_id` is in the same `list_windows()` scan
  (`w["pane_id"]` for the session's windows). After `place_in_rail`, call
  `activity.set_pane_project(pane_id, project_key)` for both. `project_key` is
  the `toplevel` already in scope (it IS the project key).
- `channels.py` `_do_spawn_claude_tool` (~L617, the `anchored` branch): tag the
  just-stamped `pane_id` with the anchored `project`'s key. The `pane_id` is
  already in scope (L602). Same one-liner.

*No helper for the tagging — it is a single accessor call at two sites with
different in-scope variable names; a wrapper would be a premature helper (Tom's
rule). The DRY concern (the placement *rule*) is centralized in
`placement_kill_set`; the *write* is just `set_pane_project`.*

### `app.py` — backfill invocation
**Rung 1.** The backfill must be **synchronous before `yield`**, NOT inside
`_pane_sessions_housekeeping` (which is `_bg`-dispatched, fire-and-forget, L77).
Add a direct blocking call in the lifespan body before `yield`:

```python
from periscope import projects as _projects
seeded = _projects.backfill_pane_projects()
if seeded:
    log.info("backfilled %d pane_projects row(s)", seeded)
```

Place it *after* the `_bg` prune kicks (those are fine fire-and-forget) and
before `yield`, gated unconditionally (both prod and dev — the rail must be
correct in dev too; this is cheap, no Haiku/network). Add
`prune_pane_projects(alive)` to the existing `_pane_sessions_housekeeping` block
next to `prune_pane_workspaces` (L73-75) — pruning *can* stay `_bg` (dead-row
cleanup tolerates the race; only the *seed* must block). *Rationale: the spec is
explicit and correct — the collapse follow-on deletes the fallback, so the seed
must already be a blocking pre-serve step. Keeping the prune in `_bg` respects
that only the seed is ordering-sensitive.*

### `routes/sessions.py` — `session_delete` placement-aware
**Rung 1.** Replace the `kill-session` + per-prefix teardown (L62-70) with:
1. `project_key = resolve_project_for_window({"session": session})`; guard
   `MAIN_KEY`/None → 400 (mirror cleanup's existing `__main__` raise).
2. `windows = [w for w in list_windows() if w["session"] == session]`.
3. `kill = projects.placement_kill_set(project_key, windows, ws_map, proj_map)`.
4. For each `(target, pane_id)` in `kill`: `_tmux_mutate("kill-pane", "-t",
   target)` then `panes.drop_target_focus(target)` (the shared per-target
   cleanup, replacing the per-`session:`-prefix sweep).
5. `_active_per_session.pop(session, None)` **only if** the session is now empty
   (no surviving windows) — the spec's "touched only when the session actually
   goes away." Check via `list_windows()` post-kill or by computing
   `survivors = windows - killed`.

*The per-target dict cleanup replaces the per-`session:`-prefix sweep — that is
the bug the spec calls out (wiping a surviving ws-pane's focus state).*

### `panes.py` — shared per-target focus cleanup
**Rung 1.** New `drop_target_focus(target)` next to the `_focused_at` /
`_acted_at` dicts it owns: pops `target` from both. The four per-target kill
sites — `session_delete` (close), `cleanup_archive`, `window_move`,
`window_delete` — call it instead of repeating the pop inline. One impure
function, four callers; DRY past the rule-of-three.

### `routes/cleanup.py` — teardown placement-aware
**Rung 1.** Same substitution at the `kill-session` site (L67-68): resolve the
candidate's `pinned_dir` to its project group and call `placement_kill_set` +
per-target `kill-pane` + `panes.drop_target_focus(target)`, instead of
`_tmux_mutate("kill-session", ...)`. The
`pinned_dir` IS the project key, so resolution is direct (no session lookup
needed — but the windows must still be filtered by the project's
`tmux_session`). `projects.py:267` (promote-rollback) is untouched — it is not
this site.

### `Rail.jsx` — confirm copy
Pure string change at L277. New copy must stop claiming "kills every tmux
window" — a dragged-away pane survives. Suggested: *"Close worktree
\"{session}\"?\n\nClaude tabs you've moved into a workspace stay open. Tabs that
live here are closed. The worktree directory on disk is not removed."* (final
wording is Tom's call). Rebuild + commit `static/dist/app.js` per CLAUDE.md.

## Patterns

- **Sibling-table replication** (pane_projects ↔ pane_workspaces) — used; the
  codebase already treats per-`pane_id` tags as a family.
- **Pure-core / impure-shell** — used: `placement_kill_set` is pure (rule +
  data), routes own tmux + dict I/O. This is what makes the rule unit-testable
  without mocking tmux.
- **Compose-at-the-orchestration-layer** (backfill in `projects.py` calling an
  `activity` accessor) — used, matching `open_ops`.
- **Shared per-target cleanup** (`drop_target_focus` in `panes.py`) — used: four
  call sites pop the same focus dicts; one function prevents drift.
- **New module `placement.py`** — *rejected*: one function + mechanical cleanup
  is not a subsystem (pushback 1).
- **Placement-kill as a class / strategy** — *rejected*: one rule, no state, no
  polymorphism; rung 1.
- **A `tag_pane_project()` wrapper around `set_pane_project`** — *rejected*:
  premature helper; two one-liner call sites with no shared logic beyond the
  accessor itself.
- **Custom exception for the `__main__` refusal** — *rejected*: `ValueError`
  with context, per Tom's error rule; no caller catches *that specific type*.

## Test strategy

| Module | Approach | Real vs mocked |
|---|---|---|
| `activity.pane_projects` | **Unit** (extend `test_activity.py`, copy the `pane_workspaces` cases L488-524): set/get/retag/clear/map/prune round-trips; prune drops dead ids. | Real SQLite (`fresh_activity_db` fixture). No mocks — it is the established norm and a mocked DB is the exact failure class CLAUDE.md warns about. |
| `projects.resolve_project_for_window` | **Unit** (extend `test_projects.py`): tag wins over session-match; untagged → session-match; external session → `MAIN_KEY`; empty session → None; `pane_id`-less dict → fallback (covers the two flagged callers). | Real state + real `pane_projects` rows via `activity.set_pane_project`. |
| `projects.placement_kill_set` | **Unit**: project + ws-tagged panes → exactly the non-overridden `pane_id`s; `MAIN_KEY` → raises `ValueError`; no-match → `[]`. | Pure — pass plain dicts + maps, zero I/O. This is the testability payoff of keeping it pure. |
| `projects.backfill_pane_projects` | **Unit** (extend `test_projects.py`): pre-seeded session→project state → identical rows; idempotent on re-run; panes with an existing row left untouched; untagged-only panes covered. | Real state + DB; `list_windows()` stubbed/monkeypatched to a fixed pane set (the repo monkeypatches `list_windows` already). |
| `routes/sessions.py` close | **Route** (`tests/routes/test_sessions.py`, TestClient + mocked `_tmux_mutate`): `kill-pane` issued for placement panes only; ws-tagged pane spared; per-target `_focused_at`/`_acted_at` popped, surviving pane's state intact; `_active_per_session` dropped only when session empties; no `kill-session` issued. | Mock `_tmux_mutate` (the repo's route-test norm — it asserts the tmux *call shape*, not real tmux); real state/DB for the tags. |
| `routes/cleanup.py` teardown | **Route** (`tests/routes/test_cleanup.py`): placement-set kill; ws-pane spared; promote-rollback path (projects.py:267) still kills its empty session (regression guard that this change didn't touch it). | Mock `_tmux_mutate`; real state/DB. |
| `app.py` backfill ordering | **Not separately tested** — the lifespan tests mock the worker (CLAUDE.md). Cover the function via `backfill_pane_projects` unit tests; the *blocking-before-yield* placement is a code-review invariant, not a unit test. | n/a |
| Rail behavior | **Browser-verified** (repo norm): drag claude→workspace, close origin worktree, claude survives. | Real. |

No testability flags — keeping `placement_kill_set` pure is precisely what
avoids the "logic only reachable through a hard-to-construct object" smell. The
two route killers are tested at the route boundary with `_tmux_mutate` mocked,
which is the existing pattern and asserts the call shape (`kill-pane` not
`kill-session`) — the meaningful regression surface.

## Decisions to sanity-check

1. **Placement helper home: `projects.py` vs a new `placement.py`.** Chose
   `projects.py` — the rule is project-grouping logic and shares the `MAIN_KEY`
   guards. Close because the helper also touches `_focused_at`-adjacent concerns
   conceptually (the routes do the cleanup) and reads `activity`, so one could
   argue for neutrality in a new module. Resolved by YAGNI: it is one function;
   a module is over-structure until the collapse follow-on adds more
   placement logic (at which point promoting it is a trivial move).

2. **`projects.py → activity.py` import direction.** Chose a top-level import
   (acyclic today). Close because the codebase is cycle-sensitive (the
   `narrator` function-level-import precedent). If planning surfaces any cycle,
   the fallback is a function-level import inside the three new functions — same
   pattern, no structural change. Worth a 30-second `grep` during planning.

3. **`__main__` refusal: raise vs empty set.** Chose raise (`ValueError`).
   Close because the spec's test table says "yields no kill set," implying empty.
   Resolved toward raise: a silent no-op hides a future caller's missing guard,
   and both current callers guard upstream so the raise never fires in normal
   flow — it is a boundary assertion, not control flow.

4. **Backfill writes `MAIN_KEY` rows for unmanaged panes, or skips them.**
   **DECIDED (Tom): skip** — a row means "belongs to managed project X";
   unmanaged/external panes stay untagged and resolve to dev by default. Cleaner
   row semantics, still byte-identical at cutover, no rows the kill-helper always
   refuses.

5. **Per-target focus cleanup: shared `drop_target_focus` vs inline per route.**
   **DECIDED (Tom): extract** `drop_target_focus(target)` in `panes.py`, called
   by all four per-target sites (close, cleanup, `window_move`, `window_delete`).
   Keeps `placement_kill_set` pure while centralizing the dict-pop so it cannot
   drift — the rule-of-three is already exceeded.
