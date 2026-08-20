# periscope — notes for Claude

## What this is

A FastAPI server (`server.py`) plus a browser frontend (`static/`) that
gives a dashboard over the host's tmux sessions. `uv run server.py` reads
its dependencies from the PEP-723 inline metadata at the top of the file
and serves `static/` as-is.

The frontend is a **Preact + `@preact/signals`** app. Source lives in
`static/src/`; `npm run build` (Vite) bundles it to the committed
`static/dist/app.js` — the one build artifact. Rebuild and commit it
whenever `static/src/` changes; `bin/periscope restart` needs no build
step because the bundle is already in the tree. `index.html` is a thin
shell that mounts `<App>` into `#app`. The only non-Preact JS left under
`static/` is the `/history` SPA (`history.js` + `util.js`), the no-op
`sw.js`, and vendored xterm.

Bolted on alongside the dashboard:
- **`history/`** — Python package that indexes every Claude Code
  conversation under `~/.claude/projects/` into SQLite + FTS5, with
  optional Haiku summaries. Mounted under `/history` and `/api/history/*`
  in `server.py`.
- **`channel_shim.py`** + the channels block in `server.py` — an
  in-process MCP server over a unix socket. Each Claude pane spawns the
  shim, which proxies stdio↔socket so periscope can offer tools
  (`notify`, `link_pr`, `link_linear`, `spawn_claude`) and push
  notifications back into the pane's prompt context.

## Running

```sh
uv run server.py     # http://127.0.0.1:8765/
```

For frontend HMR (CSS reloads instantly, JS without losing scroll position):

```sh
npm install                     # one-time
npm run dev                     # http://127.0.0.1:5174/
```

`npm run dev` runs `dev.sh`, which launches `uv run server.py` and `vite`
under a single shell with `trap 'kill 0' EXIT INT TERM` — ctrl+c kills the
whole process group at once, so uvicorn's reload-worker child never gets
orphaned regardless of how each intermediate layer forwards signals.
`vite.config.js` proxies `/api/*` and `/ws/*` to FastAPI on :8766. Vite
builds the committed `static/dist/app.js`; production serves that bundle
from FastAPI with no build step at boot (`bin/periscope restart` just
respawns the daemon against the already-built, already-committed bundle).

`dev.sh` exports `PERISCOPE_DEV=1` (so uvicorn runs with `--reload`) and
`PERISCOPE_PORT=8766` (so the dev backend stays off the :8765 prod port —
it never reclaims the launchd prod instance, never binds the MCP socket,
and never runs the Claude-spending activity worker; see `config.is_prod()`
below). The pidfile reclaim path (see below) treats a reloader child as
the same instance.

## Tests

There IS a test suite — small, surgical, run with `uv run`:

```sh
uv run pytest -q                     # full suite (incl. parse_pane / status-line regex regressions in tests/test_panes.py)
uv run pytest tests/test_channel_shim.py # channel-shim reconnect protocol (if these fail spuriously, `uv sync` — see .venv drift below)
uv run pytest tests/test_tmux_mirror.py  # mirror protocol + pyte convergence oracle (spawns a real tmux on -L periscope-mirror-test)
npm test                             # vitest over the Preact app's pure helpers (railTree, classify, attention, launcher branches, …)
```

These exist because each one tracks a class of regression that has bitten
us repeatedly: parse_pane every time Claude tweaks its TUI; the channel
smoke test every time we'd otherwise discover an SDK break only at
runtime when a pane connects. Add cases here when you find a new
variation, don't open a parallel framework.

**Test-isolation invariant — no leaked DB/network threads.** Tests must not
spawn real background threads that touch the activity DB. `cached_plan_usage()`
fires a `_bg("plan-usage", ...)` thread that does a live httpx fetch +
`record_usage_samples` write; leaked as a daemon, it lands in whatever per-test
`ACTIVITY_DB` is live when it finishes — bleeding real `usage_samples` rows into
unrelated tests AND racing `fresh_activity_db`'s connection close
(use-after-free → an intermittent CPython 3.14 sqlite segfault). Two guards in
`tests/conftest.py` keep this closed: the autouse `_no_plan_usage_refresh`
fixture seeds the cache so no plan-usage thread ever spawns, and
`fresh_activity_db` teardown holds `activity._LOCK` before closing `_CONN`.
Patching one call site (e.g. `periscope.app.cached_plan_usage`) is NOT enough —
`routes.state` holds its own binding, so the fix lives at the cache layer.

## Linting & type-checking

One gate, two languages. `bin/check` is the single entrypoint (`--fix`
applies safe autofixes); `.pre-commit-config.yaml` runs the same checks on
commit (install once with `uv tool run pre-commit install`). The gate is
kept at **zero violations** — keep it there.

```sh
bin/check            # ruff + ty + biome, report-only
bin/check --fix      # ruff --fix + biome --write, then check
uv run ruff check .  # Python lint (Astral)
uv run ty check      # Python types (Astral, pre-1.0)
npm run lint         # UI lint (Biome); npm run lint:fix to autofix
```

Rule choices and *why* (config in `pyproject.toml` `[tool.ruff]` /
`[tool.ty.*]` and `biome.json`):

- **ruff** — deep set (`E,F,W,I,UP,B,SIM,C4,PIE,RET,PERF`). `E501`
  (line length) and `E701`/`E702` (terse multi-statement lines) are OFF:
  that terse style is deliberate here. Tests per-file-ignore `E402`
  (section-divider import grouping) and `E731` (lambda fixtures).
- **ty** checks source AND tests. Source is fully strict. Tests stay in the
  gate so real test-code bugs (undefined names, bad imports, syntax) are
  caught, but six rules that *only* fire as noise on mock-heavy
  (`MagicMock`) / monkeypatched / happy-path test code are silenced there
  via `[[tool.ty.overrides]]` (`unresolved-attribute`, `invalid-argument-type`,
  `not-subscriptable`, `invalid-assignment`, `unsupported-operator`,
  `not-iterable`) — they stay strict on source. `build_icons.py` is excluded
  (`[tool.ty.src] exclude`): a manual icon script with an undeclared optional
  dep (Pillow). One inline `# ty: ignore[unresolved-attribute]` exists in
  `channels.py` for `asyncio.Server.close_clients()` (real since 3.13; ty's
  typeshed lags).
- **Biome** is a **linter only — the formatter is OFF**. An opinionated
  formatter fights the hand-written terse style (same reason E701/E702 are
  off on the Python side), so we keep lint + `organizeImports` without
  reformatting. Four interaction/semantic a11y rules are off
  (`useButtonType`, `useKeyWithClickEvents`, `noStaticElementInteractions`,
  `useSemanticElements`): this is a personal dev dashboard, not an a11y
  target — `onClick` lives on divs/cards by design and there is no `<form>`.
  Scope is `static/src/**` (the Preact app); the legacy `/history` SPA,
  vendored xterm, and the built bundle are excluded.

`ruff` and `ty` are pinned in the `dev` dependency group; Biome is a
`devDependency` (`@biomejs/biome`).

## Architecture

```
browser
 ├── /            → static/index.html → Preact app (static/dist/app.js)
 │       split view (rail + detail); polls /api/state every 3s into a signals store
 │       terminals open WS /ws/pane → xterm.js mirror of the live tmux pane
 └── /history     → static/history.html + history.js (search UI)
         hits /api/history/{search,session/:id,stats}

FastAPI (server.py, single process)
 ├── /api/*       all REST endpoints (state, prefs, send, window/*, history/*, ...)
 ├── /ws/pane     bidirectional terminal bridge (capture-pane snapshot + control-mode mirror w/ reconcile frames)
 └── unix socket  /tmp/periscope-mcp.sock — in-process MCP server for channels
                                              ▲
                                              │ stdio
                          claude (per pane) → channel_shim.py
```

### Server (`periscope/` package + `server.py` shim)

`server.py` is an ~85-line entry-point shim: PEP-723 header + `__main__`
block that does pidfile reclaim, signal install, and
`uvicorn.run("periscope.app:app", ...)`. The FastAPI app, lifespan, and
all routes live under `periscope/`. Nothing inside `periscope/` imports
from `server` — that boundary prevents Python from double-loading the
shim under both `__main__` and `server` module names.

