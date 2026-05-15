# Periscope stability: launchd, dev/prod split, frontend reconnect

## Problem

Tom uses periscope as the primary tool for managing his Claude agents. When
periscope dies, he loses visibility into his work. Two failure modes
dominate:

1. **Claude editing periscope's own source.** Claude sessions running inside
   periscope-managed panes edit `server.py` / `channel_shim.py` / `static/`
   as part of normal work. With `uvicorn reload=True`, every save bounces
   the worker. Intermediate states during a multi-step refactor frequently
   fail to import — and uvicorn's reloader only restarts the worker on
   *file change*, not on *crash*, so a failed import leaves the supervisor
   sitting on the port with no live worker until the next save.

2. **Runtime crashes unrelated to edits.** An unhandled exception in a
   request handler, WS bridge, or background task kills the worker.
   uvicorn's reloader doesn't respawn on crash. The supervisor stays
   running, the port stays bound, nothing serves requests, dashboard is
   dead until someone notices and restarts manually.

Both modes share the same root: **periscope has no real auto-respawn.** It
looks like it does (because uvicorn has a reloader process), but the
reloader only fires on file change.

A separate but compounding problem: when periscope does come back up, the
browser dashboard handles outages poorly. The grid keeps showing stale
data with no visible indication that polling is failing; the only signal
is a small `lastUpdate.textContent = "poll failed: ..."` line nobody looks
at.

## Phase 0: already landed (this session, on `main`)

These are foundational fixes the rest of the design assumes. Listed here so
the spec reads as a complete picture.

- **Real logging.** `logging.handlers.RotatingFileHandler` at
  `~/.config/periscope/periscope.log` plus stderr. Replaces the three
  `print()` calls. uvicorn `log_level` bumped from `"warning"` to
  `"info"` so handler 500s surface.
- **Background-task error capture.** `_bg(name, fn)` and `_task(coro, name)`
  helpers wrap every `threading.Thread` and `asyncio.create_task` site, so
  exceptions in daemon threads and orphaned coroutines land in the log
  instead of disappearing.
- **Pidfile + reclaim.** `~/.config/periscope/periscope.pid` written from
  `__main__`. On startup, prior pid is SIGTERM'd (escalated to SIGKILL
  after 3s), so `uv run server.py` is idempotent. `atexit` + a SIGTERM
  handler clean up the pidfile on shutdown.
- **`reload=True` gated on `PERISCOPE_DEV=1`.** Bare `uv run server.py`
  now runs as a single process. `dev.sh` sets the env var to keep
  reload-on-edit for active development. This collapses the production
  process tree from four PIDs (reload supervisor + worker +
  `multiprocessing.resource_tracker` + `multiprocessing.spawn`) to one.

## Goals

- A periscope crash is invisible. Either it doesn't happen, or it
  recovers in <2 seconds without manual intervention, and the dashboard
  shows that recovery clearly.
- A periscope process gets restarted automatically whenever it dies,
  for any reason.
- Claude editing periscope's source can't take down the dashboard Tom is
  actively using.
- Dev iteration on periscope itself remains pleasant — fast feedback, no
  ceremony.

## Non-goals

- Cross-machine HA or multi-instance load balancing. Single user, single
  Mac.
- Migrating off uvicorn / FastAPI. The single-file vanilla-everything
  shape is part of the value prop.
- Making the dashboard *fully* usable while the backend is down. Stale
  data plus a clear "disconnected" banner is the bar.
- Bounding cache sizes, adding per-route timeouts, or rate-limiting the
  fan-out. These are real issues but separate from "stability." They get
  their own future spec.

## Design

### Topology

Two periscope processes can coexist on the same Mac, on different ports,
sharing one config file:

```
┌──────────────────────────────────────────────────────────────────┐
│  launchd (com.tom.periscope.plist, RunAtLoad + KeepAlive)        │
│                                                                  │
│   ▼ supervises                                                   │
│                                                                  │
│   uv run server.py        ──────►  127.0.0.1:8765  (PROD)        │
│   workdir: ~/dev/periscope                                       │
│   ~/.config/periscope/                                           │
│     ├─ periscope-8765.pid                                        │
│     ├─ periscope-8765.log                                        │
│     └─ state.json  ◄──── shared with dev                         │
│                                                                  │
│   /tmp/periscope-mcp.sock  ◄── bound only by the :8765 instance  │
└──────────────────────────────────────────────────────────────────┘

Dev (only when actively iterating on periscope itself):
──────────────────────────────────────────────────────────────────
  cd ~/dev/periscope-feature  (a worktree on a feature branch)
  PERISCOPE_PORT=8766 PERISCOPE_DEV=1 uv run server.py
                             ──────►  127.0.0.1:8766  (DEV)
                               ~/.config/periscope/
                                 ├─ periscope-8766.pid
                                 └─ periscope-8766.log
```

Key invariants:

- **Prod is always-on.** launchd `KeepAlive=true` respawns on any exit,
  including clean shutdown. `ThrottleInterval=5` prevents tight crash
  loops.
- **Dev never serves channels.** Only the :8765 instance binds
  `/tmp/periscope-mcp.sock`. `channel_shim.py` continues to hardcode
  that path, so Claude's MCP always talks to prod. When you need to
  test channel-related changes, stop prod and run your dev instance on
  8765 temporarily.
- **State is shared.** Both instances read/write the same `state.json`.
  Concurrent writes are theoretically possible but not practically a
  problem — Tom is one user, edits are rare, last-writer-wins is
  acceptable. Flagging the risk explicitly; revisit if it bites.

### Server-side changes

**Port read once at module load.** Avoid scattering `os.environ.get(...)`
through the file (start, lifespan, pidfile path, log path) where they
could theoretically disagree after env mutation:

```python
PORT = int(os.environ.get("PERISCOPE_PORT", "8765"))
```

Lives near the other module-level constants (`MCP_SOCKET_PATH`, etc.).
Used by `uvicorn.run(port=PORT)`, the MCP gate, and the path helpers.

**Port-scoped pidfile and log paths.** Both helpers reference `PORT`:

```python
def _pidfile_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "periscope" / f"periscope-{PORT}.pid"
```

Same shape for the log path: `periscope-{PORT}.log`. Phase 0 currently
writes the un-suffixed `periscope.log` and `periscope.pid`; Phase 1
renames these in lockstep with the helper script that tails them.

**Pidfile stores `(pid, port)`, not just pid.** `_pid_is_periscope` in
Phase 0 only matches `"server.py"` in the process command line — it
cannot tell prod from dev. Path separation (`periscope-{PORT}.pid`) is
the primary isolation, but harden it by writing the pidfile as
`f"{pid}\n{port}\n"` and verifying the recorded port matches the
current `PORT` before SIGTERMing. If they disagree, log a warning and
abort the reclaim. Belt-and-suspenders against a stale pidfile that
got pointed at the wrong process by pid recycling.

