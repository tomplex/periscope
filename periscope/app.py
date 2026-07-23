"""FastAPI app construction + lifespan + static mount.

Imported by uvicorn via the `periscope.app:app` import string (set in
server.py's __main__ block). server.py itself never imports from this
module — keeping that boundary clean prevents the double-import landmine
documented in the design spec.
"""

import asyncio
import os
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from periscope.channels import _mcp_listener
from periscope.config import MCP_SOCKET_PATH, STATIC
from periscope.git_pr import prewarm_pr_cache
from periscope.lgtm import _LGTM_SSE_TASKS, _lgtm_periodic_refresh
from periscope.log import _bg, _task, log

# Routes — each module owns an APIRouter that we mount into `app` below.
from periscope.routes import (
    alerts,
    auto_rename,
    channel,
    command,
    events,
    fs,
    healthz,
    history,
    pane,
    paste_image,
    prefs,
    send,
    sessions,
    state,
    tracks,
    ws,
)
from periscope.routes import cleanup as cleanup_routes
from periscope.routes import lgtm as lgtm_route
from periscope.routes import open as open_route
from periscope.routes import projects as projects_routes
from periscope.routes import settings as settings_routes
from periscope.routes import workspaces as workspaces_routes
from periscope.usage import cached_plan_usage


@asynccontextmanager
async def lifespan(_app: FastAPI):
    from periscope import config
    log.info("periscope starting (pid=%d, port=%d)", os.getpid(), config.PORT)
    # Bound periscope.db growth — drop events older than 30 days, import the
    # legacy pane_sessions/ directory if present, then drop pane_sessions rows
    # whose tmux pane is gone.
    from periscope import activity
    from periscope.panes import list_windows
    _bg("activity-prune", activity.prune)
    _bg("ui-events-prune", activity.prune_ui_events)
    _bg("usage-samples-prune", activity.prune_usage_samples)

    def _pane_sessions_housekeeping() -> None:
        imported = activity.migrate_legacy_pane_sessions()
        if imported:
            log.info("migrated %d legacy pane_sessions row(s) into periscope.db",
                     imported)
        alive = {w["pane_id"] for w in list_windows() if w.get("pane_id")}
        dropped = activity.prune_pane_sessions(alive)
        if dropped:
            log.info("pruned %d dead pane_sessions row(s)", dropped)
        dropped_status = activity.prune_pane_status(alive)
        if dropped_status:
            log.info("pruned %d dead pane_status row(s)", dropped_status)
        dropped_ws = activity.prune_pane_workspaces(alive)
        if dropped_ws:
            log.info("pruned %d dead pane_workspaces row(s)", dropped_ws)
        dropped_tracks = activity.prune_pane_tracks(alive)
        if dropped_tracks:
            log.info("pruned %d dead pane_tracks row(s)", dropped_tracks)

    _bg("pane-sessions-housekeeping", _pane_sessions_housekeeping)
    # Kick off cache prewarms eagerly so the first /api/state poll already
    # has PR badges and the usage bars populated.
    _bg("prewarm-pr", prewarm_pr_cache)
    _bg("prewarm-usage", cached_plan_usage)
    # MCP unix-socket listener bound only by the :8765 (prod) instance.
    # channel_shim.py hardcodes /tmp/periscope-mcp.sock, so Claude's
    # channels always talk to prod. Dev periscopes on other ports leave
    # the socket alone — see spec §"Dev never serves channels."
    if config.is_prod():
        mcp_task = _task("mcp-listener", _mcp_listener())
    else:
        mcp_task = None
        log.info("non-prod (port %d, dev=%s): skipping MCP listener",
                 config.PORT, config.DEV)
    # State hub: one central compute loop that pushes /api/state over /ws/state
    # to all subscribers. Demand-gated (idle until a client connects), so it's
    # safe to run in dev too — no Claude spend, just the same tmux/git fan-out
    # the REST endpoint already does.
    from periscope import state_hub
    state_task = _task("state-hub", state_hub.run())
    # LGTM mirror: polls localhost:9900 + subscribes per-session SSE.
    # No-op while LGTM isn't running; surfaces on the dashboard the
    # moment it comes up.
    lgtm_task = _task("lgtm-refresh", _lgtm_periodic_refresh())
    # Activity worker: context-reset detection + narrator (semantic status
    # + auto-rename). Prod only — the narrator spends Haiku per pane, and
    # periscope.db is a single shared file. Gated on IS_PROD (not bare
    # PORT==8765) because dev.sh historically ran on the default 8765 and
    # spent Haiku on every narrator tick. Same guard as the MCP listener.
    # NB: _task's signature is _task(name, coro).
    if config.is_prod():
        from periscope import activity
        activity_task = _task("activity-worker", activity.run_worker())
        # Generate the static bg-commander MCP config + orchestrator prompt once
        # at boot (prod-only — dispatch needs the MCP socket + subscription auth).
        from periscope import bg_commander
        try:
            bg_commander.write_mcp_config()
        except Exception:
            log.warning("bg_commander.write_mcp_config failed", exc_info=True)
    else:
        activity_task = None
    # One-shot single-session consolidation: physically move every managed
    # window into MANAGED_SESSION, then seed tracks. Self-gated (is_prod +
    # persisted flag), so call it unconditionally. Synchronous + pre-serve so
    # windows are consolidated before any /ws/pane connects. A failure here
    # must never crash boot — degraded just means windows stay where they are,
    # and the bridge is pane_id-keyed so terminals keep working.
    from periscope import migrate_single_session
    try:
        migrate_single_session.run_if_needed()
    except Exception:
        log.warning("single-session migration failed; windows left in place",
                    exc_info=True)
    # Synchronous (pre-serve) seed: tag every managed pane with its resolved
    # track so the rail groups by track_id from the first poll. Idempotent —
    # skips already-tagged panes. NOT _bg: grouping is now track-only (no
    # session-match fallback), so this must complete before serving.
    from periscope import tracks
    try:
        folded = tracks.migrate_workspaces_to_tracks()
        if folded:
            log.info("folded %d pane(s) from workspaces into goal tracks", folded)
        seeded = tracks.seed_tracks(list_windows())
        if seeded:
            log.info("seeded %d pane_tracks row(s)", seeded)
    except Exception:
        log.warning("track seed failed; panes resolve to repo-default lazily",
                    exc_info=True)
    try:
        yield
    finally:
        log.info("periscope shutting down (pid=%d)", os.getpid())
        from periscope import tmux_input, tmux_mirror
        await tmux_input.shutdown()
        await tmux_mirror.shutdown()   # control clients must not leak
                                       # across dev reloads
        if mcp_task is not None:
            mcp_task.cancel()
        state_task.cancel()
        lgtm_task.cancel()
        if activity_task is not None:
            activity_task.cancel()
        for t in list(_LGTM_SSE_TASKS.values()):
            t.cancel()
        if mcp_task is not None:
            with suppress(asyncio.CancelledError, Exception):
                await mcp_task
            # Lifespan owns socket cleanup; periscope.channels never
            # unlinks MCP_SOCKET_PATH (see spec §"MCP_SOCKET_PATH cleanup").
            with suppress(FileNotFoundError):
                os.unlink(MCP_SOCKET_PATH)


