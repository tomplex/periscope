# Worktree integration — design

**Date:** 2026-05-13
**Status:** draft, awaiting review
**Author:** Tom + Claude

## Summary

Every "new claude" tab spawned from periscope lands in a fresh git worktree of
the session's repo, with `claude` running in that worktree's directory. The
session implies the repo; the new-window flow creates the worktree, opens the
tmux window in it, then sends `claude`. Worktree lifecycle (cleanup) is
explicitly out of scope for this design and remains a manual concern (existing
`/worktree:cleanup` or `git worktree remove`).

This is a v1 that establishes the spawn flow only. Cleanup, branch metadata
display in the card, and resume-into-worktree integration with the history
indexer are deliberate follow-ups.

## Goals

1. The default new-claude-window path produces an isolated worktree, not a
   shared cwd. No mental tax on the user — clicking `+ claude` Just Works.
2. The session ↔ repo mapping is automatic for the common case (every window
   in the session points at the same repo) and explicit-overrideable for the
   edge cases.
3. Branch and path conventions match Tom's existing `fdy-skills/worktree`
   plugin so worktrees produced by periscope are interchangeable with ones
   produced by `/worktree:spawn` (`/worktree:list`, `/worktree:cleanup`,
   `/worktree:resume` all keep working against periscope-created worktrees).
4. Non-repo sessions degrade gracefully — the worktree button is hidden, and
   a non-worktree "+ claude" button takes its place. No silent fallback.

## Non-goals

- **Worktree cleanup.** Killing a periscope window does **not** remove the
  worktree — coupling tmux window lifetime to git worktree lifetime would
  surprise users who close a window expecting to come back. The off-ramp
  is the existing `/worktree:cleanup` skill, which already handles
  periscope-spawned worktrees because they live at the same path layout
  (see "Cleanup" below).
- **Branch metadata in the card.** The card already shows `branch | git
  state` derived from cwd; that's enough. No dedicated "this is a worktree"
  badge.
- **Resume-into-worktree from history search.** Phase B of the history-search
  design will spawn `claude --resume <id>` into a window. Whether that window
  is itself a worktree is left to that spec — likely no, because resume
  targets an existing session's existing cwd.
- **Multi-repo sessions.** A session has one repo. If the user has windows
  pointing at multiple repos in one session, periscope picks one (defined
  below) and the others are simply not the worktree base.
- **Remote / bare repos.** Worktrees are local-only. If the repo's main
  checkout is unavailable (deleted, moved), the spawn fails loudly.

## Session → repo resolution

A session's "repo" is the path that worktrees fork off. Three sources, in
priority order:

1. **Explicit override** in `state.json` under `sessions[<name>].repo`. If
   present and valid, it wins.
2. **Active-window cwd**, walked up to its git toplevel. This is the
   default — when the session is observed for the first time, periscope
   records the active window's git root as the session's repo and writes it
   to `state.json` under `sessions[<name>].repo`.
3. **Other windows in the session, in tmux index order**, walked up to git
   toplevel, if (2) fails (e.g. the active window is in `~` and has no repo).
   `list_windows()` already returns each window's active-pane `cwd`, so the
   fallback is just iterating that list past the active row.

If none of the three resolves to a git toplevel, the session has no repo. The
frontend disables the worktree button for that session and shows a tooltip:
"This session isn't in a git repo — `+ claude` will open a regular shell."

### Repo is "main checkout," not a worktree

Worktrees fork off the repo's main checkout, not off another worktree. When
the resolved cwd is itself inside a worktree, periscope resolves to the
parent main checkout via `git -C <cwd> rev-parse --git-common-dir`, then
takes that path's parent directory (the main checkout's root). For a normal
checkout, `--git-common-dir` returns `<root>/.git`, so the algorithm
degenerates to "this cwd's git toplevel"; for a worktree, it returns the
shared `.git` dir of the original repo, whose parent is the main checkout.

