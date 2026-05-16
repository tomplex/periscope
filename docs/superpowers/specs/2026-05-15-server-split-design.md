# Server split — design

**Date:** 2026-05-15
**Status:** draft, revision 3 (full-scope + test-first)
**Author:** Tom + Claude

## Summary

`server.py` is 3,543 lines and has crossed the threshold where "single
file, navigate by `# --- Title ---` banners" stops paying for itself.
Split it into a `periscope/` package with subsystem modules and an
`APIRouter`-per-route-file layout. Keep `server.py` at the repo root as
a thin entry-point that preserves the `uv run server.py` promise, the
PEP-723 inline metadata header, and all the pre-uvicorn startup work
the existing `__main__` block does (pidfile reclaim, signal handler,
loop selection, scoped reload-dirs).

The split is structural — no behavior change, no API change. It DOES
introduce a real pytest test suite, written test-first per move (see
§"Testing strategy" below). Executed in **two stages of peels** across
two plan documents:

- **Stage A (Peels 0–4):** scaffold + pytest infra + the four cleanest
  subsystems (config/log/pidfile, tmux, channels, store).
- **Stage B (Peels 5–9):** panes/pids, LGTM, git_pr/usage/rename_ai,
  routes split, final `app` move.

Both stages execute back-to-back; no pause between. Each peel is one
commit on `main`. Each peel's verification gate is `uv run pytest -q`
(the full suite, growing peel by peel) plus a targeted manual smoke
for behaviors that pytest can't reasonably exercise (live MCP RPC,
WebSocket terminal bridge, end-to-end paste flow).

## Motivation

Concrete things that hurt today:

1. **`/api/state` is unreviewable in a vacuum.** It pulls state from
   six subsystems (panes, channels, git/PR, LGTM, store, focus
   tracking) and produces the dashboard's sole polled payload.
   Reading it requires holding the whole file's globals in your head.
2. **Channels is ~750 contiguous lines** (336–1086) — the largest
   single block. It's mostly self-contained but reaches into pane
   state in a few specific spots (`_resolve_pid_for_pane` calls
   `list_windows` + `_attach_git_then_resolve_pids`;
   `_do_spawn_claude_tool` calls `note_focus`/`note_action`).
   Inlining it costs context budget on every read.
3. **Forward-reference hazard in `lifespan()`.** Today it works
   because Python resolves names at call-time and the helpers all
   happen to be defined later in the same module
   (`prewarm_pr_cache`, `cached_scraped_usage`,
   `kill_orphan_usage_sessions`, `_mcp_listener`,
   `_lgtm_periodic_refresh`, `_LGTM_SSE_TASKS`, `MCP_SOCKET_PATH`).
   This is a load-bearing accident, not a design.
4. **Tests can't import cleanly.** `test_parse_pane.py` does
   `sys.path.insert(0, str(Path(__file__).parent))` then
   `import server`, and references `server.SPINNER_RE`,
   `server.ACTIVE_OP_RE`, `server.parse_pane`. Importing `server`
   triggers `_load_state()` (writes `~/.config/periscope/state.json`
   if missing) and the channels migration — pre-existing test smell
   that the split should at least not make worse.

Things that don't hurt and so don't drive the split:
- File length per se. 3.5k lines is fine if cohesive; this isn't.
- Build / test / deploy performance — all fine, none apply.

## Non-goals

- Behavior change. Endpoints behave bit-identically.
- API change. Every URL, request shape, response shape preserved.
- New abstractions. No `class StateStore`, no `dataclass WindowView`,
  no DI container. Module-level dicts + locks stay; their owning
  module just has a name now.
- Frontend changes. `static/` is untouched.
- Removing the PEP-723 dependency declaration. `server.py` keeps the
  header so `uv run server.py` keeps working.
- Type-stubs / `__all__` / public-vs-private discipline. Defer.
- Extracting `build_window_view(w)` from `/api/state`. Worth doing,
  but as a follow-up on top of the split.
- 100% test coverage. Tests focus on what's moving each peel + the
  cross-cutting invariants. WebSocket terminal forwarding, MCP wire
  loop, and `prewarm_pr_cache`'s gh subprocess are still manual-smoke
  territory after the split. Coverage grows incrementally; we don't
  block a peel on retrofitting tests for unrelated behavior.
- Fixing the test-time mutation of `~/.config/periscope/state.json`.
  Pre-existing; out of scope. Acknowledged in §Known sharp edges.

## Target structure