app = FastAPI(lifespan=lifespan)

# Route mounts. Order doesn't matter functionally — FastAPI dispatches
# by path — but we mount them before the static catch-all below so
# `/api/*` and `/ws/*` paths take precedence over `StaticFiles`.
for r in (
    alerts, auto_rename, channel, command, events, cleanup_routes, fs, healthz, history, lgtm_route,
    open_route, pane, paste_image, prefs, projects_routes, workspaces_routes, send, sessions,
    settings_routes, state, tracks, ws,
):
    app.include_router(r.router)

# Serve static with `Cache-Control: no-cache` so the browser always
# revalidates against the ETag StaticFiles already sends — a cheap 304 when
# unchanged, a fresh 200 when the file changed. The committed frontend bundle
# has a STABLE filename (`/dist/app.js`), so without this a `bin/periscope
# restart` (or any rebuild) would serve a stale cached bundle until a manual
# hard-refresh — in the browser AND the Tauri/WKWebView shell. no-cache means
# "revalidate", not "don't store", so localhost overhead is negligible.
class _RevalidateStaticFiles(StaticFiles):
    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


# Mounted last so the API/WS routes above take precedence. `html=True`
# serves index.html for `/` (and any directory request) without needing
# a separate route. Asset paths in index.html are root-relative so they
# resolve identically here and under Vite's dev server on :5174.
app.mount("/", _RevalidateStaticFiles(directory=STATIC, html=True), name="static")
