# Workflow management — design

**Date:** 2026-05-15
**Status:** draft, awaiting review
**Author:** Tom + Claude

## Context

Periscope is trellis's big brother. Trellis (`~/dev/trellis`) is a Rust
TUI that manages tmux sessions and git worktrees together: new session,
new tab, PR review, conversation history, cleanup, adoption. Periscope
already runs as a browser dashboard with all the metadata trellis has
(activity, PR linkage, Linear linkage, claude status), plus a few things
trellis doesn't (channels, focus tracking, modal terminal).

This spec ports trellis's workflow features into periscope so periscope
becomes the primary surface for project + worktree lifecycle. Trellis
remains usable in parallel; we don't break it. Path conventions match
where they can so trellis-created and periscope-created worktrees are
mutually visible.

Companion to `2026-05-15-project-model-design.md`. That spec defines
the data model (project = pinned_dir, tabs derived from tmux windows,
main as unpinned sentinel). This spec defines the verbs.

## Goals

1. **New project.** One UI gesture creates `worktree + tmux session +
   claude-launched primary window`, pinned to the worktree dir.
2. **New tab in project.** Tab opens in the project's pinned dir by
   default; "+ claude worktree" variant creates a fresh worktree off
   the project's base branch and lands the tab there.
3. **PR review.** Given a PR number or branch name, fetch via `gh`,
   create a worktree at the PR's branch, spin up a project pinned to
   it, launch claude, auto-link the PR.
4. **Conversation history.** Browse `~/.claude/conversation-index.md`,
   scoped to the current project's repo or global. Resume into the
   original cwd.
5. **Cleanup.** Surface staleness signals (PR merged/closed, branch
   merged into default, remote branch gone, idle > N days). User
   selects and bulk-archives.
6. **Adoption.** Existing worktree on disk → project. Unmanaged tmux
   session → project. Required for migration.
7. **Per-project actions menu.** Change branch, launch claude, rename,
   archive, edit pinned_repo override.
8. **Settings.** Repos directory, worktree-layout convention per repo,
   cleanup thresholds.

## Non-goals

- **Hard cutover from trellis.** Trellis stays installed and usable;
  periscope reads the same data formats where possible. Tom can wean
  off trellis or keep using both for the foreseeable future.
- **Automated cleanup.** Periscope surfaces cleanup candidates. It does
  not auto-delete worktrees. Always user-confirmed.
- **Cross-machine sync.** Project state is per-host (matches
  persistent-config-layer spec).
- **Recreating trellis's TUI inside periscope.** The dashboard is the
  UI; we add affordances to it, not a separate "trellis mode."
- **Subsystem / monorepo subdir selection in v1.** Trellis offers it
  when creating a new session; periscope defers it. The pinned_dir can
  always be a subdir, just not as a UI step.

## Verb 1: New project

**Trigger**: top-bar "+ project" button or hotkey.

**Inputs:**
- **Repo** — picked from a discovered list (see Settings: `repos_dir`).
- **Branch** — pick existing or type a new branch name.
- **Project name** — auto-generated from branch (slugged); editable.

**Flow:**