```
periscope/
  __init__.py           # empty
  __main__.py           # `python -m periscope` → uvicorn.run(app)
  app.py                # FastAPI() + lifespan + include_router(...) + StaticFiles mount
  config.py             # paths, env, constants, TTLs, MCP_SOCKET_PATH, STATIC
  log.py                # logging setup + _bg / _task crash-capture wrappers
  pidfile.py            # _reclaim_existing_instance, _write_pidfile, _remove_pidfile
  store.py              # state.json: _STATE, _STATE_LOCK, load/write, defaults, migrations
  tmux.py               # tmux(), capture(), deliver_input(), _run(), _tmux_mutate()
  panes.py              # parse_pane + regexes + smoothing + focus + list_windows + _resuming
  pids.py               # _mint_pid, _stamp_pid, _rebind_pid, resolve_pids,
                        #   _attach_git_then_resolve_pids
  git_pr.py             # git_state_for, pr_state_for, activity timeline, prewarm_pr_cache
  lgtm.py               # _LGTM_LOCK, _LGTM_BY_REPO, _LGTM_SSE_TASKS,
                        #   cached_lgtm_state, _lgtm_periodic_refresh, _lgtm_sse_loop
  usage.py              # plan-usage: JSONL parse path + tmux-scrape path
  channels.py           # MCP server, tool implementations, _mcp_listener, CHANNEL_INSTRUCTIONS
  rename_ai.py          # Anthropic SDK plumbing for /api/auto-rename-*
  routes/
    __init__.py
    state.py            # GET  /api/state
    prefs.py            # /api/prefs/*  (ui, windows/{pid}, commands CRUD + reorder)
    pane.py             # GET  /api/pane         + POST /api/rename
    send.py             # POST /api/send, /api/send-bulk, _send_to_target
    sessions.py         # /api/session/*, /api/window/* (new/move/delete)
    paste_image.py      # POST /api/paste-image  + _sweep_old_paste_images
    channel.py          # POST /api/channel/clear-unread
    history.py          # /api/history/*, /history page
    auto_rename.py      # /api/auto-rename-{session,window}
    lgtm.py             # POST /api/lgtm/start
    ws.py               # WS   /ws/pane

server.py               # ~50-line entry point: PEP-723 header + import + the existing
                        #   __main__ block (pidfile reclaim, SIGTERM handler, atexit,
                        #   uvicorn.run with loop="asyncio", reload_dirs scoping,
                        #   PERISCOPE_DEV=="1" gate). NOT a 5-line shim.
```

### Source-line provenance

All ranges verified against today's `server.py` via banner grep + symbol
grep. A reviewer can re-verify by running:

```sh
grep -n "^# ---\|^@app\." server.py
grep -n "^def parse_pane\|^def list_windows\|..." server.py  # symbols of interest
```

| Target module | Source banner / lines |
|---|---|
| `log.py` | Logging (41–67) + Background-task error capture (68–102) |
| `pidfile.py` | Pidfile / single-instance reclaim (103–219) |
| `app.py` | `lifespan` (~187–215), `app = FastAPI(...)` (217), `app.mount(...)` (3500) |
| `config.py` | `STATIC` (218), `MCP_SOCKET_PATH` (350), TTLs scattered through |
| `store.py` | Persistent state (state.json) (220–335) |
| `channels.py` | Channels-proper, lines 336–894 (CHANNEL_INSTRUCTIONS, locks, `_channel_gc`, `_resolve_pid_for_pane`, the four `_do_*_tool` functions, `emit_channel_event`, the per-connection MCP listener loop ending at ~894). Note: the `# --- Channels ---` banner at 336 visually contains lines 895–1086, but those are NOT channels code — see `panes.py` and `tmux.py` rows below. |
| `lgtm.py` | LGTM integration helpers/state, lines 1087–1262 (the next banner at 1263 marks the boundary). Route at 3168–3207 moves to `routes/lgtm.py`. |
| `git_pr.py` | Git + PR state (1263–1391) + Activity timeline (1392–1521) + `prewarm_pr_cache` (~3470). `prewarm_pr_cache` calls `list_windows` from `panes.py` — an upward import, acceptable because it's lifespan-only. |
| `usage.py` | Claude Code plan usage (1522–1610) + Authoritative scrape (1611–~1786). |
| `panes.py` | Focus/smoothing globals (895–948 — physically inside the channels banner but logically panes state), `RESUME_EXPIRY_S` (932), `_resuming` (931), `smooth_spinner`/`smooth_is_claude`/`note_focus`/`note_action`/`update_focus_from_windows` (949–989), parse-pane regexes (~992–1078), `list_windows` (1788–1825), `parse_pane` (2019–2172). |
| `tmux.py` | `tmux()` (1080 — also physically inside the channels banner), `capture()` (1994), `deliver_input()` (2000), `_run()` (1280, currently in the git_pr block but used by sessions routes too), `_tmux_mutate()` (2578, currently mid-routes). All five are subprocess wrappers; `capture` returns raw `capture-pane` output and `deliver_input` writes to a tmux paste buffer — neither calls `parse_pane` or anything panes-shaped. |
| `pids.py` | Periscope window-ids (1827–1992) |
| `rename_ai.py` | auto-rename via the Anthropic SDK helpers (~2878–2945) |
| `routes/state.py` | `/api/state` (2173–2322) |
| `routes/prefs.py` | `/api/prefs*` (2323–2477) |
| `routes/pane.py` | `/api/pane` (2478–2588) + `/api/rename` (2589–2599) |
| `routes/sessions.py` | `/api/session/*` + `/api/window/*` (2600–2795) |
| `routes/history.py` | `/api/history/*` (2796–2867) + `/history` page (2869–2877) |
| `routes/auto_rename.py` | `/api/auto-rename-*` route bodies (2947–3113) |
| `routes/send.py` | `_send_to_target` (3083) + `/api/send` (3114) + `/api/send-bulk` (3132) |
| `routes/channel.py` | `/api/channel/clear-unread` (3159–3167) |
| `routes/lgtm.py` | `/api/lgtm/start` (3168–3207) |
| `routes/paste_image.py` | Paste image (3208–3264) |
| `routes/ws.py` | Live terminal WebSocket bridge (3265–3499) |