One file per subsystem:

| Module | Role |
|---|---|
| `periscope/app.py` | `FastAPI()` + lifespan + `include_router` loop + StaticFiles mount |
| `periscope/config.py` | Cross-cutting paths + constants (STATIC, MCP_SOCKET_PATH, USAGE_SESSION_PREFIX) |
| `periscope/log.py` | Logging setup + `_bg` / `_task` crash wrappers |
| `periscope/pidfile.py` | Single-instance reclaim |
| `periscope/tmux.py` | `tmux()` / `capture()` / `deliver_input()` / `_run()` / `_tmux_mutate()` subprocess wrappers |
| `periscope/tmux_mirror.py` | Control-mode pane mirror: `%output` relay + self-healing reconcile frames |
| `periscope/store.py` | `state.json` layer (`_STATE`, load/write, migrations) |
| `periscope/channels.py` | In-process MCP server + tool implementations |
| `periscope/panes.py` | `parse_pane` + smoothing + focus tracking + `list_windows` + `_resuming` |
| `periscope/window_view.py` | `build_window_view` — the per-window dict the dashboard renders (+ parsed-pane cache) |
| `periscope/tracks.py` | Track registry: `repo_default_track` / `resolve_track_for_window` / create / rename / dissolve / teardown |
| `periscope/tabs.py` | FILE-PREVIEW tabs on a pane card (`open_tab` / `close_tab` / `activate_tab`) — NOT rail tabs; two meanings of "tab" |
| `periscope/workspaces.py` | Legacy workspace rows, folded into tracks |
| `periscope/projects.py` | Project registry (`ensure_project`, PR-worktree fetch) |
| `periscope/worktrees.py` | `git worktree list` cache + `affiliation` |
| `periscope/worktree_spawn.py` | `spawn_worktree` + `worktree_path` + `_layout_two_window` |
| `periscope/repo_locks.py` | Per-repo advisory lock around git mutations |
| `periscope/gitutil.py` | Shared git helpers (`detect_default_branch`, toplevel resolution) |
| `periscope/session_status.py` | Authoritative Claude-session state from `sessions/<pid>.json` |
| `periscope/tmux_input.py` | Persistent control-mode client for low-latency keystrokes |
| `periscope/bg_commander.py` | Background command jobs (`commands` table) + status sync |
| `periscope/resurrect.py` | tmux-resurrect save-file rewrite (`--resume <uuid>`) + `save_now()` continuum trigger |
| `periscope/migrate_single_session.py` | One-shot consolidation into `MANAGED_SESSION` |
| `periscope/pids.py` | `@periscope_id` mint / stamp / rebind / resolve |
| `periscope/git_pr.py` | Git state + GitHub PR cache + activity timeline + `prewarm_pr_cache` |
| `periscope/lgtm.py` | LGTM mirror (poll + per-session SSE) |
| `periscope/usage.py` | Claude plan usage (JSONL parse + OAuth usage-endpoint fetch) |
| `periscope/cost_pressure.py` | Pure decision core for per-pane context-cost pressure (record selection, payback math, banding, tooltip copy) |
| `periscope/rename_ai.py` | Anthropic SDK plumbing for auto-rename (`RENAME_RULES` taste block shared with the narrator) |
| `periscope/narrator.py` | Per-pane AI status lines + divergence renames (pure decision core + worker-driven tick; see "Narrator" below) |
| `periscope/open_ops.py` | Unified-open core: `open_target` dispatch (path/branch/pr descriptors → resolve → register → idempotent create-or-focus → server-side rail placement) + `ensure_session` / `worktree_for_branch` / `place_in_rail` / `build_catalog`. No HTTP (see "Unified open" below) |
| `periscope/updater.py` | Self-update: commits-behind check (worker-driven, hourly) + detached spawn of `bin/periscope update` (see "Updating" below) |
| `periscope/routes/*.py` | One APIRouter per file (alerts, auto_rename, channel, cleanup, command, events, fs, healthz, history, lgtm, open, pane, paste_image, prefs, projects, send, sessions, settings, state, tracks, update, workspaces, ws) |

Tests live under `tests/` mirroring the package structure (one
`tests/test_<module>.py` per `periscope/<module>.py`, plus
`tests/routes/test_<route>.py` per route). 1083 pytest tests on a
clean run. Run with `uv run pytest -q`. The Preact app has its own
suite: `npm test` (vitest), 252 tests over the pure helpers.

`cleanup.py` has no `tests/test_<module>.py` but is exercised
indirectly through its route tests (`tests/routes/test_cleanup.py`).
`gitutil.py`, `projects.py`, `repo_locks.py` and `worktrees.py` have no
direct test module. Add a direct `tests/test_<module>.py` when you next
touch those. (`worktree_spawn.py` has `tests/test_worktree_spawn.py` and
`open_ops.py` has `tests/test_open_ops.py`; their tmux cases are gated on
`@needs_tmux`.)

**`WORKTREES_DIR` points at the USER'S real `~/dev/worktrees`.** A
`spawn_worktree` test without a redirect fixture creates worktrees there
for real, then fails on the *next* run because `wt_path.exists()`
short-circuits. Use the `tmp_worktrees` fixture in
`tests/test_worktree_spawn.py`, which monkeypatches
`worktree_spawn.WORKTREES_DIR` into `tmp_path`. Strays from before that
fixture existed are still sitting in `~/dev/worktrees/repo/`.

`tests/test_channel_shim.py` exercises the channel-shim reconnect
protocol (spawns the shim as a real subprocess against a fake unix-socket
server) and runs as part of the normal `uv run pytest -q` suite — there
is no separate smoke script and no `collect_ignore`.

**`.venv` drift landmine:** those shim-subprocess tests spawn
`sys.executable channel_shim.py`. If the local `.venv` has drifted from
`uv.lock` (mismatched plugin/dep versions), the suite over-collects and
the shim subprocess misbehaves — surfacing as two spurious
`test_channel_shim.py` reconnect failures (`TimeoutError`/fast EOF) with
NO code change. Fix: `uv sync` to rebuild `.venv` from the lock. The
canonical locked env collects 1083 tests, all green. If you ever see only
those two channel tests fail, suspect the env before the code.

### Key invariants the split preserved

- **No `from server import …` in `periscope/`.** Double-import landmine
  (shim runs as `__main__`; a separate import would re-execute it as
  module `server` with two copies of every global). Enforced by
  grep: `grep -rn "from server import\|^import server\b" periscope/ tests/`.
- **Lifespan owns `MCP_SOCKET_PATH` cleanup.** `periscope/channels.py`
  must never `os.unlink` the socket on shutdown — that's the lifespan's
  job. Double-unlink is benign today but the ownership is the invariant.
- **`_STATE` rebind across modules.** Multiple modules do
  `from periscope.store import _STATE` (binding the dict by reference).
  The `clean_state` fixture in `tests/conftest.py` must re-bind in every
  consumer module so test mutations are seen consistently.

### Frontend (`static/src/` → `static/dist/`)

A Preact + `@preact/signals` app, built by Vite from `static/src/` to the
committed `static/dist/app.js`. `index.html` is a shell that mounts `<App>`
into `#app`. Components grouped by area:

| Area | Modules |
|---|---|
| entry / state | `src/main.jsx` (mount + boot), `src/store.js` (transient signals — the read model), `src/prefs.js` (server-prefs cache as a signal — the persistence boundary) |
| chrome | `src/chrome/{Header,FilterBar,UsagePill}.jsx` |
| poll | `src/poll.js` — the single `/api/state` poll loop (writes `windows` / `projects` / `usage` signals); `openModal` bridge for poll-driven open requests |
| split view | `src/split/{Split,Rail,RailRows,Detail,AttentionSections,SectionHeader,Transcript}.jsx` + `src/split/railTree.js` (`mergeLiveAndPrefs`) — the only dashboard view (grid retired). Rail membership is TRACK-ANCHORED: every window carries a server-resolved `track_id`, and the tree is Track → (derived Branch) → Pane. A track spanning ≥2 branches renders branch sub-clusters; otherwise it renders flat. Branch rows are DERIVED from live `w.branch`, not entities — you can't close one, only tear down the track |
| terminal | `src/terminal/{Terminal,TerminalSearch}.jsx` + `src/terminal/terminalCore.js` (imperative xterm + `/ws/pane`) + `theme.js` |
| overlays | `src/overlays/{Dialog,Toast,Overlays,CommandsModal,CleanupModal,SettingsModal,LauncherModal,OpenOmnibox}.jsx` + `src/hooks/useEscape.js` (LIFO escape stack). `OpenOmnibox` is the command-palette (↑↓↵ nav, ⌘K, grouped cards) behind the header's single `+ new` button — it replaced the old `+ session` / `+ project` / `review PR` menu and the retired `NewProjectModal` / `ReviewPrModal` / `OpenPickerModal`. `src/open/classify.js` is its pure (unit-tested) query→cards classifier |
| util | `src/util.js` (`targetQuery` last-colon split, `apiCall`, `relTime`, `prUrl`, `rewriteLgtmHost`) |

Still vanilla under `static/`: `history.js` + `util.js` (the `/history` SPA —
its own `history.html` entry, untouched by the migration), `sw.js` (no-op PWA
gate), `vendor/xterm.{js,css}` (plain `<script>` so `Terminal`/`FitAddon` land
on `window` — don't edit, replace wholesale). `connection-banner` stays in
`index.html` (read by `src/poll.js`, not rendered by any component).

Migration notes worth knowing:
- **Split is the only view.** Grid and stream were both retired; there's no view
  switch in the header. The `body[data-view]="split"` attribute is still
  asserted on mount because some legacy CSS keys off it.
- **LGTM review iframes** (modal + detail) are created imperatively and parked
  in a Preact-owned host so reconciliation never reloads them; `<Detail>` keeps
  every opened review's iframe mounted (CSS-hidden) so switching never reloads.
- **Static is served `Cache-Control: no-cache`** (`_RevalidateStaticFiles` in
  `app.py`) — ETag revalidation, so a rebuild/restart never serves a stale
  bundle (the stable `app.js` filename would otherwise cache hard).

## Key invariants (the things that broke and we fixed)

These are the non-obvious behaviors worth preserving:

1. **`focused_at` is server-tracked, not tmux's `window_activity`.**
   tmux's activity stamp bumps on any pane output (streaming logs, dev
   servers, Claude tokens). We instead record when a window becomes the
   active window in its session, or when the user acts on it via the
   dashboard (focus/send). See `update_focus_from_windows`.

2. **Claude detection requires status line in the last 4 non-empty lines.**
   Old status lines in scrollback should not trigger `is_claude=true` after
   the user has returned to a shell. See `parse_pane`.

3. **WebSocket paint is self-healing, not perfect.** The initial blob
   still mirrors tmux's size/cursor/alt-screen state (all from
   `display-message` before the capture body), but live bytes come from a
   per-session tmux control-mode client (`tmux_mirror.py`), and the
   mirror periodically ships an idempotent repaint of tmux's own grid.
   Reconcile frames are built **inside the reader task at the reply's
   `%end`** — building them in a future-woken task would let later
   `%output` land first and be reverted by the frame. Don't "optimize"
   this to futures.

   **The cursor is sampled BEFORE the body, and the order matters.** The
   two samples are separate tmux commands (`display-message` then
   `capture-pane`), so a character echoed between them lands in exactly
   one of the pair. A cursor *fresher* than the body renders one cell
   past a character the body doesn't carry, and the row's `\x1b[K` then
   erases that character — a visible gap that persists until the pane's
   next output. Staler cursor is the safe direction: text intact, cursor
   at worst one cell behind, corrected by the next byte. Reported as
   "the cursor is one ahead of where typing lands, with stuttering as it
   reconciles". Sending both as one `;`-joined command does NOT fix it —
   tmux still replies with two `%begin`/`%end` blocks, and
   `_send_command` registers one callback per write, so a combined line
   desyncs the whole reply-callback queue.

4. **`capture-pane` separates rows with bare `\n`; xterm needs `\r\n`.**
   Forgetting the carriage return staircases every line right by the
   previous line's length.

   **Every `capture-pane` that feeds a paint needs `-N`.** By default tmux
   strips trailing spaces, so a row whose content ends in whitespace renders
   SHORT while the cursor is still placed at tmux's true column — one empty
   cell between the text and the cursor. That is most of shell usage: every
   space typed between words, and every idle prompt (a `PS1` ending `"$ "`).
   Reported as "the cursor is one ahead of where typing lands, and I have to
   remember to place it one ahead". Measured on a live pane: `cursor_x=59`
   against a captured width of 58. `-N` pads to the pane width; the cost is
   1.0–1.8× on real panes (~10KB), which is worth it. Two call sites:
   `tmux_mirror._fire_reconcile` and the initial paint in `routes/ws.py`.

   Two things this is NOT, both chased first: it is not a sampling race
   between the body and cursor captures (that is real, and is why the cursor
   is sampled first — but a race is intermittent, and this offset is
   perfectly deterministic), and it is not emoji width (xterm needed the
   unicode11 provider, see below, but fixing that alone left the gap intact).
   When an offset is reproducible to the cell, look for an off-by-one in what
   gets DRAWN, not for a race.

5. **Multi-line input goes via tmux paste-buffer, then Enter via send-keys.**
   `send-keys` silently strips embedded newlines. There's a 100ms sleep
   between paste and Enter so TUIs (especially Claude Code) apply paste
   state before submit lands. See `/api/send`.

6. **Session/index are query params, not path segments.** Session names
   like `tc/foo/bar` contain slashes; path routing decoded `%2F` and 404'd.

7. **Spinner has hysteresis at the data layer.** `capture-pane` runs
   mid-redraw drop the spinner line; without smoothing, the "thinking"
   indicator flickers. Done server-side in `smooth_spinner` (panes.py),
   applied per-pane in `build_window_view`.

8. **Background-thread crashes must surface.** Every `threading.Thread`
   and `asyncio.create_task` call goes through `_bg` / `_task`. A naked
   `Thread(daemon=True)` that raises just disappears, and "the server's
   flakey" becomes uninvestigable.

9. **Pidfile reclaim treats reloader-child as the same instance.** Under
   `--reload`, uvicorn forks a worker. The pidfile holds the parent;
   killing the parent in reclaim would also nuke a healthy reloader.
   Check `PERISCOPE_DEV` and the process tree before terminating.

10. **`channel_shim.py` exits 0 on every failure mode.** Missing
    `$TMUX_PANE`, periscope not running, unreachable socket — all clean
    exits. A non-zero exit pops macOS's crash reporter every time Claude
    reconnects, which is intolerable for a nice-to-have channel.

11. **`@periscope_id` is stamped by WINDOW ID (`@N`), never `session:index`.**
    Indices renumber under `move-window` — which the single-session
    migration does in bulk and which moving a tab between tracks does
    again. A stamp aimed at `session:index` after a renumber lands on a
    *different* window, so the duplicate that triggered the re-mint is
    never cleared and the next poll re-mints again: a self-sustaining
    loop (observed 683 times on one window across three days, in
    `~/.config/periscope/periscope-8765.log`). A re-mint changes a
    window's identity, and `railSelection` is keyed `pane:<pid>` — so the
    detail pane silently detaches. Historically reported as "detail pane
    closes on cd". Regression signal on the log: same-poll duplicates
    surface as `duplicate @periscope_id ... keeper ...` (INFO, from
    arbitration — invariant 18) while `re-minting` (WARNING) covers
    cross-poll residue; `grep "re-minting\|duplicate @periscope_id"`
    catches both.

12. **Rebind eligibility (`_REBIND_TTL_S`, 15 min) is NOT GC retention
    (`_PID_TTL_S`, 30 days).** Rebind exists so persisted state reattaches
    when the tmux server restarts — window options are lost, so every
    window is re-sighted unstamped — and that happens seconds-to-minutes
    after boot. Sharing the 30-day GC TTL meant a fresh Claude at a repo
    root on master matched ANY entry from the past month via the
    `(branch, cwd)` fallback and inherited its `_IMMUNITY_FIELDS`,
    surfacing as a brand-new pane wearing a stale PR and Linear ticket.
    Both passes are collision-prone by construction now that `session` is
    a constant — the TTL is what keeps them honest.

