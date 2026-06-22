# Workspaces — goal-scoped work above the worktree

## Problem

Periscope's rail groups work **by repo** (session-anchored): every top-level
item is a registered project (a repo), with worktrees as sub-rows, and a pane
that `cd`s away keeps its slot and shows an affiliation chip. Unmanaged work
folds into the bottom-pinned `dev` group.

That model has no unit for a **goal** — a long-running initiative that spans
several worktrees/branches toward one end, persists across sessions, and (later)
accrues its own shared state. Today such work is scattered: the worktrees sit
under their repo group with nothing tying them to the goal they serve.

The evolution is one consistent move, applied one level up. The rail already
decoupled grouping from **cwd** (v2: cwd became a chip, not a move). Workspaces
decouple grouping from **repo**: the goal becomes the anchor, and the repo
demotes to a tag/chip — the same move, one rung higher.

## What a workspace is

A **workspace** is a named, persistent, goal-scoped container:

- It groups work toward one **end goal** (e.g. "Auth refactor").
- It is **tagged with a base repo**, but does not strictly nest under it — a
  workspace and a plain repo group are peers at the top of the rail.
- It **persists** as a named entity even when nothing is live (driver: park &
  resume an initiative).
- It **optionally designates a base worktree** that other work branches from.
- It is the future attach point for **shared state / a document repository**
  (explicitly out of scope for v1 — see Non-goals).

Cross-repo workspaces are out of scope. A workspace is near-always single-repo;
`base_repo` is a property, not a hard constraint enforced on members.

## Decisions (settled during brainstorming)

1. **Workspace alongside repo, not replacing it.** The current repo grouping
   stays as the fallback. Workspaces are an additive, opt-in top-level item.
   Untagged work behaves exactly as today — nothing regresses.

2. **Membership is an explicit per-tab tag, not inferred.** A tab (a Claude
   pane) carries a `workspace_id`. Membership is *not* derived from cwd, path,
   or tmux session. An untagged tab sorts normally through the existing
   repo/dev fallback. A tab belongs to at most one workspace (single field →
   single-membership for free).

3. **tmux sessions are a backend primitive, never a user-facing unit.**
   Periscope is free to map a workspace's tabs onto one session or many; the
   user never reasons about sessions. Membership therefore keys on the tab's
   periscope identity (`@periscope_id`), not on a session name.

4. **v1 persistence = "just the named shell."** A workspace persists only as a
   named entity plus `base_repo` / `base_worktree`. When nothing is live it is
   an empty parked container you re-tag tabs into, or spawn a fresh tab from its
   base. No roster of past worktrees, no resumable-session memory yet — those
   are deliberate follow-ons (see Non-goals).

5. **New top-level entity, not a virtual project.** The workspace is its own
   entity in `state.json` (parallel to `projects`), with its own module — not
   an overload of `projects.py`'s resolve-by-session machinery, and not a
   frontend-only rail-pref construct (which could not persist when nothing is
   live, nor host shared state / an MCP surface later).

6. **Workspace-aware naming is in v1.** The narrator factors the workspace into
   tab names: within a goal, the name carries what is *distinct* about the tab,
   and the goal name is context the narrator is told **not** to repeat.

## Data model

A new top-level dict in `state.json`, like `projects`:

```
workspace = {
  id:            "ws_<slug>",                        # stable key; used in rail prefs + API
  name:          "Auth refactor",                    # display
  base_repo:     "/Users/tom/dev/fdy" | null,        # the repo "tag"; stored so a
                                                      #   workspace can exist before any tab
  base_worktree: "/Users/tom/dev/fdy-auth" | null,   # optional base others branch from
  created_at:    <iso>,
  archived_at:   <iso> | null,                       # archived → hidden from payload
}
```

There is **no `members` list.** Membership is live, via a per-tab tag:

- A map `periscope_id → workspace_id`, stored keyed by the pane's
  `@periscope_id`.
- Pruned when the pane no longer exists — same lifecycle as the `pane_sessions`
  prune (`migrate_legacy_pane_sessions` / dead-pane reaping pattern).
- Storage location: the tag map lives wherever the prune cadence is cheapest to
  share with existing pane reaping; `store.py` owns the `workspaces` dict, the
  tag map can live alongside `pane_sessions` in `periscope.db` or in state —
  the plan picks one (the prune-with-dead-panes requirement is the deciding
  constraint, not where the bytes sit).

**Single-membership invariant:** a tab has exactly one `workspace_id` or none.
Tagging a tab already in another workspace re-tags it (move, not duplicate).

## Rail rendering

Workspaces interleave with repo groups in the **single shared top-level ordered
list** (the existing `repo_order`-style pref gains workspace keys, e.g.
`ws:<id>`). Persistent (non-archived) workspaces **always render**, even parked:

```
▸ Auth refactor            ⟨fdy⟩            ← workspace group; base_repo as a chip
    *  rename-flow                      ●    ← tagged tab: name owns line 1 + status dot
       ⧉ auth-ui  ·  reworking the form     ← line 2: branch chip + narrator summary
    *  token-store                      ●
       ⧉ auth-core  ·  writing migration
    + spawn into workspace
▸ fdy                                        ← plain repo group (untagged work) — UNCHANGED
    ▸ master / fdy-perf …
▸ Cache rework             ⟨periscope⟩       ← parked workspace (nothing live)
    spawn from base · periscope-cache
▾ dev                                        ← unmanaged fold — UNCHANGED
```

Rendering rules:

1. **Tabs render flat inside a workspace, with a branch/worktree chip** — not
   nested worktree sub-rows. The worktree shows as a chip (like cwd does today),
   honoring the "don't strictly nest" intent.
2. **Repo shows as one chip on the workspace header**, not per-tab (near-always
   single-repo).
