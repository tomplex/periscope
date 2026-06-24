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
```

These exist because each one tracks a class of regression that has bitten
us repeatedly: parse_pane every time Claude tweaks its TUI; the channel
smoke test every time we'd otherwise discover an SDK break only at
runtime when a pane connects. Add cases here when you find a new
variation, don't open a parallel framework.

**Known flake:** under CPython 3.14 the pytest suite intermittently hits a
C-level sqlite segfault in a background thread (`pysqlite_query_execute`
during `executemany`), and `tests/test_activity.py::test_prune_usage_samples_drops_old_rows`
can fail in the full run while passing in isolation (shared-db test
isolation). Both are pre-existing 3.14 fragilities, not your change — re-run
to confirm green before chasing them.

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
- **ty** gates **source only**. Tests and `build_icons.py` are excluded
  (`[tool.ty.src] exclude`): tests are mock-heavy (`MagicMock`), so a
  structural checker yields ~100 noise diagnostics with no bug-catching
  value — they're verified by running them; `build_icons.py` is a manual
  icon script with an undeclared optional dep (Pillow). One inline
  `# ty: ignore[unresolved-attribute]` exists in `channels.py` for
  `asyncio.Server.close_clients()` (real since 3.13; ty's typeshed lags).
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
| `periscope/pids.py` | `@periscope_id` mint / stamp / rebind / resolve |
| `periscope/git_pr.py` | Git state + GitHub PR cache + activity timeline + `prewarm_pr_cache` |
| `periscope/lgtm.py` | LGTM mirror (poll + per-session SSE) |
| `periscope/usage.py` | Claude plan usage (JSONL parse + OAuth usage-endpoint fetch) |
| `periscope/rename_ai.py` | Anthropic SDK plumbing for auto-rename (`RENAME_RULES` taste block shared with the narrator) |
| `periscope/narrator.py` | Per-pane AI status lines + divergence renames (pure decision core + worker-driven tick; see "Narrator" below) |
| `periscope/open_ops.py` | Unified-open core: `open_target` dispatch (path/branch/pr descriptors → resolve → register → idempotent create-or-focus → server-side rail placement) + `ensure_session` / `worktree_for_branch` / `place_in_rail` / `build_catalog`. No HTTP (see "Unified open" below) |
| `periscope/routes/*.py` | One APIRouter per file (alerts, auto_rename, channel, cleanup, events, fs, healthz, history, lgtm, open, pane, paste_image, prefs, projects, send, sessions, settings, state, ws) |

Tests live under `tests/` mirroring the package structure (one
`tests/test_<module>.py` per `periscope/<module>.py`, plus
`tests/routes/test_<route>.py` per route). 634 pytest tests on a
clean run. Run with `uv run pytest -q`.

`cleanup.py` has no `tests/test_<module>.py` but is exercised
indirectly through its route tests (`tests/routes/test_cleanup.py`).
`repo_locks.py` and `worktrees.py` have no direct test and no route
test — they currently lack coverage. Add a direct
`tests/test_<module>.py` when you next touch those. (`worktree_spawn.py`
gained `tests/test_worktree_spawn.py` and `open_ops.py` has
`tests/test_open_ops.py`, both real-tmux integration tests gated on
`@needs_tmux`.)

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
canonical locked env collects 634 tests, all green. If you ever see only
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
| split view | `src/split/{Split,Rail,RailRows,Detail}.jsx` + `src/split/railTree.js` (`mergeLiveAndPrefs`) — the only dashboard view (grid retired). Rail membership is SESSION-ANCHORED: windows group by their tmux session's project (`project_pinned_dir` + the projects payload), never by cwd — cd shows as an affiliation chip, not a move. Unmanaged sessions fold into the flat bottom-pinned "dev" group (`MAIN_KEY`); its pane order persists as `panes_by_worktree.__main__` |
| modal | `src/modal/Modal.jsx` (tab strip + sidebar + review pane) |
| terminal | `src/terminal/Terminal.jsx` (ref+effect wrapper) + `src/terminal/terminalCore.js` (imperative xterm + `/ws/pane`, ported ~verbatim) |
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

4. **`capture-pane` separates rows with bare `\n`; xterm needs `\r\n`.**
   Forgetting the carriage return staircases every line right by the
   previous line's length.

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

One endpoint and one UI surface materialize a session into the rail. The
header's `+ new` button (and ⌘K) open the `OpenOmnibox` command-palette,
which loads `GET /api/open/catalog` (discoverable repos + their worktrees)
and POSTs a *target descriptor* to `POST /api/open`. The server owns all
dispatch.

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

- **Idempotent create-or-focus is NAME-based, not cwd-based.** `ensure_session`
  checks `tmux has-session` on the project's recorded `tmux_session`; cwd
  collides (the documented footgun — multiple panes per dir). Live-and-ours →
  focus; dead → spawn; live-but-foreign (name reused) → dedupe the name and
  `update_project`. This is what fixes the "project already exists" dead-end:
  a dormant registered project (session killed) reopens instead of 409ing.
- **Rail placement is server-side.** `place_in_rail` writes the rail pref
  (`repo_order` / `worktrees_by_repo` keyed by tmux **session name** /
  `panes_by_worktree`) and the route returns the `ui` blob; the omnibox writes
  it straight into `prefsSignal` via `prefs.setUI`. This killed the old
  client-side ~3500ms `deferRailAdd` poll-wait race.
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
  to `"same"` (no worktree to anchor a rail item to).

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

The shell stays minimal on purpose: single-instance, window-state
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

## Development workflow (prod + dev split)

Periscope runs in two flavors:

- **Prod** — launchd-managed (`com.tom.periscope`), port 8765, runs from
  this repo's `main` branch. Never edit files in the prod working tree;
  launchd respawns on crash and picks up changes on next restart whether
  intended or not. Manage with `bin/periscope {start|stop|restart|status|tail}`.

- **Dev** — manually started in a git worktree, port 8766. This is where
  edits and iteration happen. Browse at http://localhost:8766/. Dev
  periscope doesn't bind `/tmp/periscope-mcp.sock` — Claude's channels
  always talk to prod on 8765.

Standard loop for a periscope change:

```sh
# one-time per feature
git worktree add ../periscope-feature -b feature/my-change
cd ../periscope-feature
PERISCOPE_PORT=8766 PERISCOPE_DEV=1 uv run server.py
# edit, test at http://localhost:8766/

# when done
cd ~/dev/periscope
git merge feature/my-change
bin/periscope restart       # launchd respawns prod with new code
git worktree remove ../periscope-feature
```

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
