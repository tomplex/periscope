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
   tmux **`pane_id`** (`%N`), *not* on a session name and *not* on
   `@periscope_id`. `pane_id` is invariant across `cd`/rename for the pane's
   whole life and dies exactly when the pane dies — which matches the v1 "tag
   dies with the tab" semantics (decision 4). It is also the key every existing
   per-pane table (`pane_sessions`, `pane_status`) uses, so the tag map reuses
   their dead-pane prune verbatim, and it is the only id the narrator worker
   tick has in hand (the tick gets `pid_raw`, never the resolved `@periscope_id`).
   `@periscope_id` was rejected here: its re-mint path silently orphans
   pid-keyed state (`pids.py` warns of exactly this), and it isn't indexable
   from the narrator tick. Durable-across-server-restart membership is the
   future roster layer's concern (Non-goals 2), not v1's.

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

- A map `pane_id → workspace_id`, stored in `periscope.db` alongside
  `pane_sessions` / `pane_status` (the entity dict lives in `state.json`; the
  tag map lives in the db so it shares the dead-pane prune).
- Pruned by the **existing** dead-pane reaper: the lifespan housekeeping builds
  `alive = {w["pane_id"] for w in list_windows()}` and calls
  `prune_pane_sessions(alive)` / `prune_pane_status(alive)` (`app.py` startup);
  a `prune_pane_workspaces(alive)` joins that same call site, keyed identically.
- The `state.json` half (`workspaces` dict) is never pruned by pane liveness —
  it persists until explicitly archived/GC'd (see Lifecycle).

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
   branch/affiliation chip sits inline on line 1 (`pane-row-main`) between name
   and status dot and eats the name's width, and line 2 renders **only when
   `w.status_line` is truthy**. New layout:
   - **Line 1 = identity:** tab name (full width) + status dot.
   - **Line 2 = context + activity:** branch/worktree chip · narrator summary.
   - **Line 2 now renders when `chip || status_line`** — not status-only.
     Without this, a chip-but-no-status tab (shell tabs, brand-new Claude panes
     before their first narrator line) would lose its chip entirely.
   - In a narrow rail the **chip truncates first** — it is the lower-signal half;
     the narrator summary (the verb) is what gets scanned.
   This is a render change to **every pane row, not workspace tabs only**
   (including dev panes, whose `paneChip(w, {isDev:true})` session-prefix chip
   must keep working in the new line-2 slot) — a larger blast radius than the
   workspace feature alone, called out so the plan tests all row variants.
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
  workspace. `_layout_two_window` stamps the spawned pane synchronously, so its
  `pane_id` is known in-band and the tag is written before the response returns
  (no next-poll wait). If the workspace has a `base_worktree`, the new work
  defaults to branching from it — but `spawn_worktree(repo, branch, base_branch=…)`
  takes a *branch name*, while `base_worktree` is a *path*, so the route first
  resolves `base_branch = git -C <base_worktree> rev-parse --abbrev-ref HEAD`
  and passes `fetch=False` (the base is typically an unpushed local feature
  branch — the existing new-tab convention).
- **Archive / delete:** explicit, mirrors `archive_project` — archived
  workspaces are hidden from the `/api/state` payload. A `_gc_workspaces`
  mirrors `_gc_projects` (30-day GC of archived rows); without it archived
  workspaces would accrete forever.

## Workspace-aware narrator (v1, sequenced last)

**Sequencing:** this lands *after* the entity + tag map + rail + at least one
tagging surface, as its own isolated commit(s). It depends on the tag map and
on `workspace_id` resolution, and it touches a dense-invariant subsystem; keeping
it separable preserves the `git revert` recovery path (the commit-as-you-go
convention) even though it ships in the v1 cut.

`narrator.py`'s per-pane tick gains workspace context. This is **new data
plumbing**, not merely a richer prompt string — the tick must:

- Resolve each pane to its `workspace_id` via the tag map. The tick already
  holds each window's `pane_id` (`w.get("pane_id")`, used throughout `tick`), so
  with `pane_id`-keyed tags this is a direct lookup — no resolve-pid step (the
  tick never runs `resolve_pids`; it only has `pid_raw`).
- Assemble **sibling tab names** with one pass over the tick's `panes` list,
  grouping live window names by `workspace_id`. This is a new per-tick index.
- Feed workspace name + siblings into the Haiku prompt (a slightly larger call).

