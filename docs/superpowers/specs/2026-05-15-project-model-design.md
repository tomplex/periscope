# Project model — design

**Date:** 2026-05-15
**Status:** draft, awaiting review
**Author:** Tom + Claude

## Context

Periscope started as a tmux session viewer. It has since grown PR linking,
Linear linking, channel messaging, claude-status parsing, and per-window
metadata. The "session" concept it inherited from tmux no longer matches
what users actually do with it: each session is a *project* — a focused
chunk of work pinned to one directory (a worktree or a repo root).

A separate companion spec
(`2026-05-15-workflow-management-design.md`) ports trellis's workflow
features (new-project, new-tab, PR review, cleanup, history, adoption)
into periscope. That spec depends on this one; the data model and
terminology defined here are its substrate.

This spec also supersedes the `sessions[<name>].repo` block in
`2026-05-13-worktree-integration-design.md`. That spec's `+ claude
(worktree)` flow remains valid; only the persistence shape changes.

## Goals

1. Rename "session" to "project" at the UI and state-model layer. tmux
   remains the substrate; the server still talks to tmux sessions.
2. Make project identity = pinned directory, not session name. The same
   repo can back many projects (the dominant case for Tom's `fdy` work,
   where each `tc/...` feature is its own worktree-pinned project).
3. Make worktree affiliation per tab a first-class derived property,
   visible on the card.
4. Preserve the unpinned "main" project forever — an explicit catch-all
   for investigation tabs that don't need a worktree.
5. Migrate cleanly from current state (tmux session names + the
   in-progress `sessions[]` block) without data loss.

## Non-goals

- **Renaming the term "window" in server code.** tmux's term is "window"
  and `server.py` reflects that. The UI calls them "tabs." We change UI
  strings; the API surface and Python code keep "window."
- **Multi-repo projects.** A project pins to one directory; the repo it
  resolves to is one repo. Tabs in another worktree of the *same* repo
  are fine; tabs in unrelated repos are an anti-pattern and surface as a
  warning chip, not a feature.
- **Binding tabs to specific worktrees.** A tab's worktree affiliation
  follows its `cwd` and is recomputed every poll. Users cd freely; we
  don't try to enforce or persist a tab→worktree mapping.
- **Replacing tmux as the runtime.** Tmux still owns process lifecycle.

## Concepts

### Project

A focused chunk of work. Backed by exactly one tmux session.

| Field | Source | Notes |
|---|---|---|
| `pinned_dir` | user (on create) | absolute path; identity. Must exist on disk. |
| `repo` | derived | git toplevel-or-common-dir-parent walk from `pinned_dir`; may be `null` for non-git dirs. |
| `name` | user; defaults to tmux session name | display string; mutable; not identity. |
| `tmux_session` | user; defaults to a slug of `name` | tmux's session name; ASCII, no `.`/`:`. Mutable via rename. |
| `created_at` | server | unix ts. |
| `archived_at` | server; nullable | set when user archives the project. Archived projects survive in state but don't render in the default grid view. |
| `pinned_repo` | user; nullable | optional override of the derived `repo` — for the rare case where inference picks the wrong one. |

**Identity is `pinned_dir`.** Two projects with the same pinned_dir are
not allowed. The server enforces this in three places: the v1→v2
migration collapses duplicates (keep older, warn); `POST /api/projects`
returns 409 on duplicate; `POST /api/projects/adopt` returns 409 on
duplicate. Keys are validated as absolute paths (must start with `/`),
which keeps the `__main__` sentinel non-colliding. Renaming a project
does not move tabs. The tmux session name can drift from the display
name — display name is the UI label, tmux session name is the internal
addressing.

`pinned_dir` is stored post-`os.path.realpath`, so symlinked entry points
collapse to the same project. Re-resolving on every write keeps the key
canonical.

A project's `repo` is the result of:

```
realpath(pinned_dir)
→ git -C <pinned_dir> rev-parse --show-toplevel       # worktree's own root
→ git -C <pinned_dir> rev-parse --git-common-dir       # shared .git dir
→ parent of git_common_dir's parent (= main checkout)
```

This matches the algorithm in the worktree-integration spec
(`Repo is "main checkout," not a worktree`). For a normal repo,
`pinned_dir` is the toplevel and that equals the main checkout. For a
worktree-pinned project, the algorithm walks up to the main checkout.

If any step fails, `repo` is `null` and the project is treated as a
non-git project (e.g. `~/dev/scratch`). That's a supported state — not
an error.

### Tab

A tmux window inside a project's tmux session. Tabs have their own cwd
(tmux's `pane_current_path`). The server already collects this; no new
data needed.

A tab's **worktree affiliation** is derived live:

```
realpath(tab.cwd)
→ match against `git worktree list` output for the project's repo
→ tab.worktree = the matching worktree's path (= tab.cwd itself, usually,
                  or one of its parents up to the worktree root)