This matches `spawn.py`'s `get_main_repo_root()` helper (utils/git.py:31-66
in the fdy worktree plugin), which uses `--git-common-dir` directly. We
don't import that script — it's a CLI binary — but we replicate the
algorithm in `server.py`.

### State storage

`state.json` (from the persistent-config-layer spec) gains a `sessions` block:

```json
{
  "sessions": {
    "tc/foo": {
      "repo": "/Users/tom/dev/foo",
      "ts": 1747200000
    }
  }
}
```

Inference vs. cache discipline:

- `sessions[<name>]` is written **only when inference succeeds**. A failed
  inference does not write `repo: null`; the row stays absent. This avoids
  the "infinite re-infer every poll" foot-gun.
- Every poll re-runs inference for sessions with no row. Inference is two
  git calls (`rev-parse --show-toplevel` and `rev-parse --git-common-dir`),
  both already covered by `_git_cache` keyed on the cwd. The per-poll cost
  for a `null`-repo session is therefore a single dict lookup.
- If a row exists but its `repo` no longer exists on disk, the row is
  cleared (not preserved) and inference re-runs next poll.
- `ts` refreshes every poll the session is observed. The user can change
  `repo` through the UI (see "Frontend") or by hand-editing.

### State GC

The persistent-config-layer spec GCs `windows` rows older than 30 days. The
`sessions` block follows the same rule: drop a `sessions[<name>]` row if (a)
the session was not observed this poll, and (b) `ts` is older than 30 days.
Same coarse-lock-and-rewrite story as the existing GC. An entry the user
explicitly overrode via the UI is **not** GC'd — see "Override sticky bit"
below.

### Override sticky bit

When the user sets `repo` through the UI (rather than periscope inferring
it), the row gains a `pinned: true` flag. Pinned rows are not GC'd and are
not re-inferred even if the configured repo path stops existing — the
spawn flow surfaces the error instead. Clearing the override (UI button)
removes `pinned` and the row reverts to inference-mode.

## Worktree path and branch conventions

Match the fdy worktree plugin so the artifacts are interchangeable.

### Path

```
~/.claude-worktrees/<repo-basename>/<branch-with-slashes-as-dashes>
```

Same as `WORKTREES_BASE / repo_name / branch.replace("/", "-")` from
`spawn.py` (constants.py:9, spawn.py:88). We do **not** use the in-repo
`.worktrees/` convention from the superpowers `using-git-worktrees` skill
— Tom's existing tooling uses the global location, and consistency wins.

This path is **not configurable.** Making it configurable would break
interop with `/worktree:list`, `/worktree:cleanup`, and `/worktree:resume`,
all of which assume `~/.claude-worktrees/<repo>/<branch>` and are the
explicit off-ramp for periscope-spawned worktrees (see "Cleanup").

### Branch name

```
<initials>/<YYYYMMDD>-<slug>
```

- `<initials>` is read from `~/.claude/user-initials` (same file the fdy
  worktree plugin reads), fallback `"user"`. Cached at server startup, not
  per-request.
- `<YYYYMMDD>` is local date.
- `<slug>` is the user-provided task slug, run through the same slugifier
  spawn.py uses (`utils/spawn.py:42-43` — alphanum/dash, collapse repeated
  dashes, `[:40]`, trim trailing dashes). Matching the plugin's slugifier
  matters: branch names appear on the file system at `~/.claude-worktrees/`
  and a divergent slugifier produces visually-similar-but-different paths.
- If `<slug>` is empty after slugification, the suffix is the unix epoch in
  seconds (e.g. `1747200000`) — collision-safe placeholder that the user
  can rename later.

### Collision

Before calling `git worktree add`, periscope checks for an existing branch
at the constructed name via:

```
git -C <main-checkout> show-ref --verify --quiet refs/heads/<branch>
git -C <main-checkout> show-ref --verify --quiet refs/remotes/origin/<branch>
```

