# Frontend Re-architecture: Preact + Event-Driven Push — Design Spec

**Date:** 2026-06-01
**Status:** draft, awaiting review
**Author:** Tom + Claude (brainstorm session)

---

## Summary

Re-architect periscope's frontend onto **Preact + `@preact/signals`**, and
replace the O(N)-subprocess **3-second polling** model with a **tmux
control-mode event-driven push** model. Executed as a **big-bang rewrite in a
git worktree** (prod on `main`/8765 stays untouched until a single final
merge), done as a **faithful behavior-port** — every CLAUDE.md key invariant
survives.

This is the foundation milestone. It ships **no new features** (editor, turns,
blocks). Those are re-homed and re-planned on top of it afterward.

## Strategic framing

Periscope has shifted from "a dashboard you glance at" to "the surface Tom does
all his work in." Two things creak under that shift, and neither is the backend
framework (FastAPI + the `periscope/` one-file-per-subsystem split are healthy):

1. **The render model.** Every `/api/state` poll rebuilds the entire grid via
   `innerHTML` (`grid.js:483-535`), with state scattered across `state.js`,
   `prefs.js`, and module globals. There is no single source of truth and no
   diffing. As the app becomes stateful (persistent rail selection, open
   editors, dirty flags), this bespoke model is the limiter.

2. **The poll model.** `/api/state` spawns **O(N) subprocesses every 3s,
   sequentially** — `git rev-parse` per window with no cache (200-400ms for 40
   panes) plus `capture-pane` per window in a loop (`state.py:30-78` →
   `build_window_view` → `capture()`, 800-2000ms). ~40+ forks every 3s. On
   macOS, fork+exec of a large Python process is genuinely expensive and
   contends with the keystroke `send-keys` subprocess on the focused pane —
   the most likely source of felt input lag.

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
| Push model | Event-driven via tmux control-mode **hybrid** | Idle panes cost zero; only actively-changing panes get a debounced `capture-pane` |
| Build | One Vite build → `static/dist/` | The "no bundler in prod" invariant is formally retired; the future editor bundle merges into this one pipeline |

## Goals

- **Maximally Claude-extendable frontend.** Component-per-surface; any future
  Claude session reads it as idiomatic Preact with zero ramp.
- **Kill display lag.** Keyed diffing replaces full `innerHTML` rebuild.
- **Kill input/terminal lag at the root.** Replace the O(N) `capture-pane` +
  `git rev-parse` storm with control-mode-driven targeted reads + delta push.
- **Single source of truth.** A signals store owns transient app state;
  `prefs.js` remains the *persistence boundary only* (server-backed prefs).
- **Preserve every CLAUDE.md key invariant** through the port (see §Migration).

## Non-goals

- **No new features.** Editor (workspace-v1), turns-overlay, and shell blocks
  are out of scope. Foundation only.
- **No segment/block accommodation.** Per the project's hard-YAGNI rule, the
  migration does NOT pre-build an extension seam for the segmented-transcript
  work. It faithfully ports the existing `terminal-mount` path; the blocks spec
  (`2026-06-01-segmented-transcript-design.md`) refactors the detail pane on
  its own terms when implemented next.
- **No backend module reshape** beyond adding the control-mode client. The
  one-file-per-subsystem `periscope/` split stays.
- **No change to mutation paths.** `send-keys`, `new-window`, paste, resize —
  all stay on the existing `tmux()` wrappers. Control mode is read-only
  observation.
- **Not retiring grid/stream/modal as features.** They are ported, not deleted.
  Split view remains the default.

## Architecture — Frontend (Preact + signals)

### Component mapping

Each current module maps to a small component tree. The mapping is a port, not
a redesign — behavior is preserved.

| Current module | Preact components |
|---|---|
| `app.js` | `<App>` root; mounts view + global keydown handling |
| `grid.js` (1167 LOC) | `<Grid>` → `<SessionGroup>` → `<Card>`; `<NewTile>` |
| `stream.js` | `<Stream>` → `<StreamRow>` |
| `rail.js` | `<Rail>` → `<RepoRow>`/`<WorktreeRow>`/`<PaneRow>`/`<ReviewRow>` |
| `detail.js` | `<Detail>` → `<PaneDetail>`/`<ReviewDetail>`/`<EmptyDetail>` |
| `modal.js` (1180 LOC) | `<Modal>` + `<TabStrip>` + `<Sidebar>` + per-tab components |
| chrome (header/filters/view-switch in `app.js`) | `<Header>`, `<FilterBar>`, `<ViewSwitch>` |
| `usage-pill.js`, `alerts.js`, `toast.js`, `dialog.js` | `<UsagePill>`, `<AlertsPanel>`, `<Toaster>`, `<Dialog>` |
| `overlay.js` | escape-stack hook (`useEscape`) |
| `terminal-mount.js` + `terminal.js` | `<Terminal>` wrapping the imperative xterm lifecycle (see below) |

