# periscope — notes for Claude

## What this is

A FastAPI server (`periscope/` package, `server.py` entry shim) plus a
Preact frontend (`static/src/` → committed `static/dist/app.js`) that gives a
dashboard over the host's tmux sessions. `uv run server.py` reads its
dependencies from the PEP-723 header in `server.py` and serves `static/` as-is.

Bolted on alongside the dashboard:
- **`history/`** — indexes every Claude Code conversation under
  `~/.claude/projects/` into SQLite + FTS5 (`/history`, `/api/history/*`);
  verbs and hook installation in `history/README.md`.
- **`channel_shim.py`** + `periscope/channels.py` — an in-process MCP server
  over a unix socket; each Claude pane spawns the shim, which proxies
  stdio↔socket so periscope can offer tools (`notify`, `link_pr`,
  `link_linear`, `set_name`, `spawn_claude`, …) and push notifications back
  into the pane.

## Reference docs — read BEFORE touching the area

The long-form material lives in `docs/`. Each file is the canonical account of
the failures that shaped that area; the module table below says which one
applies. Read the relevant doc before editing, not after the tests go red.

| Touching | Read first |
|---|---|
| `pids.py`, `panes.py`, `tmux_mirror.py`, `routes/ws.py`, `tmux.py`, `store.py`, `channels.py`, `tracks.py`, `routes/send.py` | `docs/invariants.md` — 19 numbered invariants, each citing the incident (cursor-before-body, `-N` on capture-pane, `@periscope_id` by window id, rebind TTLs, session-id-first identity, `report` always lands, …) |
| `app.py`, `server.py`, `periscope/__init__`, anything importing `server` | `docs/architecture-notes.md` — the `server.py`/`periscope/` split invariants (no `from server import`), `_STATE` rebind, frontend migration notes |
| `open_ops.py`, `routes/open.py`, `OpenOmnibox`, `LauncherModal`, `worktree_spawn.py` | `docs/unified-open.md` — the two open surfaces, cwd-based create-or-focus, server-side rail placement |
| the `claude` zsh wrapper, `CLAUDE_WRAPPER_PROFILE`, `ANTHROPIC_MODEL`, `session_status.py`, `resurrect.py` | `docs/wrapper-profiles.md` — profile/account/model are env-carried, never argv |
| `resurrect.py`, `tmux_persist.py`, `bin/periscope install-tmux` | `docs/tmux-persistence.md` — save-file rewrite, periscope-driven continuum save, healthz `resurrect` block |
| `channels.py`, `channel_shim.py`, `turns.py`, `pane_session_hook.py`, `codex_pane_session_hook.py` | `docs/channels.md` — MCP tools, shim reconnect protocol, pane→session mapping (live sid first, `pane_sessions` fallback) |
| `narrator.py`, `rename_ai.py`, `/api/rename`, `/api/name-pin` | `docs/narrator.md` — regeneration rules, humans win renames, name pinning |
| `lgtm.py`, Review tab, LGTM iframes | `docs/lgtm.md` — HTTP/SSE contract, debugging a blank Review tab |
| `src-tauri/` | `docs/tauri-shell.md` — build via `.app` not `cargo tauri dev`, webview recycling |
| `bin/periscope update`, `updater.py`, the launchd plist | `docs/updating.md` — why pull+restart is not `update`, the bootout guards |
| `tests/conftest.py`, any test that touches the activity DB or spawns the shim | `docs/testing.md` — the no-leaked-thread invariant (CPython 3.14 sqlite segfault), `.venv` drift, `WORKTREES_DIR` landmine |
| `pyproject.toml` `[tool.ruff]`/`[tool.ty]`, `biome.json` | `docs/linting.md` — which rules are off and why |

## Running

```sh
uv run server.py     # http://127.0.0.1:8765/
npm install && npm run dev   # frontend HMR at http://127.0.0.1:5174/, backend on :8766
```

`npm run dev` runs `dev.sh`: `uv run server.py` + vite under one process
group (ctrl+c kills both), with `PERISCOPE_DEV=1` (uvicorn `--reload`) and
`PERISCOPE_PORT=8766` — the dev backend never reclaims prod, never binds the
MCP socket, never runs the Claude-spending activity worker (`config.is_prod()`).
Vite builds the committed `static/dist/app.js`; production serves that bundle
with no build step at boot.

## Tests

