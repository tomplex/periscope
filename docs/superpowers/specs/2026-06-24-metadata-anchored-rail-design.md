# Metadata-anchored rail — demoting the tmux session from identity to container

## Problem

Closing a worktree in the rail killed a tab the user had dragged into a
workspace. Root cause: a tmux session does **double duty** — it's both the
runtime container for panes *and* the semantic identity periscope groups and
bulk-operates on. Closing a worktree is `tmux kill-session` (`sessions.py:62`),
which kills the whole session unconditionally; a pane dragged into a workspace
is only *re-tagged* (`pane_workspaces`, a `pane_id → workspace_id` row) and
never physically moved, so it still lives in its origin session and dies with
it. The rail shows it in the workspace; tmux still owns it in the session; close
operates on the session. The two axes disagree and the pane is collateral.

This is not a one-off. Workspaces already proved the pattern: rail membership
*can* be a per-pane metadata tag independent of the tmux session. The fix is to
finish that trajectory — periscope went cwd-grouping → project-grouping (the
2026-06-12 "session-anchored rail"); this is the next step, project-grouping →
**metadata-grouping**, where the tmux session is a pure runtime container and
group membership lives entirely in per-pane metadata.

## Goal / non-goals

**This spec (the semantic fix):** make rail grouping and every destructive
group operation read per-pane metadata instead of the tmux session. After this,
the bug is gone *as a consequence of the model*, not as a special case, and the
groundwork is laid so the physical collapse is pure plumbing.

**Non-goal (immediate follow-on, separate spec + task set):** physically
collapsing every pane into a single tmux session (`move-window` of live panes,
one control-mode mirror instead of N, deleting the session-name-as-identity
machinery in `open_ops`). It runs right after this lands. It is explicitly out
of scope here to keep the live-pane-moving risk off the bug fix.

## The model

A pane carries **two independent facts**, both per-`pane_id` metadata:

1. **Project context** — the pane's worktree home. Drives the affiliation chip,
   repo label, and git display, and is the *default* rail placement. Today this
   is *derived* from the session (`resolve_project_for_window` matches
   `window.session == project.tmux_session`, `projects.py:153`). **This spec
   makes it an explicit tag: a new `pane_projects` table, `pane_id →
   project_pinned_dir`.**
2. **Workspace placement override** — optional. The existing `pane_workspaces`
   tag (`pane_id → workspace_id`, `activity.py:218`). Unchanged.