## Cross-cutting decisions

### `app.py` and routes: `APIRouter` per file

Each `routes/*.py` declares `router = APIRouter()` and decorates with
`@router.get(...)`. `app.py` does:

```python
from periscope.routes import (
    state, prefs, pane, sessions, history, send, paste_image,
    channel, auto_rename, lgtm, ws,
)

app = FastAPI(lifespan=lifespan)
for r in (state, prefs, pane, sessions, history, send, paste_image,
          channel, auto_rename, lgtm, ws):
    app.include_router(r.router)
app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
```

No `from periscope.app import app` from inside routes. No circular
import.

### Lifespan dependencies must be explicit

After the split, `app.py` imports each name explicitly:

```python
from periscope.config import MCP_SOCKET_PATH
from periscope.usage import kill_orphan_usage_sessions, cached_scraped_usage
from periscope.git_pr import prewarm_pr_cache
from periscope.channels import mcp_listener  # was _mcp_listener; consider renaming
from periscope.lgtm import _lgtm_periodic_refresh, _LGTM_SSE_TASKS
```

The implicit ordering escape hatch is gone. Good.

### Shared mutable state lives in its owning module

- `_STATE` / `_STATE_LOCK` → `store.py`. Imported by `channels.py`,
  `lgtm.py`, `routes/prefs.py`, `routes/state.py`. No accessor
  wrappers (deferred).
- `_focused_at`, `_acted_at`, `_completed_at`, `_prev_state`,
  `_active_per_session`, `_resuming`, `_spinner_last_seen`,
  `_claude_last_seen` → `panes.py`. Same pattern: dicts at module
  scope, accessed by name.
- `_CHANNELS_LOCK`, `_CHANNEL_REPLIES`, `_CHANNEL_UNREAD`,
  `_MCP_SESSIONS` → `channels.py`. Read by `routes/state.py` (to
  attach channel state to each window) and `routes/channel.py`.
- `_LGTM_LOCK`, `_LGTM_BY_REPO`, `_LGTM_SSE_TASKS` → `lgtm.py`.
  Read by `routes/state.py`, `routes/pane.py`, `routes/lgtm.py`,
  and `app.py` (lifespan cancels SSE tasks on shutdown).
- TTL caches (git, PR, activity, usage, scraped usage) → with their
  fetcher functions in `git_pr.py` and `usage.py`.

### `note_focus` + `note_action` are paired across all callers

