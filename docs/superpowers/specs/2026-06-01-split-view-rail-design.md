# Split-view rail — design spec

**Date:** 2026-06-01
**Status:** draft, awaiting review
**Author:** Tom + Claude (brainstorm session)

---

## Summary

Add **split view** as periscope's new default top-level view: a persistent
three-level left rail (Repo → Worktree → Pane children, plus an auto-generated
`review` child per worktree) and a persistent right detail pane. Replaces
the click-to-open-modal pattern *inside split view*. Grid and stream views
stay; they keep using the modal.

The motivation is that periscope is no longer "a dashboard you glance at" —
it's the surface Tom does all his work on. The current grid+modal pattern
optimizes for "survey then dive in"; split view optimizes for "always-on
workspace with low-friction switching."

## Goals

- A persistent rail of *curated* work units that Tom drags into the order he
  wants. Recency is a signal, not a sort key.
- Selecting any row updates a persistent right pane — no modal open/close
  cycle.
- LGTM reviews are first-class siblings of the Claude pane, not sub-tabs of
  the modal — Tom switches between writing code and reviewing it without
  changing the layout.
- Visual aesthetic borrowed from a reference design Tom shared: hover-only
  drag handles, tree connectors, per-row-type icons, subtle selection accent,
  truncation with ellipsis.

## Non-goals (v1)

- **No deletion of grid/stream views.** Both stay; split becomes the default,
  view toggle exposes all three. Pruning happens later once usage patterns
  are clear.
- **No replacement of the existing modal.** Modal continues to open from
  grid and stream. Eventually retires if those views retire.
- **No cross-view drag** (e.g., drag a card from grid into the rail).
- **No keyboard navigation beyond the basics.** v1 ships click + arrow
  up/down + enter; a full keymap is a separate later spec.
- **No per-repo label overrides.** Labels are auto-derived from the
  filesystem; a settings UI to override them comes later if collisions are
  real.
- **No auto-create of LGTM sessions.** A worktree's review row always
  exists, but if no LGTM session is running for that worktree yet, the row
  shows a `start →` affordance that the user clicks to create one.
- **No auto-prune of rail tabs.** Removal is always explicit (right-click →
  close, or drag-out).