13. **Rebind must never hand out an id a live window still carries.**
    `resolve_pids` builds `taken` incrementally, so a window resolved LATER
    in the pass was an eligible rebind candidate — its entry was refreshed
    seconds ago, well inside `_REBIND_TTL_S`. An unstamped window sitting
    earlier in the list therefore matched a LIVE window on (session, name),
    or on the `(branch, cwd)` fallback that a spawn into the caller's own
    worktree hits by construction, and stole its identity. That was the
    duplicate FACTORY; the dedup gate in `_resolve_one` only cleans up
    afterwards, re-minting the victim. `resolve_pids` now pre-computes
    `carried` (every well-formed `pid_raw` in the pass) and excludes it from
    rebind. `spawn_claude` already worked around this locally with
    `stamp_new_window`; every other creation path was exposed. Regression
    signal on the log: `grep "re-minting\|duplicate @periscope_id"` —
    same-poll duplicates land as the INFO arbitration message
    (invariant 18), cross-poll residue as the `re-minting` WARNING.

14. **A pane can be `attached` and still deaf.** Claude registers for
    `notifications/claude/channel` only when the server is named in its
    channel flags, so a Claude started WITHOUT `config.CHANNEL_FLAG`
    connects the shim (populating `_MCP_SESSIONS`, so `attached` is true)
    and then discards every push. `send_to` / `report` returned `ok: true`
    for messages nothing could receive. `channels.pane_channel_ready`
    reads the flag out of the pane's claude argv and `_deliver` refuses
    up front. The usual way to land flagless: `claude` is a zsh function
    resolved at shell startup, so a long-lived shell keeps a stale copy —
    periscope-spawned panes use `CLAUDE_EXEC` and are always ready.
    `list_claudes` exposes this as `channel_ready`, distinct from
    `attached`.

15. **`_MCP_SESSIONS` deregistration is identity-checked.** The registry is
    keyed by pane and the shim reconnects on the same pane after a restart,
    so an unconditional `pop` in the connection teardown let a dying
    connection evict the live successor that had already replaced it.

16. **`report` always lands.** A lead that exits before its worker finishes
    is the norm, not an edge case. Hard-failing destroyed the result — the
    worker had done the work and fell back to hand-writing a file. When no
    spawner is recorded, or it has exited or is deaf, the report is recorded
    as a user-facing alert on the worker's own pane; `delivered_to` says
    which happened.

17. **A delivered channel push is a META turn, and peek must show it.** It
    lands in the recipient's transcript as an `isMeta` user turn opening
    `<channel source="periscope"` — and `messages_from_jsonl` drops every
    `isMeta` event, so the one thing a sender peeks to confirm was invisible
    to peek by construction. A sender saw no block, concluded `send_to` was
    silently dropping, spent 40 minutes on it, filed a bug that had to be
    retracted, and re-sent the same directive four times.
    `turns.channel_messages_from_jsonl` extracts them and peek merges them by
    timestamp. That block is written by the RECIPIENT'S own Claude, so its
    presence is the delivery receipt; `send_to` reports `delivery: "queued"`
    (never "delivered") because a notification surfaces only on the target's
    next turn.

    Corollary, worth knowing before diagnosing: the incident above was NOT
    peek staleness. That pane's Claude had been started without the channel
    flag (invariant 14) and genuinely received nothing — peek was the only
    tool telling the truth, and the retraction was itself wrong. The work
    landed because Tom pasted it in by hand.