### Signals store

A single `store.js` exposes signals. Transient UI state moves out of `state.js`
module globals into signals; the **server-persisted** state (`prefs`) keeps its
current persistence boundary — writes still go through `prefs.js` →
`POST /api/prefs/ui`. The store is the read model; `prefs.js` is the write-back
for the durable subset.

Signals (derived from the current state-ownership map):

```js
// transient (was state.js)
export const windows = signal([]);          // canonical window-view list
export const projects = signal([]);
export const currentFilter = signal("all");
export const view = signal("split");         // grid | stream | split
export const activeTarget = signal(null);    // modal/detail focused pane
export const railSelection = signal(null);   // { kind:"pane", pid } | { kind:"review", worktree } | null
export const dragState = signal(null);
export const streamQuery = signal("");
export const streamFocusedTarget = signal(null);
export const usage = signal(null);

// prefs cache stays in prefs.js (persistence boundary); components read it,
// mutations call prefs.patchUI(...) which POSTs and updates the cache.
```

Polling-pause guards (`editingTarget`, `dragging`, `modalRenaming`) that today
gate `poll()` become guards on **applying inbound deltas** to `windows` — same
intent, expressed against the push stream.

### Imperative widgets (xterm, LGTM iframe)

xterm.js and the LGTM iframe are imperative and stay imperative. A `<Terminal>`
component mounts them via a `ref` + `useEffect`:

- `useEffect` on mount calls the ported `mountTerminal(ref.current, target,
  opts)`; cleanup calls `unmountTerminal()`. The existing `terminal-mount.js`
  contract (`terminal-mount.js:1-56`) is preserved verbatim under the hood.
- **Single-instance reuse is preserved.** `detail.js:51-79` skips re-mount when
  the selected pid is unchanged; `<PaneDetail>` reproduces this by keying the
  `<Terminal>` on pid and reconnecting (not remounting) when the target changes
  — matching CLAUDE.md invariant #3's reconnect+prefix path.

### Keyed lists

Cards and rail rows are keyed by `pid` (stable `@periscope_id` identity). This
is what makes diffing real: an inbound delta that changes one pane re-renders
one `<Card>`, not the grid.

### Build

- Vite produces `static/dist/` (one bundle, app entry). The future editor
  bundle (workspace-v1) merges into this same pipeline rather than a second
  build.
- Dev: `dev.sh` gains `vite build --watch` (the workspace-v1 spec's approach
  (a)) OR Vite dev server proxying to FastAPI (already configured in
  `vite.config.js`). Production loads `static/dist/`.
