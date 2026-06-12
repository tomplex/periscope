# Session-anchored rail design

2026-06-12

## Problem

The rail groups windows by `repo_key`, which `git_pr.py` derives from each
pane's *current cwd* on every poll. cwd is volatile identity: a `cd` in a
tab moves its row to a different repo group (and today also blanks the
detail pane — see "Known bug" below). The "Other" bucket collects every
non-git cwd, so the user's main session smears across "Other" and whatever
repo group its tabs happen to sit in.

The server already has the right model and the rail ignores it:
`projects.py` defines a project as pinned_dir + tmux session, resolves
windows to projects **by tmux session** (stable across cd), and has a
`__main__` sentinel whose spec says tabs in it are not constrained by
repo. `window_view.py` emits `project_pinned_dir` / `project_name` /
`worktree_affiliation` per window, and `/api/state` ships the `projects`
list. This design wires the rail to that model.

## Rule

**A tab belongs to its tmux session's project. cd never moves it in the
rail.** cwd becomes display metadata (an affiliation chip), not identity.

Sessions with no registered project fold into `__main__`, presented as
**"dev"** — one catch-all, no "Other" bucket.

## Server changes (small)

1. **`resolve_project_for_window`**: a session with no matching project
   row resolves to `MAIN_KEY` instead of `None`. This is the fold-to-dev
   rule, applied at the source so every consumer agrees. (`window_view`'s
   `pinned_for_aff` guard already treats `__main__` as unpinned; unchanged.)
2. **`__main__` presents as "dev"**: the sentinel stays unpinned in state
   (no migration, no spec amendment). The two behaviors the "pin" was for
   are handled directly:
   - UI label is "dev".
   - `_window_new_plain` (routes/sessions.py): when the resolved project
     is `MAIN_KEY`, cwd defaults to `~/dev` instead of inheriting the
     active pane's cwd. This also covers folded unmanaged sessions —
     consistent with the one-rule fold, and acceptable drift from today's
     pane-cwd inheritance.

   Side effect of the fold worth knowing: `window_new_worktree`'s
   "session not owned by a project" 400 becomes unreachable (every
   session now resolves to something); unmanaged sessions land in the
   "not supported in the main project" 400 instead. Fold the two checks
   into one accurate message.

## Rail tree (frontend)

```
PROJECTS
▾ fdy                       ← group key: project.repo (stable)
  ▾ master                  ← project row (= session; key unchanged)
    ▪ claude
    ▪ shell  ⧉ periscope    ← cd'd away: chip, not a move
  ▾ feature-x
    ▪ claude
▾ dev                       ← __main__, pinned bottom (replaces "Other")
    ▪ shell   ⧉ fdy/master  ← every dev pane gets a location chip
    ▪ claude  ⧉ periscope
    ▪ scratch ⧉ ~/tmp       ← folded ad-hoc session's pane
```

- **Grouping key**: window → project via `project_pinned_dir`; project →
  repo group via the project row's `repo` field. The cwd-derived
  `repo_key` survives only as display data (chips, labels).
- **Repo level stays**: repos with multiple worktree projects (fdy) keep
  the Repo → Project → panes shape. A project whose `repo` is null is its
  own top-level group.
- **Affiliation chip** on pane rows when cwd ≠ project pin.
  `worktree_affiliation` (at-pin / sibling / off-repo / no-repo + label)
  is already computed server-side; the rail renders `⧉ <label>` for
  anything that isn't at-pin. Sibling worktree → branch name; off-repo →
  repo basename (+ branch when known); no-repo → `~`-relative cwd.
- **dev is a flat pane list** (the project-model spec's original intent
  for main). Panes from folded ad-hoc sessions appear directly in it;
  their session name rides in the chip rather than a sub-group. Every dev
  pane shows a location chip (dev has no pin): repo/branch when
  git-backed, `~`-relative cwd otherwise.
- **dev's "+ New tab" row** targets the `__main__` project's
  `tmux_session` (the row is per-session today; dev's flat list needs an
  explicit target). Folded ad-hoc sessions get no new-tab row of their
  own — spawning into them still works via the API, just not from the
  rail.
- **dev pinned to the bottom**, not draggable — same enforcement points
  `OTHER_REPO_KEY` has today (merge, isValidDropTarget, reorderRepos,
  RepoRow drag gate). `OTHER_REPO_KEY` itself is retired; `__main__` is
  the sentinel everywhere.

### Prefs migration

- `worktrees_by_repo` values and `panes_by_worktree` keys are session
  names — carry over unchanged.
- `repo_order` keys change from cwd-repo paths to project-repo paths.
  Old keys silently drop (the merge already prunes non-live entries) and
  order is re-learned once via `syncRailPrefs`. One-time, self-healing;
  no migration code.

## Known bug, separate workstream: detail blanks on cd

Selection and detail lookup are both pid-keyed (`pane:<pid>` →
`lookupWindow(pid)`), and `@periscope_id` is a tmux window option that
should survive cd — yet the detail pane goes to the empty state when a
tab cd's. That means the **pid itself churns on cd** (or the window
transiently drops from `/api/state`). Session-anchoring stops the rail
churn but cannot fix this; it is fixed independently:

1. Live repro: cd a pane across repos while watching its `/api/state`
   pid.
2. Prime suspect: `pids._resolve_one`'s rebind / duplicate-claim path.

## Testing

- `railTree.js` stays pure → unit tests for the new merge: registered
  session grouping, fold-to-dev, chip data passthrough, dev-at-bottom,
  pref-order carryover.
- Server: a test for fold-to-`MAIN_KEY` resolution in
  `resolve_project_for_window`.
- Chips, launcher default cwd, drag behavior: browser-verified.

## Out of scope

- Promoting a folded session to a real project from the rail (use the
  existing project creation flow).
- Clickable chips (jump-to-location), grouping dev by inferred repo —
  polish, post-v1 if wanted.