18. **Pane identity is session-id-first.** `resolve_pids` takes precomputed
    session hints (live sid + `--resume` lineage per pane, built by
    `_attach_git_then_resolve_pids` BEFORE `_STATE_LOCK` — hint-building forks
    tmux/ps, and list_claudes resolves on the event loop). Rebind pass 0
    matches `last_seen.sid` TTL-exempt (a sid is unique; the 15-min TTL guards
    occupancy collisions, invariant 12); pass 0b matches the argv
    `--resume <uuid>` lineage but ONLY with cwd corroboration — the hint is
    regex over ps argv, which flattens prompts, so a pane whose prompt merely
    quotes a resume command must not inherit a dead session's identity.
    Duplicate pids are arbitrated by recorded-sid evidence
    (`_arbitrate_duplicates`), not list order. Every rebind and arbitration
    decision is logged — `grep "rebind\|duplicate @periscope_id"` is the
    regression signal (the re-minting warning alone goes quiet for same-poll
    duplicates, which arbitration now intercepts). The session index scans
    EVERY live account's `<config_dir>/sessions/` with a per-pane config-dir
    tiebreak (a recycled pid leaves a stale same-pid file in the other
    account's dir).

19. **`pane_tracks` keys on `@periscope_id`, never `%N`.** The column is
    named `pid` so a raw-SQL regression against `pane_id` fails loudly, and
    the move-tab route 422s on a `pane_id` body field. Legacy `%N` rows
    migrate lazily in the first completed FULL-ROSTER resolve pass
    (`_maintain_track_rows`), which also owns the prune — gated on the pass's
    `taken` set because a boot-time prune fires before rebind can reattach,
    and a partial (single-window) pass's one-pid taken set would mass-delete
    every other pane's rows.

## Status-line parsing

Claude Code renders a two-line block at the very bottom of its pane:

```
  fdy | master | clean | github.com/fdy/repo/pull/1234 ✓
  24% | ↑235k ↓479 | $17.04 | Opus 4.7 (1M context)
```

`STATUS_RE` matches the bottom line (context %, model). `TITLE_RE` matches
the line above (project, branch, git state, PR URL). `PR_RE` pulls the PR
number and CI glyph (⟳ ✓ ✗) out of the URL field. If Claude changes its
status format, these regexes break and `is_claude` returns false for every
window — fix the regexes first when triaging "everything looks like a
shell." Add a case to `tests/test_panes.py` for any new variation.

## Unified open (`periscope/open_ops.py` + `routes/open.py`)

TWO UI surfaces materialize work into the rail, and they answer different
questions. Know which one you're touching:

| Surface | Aims at a track? | Reaches things that aren't running? |
|---|---|---|
| Header `+ new` / ⌘K → `OpenOmnibox` | No — opens into the repo-default track | Yes (whole catalog) |
| Per-track `+ New tab` → `LauncherModal` | Yes (`openLauncher(trackId)`) | Yes (catalog branches for that track's repo) |

The omnibox loads `GET /api/open/catalog` (discoverable repos + their
worktrees) and POSTs a *target descriptor* to `POST /api/open`. The launcher
loads the same catalog and POSTs to `/api/window/new` with the track id. The
server owns all dispatch in both cases.

`open_ops.open_target(descriptor)` takes one of three frozen-dataclass
variants and converges branch/PR onto the path case:
- `PathTarget(path)` — resolve git toplevel (400 if non-git) → `resolve_repo`
  (parent repo) → `ensure_project` (register if absent, **never** 409) →
  `ensure_session` → rebuild pane pids → `place_in_rail` → `OpenResult`.
- `BranchTarget(repo, branch)` — open the branch's worktree, or
  `spawn_worktree` then recurse into the path case.
- `PRTarget(repo, pr)` — `fetch_pr_into_worktree`, recurse, then stamp
  `linked_pr`; rolls back the worktree if the open fails after the fetch.

Invariants worth knowing before touching it:

- **Create-or-focus is cwd-based now, and that IS the old footgun.** Everything
  lives in one `MANAGED_SESSION`, so a per-project session name no longer
  exists to key on: `ensure_session` answers "already open?" by cwd ownership
  *within* the shared session. cwd collides (multiple panes per dir), which the
  pre-tracks design deliberately avoided. Consequence: when a pane already owns
  the target cwd, **no window is created** — the call is a pure focus.
- **A focus that isn't visible reads as a no-op.** Because of the above, the
  client MUST select the returned `claude_pid`; `OpenOmnibox.post()` sets
  `railSelection` + `prefs.setLastSelected`. Without it, opening something
  already open did nothing observable at all — the reported "I tried to open
  fdy master multiple times and nothing happened".
- **Rail placement is server-side, and writes the TRACK keys.** `place_in_rail`
  writes `track_order` / `tabs_by_track` (keyed by **track id**, values are
  `@periscope_id` pids) and the route returns the `ui` blob; the omnibox writes
  it into `prefsSignal` via `prefs.setUI`. This killed the old client-side
  ~3500ms `deferRailAdd` poll-wait race. The pre-tracks trio (`repo_order` /
  `worktrees_by_repo` / `panes_by_worktree`) is NOT read by the rail —
  `prefs.js` drops `worktrees_by_repo`, never reads `panes_by_worktree`, and
  honours `repo_order` only as the fallback when `track_order` is unset.
  Writing them persisted nothing once the rail had saved an order once.
- **Placement is ordering, not visibility.** `mergeLiveAndPrefs` already
  appends live-new tracks and tabs on the next poll, so a genuinely-created
  window shows up regardless. Placement is what makes the order the user chose
  survive. Don't diagnose a missing window by looking at prefs first.
- **`spawn_worktree` checks out an existing branch.** `git worktree add -b`
  fails outright on a branch git already knows, so a branch that exists with no
  worktree used to be unreachable from BOTH surfaces. `_branch_exists` picks
  `worktree add <path> <branch>` (checkout) vs `-b` (fork) accordingly.
- **`_layout_two_window` stamps BOTH windows** (claude + shell) so the full
  pane list is known synchronously — `place_in_rail` needs it without waiting
  for the next poll's `resolve_pids`.
- **The catalog is repo/worktree-scoped (v1).** Arbitrary non-git dirs 400;
  ad-hoc live sessions outside the discoverable roots are not rail-addable
  (the old `OpenPickerModal` was retired). Widen the catalog if that need
  returns.
- **Real-tmux tests need a clean `.venv`.** `tests/test_open_ops.py` +
  `tests/test_worktree_spawn.py` spawn real tmux on an isolated `-L` socket
  (`PERISCOPE_TMUX_SOCKET`) with a stub exec (`PERISCOPE_CLAUDE_EXEC`); both
  seams live in `periscope/tmux.py` + `config.py` and are inert in prod.

## Wrapper profiles (normal | lab)

`claude` on this machine is a **zsh function**, not the binary
(`~/.claude/bin/claude-wrapper.zsh`). It injects a system prompt and, given
`lab`, swaps the plugin set. Periscope spawns are `send-keys` into an
interactive shell, so **every periscope-spawned pane already goes through that
wrapper** — this is why a spawned pane has Tom's system prompt at all.

The launcher's Profile picker (sticky, `prefs.ui.launch_profile`) sends
`profile=lab` to `/api/window/new`, which sets `CLAUDE_WRAPPER_PROFILE=lab` on
the new tmux window via `tmux -e`.

**The profile is carried as an env var, never as the `claude lab` argv word.**
The wrapper *accepts* that word, but consumes it and execs `command claude
--settings '{...plugins...}'` — so `lab` never reaches claude's argv, and
nothing downstream could observe it. Two consumers need to:
`session_status.pane_profiles` (the rail chip) and `resurrect._rewrite_line`
(re-emitting the prefix so a lab pane survives a reboot on the lab plugin set).
Detecting it from argv instead would mean fingerprinting the wrapper's exact
plugin JSON. Env is the one carrier all three parties read — the same reason
`CLAUDE_CONFIG_DIR` works this way.

Consequences worth knowing:

- **The account and the profile are orthogonal.** Account = which subscription
  bills (`CLAUDE_CONFIG_DIR`); profile = which plugin set runs. Both ride
  `tmux.env_args`, both get scrubbed off the session by `scrub_session_env`,
  both get re-emitted by resurrect. A lab pane on account B is normal.
- **`session_status` caches the raw env TAIL, not parsed values**
  (`_pane_claude_envs`). One `ps eww` fork serves every per-pane variable; a
  per-variable cache would fork once per variable on the `/api/state` hot path.
- **Only agent windows carry it** (`profiles.sendsProfile`, mirroring
  `sendsAccount`). A shell window that inherited it would put a hand-typed
  `claude` on the lab plugin set invisibly — the chip is derived from a live
  claude process, which a shell window has none of.
- **Editing the wrapper is part of this feature.** Periscope sets the var; the
  wrapper is what honours it. It fails safe: an un-updated wrapper ignores the
  var and yields a normal pane, never a wrong-plugin-set one.

## tmux persistence (`periscope/resurrect.py`)

Session survival across reboots is tmux-resurrect + tmux-continuum, with two
periscope hooks into it.

**Save-file rewrite (`python -m periscope.resurrect <file>`).** Registered as
`@resurrect-hook-post-save-layout` via `bin/periscope resurrect-rewrite`.
Resurrect re-runs each Claude pane's captured command, but that command starts
a *fresh* session — no `--resume <uuid>`. This rewrites each Claude pane's
command in the just-written save file. It must run at SAVE time while panes are
live: the pane→session map is keyed by tmux pane id, and pane ids are reassigned
when the server restarts, so at restore time there is nothing left to map.
Import discipline: stdlib + `periscope.config` only — it runs under plain
`python3`, and `periscope.activity` would drag in the Anthropic SDK.

**Periscope drives the periodic save (`resurrect.save_now()`).**
tmux-continuum has NO timer: its save fires purely as a side effect of
`status-right` being *expanded*, which only happens when a status line is drawn
for a client. Every client periscope attaches is control-mode (the pane mirror
and the input client) and control-mode clients render no status line — so on a
host driven entirely through the dashboard, continuum silently never saves.
Observed: a 24-day gap between saves, after which a reboot restored a 24-day-old
layout. The activity worker now calls `save_now()` each tick; the script
self-gates on `@continuum-save-interval` and takes its own lock, so calling it
every 30s only writes when an interval has actually elapsed. Prod-only, and it
degrades silently when continuum isn't installed.

Diagnosing a stale restore: `ls -lt ~/.local/share/tmux/resurrect/` — gaps in
that timeline are the story. `tmux list-clients -F "#{client_flags}"` tells you
whether anything is drawing a status line.

## History (`history/`)

Standalone Python package. `python -m history backfill` does a one-shot
index of every JSONL transcript under `~/.claude/projects/` into
`~/.claude/history.db` (FTS5 + per-session Haiku summaries; ~13 min,
~$4-6 the first time). `python -m history hook` is a SessionEnd hook
entry point that ingests one file at a time.

The DB is a derived index — JSONL on disk is the source of truth, the DB
can always be rebuilt from `backfill`. Periscope mounts a search UI at
`/history` and three endpoints under `/api/history/*`.

See `history/README.md` for the verb list and hook installation.

## Channels (in-process MCP)

`# --- Channels ---` block in `server.py` plus `channel_shim.py`. The
shim is the documented stdio MCP entry point; the actual server logic
(tool implementations, notification emission, session registry) is in
`server.py` so it has access to the same state the dashboard does.

Tools exposed to Claude:
- `notify(message, kind=done|need_human|info)` — surfaces an alert on
  the pane card and in the dashboard's alert feed without opening the modal.
- `link_pr(number)` — bind a GitHub PR to the pane, even if Claude's
  status-line URL isn't visible.
- `link_linear(id, title?, status?)` — same for Linear tickets (no
  auto-detection path). Optional `title`/`status` metadata renders on
  the card and in the modal; each call fully describes the link.
- `set_name(name)` — rename the caller's own window and pin the name
  against the narrator (see the narrator section's pin invariant). The
  only self-naming path: `spawn_claude(name=…)` could name a CHILD, so a
  pane holding a standing role had no way to assert its own label.
- `spawn_claude(prompt, workspace?, session?, cwd?, name?)` — fork a
  fresh Claude pane in a new tmux window with the given first message.
  `workspace="same"` (default) adds a window to the caller's session, so
  the spawn nests under the caller's rail item (fan-out / related work —
  the rail is session-anchored, so a different `cwd` shows only as a
  chip). `workspace="new"` anchors the spawn to its `cwd`'s worktree as
  its own rail item: `open_ops.resolve_worktree_session` registers the
  project + dedupes a foreign-name clash, the spawn creates the session
  (or new-tabs into an existing worktree session), then `place_in_rail`
  records the ordering. Non-git `cwd` with `workspace="new"` falls back
  to `"same"` (no worktree to anchor a rail item to). Its result carries
  the spawned pane's `track` (the rail group it landed in, after the
  precedence above plays out).

`list_claudes` rows carry `track` (the rail group each pane sits in), the
response carries `you` (the caller's own handle + track), and
`track: "mine"` filters to the caller's roster.

Notifications go the other way as `notifications/claude/channel`
messages, surfacing in Claude's prompt as `<channel source="periscope">`
blocks. The pinned `mcp==1.27.*` is checked at startup and exercised by
`tests/test_channel_shim.py`; bump both together.

### Shim survives periscope restarts

`channel_shim.py` is not a dumb bytes proxy. When the unix socket drops
mid-session (periscope restart, dev cycle, lifespan teardown), the shim:

- Synthesizes JSON-RPC error responses for any tool calls in flight so
  Claude doesn't hang.
- Reconnects at `PERISCOPE_MCP_RECONNECT_BACKOFF_S` (default 1s) until
  the socket comes back or stdin EOFs (Claude exited).
- On the fresh socket, re-sends the hello frame, replays the captured
  `initialize` request, replays `notifications/initialized`, and synths
  a `tools/list` so periscope's `_list_tools` handler re-registers
  `_MCP_SESSIONS[pane]` — required for push notifications and tool
  routing.
- Swallows the duplicate `initialize` response from the new periscope
  and the synthetic `tools/list` response; Claude only sees them once.

Net effect: Claude's MCP connection survives `bin/periscope restart`
and most lifespan-cycle blips without needing `/clear`. The non-zero-
exit invariant (item 10 below) still holds — the shim only exits 0,
just rarely now.

### Pane → session mapping (the transcript view)

`periscope/turns.py` renders a pane's Claude conversation as a structured
transcript (the split-view "Transcript" mode + `GET /api/pane/turns`). It must
map a tmux pane to its *specific* session JSONL — **cwd alone collides** when
several Claude panes run in one directory (newest-mtime returns the same file
for all of them). The mapping lives in the `pane_sessions` table in
`~/.config/periscope/periscope.db` (`pane_id → session_id`, where `session_id`
is the JSONL stem / `CLAUDE_CODE_SESSION_ID`); `turns.py` reads it via
`activity.get_pane_session` and globs for `<id>.jsonl` (glob, not cwd-encode —
a pane that `cd`'d into a worktree has its JSONL under the *start* dir's
encoding). Lifespan runs a one-shot import from the legacy
`~/.config/periscope/pane_sessions/` directory layout (`migrate_legacy_pane_sessions`)
and prunes rows for tmux pane ids that no longer exist.

**The recorded row is the FALLBACK, not the authority.** Claude mints a new
session id when a conversation is resumed or compacted, and the hook does not
always fire for the successor — a pane then points at a superseded transcript
(cost a real conversation: `move-account` resumed the pre-rotation id and landed
~18h back). `turns.session_id_for_pane` therefore asks
`session_status.live_session_id_for_pane` first: the pane's process subtree is
walked to its claude pid (`session_status.pane_claude_pids`, the one
implementation — `session_status.pane_config_dirs` builds its scan on it, and
resurrect and window_view consume that) and
`~/.claude/sessions/<pid>.json` is read for the sessionId that process reports
*now*. `pane_sessions` answers only when there is no live claude.

The producer is **`pane_session_hook.py`**, registered on Claude's
`SessionStart` *and* `UserPromptSubmit` events by `bin/periscope install-hook`
(run from `install`; removed by `uninstall-hook`). It reads `session_id` from the
hook **payload** (current, so it survives `/clear` — which mints a new session
id) and `TMUX_PANE` from a direct child of the pane's Claude (the real pane id).
A deep `ps`/env scan is deliberately NOT used: inherited
`CLAUDE_CODE_SESSION_ID`/`TMUX_PANE` from tool/subagent subprocesses
cross-contaminate, and a `/clear` leaves a spawn-time env stale — the payload is
the only authoritative, current source.

- **SessionStart** (fires at startup + `/clear`) records the pane's session
  *immediately*, before its first prompt — so a fresh pane shows its OWN
  transcript at once instead of cwd-falling-back to whatever was most recently
  active.
- **UserPromptSubmit** (every prompt) migrates panes that predate the hook —
  they self-correct on their next message (Claude loads new hooks live; no
  plugin reload needed).

`install-hook` also invokes the provider-specific Codex installer. It merges
dedicated Periscope groups for `SessionStart`, `UserPromptSubmit`, `Stop`, and
`SessionEnd` into `$CODEX_HOME/hooks.json` (default `~/.codex/hooks.json`) with
an atomic, mode-preserving write. It never edits other groups or bypasses
Codex's hook trust; use `/hooks` to review and trust the command. The standalone
`codex_pane_session_hook.py` is stdlib-only, silent, and records sanitized
lifecycle metadata in `agent_sessions`/`agent_session_events`. Until Stage-0
live capture proves `TMUX_PANE` and root-vs-subagent behavior, it accepts only a
matching `codex-tui` rollout under `CODEX_HOME/sessions`, marks evidence
`codex-hook-unverified`, and must not be treated as authoritative for status.
`GET /api/healthz` exposes this as `codex_hook.verification: "unresolved"` and
reports installation/observation without claiming trust.

Resolution falls back to newest-mtime-in-cwd when a pane has no recorded session
yet. The earlier `channel_shim.py` recorder was removed — the hook's payload is
strictly better (current vs spawn-frozen).

## Narrator (semantic pane status + auto-rename)

`periscope/narrator.py`, driven by the activity worker's 30s tick (prod
only). Per Claude pane: when the session JSONL changes (≥90s apart), one
Haiku call returns `{"status", "rename"}` — the status line surfaces in
the rail and detail header (`status_line`/`status_at` merged into
`/api/state` windows from the `pane_status` table); a non-null rename
applies via `tmux rename-window` with a `'rename'` activity event.

Invariants worth knowing before touching it:

- **Regeneration is session-id-first, size-second.** `/clear` mints a new
  smaller JSONL; a pure "grew" check would freeze the pre-clear status
  forever. Placeholder rows (`session_id` NULL, written by rename-route
  stamps) must NOT count as a session switch or they'd wipe the cooldown
  they exist to carry.
- **Humans win renames.** All three manual rename surfaces stamp
  `pane_status.renamed_at` (30-min cooldown); `seen_name` catches
  tmux-native renames; and `_generate` re-reads the live window name +
  row immediately before applying (a tick spans multi-second Haiku
  calls — the snapshot goes stale).
- **A deliberate name is PINNED, not cooled down.** `windows[pid].name_pinned`
  (state.json, `_IMMUNITY_FIELDS`) makes `narrator.is_name_pinned` return the
  `locked=True` that shuts `rename_decision` off entirely. Three writers:
  `/api/rename` (Tom typed it, including the rail's double-click), the
  `set_name` MCP tool (a pane naming itself), and `spawn_claude(name=…)` (a
  lead naming its worker). A cooldown was the wrong shape for all three —
  nothing re-asserts a deliberate name, so `RENAME_COOLDOWN_S` expiring
  unnoticed let the orchestrator pane drift through five generated names in
  one afternoon and put one worker on the orchestrator's own role name, so the
  dashboard misidentified both. The pin is NOT scoped to the name matching
  (the old `spawn_name` lock was): it marks the window as hand-named, so a
  later rename keeps it. Released only by `POST /api/name-pin {pinned: false}`
  — the 🔒 in the rail row's hover actions, distinct from the ★ beside it,
  which pins the tab into the rail's PINNED section and touches no name.
- **No cwd fallback** when a pane has no `pane_sessions` row — on a
  shared cwd a wrong-session status is worse than none; the hook
  self-corrects on the next prompt.
- **The lifespan tests mock `activity.run_worker`.** The real worker's
  first tick runs immediately, and in tests `PORT` defaults to 8765 — an
  unmocked worker executes a LIVE narrator tick (real Haiku, real
  renames of real windows) on every pytest run. This actually happened.

## LGTM integration

`# --- LGTM integration ---` block in `server.py`. Periscope mirrors
LGTM's session list onto pane cards (a `👁 review` chip on cards whose
cwd matches a registered LGTM repo) and embeds LGTM's UI in the modal's
Review tab via an iframe. Discovery is all over HTTP against LGTM's
existing API:

- `GET http://localhost:9900/projects` — full session list, polled
  every `LGTM_REFRESH_S` (default 30s).
- `GET http://localhost:9900/project/:slug/events` — SSE stream per
  session; any event triggers a refresh.
- `POST http://localhost:9900/projects` — invoked by `/api/lgtm/start`
  when the user clicks "Start review" from the Review tab.

Override the base URL with `PERISCOPE_LGTM_URL`. Everything degrades
silently when LGTM isn't running — no log spam, the cache just stays
empty and the chips never appear.

LGTM is intentionally unaware of periscope. Don't add cross-imports or
shared types — the contract is the HTTP/SSE shape above.

### Debugging a blank Review tab

If the iframe mounts but renders blank, the failure is almost always on
LGTM's side, not in periscope's plumbing. The fast path:

1. Open the iframe URL directly in a browser tab —
   `http://127.0.0.1:9900/project/<slug>/`. If it's blank there too,
   the integration is fine and LGTM is broken.
2. View source on that page. The HTML head references a content-hashed
   JS bundle: `<script src="/assets/index-<hash>.js">`.
3. `curl -I http://127.0.0.1:9900/assets/<hash>.js` — if it 404s,
   LGTM's `frontend/dist/` is missing the bundle (common after an
   interrupted `npm run build:frontend` or a partial dev/prod toggle).