- `uv run server.py` without a build now serves a stale/absent bundle —
  `bin/periscope install` runs `npm install && npm run build`; a `command -v
  npm` precondition is added. CLAUDE.md's "no bundler in production" claim is
  replaced. After a build refresh on an installed periscope, `bin/periscope
  restart` picks up the new bundle (StaticFiles won't hot-swap on mtime).

## Architecture — Backend (event-driven push)

### The hybrid model

Control mode is the **change-detection spine**; `capture-pane` + `parse_pane`
are the **state read**, run only on dirty panes.

```
tmux control client (persistent, lifespan-owned)
  ── %output(pane) ─────────► mark pane dirty (per-pane debounce)
  ── %window-add / -close ──► update window lifecycle model
  ── %session-window-changed► update focused_at (replaces polling derivation)
  ── %layout-change ───────► update geometry
        │
        ▼  (debounced, per dirty pane only)
  capture(target) + parse_pane() + build_window_view()   ← existing fns, reused
        │
        ▼
  in-memory canonical window-view model  ──► compute delta
        │
        ▼
  broadcast delta over  /ws/state  ──► browser signals store applies ──► Preact diffs
```

### Why this kills the lag

Today: **every** pane gets a `capture-pane` every 3s (40 captures / 3s
regardless of activity). With control mode: an idle pane fires no `%output`, so
it costs **zero** subprocesses; only actively-streaming panes get a *debounced*
capture. With 40 panes of which ~2-3 are streaming, throughput drops from ~40
captures/3s to ~3-6 captures/3s. `git rev-parse` is cached per cwd and refreshed
only on relevant events. The fork storm — the root cause — is gone.

### Components

- **Control-mode client** — a new `periscope/control.py` (one-file-per-subsystem)
  owning a persistent `tmux -C` control client connection (or `tmux -CC attach`
  to a dedicated hidden control session). Parses notifications, maintains the
  dirty-pane set and window/pane lifecycle model. Reconnects on tmux server
  restart; handles the initial state dump. Runs under the lifespan via
  `_bg`/`_task` so crashes surface (invariant #8).
- **Per-pane debounce** — coalesce `%output` bursts; re-capture a changing pane
  at most every ~500ms-1s. A delta is pushed only when the *parsed view*
  actually changes (parse is cheap; the debounce bounds the expensive
  `capture-pane`). **This is the design's key risk — see Spike.**
- **`/ws/state` broadcast** — a new websocket. On connect, send a full snapshot
  (reuse `/api/state`'s build logic); thereafter send deltas. `/api/state`
  remains as REST fallback / initial-snapshot source.
- **`focused_at` from control mode.** `%session-window-changed` /
  active-window notifications are a cleaner focus signal than today's
  `update_focus_from_windows` polling derivation. Invariant #1 is preserved and
  arguably improved.
- **`/ws/pane` is untouched.** The per-pane full-fidelity terminal mirror
  (`ws.py:23-232`, pipe-pane + FIFO + capture-pane prefix) is a different
  concern (the *open* pane). Control mode is dashboard-level status across *all*
  panes. Both read tmux; they don't overlap.
- **Mutations unchanged.** `send-keys`, `new-window`, resize, paste-buffer all
  stay on the existing `tmux()` wrappers.

### The control-mode spike (gates the full build)

Before committing the backend rewrite, a time-boxed spike must confirm:

1. **`tmux -C`/`-CC` works** against the running tmux server and emits
   `%output`, `%window-add`, `%window-close`, `%session-window-changed`,
   `%layout-change` as documented.
2. **The firehose question.** A Claude pane streaming tokens emits `%output`
   continuously → "always dirty." Confirm the per-pane debounce bounds
   `capture-pane` to a tolerable rate for the handful of active panes, and that
   idle panes truly cost zero. If `%output` volume is unmanageable even
   debounced, fall back to "control mode for lifecycle/focus + a *targeted*
   capture loop over only control-mode-flagged panes" — still far fewer
   subprocesses than today.
3. **Coexistence** with the existing direct `tmux()` calls and `/ws/pane`
   pipe-pane on the same server, with no interference.
4. **OSC passthrough** (forward-looking for the blocks spec): whether OSC 133
   sequences survive to `%output`. Not required for this milestone but cheap to
   check while spiking.

## Migration strategy

### Worktree, behavior-port, demoable-in-dev

```sh
git worktree add ../periscope-preact -b feature/preact-rearch
cd ../periscope-preact
PERISCOPE_PORT=8766 PERISCOPE_DEV=1 npm run dev
# prod keeps running old vanilla main on 8765, untouched, the whole time
```

Prod stays on `main` until a single final merge — the daily-driver never breaks
mid-flight. Commit-as-you-go discipline holds *inside the branch*: the 8766 dev
instance is runnable at every commit.

### Invariant preservation (non-negotiable)

The rewrite is a **behavior-port**, not greenfield. CLAUDE.md's key-invariants
list **plus the existing modules** are the spec. Each invariant is verified
before merge:

- **#1** `focused_at` server-tracked (now from control-mode active-window).
- **#2** Claude detection requires status line in last 4 non-empty lines —
  `parse_pane` reused verbatim.
- **#3** WS initial paint mirrors tmux screen (width/height/cursor/alt-screen
  prefix) — `/ws/pane` untouched; `<Terminal>` reconnect path preserves it.
- **#4** `capture-pane` `\n` → `\r\n` for xterm.
- **#5** Multi-line input via paste-buffer + 100ms-delayed Enter.
- **#7** Spinner hysteresis at the data layer.
- **#8** Background crashes surface via `_bg`/`_task` (control client included).
- **#10** `channel_shim.py` exits 0 on every failure (untouched, but verify the
  socket lifecycle is unaffected by the control client).

### Order within the branch (each step demoable on 8766)

1. **Scaffold.** Vite build + Preact + signals store; port chrome (`<Header>`,
   `<FilterBar>`, `<ViewSwitch>`) and `<Grid>`. Dashboard works in Preact, still
   polling `/api/state`. The store is fed by the poll — its read interface is
   identical to what the push model will later feed.
2. **Modal + Terminal.** Port `<Modal>`, tab strip, sidebar, and the
   `<Terminal>` ref wrapper over `terminal-mount.js`.
3. **Rail + Detail + Stream.** Port split view and stream; preserve
   single-xterm reuse and the rail merge/order logic (`rail.js:39-90`).
4. **Push swap.** Add `periscope/control.py` + `/ws/state`; switch the store's
   feed from poll to delta push **behind the same store interface**. This is the
   highest-risk step and it is last and isolated — the frontend doesn't know or
   care whether `windows` is fed by poll or push.
5. **Verify + merge.** Walk the invariant checklist; remove vanilla modules;
   merge to `main`; `bin/periscope restart`.

### Tests

- `periscope/control.py` gets a direct test (`tests/test_control.py`) per the
  module-mirror convention — at minimum: notification parsing, dirty-set
  debounce, reconnect.
- `parse_pane` / `build_window_view` keep their existing tests (reused
  verbatim).
- Frontend: manual smoke per the existing no-frontend-tests convention. The
  testable risk stays server-side.

## Disposition of the three in-flight specs

- **split-view** (already implemented) — **ported to Preact** in step 3. Its
  design is load-bearing and survives intact; the rail/detail become the
  canonical Preact surface.
- **turns-overlay** (`2026-06-01-claude-turns-overlay-design.md`) — re-homed
  from "modal-side tab" to a **tab in the Preact `<Detail>` pane** and
  re-planned post-foundation. Its server half (`messages_from_jsonl` + parse
  cache) is unaffected and still valid. **Superseded on the UI side** by the
  segmented-transcript spec.
- **workspace-v1 editor** (`2026-05-28-workspace-v1-design.md`) — re-homed from
  "modal Code tab" to a **`<Detail>` tab** and re-planned post-foundation. Its
  server half (`workspace.py`, ripgrep endpoints) is unaffected. Its Vite build
  merges into this milestone's single build pipeline.

## Risks

1. **Control-mode firehose** (Claude panes stream output → always dirty). →
   per-pane debounce + capture-only-dirty; spike-validate; documented fallback.
2. **Control-client lifecycle** (reconnect, initial dump, coexistence) — the
   highest-risk backend piece. → existing lifespan/`_bg` patterns; spike first;
   the push swap is the last, isolated step.
3. **Invariant drift in a big-bang port.** → behavior-port discipline, the
   invariant checklist, verify-before-merge.
4. **Prod now requires a build.** → CLAUDE.md + README + `bin/periscope install`
   updated; the bundle is the one artifact; `restart` after build refresh.
5. **Tauri.** Loads `localhost:8765`, serves the built bundle; Cmd-R reload
   picks up frontend changes; no Rust rebuild needed. Build must precede prod
   restart (same StaticFiles-mtime note as workspace-v1).
6. **xterm-in-Preact.** ref + `useEffect` mount/unmount; preserve
   single-instance reuse and the reconnect+prefix path.

## Phases (commit-as-you-go inside the branch)

- **Phase 0 — Spike.** Control-mode viability (the 4 spike questions). Output: a
  throwaway script proving `tmux -C` emits usable notifications and the debounce
  bounds capture rate. **Go/no-go gate for the push half.**
- **Phase 1 — Preact scaffold + grid** (still polling). Demoable: dashboard in
  Preact on 8766.
- **Phase 2 — Modal + Terminal.**
- **Phase 3 — Rail + Detail + Stream** (split view at parity).
- **Phase 4 — Control-mode push** (`control.py` + `/ws/state`; swap the store
  feed). Demoable: lag gone on 8766.
- **Phase 5 — Invariant verification + merge.**

If Phase 0 fails, Phases 1-3 still ship (Preact migration alone fixes the render
lag), and the push model becomes a separate follow-up — the two halves are
independent.

## Open questions for spec review

1. **Control session shape.** Dedicated hidden control session vs. `-C` against
   the existing server — which coexists most cleanly with periscope's existing
   direct `tmux()` calls?
2. **Delta granularity.** Per-window-object replace (keyed by pid) vs. per-field
   diff. Per-window-replace is simpler and Preact diffs the DOM anyway;
   per-field only matters if window objects get large. Lean per-window-replace.
3. **`/ws/state` vs. reuse `/ws/pane`'s channel.** A separate socket is cleaner;
   confirm no connection-count concern for a single-user localhost tool.
4. **Signals vs. a thin custom store.** `@preact/signals` is the default;
   confirm we want the dependency vs. a hand-rolled signal primitive (the
   corpus argument favors the real library).