- **No review row for non-worktree-backed sessions** (a bare-shell session,
  a session whose cwd isn't a git worktree). They appear in the rail like
  any other tab, just without a review child. Behavior for them is a later
  question.
- **No alerts-rail rework.** The existing right-side alerts overlay stays
  as-is; in split view it overlays the right pane the same way it overlays
  the grid today.

## Architecture overview

A new view called **split** is added alongside the existing `grid` and
`stream`. The header's view toggle becomes a 3-button switch.

When split is active:

- `main#grid` (today's grid container) is hidden.
- A new `aside#rail` mounts on the left (~320px wide, resizable later).
- A new `section#detail` mounts to its right and fills the remaining width.
- The existing `#modal` is unused.

```
header (unchanged chrome — filters, +new, history, alerts toggle, view switch)
─────────────────────────────────────────────────────────────────────────────
aside#rail (~320px)        │  section#detail
  Projects header + open  │    selected pane:   xterm + side metadata
  ▾ repo                  │    selected review: LGTM iframe (or start CTA)
    ▾ worktree            │    nothing:        empty state
      ✦ Claude pane       │
      👁 review            │
      $ shell              │
      + New tab            │
    ▸ worktree            │
  ▾ repo                  │
  ...                     │
```

## Data model

### Identity

| Entity   | Identity rule                                                                                                |
| -------- | ------------------------------------------------------------------------------------------------------------ |
| Repo     | `basename(parent(git_common_dir))` of any worktree of the repo. All worktrees of one repo share this key.    |
| Worktree | The tmux session name (one session per worktree, the `+ project` convention). Worktree cwd is the join key.  |
| Pane     | Existing `@periscope_id` minted by `periscope/pids.py`.                                                      |
| Review   | Identified by the worktree it belongs to (1:1). In persisted state, encoded as the literal string `"review"` inside `panesByWorktree[<worktree_key>]`. At most one review per worktree (matches LGTM's per-repo-path session model). |

Repo grouping is *derived*: a repo appears in the rail iff ≥1 of its
worktree-sessions is in the rail. Repo identity uses `git rev-parse
--git-common-dir`, then `basename(dirname(common_dir))`. This is robust to
sibling worktrees (the standard `~/dev/worktrees/<repo>/<branch>/`
layout) because all of them share a common dir. For inline worktrees
(`<repo>/.worktrees/<branch>`) the common dir is `<repo>/.git`, so the
basename also resolves to the repo. Edge cases (worktree dir renamed
manually outside periscope) are accepted: the auto-label may drift, the
override settings UI (deferred) is the fix.

### Persisted state (extends `prefs.js`)

Five new keys on the prefs document:

```ts
type RailState = {
  repoOrder: string[];                          // top-level drag order
  worktreesByRepo: Record<string, string[]>;    // per-repo worktree order (worktree key = session name)
  panesByWorktree: Record<string, string[]>;    // children order; "review" is a sentinel for the review row
  collapsed: Record<string, boolean>;           // keys: "repo:<name>", "wt:<session>"
  lastSelected: { kind: "pane"; pid: string } | { kind: "review"; worktree: string } | null;
};
```

The sentinel `"review"` (not a pane id) appears in `panesByWorktree[<wt>]`
exactly when that worktree is worktree-backed (cwd is a git worktree). It
encodes the review row's *position* among the pane children, which is
user-draggable. On first add of a worktree to the rail, the review row is
appended last by default.

### Status rollup

Per row, a status dot replaces today's letter glyphs (`!/◐/✓/$`):

| Row type | Source                                                                          | Dot color                                                          |
| -------- | ------------------------------------------------------------------------------- | ------------------------------------------------------------------ |
| Pane     | Existing `state` field on a window (`needs-input/working/done/idle/shell`)      | red pulse / green / blue / grey / none                             |
| Review   | LGTM session state for the worktree (existing API: `/projects/:slug/events`)    | green = unresolved feedback, grey = idle, red pulse = `need_human` |
| Worktree | Max-severity child state                                                        | rolled up: red > green > blue > grey > none                        |
| Repo     | Max-severity child worktree state                                               | rolled up: same priority                                           |

Severity priority used for rollup: `needs-input > working > done > idle >
shell`. Implemented as a small pure function reused by both worktree and
repo rows.

## Left rail behavior

### Entry

A worktree-session enters the rail in exactly three ways:

1. **`+ project`** creates one and auto-adds it (today the flow ends with
   the new session opened in a new tmux window; that's the natural moment to
   add the rail entry).
2. **`review PR`** creates a worktree and auto-adds it (same reasoning).
3. **`+ open` picker** explicitly added by the user. The picker (a new
   modal) lists every tmux session whose worktree isn't already railed,
   grouped by repo, multi-select.

Pre-existing tmux sessions are *not* added on first interaction. The grid
view continues to be the place to discover them; explicit `+ open` is the
bridge into the rail. This is item C from the brainstorm — the rail is
curated, not opportunistic.

### Exit

A row leaves the rail in exactly one way: explicit close via the row's
right-click context menu ("Close from rail") or by dragging the row out of
the rail surface. Closing from rail **does not** kill the underlying tmux
session or worktree — it just removes it from the rail. The session is
still visible in grid view.

When a tmux session is deleted *outside* the rail (e.g., via the existing
cleanup flow), its rail entry is removed automatically on next state poll.
The user's drag-order is preserved for everything else.

### Selection

| Click on…                | Effect                                                                          |
| ------------------------ | ------------------------------------------------------------------------------- |
| Repo row                 | Toggle its `collapsed` flag. Right pane unchanged.                              |
| Worktree row             | Toggle its `collapsed` flag. Right pane unchanged.                              |
| Pane row                 | Select → right pane shows terminal + side. Updates `lastSelected`.              |
| Review row (LGTM live)   | Select → right pane shows LGTM iframe. Updates `lastSelected`.                  |
| Review row (no LGTM)     | Select → right pane shows "Start review" CTA (one-click POST). Updates last.    |
| `+ New tab` row          | Opens the existing `commands-modal` palette, scoped to "add to this session".   |
| `+ open` (rail top)      | Opens the new picker modal.                                                     |

Selection is exclusive — at most one row selected. `lastSelected` is
persisted; on cold reload, periscope re-selects it. If the target is gone
(worktree closed, pane killed), the right pane falls back to the empty
state.

### Drag-and-drop

Drag handle is invisible until row hover (matches reference). Drop targets:

- A **repo row** can be dropped between any two repo rows. Cross-level
  drops are rejected.
- A **worktree row** can be dropped between two worktrees *of the same
  repo*. Cross-repo drag is rejected (no UX for re-homing a worktree).
- A **pane or review row** can be dropped between two children *of the
  same worktree*. Cross-worktree drag is rejected.

Drag state is local to the rail; persistence is one POST to `/api/prefs`
after the drop commits.

### Filters

The existing filter dropdown (all / needs-input / working / done / idle /
claude / shells / CI ✗) applies to the rail by **graying non-matching rows
in place** rather than removing them. This preserves the layout the user
built. A row whose worktree has any matching child remains fully visible
(so a filter doesn't hide a worktree that has a needs-input pane buried
inside a collapsed group — its rolled-up status dot still shows).

## Right pane behavior

| Selected row     | Header                                              | Body                                                                                |
| ---------------- | --------------------------------------------------- | ----------------------------------------------------------------------------------- |
| Pane             | worktree · branch · #PR · model · context%          | xterm (left) + side metadata panel (right) — same content as today's modal terminal |
| Review (live)    | worktree · branch · #PR                             | LGTM iframe full-width, no side panel                                               |
| Review (no LGTM) | worktree · branch · #PR                             | Centered CTA: "Start review for this worktree" → POST then iframe                   |
| Nothing          | —                                                   | Empty state: "Select a tab on the left, or `+ open` to add one" + `+ open` button   |

The xterm and LGTM iframe are mounted *inside* `#detail` and **persist
across selections of the same row**. Re-selecting the previously-selected
review or pane is free (no remount). Selecting a different row tears down
the previous mount.

A single xterm instance is reused — when selection changes from pane A to
pane B, the existing xterm reconnects its `/ws/pane` to B's pane and
re-issues the alt-screen + cursor-sync prefix described in `CLAUDE.md` key
invariant #3. This matches what `modal.js` does today on first open;
moving it into `#detail` is mostly relocation, not redesign.

## Chrome (top bar)

- View toggle becomes a three-button switch: **▤ split** (default) / **▦
  grid** / **≡ stream**. The existing Tab keybinding cycles through all
  three.
- Filters, `+ new`, history, `⋯` overflow, alerts toggle — all stay in
  place.
- Alerts rail: in split view it still slides in from the right and overlays
  the right pane (rather than pushing it). No layout reflow.
- The active view preference persists in `prefs.js` under
  `prefs.view` (existing field, currently `"grid" | "stream"`; gains
  `"split"`).

## Modal coexistence

The `#modal` element stays. Grid and stream views continue to open it on
card/row click — unchanged. Split view never opens the modal. The
xterm-attachment code currently inside `modal.js` is extracted into a
shared module (`terminal-mount.js`) consumed by both `modal.js` and the
new `detail.js`.

## Files added & touched (overview, not exhaustive)

New frontend modules:

- `static/rail.js` — left-rail rendering, drag/drop, selection state, click
  handlers.
- `static/detail.js` — right-pane rendering for the four states (pane,
  review-live, review-empty, empty).
- `static/terminal-mount.js` — extracted from `modal.js`; owns the single
  xterm instance and its reconnect-on-target-change behavior.
- `static/open-picker-modal.js` — the `+ open` picker.

Frontend modules touched:

- `static/index.html` — adds `#rail`, `#detail`, third view-toggle button.
- `static/app.js` — wires view switching across three views, delegates to
  `rail`/`detail` when in split.
- `static/grid.js` — hidden when split is active; unchanged otherwise.
- `static/stream.js` — same.
- `static/modal.js` — terminal-mount extraction; LGTM tab logic stays
  scoped to modal (still used by grid/stream).
- `static/prefs.js` — new `RailState` keys; loaders/setters.
- `static/state.js` — adds rail's transient state (drag-in-progress,
  current selection mirrored for fast read).
- `static/styles.css` — new rail/detail CSS following the reference
  aesthetic; uses existing CSS variables for colors.

Backend modules touched:

- `periscope/routes/prefs.py` — accepts the new rail keys (the prefs route
  is schema-light today, so this is largely a documentation update).
- `periscope/git_pr.py` (or a new `periscope/repos.py`) — adds a function
  to resolve `(session_name) → {repo_key, worktree_key, repo_label}` so
  the frontend doesn't have to shell out. Cached per session.
- `periscope/routes/state.py` — `/api/state` response gains `repo_key`,
  `repo_label`, `worktree_key` fields per window. (Or a sibling
  `/api/state/rail` view; chosen at implementation time.)
- `periscope/routes/lgtm.py` — already has session-create; the
  `start →` button reuses the existing `POST /api/lgtm/start` endpoint.

No backend routes are removed or renamed.

## Migration & rollout

- The view toggle defaults to **split** on a fresh load. Existing users
  whose `prefs.view` is set to `grid` or `stream` keep that view; their
  next manual switch to split will become their persisted default if they
  switch.
- Rail starts empty for everyone. The grid view still shows everything,
  so no work is lost; the user adopts the rail at their own pace via `+
  open` (and `+ project`/`review PR` start auto-adding immediately).

## Open implementation questions

These are *implementation* questions deliberately left for the plan phase,
not design questions:

- Does the `xterm` instance literally reattach across selections, or is a
  fresh instance per-selection cheap enough that we don't bother? (Today
  the modal does mount-on-open; either is acceptable.)
- Where exactly does the `repo_key` / `worktree_key` derivation live —
  per-window field on `/api/state`, or a sidecar `/api/repos` map? (The
  spec is agnostic; the plan picks.)
- LGTM events SSE: when a review row's LGTM state changes, the rail row's
  status dot updates. Today the LGTM mirror polls every 30s; the rail
  doesn't need a tighter loop, but live SSE per-session would be nicer.
  (Deferred unless cheap.)

## Future work (out of scope)

- Cross-view drag from grid into rail.
- Full keyboard navigation (jk movement, `g/G` jump, slash filter).
- Per-repo label overrides UI.
- Review row behavior for non-worktree-backed sessions (e.g., "scratchpad"
  diffs).
- Alerts rail folded into the left rail as a row type instead of a
  separate overlay.
- LGTM session auto-create on worktree first-add (vs the explicit `start
  →` click in v1).
- Retiring grid and stream views once split has confirmed adoption.
- Resizable rail/detail split with persisted width.

---