1. Resolve repo's default branch via `git symbolic-ref
   refs/remotes/origin/HEAD`, fallback `main`/`master`.
2. If `branch == default`: `pinned_dir = repo`, no worktree creation.
3. If `branch != default`:
   - Resolve worktree path via the repo's worktree layout (see Settings:
     `worktree_layout`).
   - Pre-spawn fetch: `git fetch origin <default>` against the main
     checkout (non-fatal — proceed with stale tracking ref if fetch
     fails, with a warning).
   - **For a new branch**: `git worktree add -b <branch> <wt_path>
     origin/<default>`.
   - **For an existing branch (local or remote)**: `git worktree add
     <wt_path> <branch>`; if branch is remote-only, `git fetch origin
     <branch>` first.
   - `pinned_dir = <wt_path>`.
4. Register `projects[pinned_dir]` with name, tmux_session = slug,
   `base_branch = branch`.
5. Create the tmux session via `tmux new-session -d -s <tmux_session>
   -c <pinned_dir>`.
6. Apply the default layout: window 1 = claude, window 2 = shell.
   Both cd'd to `pinned_dir`. Window 1 sends `claude` after a 100ms
   shell-rc-race sleep (matches existing `/api/window/new` flow).
7. Frontend optimistically renders the new project, then a normal
   `/api/state` poll backfills.

**Error handling:**
- Branch already exists locally AND a worktree of that branch already
  exists: surface "this looks like an existing worktree — want to adopt
  it as a project instead?" → routes to Verb 6.
- `git worktree add` fails for any other reason: surface stderr, do not
  create the tmux session or project row. (No partial state.)
- tmux `new-session` fails: leave the worktree on disk, return error.
  User can retry (will detect and adopt) or remove manually.

**Per-repo conventions** (mirroring trellis):
- Default branch detected via `git symbolic-ref`.
- Branch naming: free-form. The trellis-style `<initials>/<YYYYMMDD>-<slug>`
  is a convenience offered as a default-fill in the input but not
  enforced.
- Default project name = branch with `/` preserved (matching Tom's
  existing `tc/foo` style).

## Verb 2: New tab in a project

**Trigger**: "+ tab" affordance on a project card.

**Two flavors:**

- **Plain tab**: `tmux new-window -t <tmux_session>: -c <pinned_dir>
  -P -F '#{window_index}'`. No claude, no worktree. cwd = pinned_dir.
  This is just `tmux new-window` with proper cwd.
- **Worktree tab**: same flow as the existing worktree-integration
  spec's `POST /api/window/new-worktree`, with three changes:
  - **Base branch = `project.base_branch` instead of `main`/`master`.**
    This is a deliberate divergence from the v1 spec's "fork off main
    for clean isolation" rule. Rationale: when the project itself
    represents a feature branch (the dominant `tc/...` case), new
    sub-worktrees should fork off that feature, not jump back to main.
    For projects pinned to repo root with `base_branch = default`, the
    behavior is identical to the v1 spec. Endpoint accepts an explicit
    `base_branch` query/body param; if absent, falls back to
    `project.base_branch`, then to default.
  - Worktree path follows the project's repo layout setting (see
    Verb 8: `sibling` vs `inline` vs custom).
  - Branch name slugifier and the `<initials>/<YYYYMMDD>-<slug>`
    convention from the v1 spec are preserved as the default-fill.

The existing worktree-integration endpoint already handles repo
resolution, branch collision, fetch caching, and the 100ms shell-rc
sleep. The endpoint signature gains `base_branch` and `layout` params;
the underlying logic only changes in the base-ref it passes to
`git worktree add -b`. Per-repo `threading.Lock` discipline (v1 spec
§"Implementation notes" item 2) carries forward.

Cross-tab cwd: a new tab inside a project does NOT have to be in the
project's pinned worktree. If the user cds the new tab to a sibling
worktree afterward, it shows the sibling-worktree chip per the
project-model spec.

## Verb 3: PR review

**Trigger**: top-bar action or per-project "review PR" menu item.

**Inputs**: PR number (for the current project's repo) or `owner/repo#N`
(global) or a branch name.

**Flow:**

1. Resolve PR metadata in one call:
   ```
   gh pr view <N> --json headRefName,isCrossRepository,headRepository,baseRefName
   ```
   `headRefName` is the literal branch name on the head repo (e.g.
   `feature-foo`). `isCrossRepository = true` indicates a fork PR.
   `baseRefName` is the target branch (used for `base_branch`).
   If input is a branch name on the current project's repo, skip this
   step and treat it as a same-repo PR.
2. Determine target repo: project's repo if active, else match
   `headRepository.url` (for same-repo) or the base repo from the
   `gh` context. If no discovered repo matches, error.
3. Fetch the head branch — **two paths depending on
   `isCrossRepository`:**
   - **Same-repo PR**: `git fetch origin <headRefName>` against the
     main checkout. Local branch name = `headRefName`.
   - **Fork PR**: `git fetch origin pull/<N>/head:pr-<N>` — fetches
     the PR's commits from GitHub's refs/pull namespace into a local
     branch named `pr-<N>`. Origin won't have the fork's branch name
     directly. Local branch name = `pr-<N>`.
4. Create a worktree at the resolved local branch (no `-b`):
   `git worktree add <wt_path> <local-branch>`. For same-repo it's
   `<headRefName>`; for forks it's `pr-<N>`.