```sh
uv run pytest -q                          # full suite (~1083 tests)
uv run pytest tests/test_channel_shim.py  # shim reconnect protocol (spurious failures → `uv sync`, see docs/testing.md)
uv run pytest tests/test_tmux_mirror.py   # mirror protocol + pyte convergence oracle (real tmux on -L periscope-mirror-test)
npm test                                  # vitest over the Preact app's pure helpers (~252 tests)
```

Tests live under `tests/` mirroring the package (`tests/test_<module>.py`,
`tests/routes/test_<route>.py`). Add cases to the existing modules — each one
tracks a regression class that has bitten repeatedly (`parse_pane` every time
Claude tweaks its TUI; the shim smoke test every SDK break). Don't open a
parallel framework.

**Tests must not spawn real background threads that touch the activity DB.**
The autouse `_no_plan_usage_refresh` fixture and `fresh_activity_db`'s
lock-held teardown enforce it; patching one call site is not enough. Details
and the segfault it prevents: `docs/testing.md`.

## Linting & type-checking

```sh
bin/check            # ruff + ty + biome, report-only — the gate is kept at ZERO violations
bin/check --fix      # ruff --fix + biome --write, then check
```

`.pre-commit-config.yaml` runs the same (`uv tool run pre-commit install`).
Biome is a linter only — the formatter is OFF, as are `E501`/`E701`/`E702`:
the terse hand-written style is deliberate. Rule rationale: `docs/linting.md`.

## Architecture

```
browser
 ├── /            → static/index.html → Preact app (static/dist/app.js)
 │       split view (rail + detail); polls /api/state every 3s into a signals store
 │       terminals open WS /ws/pane → xterm.js mirror of the live tmux pane
 └── /history     → static/history.html + history.js (search UI)

FastAPI (single process)
 ├── /api/*       all REST endpoints
 ├── /ws/pane     terminal bridge (capture-pane snapshot + control-mode mirror w/ reconcile frames)
 └── unix socket  /tmp/periscope-mcp.sock — in-process MCP server ← channel_shim.py (per Claude pane)
```

### Server (`periscope/` package + `server.py` shim)

`server.py` is an ~85-line shim (PEP-723 header, pidfile reclaim, signal
install, `uvicorn.run("periscope.app:app")`). **Nothing inside `periscope/`
imports from `server`** — the shim runs as `__main__`, and a second import
would double-load every global.

| Module | Role |
|---|---|
| `app.py` | `FastAPI()` + lifespan + `include_router` loop + StaticFiles (served `no-cache`, ETag-revalidated) |
| `config.py` | Cross-cutting paths + constants; `instance_file()` keeps prod/dev stores apart |
| `log.py` | Logging + `_bg` / `_task` crash wrappers — every thread/task goes through these so crashes surface |
| `pidfile.py` | Single-instance reclaim (reloader child counts as the same instance) |
| `tmux.py` / `tmux_input.py` / `tmux_mirror.py` | Subprocess wrappers / persistent control-mode input client / control-mode pane mirror with reconcile frames |
| `store.py` | `state.json` layer (`_STATE`, load/write, migrations) |
| `channels.py` | In-process MCP server + tool implementations |
| `panes.py` / `window_view.py` | `parse_pane` + spinner smoothing + focus tracking / the per-window dict the dashboard renders |
| `tracks.py` / `tabs.py` / `workspaces.py` / `projects.py` | Track registry (rail groups) / FILE-PREVIEW tabs on a pane card (not rail tabs) / legacy workspace rows / project registry |
| `worktrees.py` / `worktree_spawn.py` / `repo_locks.py` / `gitutil.py` | Worktree cache + affiliation / spawn + two-window layout / per-repo git lock / shared git helpers |
| `pids.py` | `@periscope_id` mint / stamp / rebind / resolve — pane identity |
| `session_status.py` | Authoritative Claude-session state from `<config_dir>/sessions/<pid>.json` |
| `turns.py` | Pane transcript view (`GET /api/pane/turns`) |
| `bg_commander.py` | Background command jobs + status sync |
| `resurrect.py` / `tmux_persist.py` | Save-file `--resume` rewrite + `save_now()` / provisioning the tmux side of continuation-over-reboot |
| `git_pr.py` / `lgtm.py` / `usage.py` / `cost_pressure.py` | Git state + PR cache / LGTM mirror / plan usage / per-pane context-cost pressure |
| `narrator.py` / `rename_ai.py` | Per-pane AI status lines + divergence renames / Anthropic SDK plumbing |
| `open_ops.py` | Unified-open core (path/branch/PR descriptors → create-or-focus → rail placement); no HTTP |
| `updater.py` | Self-update: commits-behind check + detached spawn of `bin/periscope update` |
| `routes/*.py` | One APIRouter per file |