If either ref exists, the branch suffix is bumped: `...-<slug>-2`,
`...-<slug>-3`, etc. until both checks miss. This is a **wider check than
spawn.py's** (which only verifies local `refs/heads/`); the difference
matters because remote branches that haven't been fetched yet still
materialize on `git worktree add -b` once the next fetch hits.

Periscope does **not** reuse existing branches — the spawn is always
"fresh worktree off main."

### Base branch

Always `main` or `master`, whichever the repo has. Detection: `git
symbolic-ref refs/remotes/origin/HEAD` if available, else first match of
`main`/`master` in `git branch --list`. If neither exists, fail the spawn
with a clear error.

Forking off the current session window's branch is a deliberate non-feature
in v1 — clean isolation is the whole point.

### Pre-spawn fetch

Before creating the worktree, run `git fetch origin <main-branch>` against
the main checkout. **Do not `checkout` or `pull`** — those mutate the user's
working HEAD in the main checkout, which is wrong for a background button:
if the user was in the middle of work on a feature branch in the main
checkout, periscope must not silently yank them onto `main`.

Instead, after the fetch, create the worktree from the freshly-fetched
remote ref directly:

```
git worktree add -b <branch> <worktree-path> origin/<main-branch>
```

`git worktree add` takes the base ref by name — it does not require the
main checkout to currently be on that branch. This is the **deliberate
divergence** from spawn.py's `fetch_and_update` (utils/git.py:186-190),
which does mutate HEAD. spawn.py runs as a CLI invoked deliberately;
periscope runs as a background dashboard the user clicks at any moment.

If the fetch itself fails (network down, ssh creds expired, etc.), proceed
without it — the worktree is created off the last-known `origin/<main>`,
which is the local tracking ref. The spawn response includes a
`{warning: "fetch failed: <reason>"}` field so the user can decide whether
to rebase later.

### Fetch caching

The first spawn in a session pays the fetch latency. To avoid re-paying on
rapid successive clicks, periscope caches the "last fetched at" timestamp
per repo in-memory (not in `state.json` — restart should re-fetch). A
fetch younger than 60 seconds is skipped. This is purely a UX nicety; the
correctness model is "we fetched recently enough that origin/<main> is
fresh."

## API

One new endpoint:

```
POST /api/window/new-worktree?session=<s>&slug=<s>
```

Body: none. Query params:

- `session` (required) — the session that owns the new window.
- `slug` (optional) — slugified into the branch suffix. Empty / missing →
  epoch-based placeholder.

Behavior:

1. Resolve the session's repo (per the inference rules above). If no repo,
   return `{ok: false, error: "session has no repo"}` and the frontend
   should never have offered the button in the first place — this is a
   belt-and-suspenders check.
2. Resolve `<initials>`, `<main-branch>`, `<branch>`, `<worktree-path>`.
3. Best-effort `git fetch origin <main-branch>` on the main checkout (see
   "Pre-spawn fetch"). No `checkout`, no `pull` — `fetch` is the only
   mutation periscope makes to the user's main checkout.
4. `git worktree add -b <branch> <worktree-path> origin/<main-branch>`.
   If this fails (disk full, branch suddenly exists due to a race, etc.),
   return the error. Branch-collision checks have already run (see
   "Collision"); this call is expected to succeed except for low-level I/O.
5. `tmux new-window -t <session>: -c <worktree-path> -P -F '#{window_index}'`.
   Unlike the existing `/api/window/new` endpoint, periscope does **not**
   run `display-message` first to inherit the session's active-pane cwd —
   the worktree path *is* the desired cwd.
6. Sleep 100ms (existing shell-rc race window from `/api/window/new`), then
   `tmux send-keys -t <target> claude Enter`.
7. `note_focus(target)`.
8. Return `{ok: true, session, index, target, worktree: <path>, branch}`.