**Opt-out flag for reclaim.** Add a `PERISCOPE_NO_RECLAIM=1` env var.
When set, `__main__` skips `_reclaim_existing_instance()` entirely. The
worktree workflow doc warns that running a manual `uv run server.py`
on port 8765 while launchd-managed prod is up will trigger a respawn
loop (see Testing #6); `PERISCOPE_NO_RECLAIM=1` is the escape hatch for
deliberate debug runs that intentionally collide with prod.

**MCP listener gated to :8765.** In `lifespan`, skip the MCP listener
unless the port is 8765:

```python
if port == 8765:
    mcp_task = _task(_mcp_listener(), "mcp-listener")
else:
    mcp_task = None
    log.info("dev mode (port %d): skipping MCP listener", port)
```

The teardown branch becomes conditional. `channel_shim.py` is unchanged —
it keeps hardcoding `/tmp/periscope-mcp.sock`.

**`/api/healthz`.** New endpoint, returns:

```json
{
  "ok": true,
  "pid": 12345,
  "port": 8765,
  "uptime_s": 1234.5,
  "version": "7f3f764"
}
```

`version` is the short git SHA of `HEAD` captured at module load (cached;
not re-read on every request). Useful for "did my `bin/periscope restart`
actually pick up the new code" and as a future-frontend reconnect probe.

### launchd setup

**Plist** lives in the repo as `com.tom.periscope.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>           <string>com.tom.periscope</string>
  <key>WorkingDirectory</key><string>/Users/tom/dev/periscope</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/tom/.local/bin/uv</string>
    <string>run</string>
    <string>server.py</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>/Users/tom/.local/bin:/opt/homebrew/bin:/usr/bin:/bin</string>
    <key>HOME</key><string>/Users/tom</string>
  </dict>
  <key>RunAtLoad</key>       <true/>
  <key>KeepAlive</key>       <true/>
  <key>ThrottleInterval</key><integer>5</integer>
  <key>StandardOutPath</key> <string>/Users/tom/.config/periscope/launchd-stdout.log</string>
  <key>StandardErrorPath</key><string>/Users/tom/.config/periscope/launchd-stderr.log</string>
</dict>
</plist>
```

The hardcoded `/Users/tom` paths (HOME and `uv`) are a known acceptable
wart — launchd doesn't expand `~`, and templating the plist is more
machinery than this personal tool needs.

**Helper at `bin/periscope`** (chmod +x):

```sh
#!/bin/sh
REPO="$(cd "$(dirname "$0")/.." && pwd)"
PLIST_SRC="$REPO/com.tom.periscope.plist"
PLIST_DST=~/Library/LaunchAgents/com.tom.periscope.plist
LABEL=gui/$UID/com.tom.periscope
LOG=~/.config/periscope/periscope-8765.log

case "$1" in
  install)   cp "$PLIST_SRC" "$PLIST_DST" && launchctl bootstrap gui/$UID "$PLIST_DST" ;;
  uninstall) launchctl bootout "$LABEL" 2>/dev/null; rm -f "$PLIST_DST" ;;
  start)     launchctl bootstrap gui/$UID "$PLIST_DST" ;;
  stop)      launchctl bootout "$LABEL" ;;
  restart)   launchctl kickstart -k "$LABEL" ;;
  status)    launchctl print "$LABEL" 2>/dev/null | grep -E 'state|pid' || echo "not loaded" ;;
  tail)      tail -F "$LOG" ;;
  *)         echo "usage: periscope {install|uninstall|start|stop|restart|status|tail}"; exit 1 ;;
esac
```

No `set -e` — `launchctl print` exit-coded behavior plus a grep with no
matches would otherwise exit 1 silently. `status` falls back to "not
loaded" if launchctl has no record.

`launchctl kickstart -k` is the right primitive for "redeploy" — it
SIGTERMs the running worker and lets launchd respawn it. Combined with
the pidfile + frontend reconnect, the dashboard sees ~1-2 seconds of
"reconnecting…" and recovers. MCP-attached Claudes see a ~2-4 second
gap because their channel_shim subprocesses hit a brief
`FileNotFoundError` on `/tmp/periscope-mcp.sock`, exit per their normal
backoff path, and get re-spawned by Claude Code on the next tool call.
No work needed in the shim — it already handles this.

**Log file naming entanglement.** `bin/periscope tail` references
`periscope-8765.log` (port-scoped), which only exists after the Phase 1
server-side change that introduces port-scoped paths. The helper and the
path rename ship together in Phase 1; until then, Phase 0's logger
writes `periscope.log` without a port suffix.

### Frontend reconnect/banner

Current state:

- **`grid.js` poll loop** (`/api/state` every 3s): catches errors, writes a
  small `lastUpdate.textContent = "poll failed: ${e.message}"`. Grid
  cards keep showing stale data with no visible signal that they're
  stale.
- **`modal.js` modal-header poll** (`/api/pane` at `MODAL_POLL_MS`):
  similar fail-silently behavior.
- **`terminal.js` WS reconnect:** already implemented. Reconnects with
  backoff `[250, 500, 1000, 2000, 4000]`ms, writes `[periscope:
  reconnecting…]` inline in the terminal, repaints on success. No
  change needed unless testing reveals the steady-state 4000ms ceiling
  is too slow.

Changes needed:

**Banner element.** Add as the *first child of `<body>`* in
`index.html`, before `<header class="periscope-header">`:

```html
<div id="connection-banner" hidden>
  <span>⚠ disconnected from periscope — retrying…</span>
</div>
```

Layout in normal flow — *not* `position: fixed` — so when the banner
shows, it naturally pushes the existing header down. No need to bake in
a body-level padding toggle:

```css
#connection-banner {
  background: var(--yellow-banner-bg, #5a4a10);
  color: var(--yellow-banner-fg, #f5e9c0);
  padding: 6px 12px;
  text-align: center;
  font-size: 13px;
}
body.disconnected .grid { opacity: 0.6; transition: opacity 0.2s; }
```

(Color variables match the existing periscope theme — exact values
chosen at implementation time, not material to the design.)

**Poll fail handler in `grid.js`.** Track consecutive failures and toggle
banner + body class:

```js
let consecutivePollFails = 0;
const bannerEl = document.getElementById("connection-banner");

export async function poll() {
  if (state.editingTarget) return;
  try {
    const res = await fetch("/api/state");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    state.lastWindows = data.windows;
    render(state.lastWindows);
    updateUsagePill(data.usage_scrape, data.usage);
    lastUpdate.textContent = `updated ${new Date().toLocaleTimeString()}`;
    if (consecutivePollFails > 0) {
      consecutivePollFails = 0;
      bannerEl.hidden = true;
      document.body.classList.remove("disconnected");
    }
  } catch (e) {
    consecutivePollFails += 1;
    if (consecutivePollFails >= 2) {
      bannerEl.hidden = false;
      document.body.classList.add("disconnected");
    }
    lastUpdate.textContent = `poll failed: ${e.message}`;
  }
}
```

Threshold of 2 consecutive fails before showing the banner — avoids
false-positive flicker from transient browser hiccups (laptop sleep
wake, a single throttled fetch in a backgrounded tab, an unrelated
500). Real outage detection takes ~6s instead of ~3s; that's invisible
next to `bin/periscope restart`'s ~2s of actual downtime, and the FP
reduction is worth the latency.

**Modal header poll.** No banner change — the grid banner already signals
the global outage. Just leave existing modal data in place on fetch
failure (don't blank it out).

### Worktree workflow

New section in the project `CLAUDE.md`:

```markdown
## Development workflow

Periscope runs in two flavors:

- **Prod** — launchd-managed, port 8765, runs from this repo's `main`
  branch. Never edit files in the prod working tree; launchd respawns
  on crash and will pick up changes on next restart whether you wanted
  that or not. Manage with `bin/periscope {start|stop|restart|status|tail}`.

- **Dev** — manually started in a git worktree, port 8766. This is
  where you (and Claude) edit and iterate. Browse it at
  http://localhost:8766/. Dev mode doesn't bind the MCP socket —
  Claude's channels always talk to prod on 8765.

Standard loop for a periscope change:

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

A one-liner near the top of `README.md` points at this section so a
fresh clone or a new Claude session sees the two-tier setup immediately.

## Testing approach

No automated tests — periscope has none and adding a test framework is
out of scope. Verification is hands-on:

1. **launchd respawn.** `bin/periscope install`, confirm dashboard works,
   then `kill -9 $(cat ~/.config/periscope/periscope-8765.pid)`. Within
   ~5s (ThrottleInterval), a new periscope is running. Tail
   `periscope-8765.log` to confirm the startup line.

2. **Crash respawn.** With prod up under launchd (reload off), simulate
   a hard crash: `kill -SEGV $(cat ~/.config/periscope/periscope-8765.pid)`.
   Within `ThrottleInterval` seconds, `bin/periscope status` shows the
   new pid; tail confirms the startup line. Editing source isn't a
   valid test because production has no reload supervisor.

3. **Dev/prod coexistence.** With prod up on 8765, start dev:
   `PERISCOPE_PORT=8766 PERISCOPE_DEV=1 uv run server.py`. Both
   dashboards respond. `/api/healthz` on each reports the right port.
   `lsof -nP -iTCP:8765,8766 | grep LISTEN` shows two distinct
   processes.

4. **MCP isolation and shim recovery.** Confirm
   `/tmp/periscope-mcp.sock` is bound by the :8765 process (`lsof
   /tmp/periscope-mcp.sock`). Open a Claude pane; confirm it can call
   periscope's MCP tools (talking to prod, not dev). Run `bin/periscope
   restart`; on Claude's next MCP-tool call, the shim should re-spawn
   and connect within ~5s (per `channel_shim.py`'s 2s reconnect
   backoff, plus Claude Code's MCP re-spawn). No code change in the
   shim — it already handles `FileNotFoundError` on the socket via its
   `_quiet_exit` backoff path.

5. **Frontend reconnect.** With dashboard open, `bin/periscope restart`.
   Banner appears within ~3s, disappears within ~3s of recovery. Open
   modals show `[periscope: reconnecting…]` in the terminal and repaint
   without intervention. Grid data resumes updating.

6. **Reclaim path: launchd interaction is destructive without
   `PERISCOPE_NO_RECLAIM=1`.** Running a bare `uv run server.py` on port
   8765 while launchd-prod is up triggers a respawn loop, not a
   one-time race: (a) manual instance reclaims (SIGTERMs) launchd's
   worker, (b) launchd respawns within `ThrottleInterval`, (c) new
   launchd worker reclaims the manual instance, (d) manual instance
   exits, (e) launchd's new worker may briefly fail to bind :8765
   because the socket is still in `TIME_WAIT`, prompting another
   respawn. Verify the escape hatch works:
   `PERISCOPE_NO_RECLAIM=1 PERISCOPE_PORT=8766 uv run server.py` runs a
   dev instance cleanly, never touches the prod pidfile, and exits
   without leaking pidfiles. The worktree workflow doc spells out that
   manual collisions on 8765 should not happen casually.

7. **Reclaim cross-port safety.** Manually overwrite
   `~/.config/periscope/periscope-8766.pid` with a number that points
   at the running prod worker's pid (i.e., forge a stale dev pidfile
   that points at prod). Start a dev periscope. The port-aware reclaim
   reads the recorded port in the pidfile, sees it doesn't match 8766,
   logs a warning, and refuses to SIGTERM. Prod stays up.

## Known weak points (accepted)

- **Healthz `version`** — captured from `git rev-parse --short HEAD` at
  module load, wrapped in `try/except` with a `"unknown"` fallback (so
  `git` not on PATH or a non-git working tree doesn't crash boot). The
  cached value goes stale after `git pull`; that's the intended
  behavior — `bin/periscope restart` is the explicit step that
  refreshes it, and that confirms the restart actually picked up new
  code.

- **`state.json` concurrent writes.** `_write_state` uses
  `path.with_suffix(path.suffix + ".tmp")` as a fixed temp filename
  (`state.json.tmp`), then `os.replace`. The `os.replace` is atomic at
  the filesystem level, but two processes can race on the *temp file
  itself* — process A's bytes get overwritten by process B mid-write,
  and the published file is whichever lost the race. For Tom's actual
  usage (one user, infrequent writes, dev rarely running) this is fine,
  but the design's safety claim is weaker than full cross-process
  serialization. Real fix when/if it matters: `tempfile.mkstemp(dir=...,
  prefix='state-', suffix='.tmp')` so each writer has a unique temp
  name. Deferred.

- **`bin/periscope tail` hardcodes port 8765.** Add `bin/periscope
  tail-dev` if it turns out to be a regular need.

- **`vite.config.js` proxies to :8765.** `npm run dev` (vite on :5174)
  proxies `/api` and `/ws` to prod regardless of `PERISCOPE_PORT`. The
  dev-on-:8766 workflow accesses the dev backend *directly* at
  `http://127.0.0.1:8766/` (not through vite), so this isn't a
  blocker — vite is only useful when you specifically want frontend
  HMR against the dev backend. If that combination becomes the common
  case, parameterize vite's proxy target. Out of scope for this spec.