4. Fix: `cd ~/dev/claude-review && npm run build:frontend`.

Symptom is "Review tab is blank" because the SPA never bootstraps —
`<div id="root">` stays empty, the iframe shows nothing. Easy to
mistake for a periscope iframe sizing bug; it isn't.

## Tauri shell (`src-tauri/`)

Optional native `.app` wrapper for periscope, so it shows up as its
own entry in Cmd-Tab / Dock instead of living inside a browser tab.
The shell is just a Tauri 2 window that loads `http://127.0.0.1:8765`
— launchd still manages the FastAPI server, the GUI app is a pure
presentation layer. Quitting the app doesn't stop the dashboard;
killing the server doesn't kill the app (it just shows a connection
error until the server is back).

Build + launch:

```sh
cd src-tauri
cargo tauri build --debug                  # produces target/debug/bundle/macos/Periscope.app
open target/debug/bundle/macos/Periscope.app
```

`cargo tauri dev` is **broken on this machine** — the raw debug
binary trips AMFI during icon load (`PNGReadPlugin::Initialize` on
the main thread), kernel sets PC=`0x000000000bad4007` and kills the
process before any window appears. Known Tauri-on-macOS class of
bug (tauri-apps/tauri#7351, #11912); no upstream fix. The `.app`
bundle launched via `open` goes through LaunchServices and is not
affected. So the dev loop is "build --debug + open .app" rather
than watch-mode HMR — incremental rebuilds are 5-10s once warm.

Frontend (`static/*`) changes don't need any rebuild — the shell
loads `localhost:8765`, so editing JS/CSS and reloading the window
(Cmd-R inside the app) picks up changes immediately. Only changes
to `src-tauri/src/*.rs` or config need a rebuild.

**Webview recycling (`src-tauri/src/recycle.rs`).** WKWebView leaks
IOSurface-backed graphics regions under real (trusted) user input — ~2.5
regions per rail click, measured 2026-07-28; 2645 regions ≈ 4.8GB killed a
4-day renderer. Nothing in-page fixes it: reload reuses the WebContent
process, memory_pressure doesn't reclaim, synthetic events don't even
reproduce it. The shell therefore destroys and recreates the webview window
when the renderer's phys_footprint (via WKWebView's private
`_webProcessIdentifier` + `proc_pid_rusage`) tops `PERISCOPE_RECYCLE_GB`
(default 1.0) AND system input has been idle `PERISCOPE_RECYCLE_IDLE_S`
(default 300s) — plus a manual View → Recycle Webview item. Two hard-won
invariants: the rebuild must happen on a LATER tick than destroy() (same-tick
rebuild exits the whole app; an ExitRequested veto in main.rs covers the
windowless gap), and the displaced WebContent must be SIGKILLed after a grace
period (WebKit parks it in its process cache at full leaked size instead of
exiting — pid-identity-checked via proc_pidpath before the kill). Actions log
to `~/.config/periscope/shell.log`. Test by lowering the thresholds via
`launchctl setenv` (GUI apps don't inherit shell env) and watching that log.

The shell otherwise stays minimal on purpose: single-instance, window-state
persistence, notification plugin available. Native badge + native
notifications routing from the JS side via `window.__TAURI__` is
the next layer up (`static/src/tauri.js`), additive to existing UI —
the dashboard keeps working unchanged in a regular browser.

## Conventions

- The frontend is Preact (`static/src/`, bundled to the committed
  `static/dist/app.js` by `npm run build`). Rebuild + commit `static/dist/`
  when `static/src/` changes — `uv run server.py` / `bin/periscope restart`
  serve the committed bundle with no build step at boot. Node is a dev/build
  prerequisite (`npm install && npm run build`), not a runtime one.
- Comments explain *why*, not what. The existing comments around
  pipe-pane, the cursor sync, and the bracketed-paste delay are the
  template — terse, point at the failure that motivated the code.
- `uv run server.py` must keep working — keep dependencies declared in
  the PEP-723 header at the top of `server.py`.
- The `.env` file is for local Anthropic API key only; never commit.
- Route error convention: report errors with `raise HTTPException(status,
  detail)` — never `return {"ok": False, "error": ...}`. Use real status
  codes (400 bad input, 404 not found, 409 conflict, 500 internal). Success
  responses return their normal payload (often `{"ok": True, ...}`); the
  `ok` key, where present, means "this operation succeeded" — don't reuse it
  for batch/delivery status.

### Commit as you go — always

**Every meaningful change gets its own commit, immediately, before
moving on.** Not at the end of the session. Not "I'll commit when this
feature is done." A working tweak to spinner detection, a fix to the
modal header layout, a new endpoint — each is a commit on its own.

Why: this is a single-user tool that runs against the live dashboard
with no test suite for most of the surface area. The git log IS the
audit trail. When something regresses, `git bisect` or a one-line
`git revert` is the recovery — both require small atomic commits to
work. Batched commits ("did a bunch of stuff to the modal") destroy
that recovery path.

Practical rules:
- Commit straight to `main`. Feature branches are overhead here.
- Single-line commit messages (project-wide rule, see global
  `~/.claude/CLAUDE.md`).
- If a change touches multiple unrelated concerns, split it into
  multiple commits before pushing.
- Don't ask "should I commit?" after a self-contained change — just
  commit it.

## Updating (`bin/periscope update` + `periscope/updater.py`)

`bin/periscope update` pulls, re-provisions, and restarts. **`git pull` +
`bin/periscope restart` is NOT equivalent**, which is the whole reason the verb
exists:

- The launchd plist is *generated* by this script, so plist changes ship as
  changes to the generator. A pull doesn't rewrite `~/Library/LaunchAgents/`,
  and `restart` (`launchctl kickstart -k`) restarts the job against the
  **already-loaded** config — plist changes need `bootout` + `bootstrap`. A
  checkout that pulled past the `NumberOfFiles` 256→1024 fix but never
  re-provisioned still runs with the 256 cap that silently wedges the server.
- Hook registration (`install-hook`) is likewise a script action, not a file in
  the repo. A pull past the Codex-hook or multi-account-config-dir commits
  leaves those panes unhooked, and the transcript view / narrator / resurrect
  go dark for them with no error anywhere.

**Ordering is the safety property.** `git pull --ff-only` runs before anything
touches launchd, so the common failures (dirty tree, diverged branch) abort
with the running server completely untouched. That's what makes the
dashboard-driven path viable: a failed update leaves the server alive to serve
the reason back. Verified by running the verb with a dirty tree and on a branch
with no upstream — both exit 1 with prod's pid unchanged.

The verb deliberately does **not** run `npm run build`: `static/dist/app.js` is
committed, so the pull already carries it, and a build against drifted
`node_modules` can emit a different bundle — dirtying the tree and breaking the
NEXT `--ff-only` pull. It ends by polling `/api/healthz` until the served SHA
matches what it pulled, so "updated" is evidence rather than a claim (and
treats healthz's `unknown` — git absent from the launchd PATH — as success, or
it would report a timeout for an update that landed).

**Past `bootout`, a failure means nothing is running at all**, and the
dashboard that would report it is gone with it. Three guards, in order: `uv` is
resolved BEFORE the pull (a pull that lands then aborts leaves the new
committed bundle talking to the old Python — silent, permanent skew);
`plutil -lint` validates the generated plist before anything is torn down; and
`bootout` is followed by a poll on `launchctl print` until the job actually
leaves, then `bootstrap` retries. `bootout` is asynchronous and periscope has
twice lingered in teardown (20s once, 3+ min once — see below), which would
otherwise land exactly here.

**The verb refuses to run from a linked worktree.** `$REPO` is `dirname "$0"`,
so running it from `.claude/worktrees/foo` would pull the FEATURE branch and
write `WorkingDirectory=<worktree>` into the *prod* plist — leaving prod
pointing at a directory `ExitWorktree` later deletes. Detected by
`git rev-parse --git-dir` differing from `--git-common-dir`. This is separate
from `updater.start()`'s `is_prod()` gate; the script is a user-facing verb and
needs its own.

`GIT_TERMINAL_PROMPT=0` + `ssh -oBatchMode=yes`: under launchd there is no tty
to answer a credential or host-key prompt, and a wedged `git pull` would pin
`updater.running()` true forever, 409ing every later attempt. `STALE_PROC_S`
(15 min) is the backstop — past it, `start()` kills the wedged updater rather
than refusing forever.

**From the dashboard.** `updater.check()` runs on the activity worker's tick
(self-throttled hourly) and counts commits behind the tracked upstream; the
count rides `/api/state` as `update` and renders as a header pill. A probe that
can't answer (offline, no upstream) LEAVES THE LAST COUNT STANDING — going
offline doesn't make the checkout less behind, and publishing 0 would render as
"up to date", the one wrong answer. Assert that through `summary()`, not
`check()`'s return value: the caller discards the return, so a test on it
passes even while `_behind` is being clobbered. Clicking it
POSTs `/api/update`, which spawns the script **detached**
(`start_new_session=True`) — non-negotiable, because the script's `bootout`
tears down the launchd job and would otherwise kill the very process running
it. The POST cannot report success (a successful update kills the server
mid-request), so the two outcomes are read differently: success = the server
dies, the connection banner shows, and the next poll carries `behind: 0`;
failure = the server is still alive and `/api/update/status` has the log tail.

Both `check()` (worker-gated) and `start()` (explicitly gated) are prod-only. A
dev instance runs from a worktree on a feature branch, where `git pull
--ff-only` would fail or pull the WRONG branch over work in progress; `POST
/api/update` 409s there. This also means the pill is invisible in dev by
construction — hence the render test in
`static/src/chrome/__tests__/updatePillRender.test.jsx`, since the browser
can't exercise those states.

## Development workflow (prod + dev split)

Periscope runs in two flavors:

- **Prod** — launchd-managed (`com.tom.periscope`), port 8765, runs from
  this repo's `main` branch. Never edit files in the prod working tree;
  launchd respawns on crash and picks up changes on next restart whether
  intended or not. Manage with `bin/periscope {start|stop|restart|status|tail}`.

- **Dev** — runs in a worktree on port 8766. This is where edits and
  iteration happen. Browse at http://localhost:8766/. Dev periscope doesn't
  bind `/tmp/periscope-mcp.sock` — Claude's channels always talk to prod on
  8765 — and it writes its OWN persistent stores: `state-dev.json` +
  `periscope-dev.db` (see `config.instance_file`). `state-dev.json` is
  seeded once from prod's on first boot so the dev dashboard isn't empty;
  after that the two never touch again.

**Never let two instances share a persistent store.** Both `state.json` and
`periscope.db` are read wholesale into memory at boot and written back
wholesale, so sharing one file is last-writer-wins: a dev server started at T
silently reverts every prod change made after T, with no error on either side.
This cost hours on 2026-07-23 — a dev server on :8766 repeatedly reverted
prod's `state.json`, undoing edits that had been verified correct seconds
before. If you add another persistent store, route it through
`config.instance_file()`.

Corollary for anything that edits `state.json` by hand: **the running server
will clobber you.** It holds `_STATE` in memory and rewrites the whole file
(observed within 8s). Prefer the API (`/api/prefs/*`). If you must edit the
file: stop the server, poll until the *specific* process is gone — `bin/periscope
stop` returns before the process exits, and it has twice ignored SIGTERM and
lingered (once 20s, once 3+ min hung in teardown with the port already
released) — then edit, then start. Verify AFTER a delay, not just immediately;
a clean read seconds after a restart proves nothing.

This repo works in worktrees, which is the standing instruction that
sanctions `EnterWorktree` here. Use the built-in tools — never
`git worktree add` / `remove`.

Standard loop for a periscope change:

1. **Push `main` first.** `EnterWorktree` branches from `origin/main`
   (`worktree.baseRef` defaults to `fresh`), so unpushed commits on local
   `main` are silently absent from the worktree. This repo commits straight
   to `main` and pushes rarely, so local `main` is routinely dozens of
   commits ahead — skipping this step means developing against stale code.
2. `EnterWorktree(name: "my-change")` → creates `.claude/worktrees/my-change`
   and switches the session into it.
3. `PERISCOPE_PORT=8766 PERISCOPE_DEV=1 uv run server.py`, then edit and test
   at http://localhost:8766/. Commit as you go (see "Commit as you go").
   Rebuild the bundle (`npm run build`) and commit `static/dist/` if you
   touched `static/src/`.
4. Merge **without leaving the worktree**:
   `git -C ~/dev/periscope merge <branch>`. Merging a branch that's checked
   out in a linked worktree is fine — git only blocks *checking it out*
   twice.
5. `bin/periscope restart` — `launchctl kickstart -k`, so it always respawns
   prod from `~/dev/periscope` (the plist pins `WorkingDirectory` there)
   regardless of where you run it. Step 4 is what makes the code live, not
   this.
6. `ExitWorktree(action: "remove")` — deletes the worktree and its branch.
   This only succeeds *after* step 4: before the merge, the commits aren't
   on `main` and the tool refuses. That refusal is the safety net — don't
   reach for `discard_changes: true` to get past it.

`PERISCOPE_NO_RECLAIM=1` skips the pidfile-reclaim step in `__main__`.
Set it when intentionally running a second instance that must not kill
the existing one — rare; debug only.

`bin/periscope install` generates the launchd plist — paths resolved
from the current checkout and the `uv` location — writes it to
`~/Library/LaunchAgents/com.tom.periscope.plist`, and loads it.

## Releases

`bin/release X.Y.Z` cuts a release: it bumps the version in
`src-tauri/tauri.conf.json`, `src-tauri/Cargo.toml`, and `pyproject.toml`,
re-syncs `src-tauri/Cargo.lock`, commits `release: vX.Y.Z`, tags it, and
pushes. The pushed `v*` tag triggers `.github/workflows/release.yml`,
which builds the universal macOS `.dmg` via `tauri-apps/tauri-action`
and publishes a GitHub Release.

The `.dmg` is unsigned — no Apple Developer ID is wired up.
`tauri-action` builds unsigned when no signing secrets are present; to
ship signed and notarized builds later, add the `APPLE_*` secrets the
action documents — no workflow restructuring needed.