`RENAME_RULES` (shared with `rename_ai.py`) gains a clause: *when a workspace is
supplied, don't repeat the goal in the name; carry what distinguishes this tab
from its siblings.*

The return shape (`{status, rename}`), the human-rename cooldown, and the
session-id-first regeneration are **unchanged** — the new work is the
workspace/sibling lookup feeding the prompt, not the decision/apply control flow.

## Modules & files

| Module | Role |
|---|---|
| `periscope/workspaces.py` (new) | Entity CRUD (`state.json`) + tag map (`pane_id → ws_id`, `periscope.db`) + `resolve_workspace_for_window` + `prune_pane_workspaces(alive)` + `_gc_workspaces` |
| `periscope/store.py` | `workspaces` dict in `state.json` + migration |
| `periscope/app.py` | lifespan: add `prune_pane_workspaces(alive)` to the existing prune call site |
| `periscope/window_view.py` | emit `workspace_id` per window (tag-map lookup by `w["pane_id"]`) |
| `periscope/routes/workspaces.py` (new) | REST: create / promote / tag / untag / archive / delete |
| `periscope/open_ops.py` | spawn-into-workspace (pre-tag the spawned tab; honor `base_worktree`) |
| `periscope/narrator.py` | workspace-aware prompt (name + siblings; don't-repeat-goal rule) |
| `periscope/rename_ai.py` | `RENAME_RULES` gains the workspace clause (shared taste block) |
| `/api/state` (`routes/state.py`) | new `workspaces` payload (like `projects`) |
| `static/src/split/railTree.js` | merge gains a workspace-grouping pass **before** the repo fallback; `ws:<id>` keys adopt the **dev-flat (`MAIN_KEY`) child shape** (flat pid list, synthetic child key) so the existing pane drag rules apply unchanged; persistent workspaces always render; line-2 chip in the row shape |
| `static/src/split/Rail.jsx`, `RailRows.jsx` | workspace groups, parked state, chip moved to line 2, spawn-into affordance |
| `static/src/overlays/OpenOmnibox.jsx`, `open/classify.js` | "new workspace" + spawn-into + promote actions |
| `static/src/store.js`, `poll.js` | `workspaces` signal fed from `/api/state` |
| `static/dist/app.js` | rebuilt + committed per repo convention |

## Testing

| Module | Approach |
|---|---|
| `railTree.js` | **vitest unit**: tagged window → workspace group **and removed from repo fallback** (exactly one group); untagged → repo fallback unchanged; empty (parked) workspace renders; top-level interleave ordering (`ws:` keys kept, not bottom-pinned); `ws:<id>` dev-flat child shape + pref carryover; line-2 chip renders on `chip || status_line` (incl. chip-without-status); old prefs self-heal. |
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

## Open implementation wrinkles for the plan

1. **Single-top-level-group, via the dev-flat shape.** The workspace pre-pass
   runs before `groupKeyForWindow`'s repo/`MAIN_KEY` partition and diverts any
   tagged window into its `ws:<id>` bucket, so a tagged window appears in
   **exactly one** top-level group (it must *not* also land in repo fallback).
   `ws:<id>` groups are modeled on `MAIN_KEY` (dev), not on repo groups: a flat
   `panesByWorktree["ws:<id>"]` pid list with `worktreesByRepo["ws:<id>"] = []`
   and a synthetic child key — this is what makes the existing pane drag
   descriptors (`isValidDropTarget`'s same-`worktreeKey` rule) apply without new
   plumbing. The mockup (flat tabs inside a workspace) already matches this.

2. **The five `MAIN_KEY` enforcement points + `syncRailPrefs` each need a
   `ws:`-key sibling decision.** Unlike `MAIN_KEY`, workspace keys are **kept and
   interleaved** in `repo_order` (not bottom-pinned, not stripped). `syncRailPrefs`
   must persist `panesByWorktree["ws:<id>"]` (like it does for `MAIN_KEY`) *and*
   keep `ws:<id>` in `repo_order` (unlike `MAIN_KEY`, which it strips). The plan
   must enumerate all of these.

3. **`pane_id` vs `@periscope_id` coexist by design.** Existing rail worktree
   membership keys on tmux session name; per-pane state (`pane_sessions`,
   `pane_status`, and now the workspace tag) keys on `pane_id`; `@periscope_id`
   remains the drag/detail identity. The workspace pass keys on `pane_id` and
   never touches the session-name keying — they don't interact.