### Frontend (`static/src/` → `static/dist/`)

Preact + `@preact/signals`, built by Vite. **Rebuild (`npm run build`) and
commit `static/dist/` whenever `static/src/` changes** — boot has no build step.

| Area | Modules |
|---|---|
| entry / state | `main.jsx`, `store.js` (transient signals), `prefs.js` (server-prefs cache — the persistence boundary), `poll.js` (the single `/api/state` loop) |
| chrome | `chrome/{Header,FilterBar,UsagePill}.jsx` |
| split view | `split/{Split,Rail,RailRows,Detail,…}.jsx` + `split/railTree.js` — the only view. Rail is TRACK-anchored: Track → (derived Branch) → Pane; branch rows are derived from live `w.branch`, not entities |
| terminal | `terminal/{Terminal,TerminalSearch}.jsx` + `terminalCore.js` (imperative xterm + `/ws/pane`) |
| overlays | `overlays/*.jsx` + `hooks/useEscape.js`; `OpenOmnibox` (⌘K) is the command palette, `open/classify.js` its pure classifier |
| still vanilla | `history.js` + `util.js` (the `/history` SPA), `sw.js`, `vendor/xterm.*` (replace wholesale, never edit) |

## Status-line parsing

Claude Code renders a two-line block at the bottom of its pane; `STATUS_RE`
matches the bottom line (context %, model) and `TITLE_RE` the line above
(project, branch, git state, PR URL). **If Claude changes its status format
these break and every window looks like a shell** — fix the regexes first when
triaging that, and add a case to `tests/test_panes.py`.

## Conventions

- Comments explain *why*, not what — terse, pointing at the failure that
  motivated the code (the pipe-pane, cursor-sync, and bracketed-paste comments
  are the template).
- `uv run server.py` must keep working — dependencies stay in the PEP-723 header.
- `.env` holds the local Anthropic API key only; never commit.
- Routes report errors with `raise HTTPException(status, detail)` — never
  `return {"ok": False}`. Real status codes. `ok`, where present, means "this
  operation succeeded" — don't reuse it for batch/delivery status.
- Session/index are query params, not path segments (session names contain `/`).
- Two persistent stores (`state.json`, `periscope.db`) are read wholesale at
  boot and written wholesale — **never let two instances share one**
  (`config.instance_file()`); a running server will clobber hand edits within
  seconds, so prefer `/api/prefs/*`.

### Commit as you go — always

Every meaningful change gets its own commit, immediately. The git log IS the
audit trail: `git bisect` / one-line `git revert` are the recovery path and
both need small atomic commits. Commit straight to `main`; single-line
messages; split unrelated concerns; don't ask "should I commit?".

## Development workflow (prod + dev split)

- **Prod** — launchd (`com.tom.periscope`), port 8765, runs `main` from this
  checkout. Never edit files in the prod tree. `bin/periscope {start|stop|restart|status|tail}`.
- **Dev** — a worktree on port 8766 with its own `state-dev.json` +
  `periscope-dev.db` (seeded once from prod). Doesn't bind the MCP socket —
  Claude's channels always talk to prod.

Standard loop:

1. **Push `main` first** — `EnterWorktree` branches from `origin/main`, and
   local `main` is routinely ahead.
2. `EnterWorktree(name: …)` → `.claude/worktrees/<name>`.
3. `PERISCOPE_PORT=8766 PERISCOPE_DEV=1 uv run server.py`; edit, test at
   :8766, commit as you go; `npm run build` + commit `static/dist/` if
   `static/src/` changed.
4. Merge without leaving the worktree: `git -C ~/dev/periscope merge <branch>`.
5. `bin/periscope restart` — always respawns prod from `~/dev/periscope`
   (the plist pins `WorkingDirectory`); step 4 is what makes the code live.
6. `ExitWorktree(action: "remove")` — only succeeds after the merge; that
   refusal is the safety net, don't `discard_changes` past it.

`bin/periscope install` generates the launchd plist and loads it;
`bin/periscope update` is pull + re-provision + restart (see `docs/updating.md`
for why `git pull` + `restart` is not equivalent).

## Releases

`bin/release X.Y.Z` bumps `src-tauri/tauri.conf.json`, `src-tauri/Cargo.toml`,
`pyproject.toml`, re-syncs `Cargo.lock`, commits `release: vX.Y.Z`, tags, and
pushes; the `v*` tag triggers `.github/workflows/release.yml` (unsigned
universal macOS `.dmg` via `tauri-apps/tauri-action`).