**Rail placement** resolves exactly as today, now from metadata only:
- workspace override if present → top-level `ws:<id>` group, else
- project context → its `repo` group (nested worktree row), else
- no tag → `__main__` / dev fallback (covers external, periscope-unmanaged
  tmux sessions, preserving periscope's "watch every pane" purpose).

The two facts stay separate on purpose: a workspace-tagged pane keeps its
project context, so it still shows its repo/branch chip in the workspace.

## Changes

### Data (`periscope/activity.py`)

New `pane_projects` table mirroring `pane_workspaces` exactly (the codebase
already treats these as sibling per-`pane_id` tenants — `pane_sessions`,
`pane_workspaces`, `pane_status`):

```sql
CREATE TABLE IF NOT EXISTS pane_projects (
  pane_id    TEXT PRIMARY KEY,
  project    TEXT NOT NULL,        -- project pinned_dir (or __main__)
  updated_at INTEGER NOT NULL
)
```

Accessors mirror the `pane_workspaces` set: `set_pane_project`,
`get_pane_project`, `pane_project_map`, `prune_pane_projects(alive_pane_ids)`
(wired into the existing lifespan prune that already calls
`prune_pane_workspaces`).

### Backfill (one-shot, lifespan startup)

Seed `pane_projects` from today's grouping so the rail is byte-identical at
cutover: for every live pane with no `pane_projects` row, write the result of
the *current* session-match `resolve_project_for_window`. Idempotent; runs once
per startup before serving, alongside the existing
`migrate_legacy_pane_sessions` step.

### Resolution (`periscope/projects.py`)

`resolve_project_for_window` reads the tag first, falls back to the existing
session-match for any untagged pane:

```python
def resolve_project_for_window(window):
    pane_id = window.get("pane_id")
    if pane_id:
        proj = activity.get_pane_project(pane_id)
        if proj:
            return proj
    # fallback: untagged (external session / pre-backfill race) → session match
    ... existing session-match body ...
```

The session-match fallback is retained *only* for this phase — it's the safety
net for external/unmanaged panes and the collapse follow-on deletes it (after
collapse there's one session and the fallback is meaningless). `window_view.py`
is unchanged: it still calls `resolve_project_for_window` (`:172`) and emits
`project_pinned_dir` + `workspace_id`; both are now metadata-sourced.

### Tag on create (`periscope/open_ops.py`)

Every pane periscope creates gets a `pane_projects` row at creation, next to
where the rail pref is written (`place_in_rail`, `open_ops.py:203`). The claude-
pane `pane_id` is already recovered there (`:198`); the shell pane's is in the
same `list_windows()` scan. `spawn_claude` / worktree-spawn paths tag too.

### Destructive ops become placement-aware (the bug fix)

All three `kill-session` callers stop killing the session and instead kill the
panes **whose rail placement is the group being closed** — i.e. project-context
matches AND no workspace override — via `kill-pane` by stable `pane_id`:

- `routes/sessions.py:62` — rail "close worktree". The dragged-into-workspace
  pane has a workspace override → not in the placement set → **survives.**
- `routes/cleanup.py:68` — cleanup modal stale-worktree teardown. Same rule.
- `routes/projects.py:267` — project delete/archive. Same rule.

A shared helper (placement → list of `pane_id` to kill) backs all three so the
rule lives in one place. If killing the placement set empties the underlying
tmux session, tmux drops it naturally; if a workspace-tagged pane remains, the
session lingers as its host until the collapse follow-on removes per-worktree
sessions entirely. That lingering session is the one accepted wart of the
interim phase.

### Frontend

No change to `railTree.js` grouping in this spec — the worktree rows stay keyed
by session while session and project remain 1:1, and the rail reads the same
`project_pinned_dir` / `workspace_id` fields (now metadata-sourced). See
Decision 1: re-keying worktree rows session→project is collapse-prep and its
placement in this spec vs the follow-on is the one open call.

## What the bug becomes (verification scenario)

1. New worktree → project P, session S = `[claude, shell]`, one worktree row.
2. Drag the claude tab into workspace W → `pane_workspaces[claude] = W`. Rail
   shows claude under W, shell under P's worktree row. claude keeps its
   `pane_projects[claude] = P` context (repo chip still shows).
3. Close P's worktree row → kill the **placement set of P** = panes with
   project P and *no* workspace override = `{shell}`. `kill-pane shell`.
4. claude has a workspace override → excluded → **alive.** Session S lingers
   hosting claude until collapse. Bug fixed.

## Edges / invariants

- **`pane_id` stability** across mirror/channel reconnects — it's the tmux `%N`,
  stable for the pane's life; already the key for `pane_workspaces` /
  `pane_sessions`, so this introduces no new assumption.
- **External/unmanaged sessions** stay observed and fold to dev via the
  no-tag→fallback path.
- **Archiving a workspace** still folds its panes back to project placement
  (`resolve_workspace_for_window` returns None for archived) — unchanged.
- **Prune** dead `pane_projects` rows in the same lifespan sweep as the other
  per-pane tables, so killed panes don't leave stale context tags.
- **Backfill must precede serving** or untagged panes briefly fall back to
  session-match — harmless (same answer) but the ordering matters for the
  collapse follow-on where the fallback is gone.

## Testing

| Surface | Approach |
|---|---|
| `activity.pane_projects` | pytest unit (mirror `pane_workspaces` tests): set/get/map/prune round-trips; prune drops dead pane ids. |
| backfill | pytest: pre-seeded session→project state produces identical `pane_projects` rows; idempotent on re-run; untagged-only panes covered. |
| `projects.resolve_project_for_window` | pytest (extend `test_projects.py`): tag wins; untagged falls back to session-match; external session → MAIN_KEY; empty session → None. |
| placement-kill helper | pytest unit: given a project + workspace-tagged panes, returns exactly the non-overridden panes' `pane_id`s. |
| 3 route killers | pytest route tests (TestClient + mocked `_tmux_mutate`): assert `kill-pane` issued for placement panes only, workspace-tagged pane spared; assert the `kill-session` calls are gone. |
| rail behavior | browser-verified (repo norm): drag claude→workspace, close origin worktree, claude survives. |

## Decisions to sanity-check

1. **Re-key rail worktree rows session→project in this spec, or defer to the
   collapse follow-on?** Doing it now is *safe* (session==project is 1:1 today,
   so it's a no-behavior-change refactor) and makes the collapse a pure
   tmux-plumbing no-op. Deferring keeps this spec smaller but leaves the
   re-keying coupled to the riskier collapse step. **Recommendation: defer** —
   this spec is the bug fix + metadata demotion; the rail key is only load-
   bearing once sessions actually merge.
2. **Retain the session-match fallback** in `resolve_project_for_window` through
   this phase (deleted in the follow-on) vs. require full backfill coverage now.
   Recommendation: retain — it's the robustness net for external panes and any
   pre-backfill race, and removal is naturally the collapse step's job.
3. **One shared placement-kill helper** vs. inlining the rule in each of the
   three routes. Recommendation: shared helper — the "placement = project ∧ ¬ws
   override" rule must not drift between close, cleanup, and delete.
4. **`project` column stores `pinned_dir`** (the project identity) rather than a
   synthetic project id — projects have no id, `pinned_dir` *is* the key
   (`projects.py:3`), and `pane_workspaces` already stores the workspace's
   string id, so this matches the sibling table.