5. Run Verb 1's project-creation steps from step 4 onward: register
   project (with `base_branch = baseRefName` so future worktree-tabs
   fork off the PR's target branch, not the PR's own branch), create
   tmux session, apply layout, launch claude.
6. **Auto-link the PR**: write `state.windows[pid].linked_pr` for the
   claude window using the existing link_pr MCP tool's persistence
   (`_do_link_pr_tool` at `server.py:460`). For forks, `is_fork: true`
   flag is also written so the cleanup verb knows not to suggest
   deleting the local `pr-<N>` branch on archive (deleting it loses
   the fetched commits since they're not in `origin`).

**Naming**: project name defaults to `pr-<N>` for forks (the local
branch name) and to a slug of `headRefName` for same-repo. Editable.

**Error modes:**
- `gh` not authenticated: surface gh's error verbatim, link to `gh
  auth status`.
- PR closed/merged: still fetch and create the worktree (review of
  already-merged PRs is valid); show a banner on the project header.

## Verb 4: Conversation history

**Trigger**: top-bar "history" button.

**Data source**: `~/.claude/conversation-index.md`, the file Tom's
SessionEnd hook already maintains. Format documented in the trellis
parser (`trellis/src/conversation_index.rs`); we port that parser to
Python.

**View**: a modal listing conversations newest-first. Columns: date,
project basename, branch, intent summary. Filter input (`/`-key) does
substring match on intent.

**Scope toggle**: "this repo" (default if a project is selected) /
"all". Repo scope filters where `project` (from the index) starts with
the current project's repo path.

**Resume action** (Enter on a row):
1. Look up the session-id via `~/.claude/projects/<encoded>/...jsonl`
   (matches trellis's `resolve_session_id`).
2. If the conversation's original `project` path exists on disk:
   - If a periscope project is already pinned to that path, open a new
     tab in it with `claude --resume <session-id>`.
   - Otherwise adopt the path as a new project, then open a tab.
3. If the original path is missing (worktree was cleaned up): offer to
   spawn a new worktree at the recorded branch, then resume there.

## Verb 5: Cleanup

**Trigger**: top-bar "cleanup" button, or per-project "cleanup" action.

**View**: a checklist modal of cleanup candidates. Each row shows:
worktree path, branch, project name (if registered), and one-or-more
staleness reasons.

**Staleness signals** (all opt-in via Settings; defaults below).
**The PR-state signal is the primary one** — `git branch --merged`
silently misses squash-merges, which are the dominant GitHub workflow
and the dominant case in Tom's fdy repo. Branch-merged is a fallback,
not a coequal signal.

| Signal | Default | Computed by |
|---|---|---|
| PR merged or closed | on (primary) | `gh pr view <N> --json state,mergedAt` where `N = state.windows[pid].linked_pr` for any window of the worktree. Survives branch rename and catches squash-merges. **Not** derived from `cached_pr_state`, which is open-PRs-only (`server.py:1311` queries `--state open`). |
| Branch merged into default | on (fallback) | `git branch --merged <default> --format=%(refname:short)`. Cached per repo, 5min TTL. Caveat: misses squash-merge. Surfaced only when the PR-state signal is unavailable (no `linked_pr`, or `gh` call fails). |
| Remote branch deleted | on | `git ls-remote --heads origin <branch>` returns empty. Cached. Skipped for fork-PR projects (`is_fork: true`), where the local `pr-<N>` branch was never on origin. |
| Idle > N days | on, N=14 | `last_focused_at` (already tracked) and tab activity. |
| Uncommitted changes | warning, not selector | `git status --porcelain` non-empty. Surfaced as a "dirty" badge. |

**Scope**: by default, all worktrees of all known repos. Per-project
scope available from a project's action menu (only shows that project's
repo's worktrees).

**Action** (Archive selected):
1. For each selected row:
   - If a periscope project is pinned to this worktree, archive the
     project (sets `archived_at`).
   - Kill the tmux session if it exists (`tmux kill-session -t ...`).
   - `git worktree remove --force <wt_path>`.
   - Optionally `git branch -D <branch>` if the branch is local and
     merged. Off by default; opt-in checkbox.
2. Refresh state.

**Safety**:
- Worktrees with uncommitted changes are not auto-selected. The user
  has to explicitly check the row.
- If `git worktree remove` fails, leave everything else intact and
  surface the per-row error. Don't half-archive.

## Verb 6: Adoption

Two flows; both write a `projects[pinned_dir]` row.

**Adopt existing worktree as project**: from the cleanup view or from
"+ project → adopt existing." Inputs: worktree path. Effect: creates
the project row pinned to that path; optionally creates a tmux session
or attaches to an existing one of the same name. base_branch = the
worktree's current branch.

**Adopt unmanaged tmux session as project**: from the project list —
unmanaged tmux sessions (no `projects[<pinned_dir>]` row) render with
an "adopt" affordance. Inputs: confirm the pinned_dir (defaulted from
window 1's cwd). Effect: writes the project row, leaves the tmux
session untouched.

Both flows are how the migration in the project-model spec converts
existing tmux sessions to projects on first run.

## Verb 7: Per-project actions menu

A `⋯` button on the project header. Items:

- **Change branch** (worktree-pinned projects only): runs `git switch
  <branch>` in the pinned_dir. Safety: aborts if pinned_dir has
  uncommitted changes; surfaces stash-or-abort dialog.
- **Launch claude**: opens a tab with `claude` if the project doesn't
  already have one. Skipped if any window's title parses as a claude
  status line.
- **Rename**: edits `name`. With a checkbox "also rename tmux
  session," default on.
- **Archive**: sets `archived_at`. Optionally kills tmux session and
  removes worktree (with safety checks); confirm dialog.
- **Edit repo override** (`pinned_repo`): for when the derived repo is
  wrong. Free-form path input; cleared with a "revert to inferred"
  button.
- **Promote to new project** (main project's per-tab menu only):
  creates a new project pinned to the tab's cwd, moves the tab there.
  The destination tmux session is named from the new project's slug.
  The tab's source tmux session is always the main project (whatever
  it's actually called in tmux — `main`, `general`, or migrated under
  another name); periscope just calls `tmux move-window -s <main-tmux>
  -t <new-tmux>` and leaves the main session intact. The "rename tmux
  session" concern from the project-model spec only applies to
  renaming projects, not to promote.

## Verb 8: Settings

A new settings modal (or a section in an existing one). Surfaced
preferences:

- **`repos_dir`**: where to scan for repos when offering "new project."
  Default `~/dev`. Multi-path supported.
- **`worktree_layout`**: per-repo or global default. Two presets:
  - `sibling`: `<worktrees_dir>/<repo-basename>/<branch-slugged>`
  - `inline`: `<repo>/.worktrees/<branch-slugged>`
  - And a free-form path-template field for advanced cases.
  Detection rule: if **all** existing non-main worktrees of a repo
  share a layout (≥1 worktree, unambiguously sibling or inline),
  default to that layout. Otherwise — 0 worktrees, or mixed — default
  to `sibling` (matches trellis) and prompt the user on first new-
  project use with both options visible. The prompt's choice is
  recorded as a per-repo override so the question doesn't repeat.
- **`worktrees_dir`**: only relevant for the `sibling` layout. Default
  `~/dev/worktrees`. Matches trellis's default.
- **Cleanup thresholds**: idle-days N (default 14); auto-suggest-branch-
  delete (default off).
- **User initials** (for branch-name template): read from
  `~/.claude/user-initials` to match the existing worktree-integration
  spec; settable here too if the file is missing.

Stored under `state.json` `settings` block:

```json
{
  "settings": {
    "repos_dir": ["~/dev"],
    "worktrees_dir": "~/dev/worktrees",
    "worktree_layout_default": "sibling",
    "worktree_layout_overrides": {
      "/Users/tom/dev/splash": "inline"
    },
    "cleanup_idle_days": 14,
    "cleanup_suggest_branch_delete": false
  }
}
```

## API surface

New endpoints; all extend `server.py`'s existing route surface:

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/projects` | Create project (Verb 1). Body: `{repo, branch, name, tmux_session?}`. Returns 409 if a project already exists at the resolved `pinned_dir`. |
| POST | `/api/projects/adopt` | Adopt existing worktree or unmanaged session (Verb 6). Body: `{pinned_dir}` or `{tmux_session}`. Returns 409 if a project already exists at the resolved `pinned_dir`. |
| PATCH | `/api/projects/{pinned_dir}` | Rename, edit base_branch, edit pinned_repo. Body: partial. |
| POST | `/api/projects/{pinned_dir}/archive` | Archive (Verb 7). Body: `{remove_worktree?, kill_tmux?, delete_branch?}`. |
| POST | `/api/projects/{pinned_dir}/change-branch` | Switch branch in the pinned worktree (Verb 7). Body: `{branch}`. |
| POST | `/api/pr-review` | PR review flow (Verb 3). Body: `{pr_number?, branch?, repo?}`. |
| GET | `/api/history` | Conversation index (Verb 4). Query: `?repo=<path>`. |
| POST | `/api/history/resume` | Resume a conversation (Verb 4). Body: `{session_id, project_path}`. |
| GET | `/api/cleanup/candidates` | List staleness-flagged worktrees (Verb 5). Query: `?repo=<path>`. |
| POST | `/api/cleanup/archive` | Bulk-archive (Verb 5). Body: `{candidates: [{wt_path, delete_branch}], ...}`. |
| GET/PATCH | `/api/settings` | Read/write settings block (Verb 8). |

`{pinned_dir}` in path params is URL-encoded.

The existing `POST /api/window/new-worktree` stays as the foundation
for Verb 2 worktree-tab creation; it gets `base_branch` and
`layout` parameters added.

## Phasing

Each phase is one PR. Each is independently shippable.

**Phase 1 — Project model + adoption.** Lands the project-model spec
in full: state v2, migration, project header rename, worktree chip
rendering, adopt flows. No new project creation yet; user keeps using
trellis or manual tmux to spin up new sessions, but periscope sees
them as proper projects. The migration auto-adopts existing sessions.

**Phase 2 — New project (Verb 1).** Builds on top of worktree-
integration spec's existing `POST /api/window/new-worktree`. New
top-bar gesture, the create flow, layout application.

**Phase 3 — New tab + per-project actions menu (Verbs 2, 7).**
Extends `/api/window/new-worktree` with `base_branch`/`layout`, adds
the actions menu.

**Phase 4 — PR review (Verb 3).** Adds `/api/pr-review`, the modal,
PR-link auto-population.

**Phase 5 — Conversation history (Verb 4).** Port the trellis index
parser, history modal, resume flow.

**Phase 6 — Cleanup (Verb 5).** Staleness detection, cleanup modal,
bulk-archive endpoint.

**Phase 7 — Settings polish (Verb 8).** Worktree-layout per-repo,
cleanup thresholds, repos_dir.

Phases 4–7 can ship in any order after phase 3.

## Trellis interop

We don't import trellis code. Path conventions match where they can.

- **trellis → periscope**: trellis-created worktrees live at trellis's
  configured `<worktrees_dir>/<repo>/<branch>`. Periscope's `sibling`
  preset matches this and adoption (Verb 6) picks them up as
  registerable projects.
- **periscope → trellis**: periscope-created worktrees in the
  `sibling` layout appear in `trellis cleanup` because trellis scans
  its `worktrees_dir`. **Periscope-created worktrees in the `inline`
  layout (`<repo>/.worktrees/<branch>`) are invisible to trellis** —
  trellis doesn't recurse into per-repo worktree dirs. That's
  acceptable: the `inline` layout is opt-in per repo (splash uses it),
  and once a repo uses inline, the user is implicitly choosing
  periscope-only cleanup for that repo.
- Branch detection / merged check: same `git` commands.
- `conversation-index.md` parsing: same format
  (`trellis/src/conversation_index.rs` ported to Python).

If trellis ever evolves its conventions, periscope follows. (Or vice
versa — Tom owns both.)

## Decisions

Resolved in conversation, recorded for traceability.

1. **Conversation history scope when no project is selected**: defaults
   to "all"; user can narrow with the scope toggle.
2. **"Promote to project" lives in the per-tab menu**, not on the card
   itself. Card surface is precious; menu is fine for v1.
3. **Cleanup branch-delete is opt-in (off by default).** Accidentally
   deleting a branch the user cared about is worse than leaving cruft.
4. **PR review prompts for repo with default-fill from current project**
   — saves a click most of the time, doesn't surprise when reviewing a
   different repo's PR.
5. **Worktree-layout detection** is specified in §Verb 8 (unambiguous
   existing pattern wins; otherwise default to `sibling` and prompt on
   first new-project use; choice recorded as per-repo override).