The new endpoint deliberately does **not** subsume `/api/window/new`. That
endpoint stays for shell-only / cwd-inherited spawns and for the
configurable-commands flow from phase 4 of the persistent-config-layer
spec. The two endpoints are siblings.

### Failure modes the endpoint must handle

- Session has no repo → 4xx with explicit error.
- Main checkout doesn't exist on disk → 4xx; suggest user check
  `state.json`'s `sessions[<name>].repo`.
- `git worktree add` fails for any reason → 5xx with stderr surfaced.
- tmux `new-window` fails → 5xx; if the worktree was already created, leave
  it — the user can either retry (spawn will detect existing branch and
  bump suffix) or `git worktree remove` it. Silent rollback is wrong here
  because partial state is information.

## Frontend

### New-window tile

Today the tile has two pinned buttons (`+ claude`, `+ shell`). Phase 4 of
the persistent-config-layer spec replaces these with one button per entry
in `prefs.getCommands()`.

Worktree integration adds a **separate, always-present row** to the new-
window tile, above the configurable-commands row:

```
[ + claude (worktree) ]    [ slug input ]
[ + claude ] [ + shell ] [ + vim ] ...  ← configurable commands row
```

- The worktree row only renders if the session has a resolved repo. Otherwise
  the row collapses entirely and the configurable-commands row is the whole
  tile — same as today.
- The slug input is a small `<input>` next to the button. Empty is fine
  (placeholder branch). The input is cleared after a successful spawn.
- The button is a single fixed control, **not** part of `prefs.commands`. We
  considered making it a first-class command type (`{ type: "worktree" }`)
  but rejected it: the worktree spawn has a different signature (slug
  input, repo-required guard) and lives behind a different endpoint. Mixing
  it into the flat command list would force the command schema to grow a
  discriminator for one specific case.

### Layout change to `.card-new`

`static/grid.js` currently renders `.card-new` as a single horizontal flex
container of buttons. Adding a worktree row above the existing row turns
`.card-new` into a vertical flex container with two horizontal-flex rows:
worktree-row on top, commands-row below. The CSS in `static/styles.css`
needs the corresponding flex-direction change.

Sequencing relative to persistent-config-layer phase 4: phase 4 already
rewrites `renderNewTile` to render one button per `prefs.getCommands()`
entry. Worktree integration sits on top of that rewrite — if phase 4 has
not landed, the layout change still applies, but the bottom row contains
the hardcoded `+ claude` / `+ shell` buttons instead of dynamic ones.

### Session header

The session header gets a small label showing the resolved repo:

```
tc/foo · ~/dev/foo
```

Clicking the repo path opens a small popover with:
- The current `sessions[<name>].repo` value.
- An input to override it (with a path-validation hint).
- A "clear override" button that reverts to inferred.

This isn't strictly required for v1 — inference Just Works for ~all
sessions — but the cost is small and the alternative (hand-edit
`state.json`) is a real footgun.

### Prefs surface additions

`prefs.js` (from the persistent-config-layer spec) gains:

- `getSessionRepo(name) → string | null`
- `setSessionRepo(name, path)` — calls `PUT /api/prefs/sessions/<name>`
- `clearSessionRepo(name)` — calls `DELETE /api/prefs/sessions/<name>`

The wire endpoints follow the persistent-config-layer spec's convention.

## Interaction with persistent-config-layer spec

This spec assumes phases 1 and 4 of the persistent-config-layer spec have
landed:

- **Phase 1** ships `state.json` and the prefs surface, which this spec
  extends with the `sessions` block.
- **Phase 4** ships the configurable-commands tile. Worktree row sits
  above it; the two are visually distinct.

If phase 4 has not landed when worktree integration ships, the worktree row
sits above the existing pinned `+ claude` / `+ shell` buttons with no
behavioral change to those. Worktree integration is **not** blocked on phase
4 landing.

Phase 2 (periscope ids) is unrelated — no `pid` involvement here.