3. **The chip moves to the tab's metadata line (line 2).** Today the
   branch/affiliation chip sits inline on line 1 between name and status dot and
   eats the name's width. New layout:
   - **Line 1 = identity:** tab name (full width) + status dot.
   - **Line 2 = context + activity:** branch/worktree chip · narrator summary.
   - In a narrow rail the **chip truncates first** — it is the lower-signal half;
     the narrator summary (the verb) is what gets scanned.
   This new line-2 layout applies to tab rows generally, not only workspace tabs.
4. **Parked workspace** renders its header + base affordance (`spawn from base ·
   <base_worktree-or-repo>`), no tab rows.

## Lifecycle

- **Create:** header `+ new` / omnibox gains "new workspace" → name + optional
  base repo → empty parked workspace.
- **Promote (emergent goal):** select live tab(s) → "group into workspace" →
  creates the workspace and tags them in one step.
- **Tag / untag:** drag a tab into a workspace group, or a row action; untag
  reverts the tab to normal sort. Re-tagging moves (single-membership).
- **Spawn into:** the open-omnibox can spawn a new tab **pre-tagged** to a
  workspace; if the workspace has a `base_worktree`, the new work defaults to
  branching from it.
- **Archive / delete:** explicit, mirrors `archive_project` — archived
  workspaces are hidden from the `/api/state` payload.

## Workspace-aware narrator (v1)

`narrator.py`'s per-pane tick gains workspace context:

- Look up the pane's `workspace_id` → workspace name + the **sibling tab names**
  in that workspace.
- Feed both into the Haiku prompt.
- `RENAME_RULES` (shared with `rename_ai.py`) gains a clause: *when a workspace
  is supplied, don't repeat the goal in the name; carry what distinguishes this
  tab from its siblings.*

The return shape (`{status, rename}`), the human-rename cooldown, the
session-id-first regeneration, and all other narrator invariants are unchanged —
this is strictly better prompt context, not new control flow.

## Modules & files

| Module | Role |
|---|---|
| `periscope/workspaces.py` (new) | Entity CRUD + tag map (`periscope_id → ws_id`) + `resolve_workspace_for_window` + prune-dead-tags |
| `periscope/store.py` | `workspaces` dict in `state.json` + migration; tag-map persistence |
| `periscope/window_view.py` | emit `workspace_id` per window (lookup by `@periscope_id`) |
| `periscope/routes/workspaces.py` (new) | REST: create / promote / tag / untag / archive / delete |
| `periscope/open_ops.py` | spawn-into-workspace (pre-tag the spawned tab; honor `base_worktree`) |
| `periscope/narrator.py` | workspace-aware prompt (name + siblings; don't-repeat-goal rule) |
| `periscope/rename_ai.py` | `RENAME_RULES` gains the workspace clause (shared taste block) |
| `/api/state` (`routes/state.py`) | new `workspaces` payload (like `projects`) |
| `static/src/split/railTree.js` | merge gains a workspace-grouping pass **before** the repo fallback; persistent workspaces always render; line-2 chip in the row shape |
| `static/src/split/Rail.jsx`, `RailRows.jsx` | workspace groups, parked state, chip moved to line 2, spawn-into affordance |
| `static/src/overlays/OpenOmnibox.jsx`, `open/classify.js` | "new workspace" + spawn-into + promote actions |
| `static/src/store.js`, `poll.js` | `workspaces` signal fed from `/api/state` |
| `static/dist/app.js` | rebuilt + committed per repo convention |

## Testing

| Module | Approach |
|---|---|
| `railTree.js` | **vitest unit**: tagged window → workspace group; untagged → repo fallback unchanged; empty (parked) workspace renders; top-level interleave ordering; line-2 chip shape; old prefs self-heal. |
| `workspaces.py` | **pytest unit** against `clean_state`: CRUD, tag/untag, single-membership (re-tag moves), prune-on-dead-pane, archived hidden. |
| `routes/test_workspaces.py` (new) | **pytest route tests** (TestClient + mocked tmux, established pattern): create / promote / tag / untag / archive / delete; error conventions (`HTTPException`, real status codes). |
| `narrator.py` | **pytest**: workspace name + siblings present in the prompt; don't-repeat-goal rule applied; human-rename cooldown still holds; no-workspace path unchanged. |
| `window_view.py` | **pytest**: `workspace_id` emitted for tagged windows, absent/null for untagged. |
| `open_ops.py` | **real-tmux integration** (`@needs_tmux`, isolated `-L` socket + stub exec): spawn-into-workspace pre-tags the new tab; `base_worktree` honored. |
| `Rail.jsx` / `RailRows.jsx` | **Browser-verified** (repo norm): workspace groups render, parked state, chip on line 2, drag-to-tag, promote, spawn-into. |

## Non-goals (deliberate follow-ons)

1. **Shared state / document repository.** The workspace entity is the attach
   point; no `state_dir`, no docs panel, no MCP doc surface in v1. No dead field
   is added now.
2. **Persistence roster.** v1 remembers no past worktrees/sessions (decision 4).
   A future layer can durably record a workspace's worktrees and/or resumable
   Claude sessions so a parked workspace lists resumable work.
3. **Cross-repo workspaces.** Single-repo assumption holds; widen only if the
   need appears.
4. **MCP exposure.** `spawn_claude` gaining a `workspace` param (so a Claude can
   fan out work into a workspace) is a natural extension, not v1.

## Open implementation wrinkle for the plan

The current rail keys worktree membership by tmux **session name**; workspace
membership keys on `@periscope_id`. These two keying schemes coexist (workspaces
are a separate top-level pass) but the plan must confirm the merge cleanly
routes a tagged window into its workspace group and *removes* it from the
repo-fallback grouping for that poll — a tagged window must appear in exactly
one top-level group.