Every "user touched this pane" event calls **both** `note_focus(target)`
and `note_action(target)` (always together). Verified call sites:
`/api/session/new` (2612–2613), `/api/window/new` (2699–2700),
`/api/window/move` (2740–2741), `_send_to_target` (3109–3110),
`/api/paste-image` (3260–3261), `/ws/pane` (3286 — `note_action` only,
not `note_focus`; modal-open isn't a tmux focus shift).
`channels._do_spawn_claude_tool` also calls both (586–587).

After the split, every route module + `channels.py` imports both
helpers from `panes.py`.

### `server.py` entry point

The current `__main__` block (lines 3504–3543) is **not** a 5-liner.
It does:

1. `_reclaim_existing_instance()` — must run before uvicorn binds the
   port; uvicorn binds before lifespan, so this can't move into
   lifespan.
2. `_write_pidfile()` + `atexit.register(_remove_pidfile)`.
3. `signal.signal(SIGTERM, _on_sigterm)` — atexit doesn't fire on raw
   SIGTERM without this.
4. `uvicorn.run("server:app", loop="asyncio", reload=dev_mode,
   reload_dirs=[Path(__file__).parent] if dev_mode else None,
   log_level="info")` — `loop="asyncio"` is a uvloop+CPython 3.14
   workaround (commented in source); `reload_dirs` is scoped to
   prevent `static/` edits from bouncing the worker.
5. `dev_mode = os.environ.get("PERISCOPE_DEV") == "1"` — strict
   string equality, NOT `bool(...)` (so `PERISCOPE_DEV=0` doesn't
   enable reload).

#### Critical: don't trigger a double-import of `server.py`

When `uv run server.py` runs, the module is loaded as `__main__`,
NOT as `sys.modules["server"]`. If anything in the codebase does
`from server import X`, Python re-executes `server.py` from scratch
under the name `server`, producing two distinct module objects with
their own copies of every global (`_STATE`, `_CHANNEL_REPLIES`, the
`app` instance). Uvicorn then serves the wrong copy. Pidfile reclaim,
state mutation, and channel sessions silently get the orphan.

**The mitigation is structural, not a workaround:** never let any
`periscope/` module do `from server import ...`. The migration plan
maintains this by keeping the uvicorn import string as `"server:app"`
through Peels 0–8 (so `app` lives in `server.py` throughout the move
and `periscope/` modules never need to reach back), then in Peel 9
moving `app = FastAPI(lifespan=lifespan)` and the lifespan + route
mounts into `periscope/app.py` *and* changing the uvicorn target to
`"periscope.app:app"` *in the same commit*. After that final peel,
nothing imports `server`; the shim is pure entry-point.

#### Final post-Peel-9 shim

```python
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "fastapi", "uvicorn[standard]", "anthropic", "httpx",
#     "python-dotenv", "mcp==1.27.*",
# ]
# ///
"""Periscope — live tmux dashboard. Run with: uv run server.py"""

if __name__ == "__main__":
    import atexit, os, signal, sys
    from pathlib import Path
    import uvicorn

    # Imports are inside __main__ so a hypothetical `import server` from
    # elsewhere (e.g. an REPL session) doesn't trigger pidfile reclaim or
    # signal handler installation. Won't happen in practice; cheap to enforce.
    from periscope.pidfile import (
        _reclaim_existing_instance, _write_pidfile, _remove_pidfile,
    )
    from periscope.log import log

    _reclaim_existing_instance()
    _write_pidfile()
    atexit.register(_remove_pidfile)
    def _on_sigterm(signum, _frame):
        log.info("received signal %d; exiting", signum)
        sys.exit(0)
    signal.signal(signal.SIGTERM, _on_sigterm)

    dev_mode = os.environ.get("PERISCOPE_DEV") == "1"
    uvicorn.run(
        "periscope.app:app",   # uvicorn loads this fresh in the worker process
        host="127.0.0.1",
        port=8765,
        log_level="info",
        loop="asyncio",
        reload=dev_mode,
        reload_dirs=[str(Path(__file__).parent)] if dev_mode else None,
    )
```

The shim never imports `app` at module top — uvicorn loads
`periscope.app` from the import string in its worker process. That
worker's import of `periscope/app.py` is the one and only time the
package gets loaded; `server.py` never imports anything from
`periscope/` at module level. Python and `uv run` put the script's
directory on `sys.path[0]` automatically, so `periscope/` is
discoverable without `sys.path.insert`.

`httpx` is in the dependency list because `lgtm.py` (post-Peel-6)
imports it, even though no top-level code in the shim does — uv
resolves PEP-723 deps once for the script's entire runtime.

`reload_dirs` points at `Path(__file__).parent` (= the repo root),
which contains `periscope/` as a subdirectory. Uvicorn's default
reloader recurses into subdirectories, so edits to `periscope/app.py`
and the rest of the package trigger reload without further config.

## Testing strategy

### Test-first per move

Every symbol or cohesive group of symbols that moves out of `server.py`
gets a pytest test BEFORE the move. The flow per symbol:

1. Write a pytest module under `tests/` that exercises the symbol via
   `from server import X` (or whatever import path is current).
2. Run the test → must pass against the current code.
3. Move the symbol to `periscope/<module>.py`.
4. Update the test's import to `from periscope.<module> import X`.
5. Run the test → must pass against the moved code.
6. Commit (test + move + import update together).

This is not aspirational. Each peel's plan enumerates the tests
required up front; no peel ships without them.

### Why pytest now

Two reasons. (1) The existing `test_parse_pane.py` is a hand-rolled
`# /// script` runner that only covers two regexes and `parse_pane`.
It can't grow to cover `_do_link_pr_tool`, `_load_state`,
`smooth_spinner`, or `/api/state` without becoming an ad-hoc framework
— pytest already exists in `pyproject.toml`'s dev deps and the
`history/tests/` directory uses it idiomatically. (2) A real test
suite is exactly the artifact the move makes possible: pre-split,
testing `parse_pane` required importing 3500 lines of server side
effects. Post-split, each module is importable in isolation.

### Layout: mirror the package

```
tests/                              # NEW — periscope-proper test suite
├── conftest.py                     # shared fixtures (tmp_xdg_home, fake_tmux, ...)
├── test_config.py                  # paths + constants resolve correctly
├── test_log.py                     # _bg / _task crash capture
├── test_pidfile.py                 # reclaim heuristics with monkeypatched os.kill
├── test_tmux.py                    # subprocess wrappers, ANSI strip regexes
├── test_channels.py                # _do_*_tool implementations against in-memory _STATE
├── test_store.py                   # load/write atomicity, migration idempotency
├── test_panes.py                   # parse_pane (folds in test_parse_pane.py), smoothing, focus
├── test_pids.py                    # mint/stamp/rebind/resolve
├── test_git_pr.py                  # git_state_for parsing, PR cache behavior with mocked gh
├── test_lgtm.py                    # cached_lgtm_state + slug mapping (SSE loop is manual smoke)
├── test_usage.py                   # parse_usage_screen + JSONL parsing
├── test_rename_ai.py               # build_rename_prompt + Anthropic SDK plumbing (mocked)
├── test_app.py                     # lifespan startup + shutdown, app construction
└── routes/
    ├── __init__.py
    ├── test_state.py               # /api/state via TestClient
    ├── test_prefs.py               # /api/prefs/* CRUD
    ├── test_pane.py                # /api/pane + /api/rename
    ├── test_send.py                # /api/send + /api/send-bulk with mocked tmux
    ├── test_sessions.py            # /api/session/*, /api/window/* with mocked tmux
    ├── test_paste_image.py         # /api/paste-image with tempdir
    ├── test_channel.py             # /api/channel/clear-unread
    ├── test_history.py             # /api/history/* with stub history.db
    ├── test_auto_rename.py         # /api/auto-rename-* with mocked Anthropic
    ├── test_lgtm.py                # /api/lgtm/start with mocked httpx
    └── test_ws.py                  # /ws/pane smoke via TestClient.websocket_connect

history/tests/                      # UNCHANGED — already pytest-shaped
tests/test_channel_smoke.py         # UNCHANGED — exercises channel_server.py,
                                    # not periscope/. Keep at this path; the new
                                    # tests/ dir is a superset.
test_parse_pane.py                  # DELETED in Peel 5 — folded into tests/test_panes.py
```

`pyproject.toml`'s `testpaths` extends to include `tests/`:

```toml
[tool.pytest.ini_options]
testpaths = ["tests", "history/tests"]
python_files = ["test_*.py"]
addopts = "-ra --strict-markers --strict-config"
```

### Fixtures

`tests/conftest.py` provides:

- `tmp_xdg_home(monkeypatch, tmp_path)` — sets `$XDG_CONFIG_HOME` to a
  per-test tempdir so `_state_path()`, `_pidfile_path()`,
  `_log_path()` all land under it. Cleans up automatically.
- `fake_tmux(mocker)` — monkeypatches `periscope.tmux.tmux` with a
  recording mock. Returns the mock so tests can assert
  `mock.assert_called_with("rename-window", "-t", "...", "new")`.
- `client(app)` — `fastapi.testclient.TestClient` for the FastAPI
  `app`. Used by `tests/routes/*`.
- `clean_state(tmp_xdg_home)` — returns a fresh `_STATE`-shaped dict
  and seeds it into `periscope.store._STATE` for the test, restoring
  the original after. Used by tests that mutate `_STATE` (channels,
  prefs routes).
- `mcp_socket_path(tmp_path, monkeypatch)` — overrides
  `periscope.config.MCP_SOCKET_PATH` to a tempdir socket path for
  channels tests that need to bind.

### What pytest doesn't cover

- **WebSocket terminal forwarding** (`/ws/pane`). TestClient's
  `websocket_connect` can do a smoke test (connect, receive size
  frame, disconnect), but real tmux integration is out of scope.
- **Live MCP RPC via channel_shim.** Already covered by
  `tests/test_channel_smoke.py` for the wire format. End-to-end
  attach-from-Claude is manual smoke per peel.
- **`prewarm_pr_cache`'s `gh pr list` shellout.** Mock the
  subprocess.
- **`tmux()`'s actual subprocess.** Tests of code that calls `tmux()`
  use the `fake_tmux` fixture; only `tests/test_tmux.py` itself
  exercises real subprocess invocation (skipped if `tmux` not on
  PATH).

## Migration plan: two stages, ten peels

Each peel is a separate commit on `main`. Verification gate after
every peel:

```sh
uv run pytest -q                # full suite (grows peel by peel)
uv run server.py &              # boots cleanly to "Application startup complete"
sleep 2 && curl -fsS http://127.0.0.1:8765/api/state >/dev/null && echo OK
# (kill the background server before next peel)
```

The `pytest` suite is the authoritative gate. Boot + curl is a
smoke that exercises real lifespan + the static-mount + at least one
route end-to-end (catches import-cycle errors that unit tests don't).

#### What the gate doesn't catch

Pytest covers per-symbol logic + routes via TestClient. Boot + curl
covers startup + one route. Still uncovered, and surfaced via manual
smoke per peel:

- **Channels:** `_resolve_pid_for_pane`, the four `_do_*_tool`
  implementations, and the live MCP RPC path. Manual end-to-end is
  the gate (see below).
- **LGTM:** `cached_lgtm_state`, the SSE loop, the `/api/lgtm/start`
  route. Surface in the UI by opening a project that has LGTM
  running on :9900.
- **Send / paste path:** `_send_to_target`, `/api/send`,
  `/api/send-bulk`, `/api/paste-image`. Manual: send a phrase to a
  pane through the dashboard and confirm Enter lands.
- **Pids:** `_mint_pid`, `_stamp_pid`, `_rebind_pid`,
  `resolve_pids`, `_attach_git_then_resolve_pids`. Surface by
  watching a fresh pane get a `@periscope_id` stamped after the
  first `/api/state` poll.
- **WebSocket bridge:** `/ws/pane`. Manual: open a modal, type into
  the terminal, resize the modal.

For each peel, run a 30-second manual smoke targeted at whatever
subsystem the peel touched. The plan document (separate artifact)
spells these out per-peel.

`tests/test_channel_smoke.py` does NOT exercise `server.py`'s channels
block — it imports `channel_server.py` directly. Run it after Peel 3
to confirm `channel_server.py` still works (likely unaffected), but
don't treat it as coverage for the channels peel itself.

If any verification fails: revert the peel, diagnose, retry. Don't
land peels stacked on a broken base.

### Stage A — scaffold + clean subsystems

#### Peel 0: scaffold

Create empty `periscope/__init__.py`, `periscope/__main__.py`,
`periscope/routes/__init__.py`. **Do not change `server.py` or its
uvicorn import string** — `"server:app"` stays through Peel 8. No
content moves, no re-exports, no `from server import …` anywhere.

Why no `periscope/app.py` re-export trick: doing `from server import
app` from inside `periscope/app.py` would re-execute `server.py` as
the module `server` (separate from `__main__`), producing two `app`
instances and two copies of every global. See §"Critical: don't
trigger a double-import" above.

Peel 0's only job is to make `periscope/` importable as a package, so
later peels can `from periscope.tmux import tmux` etc. and `server.py`
can `from periscope.X import Y` to pull moved symbols back.

#### Peel 1: infra (`config.py`, `log.py`, `pidfile.py`)

Move logging setup + `_bg` / `_task` + pidfile reclaim. Move `STATIC`
and `MCP_SOCKET_PATH` to `config.py`. ~250 lines.

#### Peel 2: `tmux.py`

Move `tmux()`, `capture()`, `deliver_input()`, `_run()`,
`_tmux_mutate()`, `_ANSI_SGR_RE`, `_FG_COLOR_RE`. ~70 lines.

#### Peel 3: `channels.py`

Largest single block. Cohesive but with three concrete out-edges to
other subsystems the spec must own:

- `_resolve_pid_for_pane` (line 448) calls `list_windows()` and
  `_attach_git_then_resolve_pids()`. Both still live in `server.py`
  at this point (Peel 5 moves them).
- `_do_spawn_claude_tool` (~line 580) calls `note_focus`,
  `note_action`, `list_windows`. Same.
- `_do_link_pr_tool` and `_do_link_linear_tool` write to `_STATE` and
  call `_write_state(_STATE)`. After Peel 4 these become
  `from periscope.store import _STATE, _STATE_LOCK, _write_state`.

**Bridge strategy: function-local imports, never module-top.** The
naive bridge `from server import list_windows, ...` at the top of
`periscope/channels.py` would re-execute `server.py` under the
module name `server` (separate from `__main__`), see §"Critical:
don't trigger a double-import." Instead, `_resolve_pid_for_pane` and
`_do_spawn_claude_tool` do their `from server import ...` inside the
function body. By the time those functions actually run (during a
live MCP request), `sys.modules["server"]` already exists because
the worker process imported `server` to satisfy the `"server:app"`
uvicorn target. The local import returns the cached module, no
re-execution.

In Peel 5 the local imports become module-top
`from periscope.panes import list_windows, note_focus, note_action`
and `from periscope.pids import _attach_git_then_resolve_pids` —
safe at module-top because `periscope.panes` and `periscope.pids`
don't import from `server`.

The bridge code lives in two ~3-line helpers; document them in the
plan with explicit `# BRIDGE: removed in Peel 5` comments so they
don't ossify.

#### Peel 4: `store.py`

Move `_STATE`, `_STATE_LOCK`, `_load_state`, `_write_state`,
`_STATE_DEFAULTS`, `_DEFAULT_COMMANDS`, `_seed_commands_if_empty`,
`_channels_migration_v1`. ~115 lines. Re-resolve channels' bridge
import to `periscope.store`.

**End of Stage A.** `server.py` drops by roughly a third (~1185 of
3543 lines moved). Channels (the largest single block) is out, store
is out, infra and tmux helpers are out. A real pytest suite covers
everything moved so far. Proceed immediately to Stage B (Tom's call:
no pause, full split run-to-completion).

### Stage B — trickier subsystems and routes

#### Peel 5: `panes.py` + `pids.py`

Pane introspection: `parse_pane` + regexes + smoothing + focus
tracking + `list_windows` + `_resuming` go to `panes.py`. The
`@periscope_id` minting and resolution (including
`_attach_git_then_resolve_pids`) go to `pids.py` because they depend
on git_pr (`cached_git_state`).

**Update `test_parse_pane.py` in this peel.** Replace
`sys.path.insert(0, str(Path(__file__).parent)); import server` with
`from periscope.panes import SPINNER_RE, ACTIVE_OP_RE, parse_pane`,
and rewrite **five** references: `server.SPINNER_RE` (line 322),
`server.ACTIVE_OP_RE` (line 323), and three `server.parse_pane(...)`
calls at lines 338, 357, 371. A `replace_all` on the literal `server.`
prefix is the cleanest move.

This also changes the test's import-time side effects: `import server`
runs `_load_state()` and the channels migration; importing
`periscope.panes` does not (panes has no module-level state load).
A modest pre-existing bug fix as a side-benefit.

Also move `RESUME_EXPIRY_S` (line 932, currently adjacent to
`_resuming`) to `panes.py` alongside `_resuming`. `routes/state.py`
uses it at server.py:2312.

#### Peel 6: `lgtm.py`

Move LGTM helpers/state (1087–1262). Resolve forward references in
lifespan to `from periscope.lgtm import _lgtm_periodic_refresh,
_LGTM_SSE_TASKS`. ~175 lines.

#### Peel 7: `git_pr.py`, `usage.py`, `rename_ai.py`

Independent helper subsystems. Order doesn't matter; each is
~100–200 lines.

#### Peel 8: routes split

Largest peel by file count, smallest by line count per file. Within
this peel, do the routes in dependency order (least to most coupled):

1. `routes/ws.py` — depends on `tmux`, `panes.note_action`.
2. `routes/history.py` — depends on `history/` package, `panes._resuming`,
   `config.STATIC`. (Spec rev-1 correction: NOT history/ alone.)
3. `routes/paste_image.py` — depends on `tmux`, `panes` (note pair).
4. `routes/channel.py` — depends on `channels`.
5. `routes/lgtm.py` — depends on `lgtm`.
6. `routes/auto_rename.py` — depends on `rename_ai`, `panes`, `tmux`.
7. `routes/send.py` — depends on `tmux`, `panes`, `store`.
8. `routes/sessions.py` — depends on `tmux`, `store`, `pids`,
   `panes._resuming`, `panes.note_focus`, `panes.note_action`,
   `git_pr._run`. (Spec rev-1 correction: depends on `panes._resuming`,
   NOT `channels._resuming`.)
9. `routes/pane.py` — depends on `tmux`, `panes`, `lgtm`.
10. `routes/prefs.py` — depends on `store`.
11. `routes/state.py` — depends on essentially everything; do last.

After all routes move, `server.py` still owns `app = FastAPI()`,
the lifespan, the `app.include_router` calls, and the StaticFiles
mount. Routes register on this `app` via `from periscope.routes import
…` followed by `app.include_router(...)`. The uvicorn import string
remains `"server:app"`.

#### Peel 9: final `app` move + uvicorn import string flip

Single atomic commit:

1. Move `app = FastAPI(lifespan=lifespan)`, the `lifespan` function,
   the `app.include_router(...)` loop, and the `app.mount(...)` call
   from `server.py` into `periscope/app.py`. `lifespan`'s forward
   references become explicit imports as listed in §"Lifespan
   dependencies must be explicit."
2. Strip `server.py` down to the post-Peel-9 shim shown in
   §"`server.py` entry point". No `app` definition, no route
   imports, no `from periscope.app import …` at module top.
3. Change the uvicorn target string from `"server:app"` to
   `"periscope.app:app"`.

Steps 1 and 3 must land together — uvicorn imports the target string
in its worker process, so the moment the string changes, the file
behind that string must exist. Verifying Peel 9: `uv run server.py`
boots, `/api/state` returns 200, and `ps -ef | grep server.py` shows
exactly one Python process under reload-off (one parent + one worker
under reload-on, same as today).

## Risks and what will bite

### Forward-reference assumptions break loudly

Today `server.py` works in part because Python resolves names at
call-time and the module happens to define everything before anything
calls it. The peels surface this: any function that referenced a
later-defined name now fails at import. Verification gate catches it
immediately (server fails to boot), so the failure mode is loud, not
silent.

### `_STATE` write contention from more import sites

After the split, four modules import `_STATE` (channels, lgtm, prefs,
state). If any holds `_STATE_LOCK` while calling into another
subsystem that also wants the lock, we deadlock. Today this risk is
implicit; after the split it's greppable: `grep -rn "with _STATE_LOCK"
periscope/`. Keep `with _STATE_LOCK:` blocks short and self-contained:
no I/O, no calls into other subsystems, only dict ops + a final
`_write_state(_STATE)`. This rule is already followed today; the
split makes auditing it trivial.

### `--reload` watching the right tree (already handled)

Uvicorn's default reloader recurses into subdirectories of
`reload_dirs`. Today's value (`[Path(__file__).parent]`) already
covers `periscope/` once it exists. No change needed; verify in
Peel 0 that an edit under `periscope/app.py` triggers reload.

### `tests/test_channel_smoke.py` doesn't gate the channels peel

Acknowledged above; the gate is manual end-to-end. If we want
automated coverage post-split, a follow-up issue can add a
`tests/test_channels_unit.py` that imports `periscope.channels` and
exercises `_do_link_pr_tool` / `_do_link_linear_tool` against an
in-memory `_STATE`. Out of scope for this split.

### `MCP_SOCKET_PATH` cleanup: lifespan owns it

Today, lifespan's `finally` block does `os.unlink(MCP_SOCKET_PATH)`.
After the split, `MCP_SOCKET_PATH` lives in `config.py` (Peel 1) and
the listener lives in `channels.py` (Peel 3). It's tempting for
`channels.py` to also try to clean up the socket; it must not.
**Invariant: lifespan owns socket cleanup. `channels.py` never
unlinks `MCP_SOCKET_PATH`.** Double-unlink is benign in the
current FileNotFoundError-tolerant code, but the ownership is the
real invariant: a second unlinker would mask the case where the
first one didn't run.

### `_load_state()` runs on `import periscope.store`

`_STATE = _load_state()` at module top means importing `store.py`
hits `~/.config/periscope/state.json`. Same behavior today (importing
`server` does the same). Fine in production; mildly annoying for
tests. The split doesn't make it worse, but `test_parse_pane.py`'s
post-Peel-5 rewrite incidentally improves it (panes has no such
import-time work).

### Tooling: `refactor-mcp` is for renames, not moves

`refactor-mcp` exposes an LSP-backed rename tool — useful for
renaming a symbol in place (e.g. `_mcp_listener` → `mcp_listener`,
which the spec wants in §"Lifespan dependencies must be explicit").
It does NOT move symbols between files. The actual cut-and-paste from
`server.py` to `periscope/foo.py` is hand-edit + grep for callers.

The plan should use grep + `Edit` for moves, and reach for
`refactor-mcp` only for the explicit renames it lists (currently:
just `_mcp_listener` → `mcp_listener`; possibly drop the leading
underscore on more cross-module symbols once the modules exist, but
that's discretionary).

## Known sharp edges (acknowledged, out of scope)

- **Test imports trigger state.json mutation** — pre-existing; partly
  improved by Peel 5's rewrite of `test_parse_pane.py`, fully fixed
  by either dependency-injecting the state path or skipping the load
  in tests. Future work.
- **`_run` is in `tmux.py` despite originally living in the git_pr
  block** — placed there because it's a generic subprocess wrapper
  used by both `git_pr` and `routes/sessions.py` (for `tmux
  has-session` checks). Calling it `subprocess.py` would be more
  honest; keeping it in `tmux.py` because that's the only other
  subprocess-shaped helper file. Re-evaluate if a third caller shows
  up.

## What's deferred

- **Typed accessors over `_STATE`.** A `WindowAnnotation.get(pid)` /
  `.set(pid, **kwargs)` API would hide the dict structure and let
  schema changes propagate via type errors. Worth doing; not part of
  this split.
- **Extracting `build_window_view(w)` from `/api/state`.** Today the
  route is 150 lines of per-window assembly. After the split it's
  still 150 lines, just in `routes/state.py`. Extracting it to a
  pure function in `panes.py` (and unit-testing it) is the obvious
  next step.
- **`pyproject.toml` script entry / proper package install.** Would
  enable `pip install -e .` and a `periscope` console command. Not
  needed; `uv run server.py` is the canonical entry.
- **`__all__` discipline / public-vs-private boundaries.** Skip until
  there's a second consumer of any of these modules.
- **A real `tests/test_channels_unit.py`.** Would close the
  verification-gate hole flagged above. Add as follow-up.

## Open questions

1. **Bridge imports vs. peel reordering.** The spec accepts temporary
   `from server import ...` bridges in Peel 3 (channels). The
   alternative is to do panes/pids/store before channels, eliminating
   bridges at the cost of shipping the largest peel later. Default:
   bridges, because shipping channels early proves the split's value.
2. **Where does `_send_to_target` live — `routes/send.py` or
   `panes.py`?** Used only by the send route, but is a side-effecting
   helper that calls `note_focus`/`note_action`. Default to
   `routes/send.py` since YAGNI; revisit if a second caller appears.
3. **`include_router` loop vs. side-effect imports.** Loop is
   clearer and keeps registration in one place. Going with the loop.
