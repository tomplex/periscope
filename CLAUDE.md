# periscope — notes for Claude

## What this is

A FastAPI server (`server.py`) plus a browser frontend (`static/`) that
gives a dashboard over the host's tmux sessions. `uv run server.py` reads
its dependencies from the PEP-723 inline metadata at the top of the file
and serves `static/` as-is.

The frontend is mid-migration from vanilla ES modules to Preact +
`@preact/signals`. The committed `static/dist/` bundle (built by
`npm run build` from `static/src/`) is the one build artifact — rebuild
and commit it whenever `static/src/` changes; `bin/periscope restart`
needs no build step because the bundle is already in the tree. During the
migration the Preact app mounts behind the `?preact=<surface>` switch in
`index.html`; the vanilla modules under `static/` stay the fallback until
the final cutover.

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
`vite.config.js` proxies `/api/*` and `/ws/*` to FastAPI on :8765. Vite is
purely a dev convenience — production keeps loading the modules in
`static/` directly from FastAPI with no build artifact.

`dev.sh` exports `PERISCOPE_DEV=1` so uvicorn runs with `--reload`. The
pidfile reclaim path (see below) treats a reloader child as the same
instance.

## Tests

There IS a test suite — small, surgical, run with `uv run`:

```sh
uv run test_parse_pane.py            # spinner / status-line regex regressions
uv run tests/test_channel_smoke.py   # MCP wire-format compat against pinned mcp==1.27.*
```

These exist because each one tracks a class of regression that has bitten
us repeatedly: parse_pane every time Claude tweaks its TUI; the channel
smoke test every time we'd otherwise discover an SDK break only at
runtime when a pane connects. Add cases here when you find a new
variation, don't open a parallel framework.

## Architecture

```
browser
 ├── /            → static/index.html + app.js (grid dashboard)
 │       polls /api/state every 3s, renders cards, opens modal on click
 │       modal opens WS /ws/pane → xterm.js mirror of the live tmux pane
 └── /history     → static/history.html + history.js (search UI)
         hits /api/history/{search,session/:id,stats}

FastAPI (server.py, single process)
 ├── /api/*       all REST endpoints (state, prefs, send, window/*, history/*, ...)
 ├── /ws/pane     bidirectional terminal bridge (capture-pane snapshot + pipe-pane FIFO)
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
| `periscope/store.py` | `state.json` layer (`_STATE`, load/write, migrations) |
| `periscope/channels.py` | In-process MCP server + tool implementations |
| `periscope/panes.py` | `parse_pane` + smoothing + focus tracking + `list_windows` + `_resuming` |
| `periscope/pids.py` | `@periscope_id` mint / stamp / rebind / resolve |
| `periscope/git_pr.py` | Git state + GitHub PR cache + activity timeline + `prewarm_pr_cache` |
| `periscope/lgtm.py` | LGTM mirror (poll + per-session SSE) |
| `periscope/usage.py` | Claude plan usage (JSONL parse + `claude /usage` TUI scrape) |
| `periscope/rename_ai.py` | Anthropic SDK plumbing for auto-rename |
| `periscope/routes/*.py` | One APIRouter per file (11 modules: state, prefs, pane, send, sessions, paste_image, channel, history, auto_rename, lgtm, ws) |

Tests live under `tests/` mirroring the package structure (one
`tests/test_<module>.py` per `periscope/<module>.py`, plus
`tests/routes/test_<route>.py` per route). 222 pytest tests on a
clean run. Run with `uv run pytest -q`.

Five modules deviate from the one-test-per-module mirror.
`cleanup.py` and `projects.py` have no `tests/test_<module>.py` but
are exercised indirectly through their route tests
(`tests/routes/test_cleanup.py`, `tests/routes/test_projects.py`).
`repo_locks.py`, `worktrees.py`, and `worktree_spawn.py` have no
direct test and no route test — they currently lack coverage. Add a
direct `tests/test_<module>.py` when you next touch those.

`tests/test_channel_smoke.py` is a separate PEP-723 `# /// script`
that exercises an older `channel_server.py` shape; it's excluded from
pytest collection via `tests/conftest.py:collect_ignore`. Run it
directly with `uv run tests/test_channel_smoke.py` if needed.

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

### Frontend (`static/`)

Plain ES modules, no bundler. Files are small, single-purpose, and
import each other directly:

| Module | Role |
|---|---|
| `app.js` | Entry point — wires header buttons, view switch, bootstraps grid + modal |
| `state.js` | Cross-module mutable in-flight state (no persistence) |
| `prefs.js` | Cache of `/api/prefs` + mutators; the persistence boundary |
| `grid.js` | Card rendering, `/api/state` polling, drag-reorder, event delegation |
| `modal.js` | Pane modal lifecycle, header, rename, image paste |
| `terminal.js` | xterm.js + `/ws/pane` lifecycle, reconnect logic |
| `commands-modal.js` | "+ command" palette editor |
| `overlay.js` | Shared Escape-handler registry so multiple modals don't fight |
| `history.js` | `/history` SPA — search, results list, detail pane |
| `util.js` | Pure helpers (escapeHtml, apiCall, …) |
| `sw.js` | No-op service worker — exists only as a PWA installability gate |
| `vendor/xterm.{js,css}` | Vendored upstream; loaded as plain `<script>` so `Terminal`/`FitAddon` land on `window`. Don't edit; replace wholesale to upgrade. |

`grid.js` ↔ `modal.js` is a tolerated circular import (modal needs to
trigger a poll after rename; grid needs to open the modal on click).

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

3. **WebSocket initial paint mirrors tmux's screen state.** Width, height,
   cursor position, and alt-screen mode all come from `display-message`
   before the capture-pane body is sent. The prefix enters alt-screen if
   needed, clears the buffer, and the suffix parks the cursor where tmux
   thinks it is — without all three, incremental updates from `pipe-pane`
   land at the wrong cursor and leave ghost text.

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
   indicator flickers. Done in `app.js`, not the server.

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
shell." Add a case to `test_parse_pane.py` for any new variation.

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
- `spawn_claude(prompt, session?, cwd?, name?)` — fork a fresh Claude
  pane in a new tmux window with the given first message.

Notifications go the other way as `notifications/claude/channel`
messages, surfacing in Claude's prompt as `<channel source="periscope">`
blocks. The pinned `mcp==1.27.*` is checked at startup and exercised by
`tests/test_channel_shim.py`; bump both together.

`.empty-mcp.json` at the repo root (`{"mcpServers":{}}`) is read only
by `periscope/usage.py` — it's passed to `claude --strict-mcp-config`
so the hidden `/usage`-scrape session boots with no MCP servers.
`scrape_usage_via_tmux` recreates it if missing, so deleting it is
harmless.

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
the next layer up (`static/tauri.js`), additive to existing UI —
the dashboard keeps working unchanged in a regular browser.

## Conventions

- The frontend is migrating to Preact (`static/src/`, built to the
  committed `static/dist/app.js` by `npm run build`). Rebuild + commit
  `static/dist/` when `static/src/` changes — `uv run server.py` serves
  the committed bundle with no build step at boot. The remaining vanilla
  ES modules under `static/` are the migration fallback and are deleted
  in the final cutover.
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
