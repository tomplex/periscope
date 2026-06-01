# Frontend Re-architecture: Preact + Poll Optimization — Design Spec

**Date:** 2026-06-01
**Status:** draft, revised after spec-reviewer pass, awaiting Tom review
**Author:** Tom + Claude (brainstorm session)

---

## Summary

Re-architect periscope's frontend onto **Preact + `@preact/signals`**, porting
**grid and split views only** (stream view is cut), and kill the dashboard lag
with **Preact keyed diffing + a targeted poll optimization** — *not* a new
transport. Executed as a **big-bang rewrite in a git worktree** (prod on
`main`/8765 stays untouched until a single final merge), done as a **faithful
behavior-port** — every CLAUDE.md key invariant survives.

This is the foundation milestone. It ships **no new features** (editor, turns,
blocks). Those are re-homed and re-planned on top of it afterward.

A full event-driven (tmux control-mode) push model was considered and
**deferred** — see §Deferred. The spec-reviewer's live experiments showed it is
both more complex (one control client per session, not one) and far less
beneficial (Claude panes emit output continuously, so "idle panes cost zero"
doesn't apply to the dominant pane type) than first assumed.

## Strategic framing

Periscope has shifted from "a dashboard you glance at" to "the surface Tom does
all his work in." Two things creak under that shift, and neither is the backend
framework (FastAPI + the `periscope/` one-file-per-subsystem split are healthy):

1. **The render model.** Every `/api/state` poll rebuilds the entire grid via
   `innerHTML` (`grid.js:483-535`), with state scattered across `state.js`,
   `prefs.js`, and module globals. There is no single source of truth and no
   diffing. As the app becomes stateful (persistent rail selection, open
   editors, dirty flags), this bespoke model is the limiter.

2. **The poll model.** `/api/state` runs `capture-pane` **per window in a
   sequential loop** every 3s (`state.py:30-78` → `build_window_view` →
   `capture()`, `window_view.py:46`). With ~40 panes that's ~40 forks serially,
   ~800-2000ms wall-clock. On macOS, fork+exec of a large Python process is
   expensive and contends with the keystroke `send-keys` subprocess on the
   focused pane — the most likely source of felt input lag. (Note: `git
   rev-parse` and PR state are **already cached** — `git_pr.py:32`, 15s/60s TTLs
   — so the only genuine per-poll subprocess is `capture-pane`.)

### The dominant design criterion: Claude extends this code

The primary maintainer of periscope is Claude, directed by Tom. Under that
reality the dominant maintainability factor is **how fluently the agent already
knows the paradigm** — not dependency count. React is the single densest
paradigm in Claude's training corpus; the current bespoke vanilla architecture
is in no corpus and is re-learned each session from a 200-line CLAUDE.md.
**Preact** gives React's programming model and JSX (full corpus benefit) at
~3KB, with `@preact/signals` for state. This criterion is why we accept a build
step and a framework where the project historically prized neither.

## Decisions locked (from the brainstorm)

| Decision | Choice | Rationale |
|---|---|---|
| Render/state model | Preact + `@preact/signals` | React-corpus fluency at 3KB; signals give reactivity without hand-rolled diffing |
| Migration strategy | Big-bang rewrite in a worktree, behavior-port | Prod/dev worktree split neutralizes daily-driver risk; mixed-paradigm would dilute the corpus-fluency win |
| Views ported | **grid + split only** (stream cut) | Stream is redundant with split for Tom's workflow; cutting it shrinks the port and simplifies the view switch to 2-way |
| Lag fix | Preact diffing + **targeted poll optimization** | Diffing kills render cost; parallelizing the capture fan-out + skipping idle panes kills server latency, with zero new transport infra |
| Event-driven push | **Deferred** | Reviewer-verified: per-session client pool needed, and Claude panes are never `%output`-idle, so the efficiency win largely evaporates |
| Build | One Vite build → `static/dist/` | The "no bundler in prod" invariant is retired; the future editor bundle merges into this one pipeline |

## Goals

- **Maximally Claude-extendable frontend.** Component-per-surface; any future
  Claude session reads it as idiomatic Preact with zero ramp.
- **Kill display lag.** Keyed diffing replaces full `innerHTML` rebuild — a
  one-pane change re-renders one `<Card>`, not the grid.
- **Kill input/terminal lag at the root.** Parallelize the `capture-pane`
  fan-out and skip capturing idle panes, so the focused pane's keystroke path
  isn't contending with a serial 40-fork storm.
- **Single source of truth.** A signals store owns transient app state;
  `prefs.js` remains the *persistence boundary only* (server-backed prefs).
- **Preserve every CLAUDE.md key invariant** through the port (see §Migration).

## Non-goals

- **No new features.** Editor (workspace-v1), turns-overlay, and shell blocks
  are out of scope. Foundation only.
- **No event-driven push in this milestone.** Deferred (see §Deferred).
- **No stream view.** It is cut, not ported. The view switch becomes a 2-way
  grid/split toggle; `stream.js`, stream signals, and the stream keybindings are
  removed.
- **No segment/block accommodation.** Per the project's hard-YAGNI rule, the
  migration does NOT pre-build an extension seam for the segmented-transcript
  work. It faithfully ports the existing `terminal-mount` path; the blocks spec
  (`2026-06-01-segmented-transcript-design.md`) refactors the detail pane on its
  own terms when implemented next.
- **No backend module reshape.** The one-file-per-subsystem `periscope/` split
  stays. The poll optimization edits `state.py`/`window_view.py` in place.
- **No change to mutation paths.** `send-keys`, `new-window`, paste, resize —
  all stay on the existing `tmux()` wrappers.

## Architecture — Frontend (Preact + signals)

### Component mapping

Each ported module maps to a small component tree. The mapping is a port, not a
redesign — behavior is preserved. **Stream is omitted** (cut).

| Current module | Preact components |
|---|---|
| `app.js` | `<App>` root; mounts view + global keydown handling |
| `grid.js` (1167 LOC) | `<Grid>` → `<SessionGroup>` → `<Card>`; `<NewTile>` |
| `rail.js` | `<Rail>` → `<RepoRow>`/`<WorktreeRow>`/`<PaneRow>`/`<ReviewRow>` |
| `detail.js` | `<Detail>` → `<PaneDetail>`/`<ReviewDetail>`/`<EmptyDetail>` |
| `modal.js` (1180 LOC) | `<Modal>` + `<TabStrip>` + `<Sidebar>` + per-tab components |
| chrome (header/filters/view-switch in `app.js`) | `<Header>`, `<FilterBar>`, `<ViewSwitch>` (2-way) |
| `usage-pill.js`, `alerts.js`, `toast.js`, `dialog.js` | `<UsagePill>`, `<AlertsPanel>`, `<Toaster>`, `<Dialog>` |
| `overlay.js` | escape-stack hook (`useEscape`) |
| `terminal-mount.js` + `terminal.js` | `<Terminal>` wrapping the imperative xterm lifecycle (see below) |
| `stream.js` | **removed (view cut)** |

### Signals store

A single `store.js` exposes signals. Transient UI state moves out of `state.js`
module globals into signals; the **server-persisted** state (`prefs`) keeps its
current persistence boundary — writes still go through `prefs.js` →
`POST /api/prefs/ui`. The store is the read model; `prefs.js` is the write-back
for the durable subset.

```js
// transient (was state.js); stream-related signals dropped
export const windows = signal([]);          // canonical window-view list
export const projects = signal([]);
export const currentFilter = signal("all");
export const view = signal("split");         // grid | split   (stream removed)
export const activeTarget = signal(null);    // modal/detail focused pane
export const railSelection = signal(null);   // { kind:"pane", pid } | { kind:"review", worktree } | null
export const dragState = signal(null);
export const usage = signal(null);

// prefs cache stays in prefs.js (persistence boundary); components read it,
// mutations call prefs.patchUI(...) which POSTs and updates the cache.
```

The store is fed by the `/api/state` poll. Its read interface is independent of
the data source — if event-driven push is ever added (§Deferred), it swaps
behind the store with no component changes.

Polling-pause guards (`editingTarget`, `dragging`, `modalRenaming`) that today
gate `poll()` become guards on **applying inbound data** to `windows` — same
intent.

### Imperative widgets (xterm, LGTM iframe)

Confirmed sound by the reviewer. xterm.js and the LGTM iframe stay imperative.
A `<Terminal>` component mounts them via a `ref` + `useEffect`:

- `useEffect` on mount calls the ported `mountTerminal(ref.current, target,
  opts)`; cleanup calls `unmountTerminal()`. The `terminal-mount.js` contract
  (`terminal-mount.js:27-55`) is preserved verbatim.
- **Single-instance reuse is preserved.** `detail.js:51-79`'s `sameMount`
  skip-remount-if-same-pid is reproduced by keying `<Terminal>` on pid and
  reconnecting (not remounting) when the target changes — matching CLAUDE.md
  invariant #3's reconnect+prefix path (server re-syncs the initial paint).

### Keyed lists

Cards and rail rows are keyed by `pid` (stable `@periscope_id` identity). This
is what makes diffing real.

### Build

- Vite produces `static/dist/` (one bundle, app entry). The future editor bundle
  (workspace-v1) merges into this same pipeline rather than a second build.
- Dev: `dev.sh` gains `vite build --watch` (workspace-v1's approach (a)) OR the
  Vite dev server proxying to FastAPI (already in `vite.config.js`). Production
  loads `static/dist/`.
- **Prod build path (reviewer finding 7).** Prod runs from the `main` working
  tree under launchd. The built bundle must be available there. Decision:
  **`static/dist/` is committed to `main`** (the bundle is a small artifact, and
  committing it means `bin/periscope restart` needs no build step and a fresh
  checkout works with plain `uv run server.py`). `bin/periscope install`/`dev.sh`
  rebuild it; the build output is committed alongside source changes. CLAUDE.md's
  "no bundler in production" claim is replaced with "the committed `static/dist/`
  bundle is the one build artifact; rebuild and commit it when frontend source
  changes." A `command -v npm` precondition is added to the dev/build path.

## Architecture — Backend (poll optimization, no new transport)

Keep the `/api/state` 3s poll and `/api/pane` 1.5s modal poll. Two in-place
edits to `state.py` / `window_view.py` remove the fork storm:

1. **Parallelize the capture fan-out.** Today the per-window loop calls
   `capture()` → `parse_pane()` sequentially. Run the captures concurrently via
   a `ThreadPoolExecutor` (the handler is sync `def`, runs in the Starlette
   threadpool; subprocess waits release the GIL). Wall-clock drops from
   sum-of-captures to max-of-captures. Trivial, no restructuring.

2. **Skip capturing idle panes.** `list_windows()` already runs once per poll;
   extend its `tmux list-windows` format to include `#{window_activity}`. Cache
   per-window `{last_activity, parsed_view}`. Re-`capture()`+`parse_pane()` only
   for windows whose `window_activity` advanced since last poll (or which have
   no cached view yet). Idle shells cost **zero** subprocesses; their last
   parsed view is reused unchanged (correct — no output, no change).

   - **Invariant #1 is untouched.** `window_activity` is used here only as a
     *capture-skip hint* ("did this pane emit output?"), never for `focused_at`.
     `update_focus_from_windows` (`panes.py:113-124`) and the `_focused_at` /
     `_acted_at` split (`panes.py:27-49`) are unchanged. This is exactly the
     distinction invariant #1 draws: the bug was using `window_activity` for
     *focus*; using it to decide *whether to re-capture* is what it's for.
   - **Claude panes** bump `window_activity` constantly (spinner), so they
     always re-capture — fine, because (a) captures are now parallelized and (b)
     the genuinely-idle shells (the safe-to-skip set) get skipped. Net: capture
     only active panes, concurrently. Strictly better than today, zero new infra.

`git`/PR state stay on their existing caches. `parse_pane`/`build_window_view`
are reused verbatim (invariants #2, #7 preserved). `/ws/pane` is untouched
(invariants #3, #4).

## Deferred: event-driven push (only if lag persists)

If, after Preact diffing + the parallelized poll, lag is still felt at high pane
counts, event-driven push becomes its own future spec — **correctly scoped per
the reviewer's findings:**

- **One control client per session, not one global** — `%output` is
  session-scoped (verified: panes in other sessions never emit to a client
  attached elsewhere). Needs a client pool keyed by session, grown on
  `%sessions-changed`/session-create, reaped on session-close.
- **Claude panes are never `%output`-idle** (continuous spinner frames), so the
  honest win is "skip capturing *static shell* panes," which the poll
  optimization above already achieves via `window_activity`. Event-driven push
  buys little beyond that unless pane counts grow large.
- Real lifecycle notification set (verified): `%window-add`, `%window-close`,
  `%unlinked-window-close`, `%session-window-changed`, `%sessions-changed`,
  `%session-changed`, `%layout-change`, `%output`, `%exit`.

The store interface (§Signals) is designed so this swaps in behind it without
touching components. **Not built now.**

## Migration strategy

### Worktree, behavior-port, demoable-in-dev

```sh
git worktree add ../periscope-preact -b feature/preact-rearch
cd ../periscope-preact
PERISCOPE_PORT=8766 PERISCOPE_DEV=1 npm run dev
# prod keeps running old vanilla main on 8765, untouched, the whole time
```

Prod stays on `main` until a single final merge. Commit-as-you-go holds *inside
the branch*: the 8766 dev instance is runnable at every commit.

### Invariant preservation (non-negotiable)

Behavior-port, not greenfield. CLAUDE.md's key-invariants list **plus the
existing modules** are the spec. Verified before merge:

- **#1** `focused_at` server-tracked, distinct from `_acted_at` —
  `update_focus_from_windows` and the focus/acted split unchanged; the
  capture-skip hint must not feed either.
- **#2** Claude detection (status line in last 4 non-empty lines) — `parse_pane`
  reused verbatim.
- **#3** WS initial paint mirrors tmux screen — `/ws/pane` untouched;
  `<Terminal>` reconnect path preserves it.
- **#4** `capture-pane` `\n` → `\r\n`.
- **#5** Multi-line input via paste-buffer + 100ms-delayed Enter.
- **#7** Spinner hysteresis at the data layer.
- **#8** Background crashes surface via `_bg`/`_task`.
- **#10** `channel_shim.py` exits 0 (untouched; verify socket lifecycle
  unaffected).

### Order within the branch (each step demoable on 8766)

1. **Scaffold + grid.** Vite build + Preact + signals store; port chrome
   (`<Header>`, `<FilterBar>`, 2-way `<ViewSwitch>`) and `<Grid>`. Dashboard
   works in Preact, fed by the existing poll.
2. **Modal + Terminal.** Port `<Modal>`, tab strip, sidebar, and the
   `<Terminal>` ref wrapper.
3. **Rail + Detail (split).** Port split view; preserve single-xterm reuse and
   the rail merge/order logic (`rail.js:39-90`).
4. **Poll optimization.** Parallelize captures + skip idle panes in
   `state.py`/`window_view.py`. Demoable: lag gone on 8766.
5. **Verify + merge.** Walk the invariant checklist; remove vanilla modules
   (including `stream.js`); build + commit `static/dist/`; merge; `bin/periscope
   restart`.

### Tests

- The poll optimization gets coverage: `window_activity`-skip logic and the
  per-window view cache (unit-testable against a faked `list_windows`).
- `parse_pane` / `build_window_view` keep their existing tests (reused
  verbatim).
- Frontend: manual smoke per the existing no-frontend-tests convention.

## Disposition of the in-flight specs

(Reworded per reviewer finding 6 — these specs are unbuilt drafts; their
*designs* are migration-independent.)

- **split-view** (already implemented) — **ported to Preact** in step 3. Its
  design is load-bearing and survives intact; rail/detail become the canonical
  Preact surface.
- **turns-overlay** (`2026-06-01-claude-turns-overlay-design.md`, draft, unbuilt)
  — its server *design* (`messages_from_jsonl` + parse cache, proposed for
  `history/search.py`) is frontend-independent and remains valid. The UI is
  re-homed to a tab in the Preact `<Detail>` pane and **superseded** by the
  segmented-transcript spec.
- **workspace-v1 editor** (`2026-05-28-workspace-v1-design.md`, draft, unbuilt)
  — its server *design* (`workspace.py`, ripgrep endpoints) is
  frontend-independent and remains valid. The UI is re-homed to a `<Detail>` tab
  and re-planned post-foundation; its Vite build merges into this milestone's
  single pipeline.

## Risks

1. **Invariant drift in a big-bang port.** → behavior-port discipline, the
   invariant checklist, verify-before-merge.
2. **Skip-idle-capture staleness.** A pane that stops emitting keeps its last
   parsed view — correct, since `window_activity` bumps on *any* output, so a
   changed pane is never skipped. Verify in the poll-optimization tests.
3. **Prod now requires a committed bundle.** → `static/dist/` committed to
   `main`; CLAUDE.md + README updated; `restart` is build-free because the
   artifact is committed.
4. **Tauri.** Loads `localhost:8765`, serves the committed bundle; Cmd-R reload
   picks up frontend changes; no Rust rebuild needed.
5. **xterm-in-Preact.** ref + `useEffect` mount/unmount; preserve
   single-instance reuse and the reconnect+prefix path (reviewer-confirmed
   feasible).

## Phases (commit-as-you-go inside the branch)

- **Phase 1 — Preact scaffold + grid** (fed by existing poll). Demoable.
- **Phase 2 — Modal + Terminal.**
- **Phase 3 — Rail + Detail (split view at parity).**
- **Phase 4 — Poll optimization** (parallelize + skip-idle). Demoable: lag gone.
- **Phase 5 — Invariant verification, drop `stream.js`, build + commit bundle,
  merge.**

## Resolved decisions (spec review)

1. **Bundle: commit `static/dist/` to `main`.** ✅ `bin/periscope restart` stays
   build-free; a fresh `uv run server.py` works without an npm step.
2. **State: `@preact/signals` (the library), not a hand-rolled primitive.** ✅
   The corpus-fluency criterion favors the real library.
3. **Poll cadence: keep 3s for now.** ✅ Cheaper post-optimization but unchanged;
   tunable later.
4. **Stream view cut.** ✅ Removes its Tab-cycle position and the
   `prefs.view === "stream"` path (existing stream users fall back to split).