## Interaction with claude-history-search-design

History search's Phase B will land `/api/window/new` with `mode="resume"`
spawning `claude --resume <id>` into a session. That flow is **not** a
worktree spawn — resume targets a specific past session whose cwd implies a
specific worktree (or no worktree, if the original session wasn't in one).
We pass the resumed-session's recorded cwd as `-c` to `tmux new-window` and
leave worktree management out of it.

A future feature might offer "resume into a fresh worktree off this
session's repo" — explicit non-goal here.

## Implementation notes for plan-writing time

A few wrinkles worth flagging up front so the implementation plan can
account for them, not buried in code:

1. **The 100ms post-create sleep matters.** Existing `/api/window/new` sleeps
   before `send-keys claude` to let zsh finish loading rc files. Worktree
   spawn must do the same — the shell rc race is identical.
2. **`git worktree add` is not atomic with branch creation.** If two spawn
   requests race on the same `(repo, slug)`, the collision check + bump +
   `git worktree add` sequence must be serialized per repo. Use a
   `threading.Lock` per repo (FastAPI runs `def` endpoints on a threadpool;
   the rest of `server.py` is sync — `asyncio.Lock` would be the wrong
   primitive here), keyed by `os.path.realpath(repo)` so symlink aliases
   share the lock. The `state.json` global lock is too coarse and would
   serialize unrelated mutations.
3. **`git fetch` can take seconds.** Run it inside the endpoint synchronously
   for now — the spawn already takes ~100ms for tmux plumbing, so an
   additional 1–2s for fetch is not surprising. If it becomes a UX problem,
   move to a background-fetch model with the worktree spawning from the
   last-fetched state.
4. **The fdy worktree plugin's `spawn.py` cannot be imported.** It's a
   standalone CLI. We don't import it; we reimplement the small subset
   we need (main-checkout detection, branch naming, worktree path) directly
   in `server.py`. This keeps periscope's "single-file server" property and
   avoids coupling to a plugin that may move or change.
5. **`~/.claude/user-initials` may not exist.** Fallback to `"user"` like
   the fdy plugin does. Don't fail the spawn over this.

## Phasing

This spec is small enough to ship as one phase, not a multi-PR sequence. A
single PR introduces:

- `state.json` `sessions` block + load/save plumbing
- Repo-inference helper in `server.py`
- `POST /api/window/new-worktree` endpoint
- `PUT`/`DELETE /api/prefs/sessions/<name>` endpoints
- Frontend: worktree row + slug input on new-window tile, session-header
  repo label + override popover
- `prefs.js` additions

Ship-criteria: in a session whose active window is in a git repo, clicking
`+ claude (worktree)` produces a new tmux window in a fresh `<initials>/...`
branch off main, with `claude` running. The fdy worktree plugin's
`/worktree:list` shows the periscope-created worktree alongside its own.

## Cleanup

Periscope does not clean up worktrees. The off-ramp is the existing
`/worktree:*` plugin commands, which work against periscope-spawned
worktrees because they live at the same path layout
(`~/.claude-worktrees/<repo>/<branch>`):

- `/worktree:list` shows periscope's worktrees alongside any plugin-spawned ones.
- `/worktree:cleanup` removes merged + clean worktrees regardless of who
  created them.
- `git worktree remove <path>` works as the manual fallback.

This is the **answer to "is this technical debt?"** — it's not, because
the tooling for cleanup already exists. The non-goal is intentional, not
a deferral.

## Open questions

1. **Slug input position.** Inline next to the button vs. button-opens-a-
   tiny-inline-prompt. Either renders the same data; pick at implementation
   time. Slug *semantics* (slugifier, fallback to epoch) are pinned above —
   only the visual placement is open.
2. **Should the worktree row show the resolved repo path?** Today the
   session header has it; the new-window tile could too. Defer — start
   with header-only and see if it's missed.

None blocking.
