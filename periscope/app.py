"""FastAPI app construction + lifespan + static mount.

Imported by uvicorn via the `periscope.app:app` import string (set in
server.py's __main__ block). server.py itself never imports from this
module — keeping that boundary clean prevents the double-import landmine
documented in the design spec.
"""

import asyncio
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from periscope.channels import _mcp_listener
from periscope.config import MCP_SOCKET_PATH, STATIC
from periscope.git_pr import prewarm_pr_cache
from periscope.lgtm import _LGTM_SSE_TASKS, _lgtm_periodic_refresh
from periscope.log import log, _bg, _task
from periscope.usage import cached_scraped_usage, kill_orphan_usage_sessions

# Routes — each module owns an APIRouter that we mount into `app` below.
from periscope.routes import (
    alerts, auto_rename, channel, healthz, history, pane, paste_image, prefs,
    send, sessions, state, ws,
)
from periscope.routes import lgtm as lgtm_route
from periscope.routes import projects as projects_routes
from periscope.routes import cleanup as cleanup_routes
from periscope.routes import settings as settings_routes


@asynccontextmanager
async def lifespan(_app: FastAPI):
    from periscope import config
    log.info("periscope starting (pid=%d, port=%d)", os.getpid(), config.PORT)
    # Reap any periscope-usage-* tmux sessions left behind by a prior
    # crash before the new scrape thread spawns a fresh one.
    kill_orphan_usage_sessions()
    # Kick off cache prewarms eagerly so the first /api/state poll already
    # has PR badges and the usage bars populated.
    _bg("prewarm-pr", prewarm_pr_cache)
    _bg("prewarm-usage", cached_scraped_usage)
    # MCP unix-socket listener bound only by the :8765 (prod) instance.
    # channel_shim.py hardcodes /tmp/periscope-mcp.sock, so Claude's
    # channels always talk to prod. Dev periscopes on other ports leave
    # the socket alone — see spec §"Dev never serves channels."
    if config.PORT == 8765:
        mcp_task = _task("mcp-listener", _mcp_listener())
    else:
        mcp_task = None
        log.info("dev port %d: skipping MCP listener", config.PORT)
    # LGTM mirror: polls localhost:9900 + subscribes per-session SSE.
    # No-op while LGTM isn't running; surfaces on the dashboard the
    # moment it comes up.
    lgtm_task = _task("lgtm-refresh", _lgtm_periodic_refresh())
    try:
        yield
    finally:
        log.info("periscope shutting down (pid=%d)", os.getpid())
        if mcp_task is not None:
            mcp_task.cancel()
        lgtm_task.cancel()
        for t in list(_LGTM_SSE_TASKS.values()):
            t.cancel()
        if mcp_task is not None:
            try:
                await mcp_task
            except (asyncio.CancelledError, Exception):
                pass
            # Lifespan owns socket cleanup; periscope.channels never
            # unlinks MCP_SOCKET_PATH (see spec §"MCP_SOCKET_PATH cleanup").
            try:
                os.unlink(MCP_SOCKET_PATH)
            except FileNotFoundError:
                pass


app = FastAPI(lifespan=lifespan)

# Route mounts. Order doesn't matter functionally — FastAPI dispatches
# by path — but we mount them before the static catch-all below so
# `/api/*` and `/ws/*` paths take precedence over `StaticFiles`.
for r in (
    alerts, auto_rename, channel, cleanup_routes, healthz, history, lgtm_route,
    pane, paste_image, prefs, projects_routes, send, sessions, settings_routes,
    state, ws,
):
    app.include_router(r.router)

# Mounted last so the API/WS routes above take precedence. `html=True`
# serves index.html for `/` (and any directory request) without needing
# a separate route. Asset paths in index.html are root-relative so they
# resolve identically here and under Vite's dev server on :5174.
app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