```

If the tab's cwd is not inside any worktree of the project's repo, the
tab is **off-repo** — it's cd'd into something unrelated. UI shows a
warning chip; we don't try to "fix" it.

Three display states for tab cwd:

| State | Condition | UI |
|---|---|---|
| At pinned worktree | tab.worktree == project.pinned_dir (or subdir thereof) | no chip |
| Sibling worktree | tab.worktree != project.pinned_dir but same repo | `↪ <branch-of-that-worktree>` chip |
| Off-repo | tab.cwd not in any worktree of project.repo | `⚠ <basename of cwd>` chip |

### Main project

A single sentinel project named `main`. Unpinned (`pinned_dir = null`,
`repo = null`). Cannot be renamed, archived, or have its pinned_dir set.
The server guarantees its existence: if state has no `main` row, one is
created on load.

Tabs in the main project are not constrained by repo — they can be
anywhere. UI treats main as a flat list of tabs, each rendered with
enough cwd context to disambiguate (no worktree chip logic since there's
no project repo to compare against). Optionally groupable by inferred
repo as a polish move; not in v1.

The point of main: investigation tabs that don't deserve a project. Long
shells, ad-hoc reproduction work, claude-without-a-repo. Tom: "there are
times I just want to investigate something that doesn't need a worktree."

## State model

`state.json` v2 introduces `projects` (replacing the in-progress
`sessions` block from the worktree-integration spec, which is not yet
shipped):

```json
{
  "version": 2,
  "projects": {
    "/Users/tom/dev/worktrees/fdy/tc-canonical-attribute-selectors": {
      "name": "tc/canonical-attribute-selectors",
      "tmux_session": "tc/canonical-attribute-selectors",
      "repo": "/Users/tom/dev/fdy",
      "pinned_repo": null,
      "created_at": 1747200000,
      "archived_at": null,
      "base_branch": "tc/canonical-attribute-selectors"
    },
    "/Users/tom/dev/periscope": {
      "name": "periscope",
      "tmux_session": "periscope",
      "repo": "/Users/tom/dev/periscope",
      "pinned_repo": null,
      "created_at": 1747100000,
      "archived_at": null,
      "base_branch": null
    },
    "__main__": {
      "name": "main",
      "tmux_session": "main",
      "repo": null,
      "pinned_repo": null,
      "created_at": 0,
      "archived_at": null,
      "base_branch": null
    }
  },
  "ui": { ... },
  "windows": { ... },
  "commands": [ ... ]
}
```

Notes:

- The key is `pinned_dir`. The main project uses the literal string
  `__main__` since it has no pinned_dir.
- `repo` is cached; if it's `null` and the pinned_dir exists, re-resolve
  on next poll. If it's set and the cached path no longer resolves, clear
  it and re-resolve. `pinned_repo` is a sticky override — never re-derived.
- `base_branch` is the branch tabs spawn off when creating a new
  worktree-tab (workflow-management spec uses this). Recorded at project
  creation (typically the branch of `pinned_dir` at creation time);
  user-mutable. `null` means "fall back to repo default." **This
  supersedes the v1 worktree-integration spec's "always fork off
  `main`/`master`" rule** — see "Relationship to other specs."
- `archived_at` keeps history. Archived projects still occupy state until
  the user explicitly purges; this is so re-creating an accidentally-
  archived project preserves window metadata.

### Removed from this layer

The worktree-integration spec proposed a `sessions[<name>]` block keyed
on tmux session name. That spec hasn't shipped. We remove it from scope
— `projects[pinned_dir]` covers the same need with better identity.

If any in-flight branch already implemented `sessions[]`, treat that
implementation as draft and rebase onto `projects[]` before merging.

### Write discipline

Same model as the persistent-config-layer spec for state writes: a
single `threading.Lock`, atomic-replace, `projects` mutations through
helpers in `server.py`.

**Git mutations need a separate lock dimension.** The worktree-
integration spec (§"Implementation notes" item 2) already established
that `git worktree add` is non-atomic with branch creation and requires
a per-repo `threading.Lock` keyed on `os.path.realpath(repo)`. This
spec inherits that requirement and extends it: every verb that calls
`git worktree add` or `git worktree remove` (workflow-management spec
Verbs 1, 2, 3, 5) must acquire the per-repo lock for the duration of
the git operation. The coarse `_STATE_LOCK` is held only for the
subsequent state-write, not the git call — otherwise a slow worktree
operation would block unrelated state mutations.

### GC

Three rules:

1. The `main` project never GCs.
2. An archived project GCs from state 30 days after `archived_at`. The
   `windows` entries it owns GC under an **extended** windows-GC rule
   (see below).
3. A non-archived project whose `tmux_session` is absent from live tmux
   surfaces as a **cleanup candidate** in the workflow-management
   spec's Verb 5 view — it does *not* auto-archive on tmux absence
   alone. Auto-archive fires only when *all* of the following hold for
   >14 days: tmux session missing, no open linked PR
   (`linked_pr` is null OR its tracked PR's `state` is merged/closed),
   no recent git activity in the project's pinned_dir (last commit on
   `base_branch` > 14 days ago). This avoids the "I left this dormant
   for a week, periscope nuked it" failure mode that Tom's `tc/...`
   workflow would otherwise hit.

### `windows` GC extension

The persistent-config-layer spec drops `windows` rows older than 30
days unless they carry `notes` or `tags`. **This spec extends that
immunity** to also cover `linked_pr`, `linked_linear`, `acked_at`, and
`completed_at` — fields written by the channels MCP tools
(`_do_link_pr_tool` / `_do_link_linear_tool` at `server.py:460` and
`486`) and by ack/complete flows. Without this extension, archiving a
project freezes the polling for its windows; 30 days later, the GC
drops the rows and silently erases the PR/Linear linkage, which the
user expects to find intact when re-opening the archived project.

The immunity is straightforward: a row with any of the new fields set
sticks until the project's own GC removes it (rule 2).

## Migration

State version bump v1 → v2. The migration runs on server startup when
the loader sees `version: 1` (or no `version` for legacy state.json).

**Steps:**

1. Walk live tmux sessions in name order (sorted deterministically).
   For each session:
   - Resolve `pinned_dir`: the cwd of window 1's active pane (the
     lowest-index window). If that's not inside a git repo, walk
     windows in **ascending tmux index order** until one is, then use
     that window's cwd. Focus time is not used as a tiebreaker — focus
     state is in-memory and reset on every server start, so it's not
     available during a migration that runs before the first poll.
     Window index is stable and deterministic.
   - If `pinned_dir` is a valid path, register `projects[pinned_dir]`
     with `name = tmux session name`, `tmux_session = tmux session
     name`. If the same `pinned_dir` is seen twice (two tmux sessions
     both pointing at the same dir), keep the one with the smaller
     tmux session id (older) and log a warning naming both; the user
     can rename or kill one to resolve.
   - If the tmux session is literally named `main` or `general`, skip
     this loop and bind it to `__main__` instead (preserves the
     "unpinned catch-all" semantic that the user's existing `main`
     session already serves).
2. Initialize `projects.__main__` if missing.
3. Carry forward `windows`, `ui`, and `commands` blocks unchanged.
4. If the legacy `sessions[<name>].repo` block exists from a partial
   worktree-integration implementation, merge it: any session whose
   `pinned_repo` is set becomes `projects[<that-pinned_dir>].pinned_repo`.
5. Bump `version` to `2`.

The migration is idempotent and side-effect-free against tmux. It only
reads tmux state and rewrites `state.json`.

**Unmigratable tmux sessions:** if a tmux session exists but none of its
windows have a usable cwd (e.g. all cd'd to `/tmp`), the session becomes
an *unmanaged tmux session*. UI surfaces these as an "adopt" affordance
(workflow-management spec details). No automatic project row is created.

## UI implications

- **Project header**: shows `name` prominently, with `pinned_dir`
  (homedir-truncated) as a secondary line. Repo basename optionally
  shown if `name` and `repo` basename diverge meaningfully.
- **Tab card**: gains a worktree chip per the three-state matrix above.
  Subdir-of-pinned-worktree does not get a chip; only cross-worktree or
  off-repo do. Subdir context already shows up via the existing
  `branch | git state` row.
- **Project list**: ordered by `last_focused_at` (existing) within the
  pinned/non-archived set. Main pins to the top by default; user can
  re-order. Archived projects hide behind a toggle.
- **Rename**: project `name` is the editable field. Renaming the tmux
  session is a separate, explicit action (and the server keeps them in
  sync on create; lets them drift after rename if the user wants).
- **Promote tab to project**: a per-tab action in the main project.
  Creates `projects[<tab.cwd's resolved pinned_dir>]`, renames the tmux
  session if currently `main`, moves the tab. Detailed in
  workflow-management spec.

## Relationship to other specs

| Spec | Relationship |
|---|---|
| `2026-05-13-persistent-config-layer-design.md` | This spec extends `state.json`. The v2 bump uses the migration framework that spec proposes. The `windows` GC immunity list is extended (see §"GC"). |
| `2026-05-13-worktree-integration-design.md` | **Partially superseded.** The `sessions[<name>].repo` state block is replaced by `projects[pinned_dir]`. The `POST /api/window/new-worktree` endpoint stays valid and remains the foundation of workflow-management Verb 2 — but its hardcoded "fork off `main`/`master`" behavior is replaced by "fork off the project's `base_branch`," so the endpoint's body or query gains a base-branch parameter. The spec's "Cleanup is out of scope" non-goal is also reversed (workflow-management Verb 5). The path convention `~/.claude-worktrees/<repo>/<branch>` becomes one of several configurable layouts (workflow-management Verb 8). The branch-naming `<initials>/<YYYYMMDD>-<slug>` convention is preserved as a default-fill, not a hardcode. |
| `2026-05-13-ui-redesign.md` | Compatible. Card markup changes are independent of the rename; new worktree chip slots into `.card-meta`. |
| `2026-05-15-workflow-management-design.md` | Companion spec. Project model is its data substrate. |

## Implementation notes

- `pinned_dir` lookups are cheap; the existing per-poll `_git_cache` keys
  cover toplevel/common-dir/worktree-list calls. Adding "is path a
  worktree of repo X" doesn't change cardinality.
- The `git worktree list --porcelain` output covers the cross-repo
  affiliation check. Cache per repo, invalidate on a coarse 60s TTL or
  on `POST /api/projects/*` writes that imply worktree changes.
- The migration step that walks live tmux sessions runs once at startup.
  It's O(sessions × windows) shell calls — fine for Tom's ~15 sessions.
  Subsequent reads come from `state.json`.

## Future revisit: tmux server topology

This spec assumes periscope continues to use the **default tmux
server** — the same socket every other tmux client on the host attaches
to. That gives periscope a "wraps your existing tmux" property: any
session a user already has is visible on first launch, no migration,
no import. It's the right product default.

The alternative — a **dedicated tmux server** (`tmux -L periscope`) —
would give periscope clean ownership of every session it sees:
adoption becomes a non-issue, auto-archive can be aggressive without
risk of clobbering external work, and trellis-style "this tool owns
its sessions" semantics fall out naturally. It costs the wraps-your-
tmux story (existing sessions live on the default socket and don't
migrate cleanly across servers) and forces CLI muscle memory to
adjust (`tmux a` no longer attaches to periscope's sessions).

**Deferred, not declined.** The design as drafted is ~90% topology-
agnostic — only the adoption verb, auto-archive thresholds, and the
"unmanaged session" UI surface change between models. None of the
identity/state/migration choices in this spec lock periscope into the
shared-server model.

**Triggers for revisit:**
- Adoption prompts ("this looks like an external session — adopt?")
  become a frequent source of friction rather than a useful affordance.
- Auto-archive's conservative thresholds (14 days + all-of conditions)
  start producing visible clutter in the cleanup view.
- The "session created externally" pattern stops happening organically
  for the primary user — i.e. periscope has so fully absorbed the
  workflow that nothing else creates tmux sessions anyway.

If any of those bite, add a sibling spec covering the dedicated-server
migration (socket selection, the one-time bulk-recreate tool for moving
default-server sessions across, the adoption-verb scope-down).

## Decisions

Resolved in conversation, recorded for traceability.

1. **Project name vs. tmux session name divergence.** Allowed by the
   data model but not user-surfaced in v1. Default: keep them in sync;
   renaming the project renames the tmux session too. The "advanced:
   rename tmux only" affordance is a later add if it earns its keep.
2. **Off-repo tabs are not auto-evicted.** E.g. a tab in `periscope`
   cd'd to `~/dev/fdy` keeps living in the project, with the warning
   chip surfaced. The user knows what they're doing.
3. **`base_branch` appears on the project header** when non-null and
   non-default. Workflow-management spec details rendering.
