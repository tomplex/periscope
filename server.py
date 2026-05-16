# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "fastapi",
#     "uvicorn[standard]",
#     "anthropic",
#     "httpx",
#     "python-dotenv",
#     "mcp==1.27.*",
# ]
# ///
"""Periscope — live tmux dashboard. Run with: uv run server.py"""

import asyncio
import atexit
import json
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from periscope.config import STATIC, MCP_SOCKET_PATH
from periscope.log import log, _bg, _task
from periscope.pidfile import (
    _reclaim_existing_instance,
    _write_pidfile,
    _remove_pidfile,
)
from periscope.tmux import (
    tmux, capture, deliver_input, _run, _tmux_mutate,
    _ANSI_SGR_RE, _FG_COLOR_RE,
)
from periscope.store import (
    _STATE, _STATE_LOCK, _write_state, _state_path,
    _seed_commands_if_empty, _channels_migration_v1, _load_state,
)
from periscope.lgtm import (
    LGTM_BASE_URL, _LGTM_LOCK, _LGTM_BY_REPO, _LGTM_SSE_TASKS,
    cached_lgtm_state, _lgtm_submitted, _lgtm_refresh_all,
    _lgtm_periodic_refresh,
)
from periscope.channels import (
    _CHANNELS_LOCK, _CHANNEL_REPLIES, _CHANNEL_UNREAD, _MCP_SESSIONS,
    _channel_gc, _mcp_listener,
)
from periscope.panes import (
    _focused_at, _acted_at, _completed_at, _prev_state, _active_per_session,
    _resuming, RESUME_EXPIRY_S,
    smooth_spinner, smooth_is_claude,
    note_focus, note_action, update_focus_from_windows,
    list_windows, parse_pane,
)
from periscope.pids import _attach_git_then_resolve_pids
from periscope.git_pr import (
    cached_git_state, cached_pr_state, cached_pane_activity, prewarm_pr_cache,
)
from periscope.usage import (
    cached_claude_usage, cached_scraped_usage, kill_orphan_usage_sessions,
)
from periscope.rename_ai import claude_complete, build_rename_prompt

# Load .env from the script's directory (existing env vars take precedence).
load_dotenv(Path(__file__).parent / ".env")


# Logging + background-task wrappers now live in periscope/log.py.


# Pidfile / single-instance reclaim now lives in periscope/pidfile.py.


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # prewarm_pr_cache, cached_scraped_usage, and kill_orphan_usage_sessions
    # are defined later; Python resolves the names at call-time, so forward
    # references are fine.
    log.info("periscope starting (pid=%d)", os.getpid())
    # Reap any periscope-usage-* tmux sessions left behind by a prior crash
    # before the new scrape thread spawns a fresh one.
    kill_orphan_usage_sessions()
    # Kick off cache prewarms eagerly so the first /api/state poll already
    # has PR badges and the usage bars populated.
    _bg("prewarm-pr", prewarm_pr_cache)
    _bg("prewarm-usage", cached_scraped_usage)
    # MCP unix-socket listener: accepts connections from channel_shim.py
    # (one per Claude pane), runs an MCP Server per connection in-process.
    mcp_task = _task(_mcp_listener(), "mcp-listener")
    # LGTM mirror: polls localhost:9900 + subscribes per-session SSE.
    # No-op while LGTM isn't running; surfaces on the dashboard the
    # moment it comes up.
    lgtm_task = _task(_lgtm_periodic_refresh(), "lgtm-refresh")
    try:
        yield
    finally:
        log.info("periscope shutting down (pid=%d)", os.getpid())
        mcp_task.cancel()
        lgtm_task.cancel()
        for t in list(_LGTM_SSE_TASKS.values()):
            t.cancel()
        try:
            await mcp_task
        except (asyncio.CancelledError, Exception):
            pass
        try:
            os.unlink(MCP_SOCKET_PATH)
        except FileNotFoundError:
            pass


app = FastAPI(lifespan=lifespan)

# Persistent state (state.json) now lives in periscope/store.py.


# Channels code now lives in periscope/channels.py.

# Panes code (focus tracking + smoothing + list_windows + parse_pane + regexes)
# now lives in periscope/panes.py.


# LGTM integration helpers now live in periscope/lgtm.py.
# (The /api/lgtm/start route stays in server.py until Peel 8.)


# Git + PR state + activity timeline now live in periscope/git_pr.py.
# Claude usage tracking (JSONL + TUI scrape) now lives in periscope/usage.py.


# list_windows now lives in periscope/panes.py.

# Periscope window-ids (@periscope_id) now live in periscope/pids.py.


# parse_pane and the pids block now live in periscope/panes.py and
# periscope/pids.py respectively.


@app.get("/api/state")
def state():
    windows = list_windows()
    update_focus_from_windows(windows)
    _attach_git_then_resolve_pids(windows)
    now_ts = int(time.time())
    # Accumulate (pid, completed_at, acked_at) tuples for stamp persistence
    # at the end of the loop. Single lock acquisition + single write covers
    # every pane in this poll.
    stamp_updates: list[tuple[str, int, int]] = []
    result = []
    for w in windows:
        target = f"{w['session']}:{w['index']}"
        pid = w.get("pid") or ""
        try:
            content = capture(target)
            parsed = parse_pane(content)
        except Exception as e:
            parsed = {"error": str(e), "state": "error", "is_claude": False}
        # Hysteresis: smooth out per-poll detection gaps so cards / modal
        # subtitles don't flicker between "thinking" and idle.
        parsed["spinner"] = smooth_spinner(target, parsed.get("spinner"))
        # is_claude stickiness: dialogs hide the bottom status line; without
        # this the card would flip to "shell" mid-prompt and lose its state
        # coloring + needs-input classification.
        parsed["is_claude"] = smooth_is_claude(target, parsed.get("is_claude", False))
        if not parsed["is_claude"]:
            parsed["state"] = "shell"
        # Spinner hysteresis can promote a momentarily-blank parse back to
        # "working" — but only if we're not already in a louder state.
        # needs-input must never be downgraded back to working: the dialog
        # commonly lingers below a stale spinner glyph in scrollback.
        if (
            parsed.get("is_claude")
            and parsed.get("spinner")
            and parsed.get("state") not in ("working", "needs-input")
        ):
            parsed["state"] = "working"

        # done-vs-idle refinement. Uses per-pid stamps (persisted via
        # state.json) so a server restart preserves the "Claude finished
        # something you haven't looked at" signal across the gap.
        #
        # Edge detection: if the previous parse was busy and now we're idle,
        # stamp `_completed_at` so the refinement below promotes us to
        # "done" until the user acknowledges via a periscope action.
        # Targets without a pid (rare — only if pid resolution failed)
        # skip persistence; the in-memory value still works for the
        # current process lifetime.
        prev = _prev_state.get(pid) if pid else None
        cur = parsed.get("state")
        if pid and prev in ("working", "needs-input") and cur == "idle":
            _completed_at[target] = now_ts
        if pid:
            _prev_state[pid] = cur

        # Pull persisted stamps; in-memory may be ahead (just bumped) or
        # behind (fresh process, never observed a transition this run).
        wblock = _STATE.get("windows", {})
        persisted = wblock.get(pid, {}) if pid else {}
        completed = max(_completed_at.get(target, 0), int(persisted.get("completed_at") or 0))
        acked = max(_acted_at.get(target, 0), int(persisted.get("acked_at") or 0))

        if cur == "idle" and parsed.get("is_claude") and completed > acked:
            parsed["state"] = "done"

        # Schedule a state.json write if either stamp is newer than what's
        # on disk. The write itself runs once, under the lock, after the
        # loop.
        if pid and (
            completed > int(persisted.get("completed_at") or 0)
            or acked > int(persisted.get("acked_at") or 0)
        ):
            stamp_updates.append((pid, completed, acked))

        git = cached_git_state(w.get("cwd", "")) or {}
        pr = cached_pr_state(w.get("cwd", ""), git.get("branch")) or {}
        lgtm = cached_lgtm_state(w.get("cwd", ""))

        # Channel state (added by 2026-05-14-channels-design.md).
        pane_id = w.get("pane_id") or ""
        with _CHANNELS_LOCK:
            channel_attached = pane_id in _MCP_SESSIONS if pane_id else False
            channel_unread = _CHANNEL_UNREAD.get(pane_id, 0) if pane_id else 0
            channel_replies = list(_CHANNEL_REPLIES.get(pane_id, [])) if pane_id else []

        # Persisted Claude-driven links (via the link_pr / link_linear MCP
        # tools). `linked_pr` overrides the auto-detected `pr` field — when
        # Claude has explicitly told us "this pane is for PR #N", we trust
        # that over heuristic title-bar parsing.
        linked_pr = persisted.get("linked_pr")
        linked_linear = persisted.get("linked_linear")
        if linked_pr:
            pr = dict(pr)
            pr["pr"] = str(linked_pr)
            pr["pr_linked"] = True
            # `ci` (CI glyph) is keyed to the auto-detected PR; an explicit
            # linked PR may not have a fresh CI signal until a future poll
            # resolves it. Drop the stale glyph rather than mislead.
            pr.pop("ci", None)

        result.append(
            {
                **w, **parsed, **git, **pr,
                "target": target,
                "focused_at": _focused_at.get(target, 0),
                # 0 means "never engaged through periscope" — stream view
                # filters these out; grid view sorts cards within each session
                # by acted_at desc (most-recently-opened leftmost).
                "acted_at": acked,
                "completed_at": completed,
                "channel_attached": channel_attached,
                "channel_unread": channel_unread,
                "channel_replies": channel_replies,
                "linked_linear": linked_linear,
                "lgtm": lgtm,
            }
        )
    _channel_gc({w["pane_id"] for w in windows if w.get("pane_id")})
    if stamp_updates:
        with _STATE_LOCK:
            wblock = _STATE.setdefault("windows", {})
            dirty = False
            for pid, completed, acked in stamp_updates:
                entry = wblock.setdefault(pid, {})
                if int(entry.get("completed_at") or 0) != completed:
                    entry["completed_at"] = completed
                    dirty = True
                if int(entry.get("acked_at") or 0) != acked:
                    entry["acked_at"] = acked
                    dirty = True
            if dirty:
                _write_state(_STATE)
    # Garbage-collect stale resumes: targets that are no longer in tmux's
    # list-windows output, or older than 30 min.
    now = int(time.time())
    live_targets = {f"{w['session']}:{w['index']}" for w in windows}
    for sid in list(_resuming):
        entry = _resuming[sid]
        if entry["target"] not in live_targets or now - entry["started_at"] > RESUME_EXPIRY_S:
            del _resuming[sid]
    return {
        "windows": result,
        "ts": int(time.time()),
        "usage": cached_claude_usage(),
        "usage_scrape": cached_scraped_usage(),
    }



# --- /api/prefs endpoints -------------------------------------------------
# All /api/prefs/* (+ UIPatch / WindowAnnotation / Command / CommandPatch /
# CommandsReorder bodies) now live in periscope/routes/prefs.py.
#
# /api/pane + /api/rename (incl. RenameBody) now live in periscope/routes/pane.py.
# SendBody / SendBulkBody now live in periscope/routes/send.py.
# NewSessionBody now lives in periscope/routes/sessions.py.


# /api/session/* + /api/window/* now live in periscope/routes/sessions.py.


# --- history index API ----------------------------------------------------


# /api/history/* + /history page now live in periscope/routes/history.py.


# Anthropic-SDK helpers for auto-rename now live in periscope/rename_ai.py.


# /api/auto-rename-{session,window} now live in periscope/routes/auto_rename.py.


# _send_to_target + /api/send + /api/send-bulk now live in periscope/routes/send.py.


# /api/channel/clear-unread now lives in periscope/routes/channel.py.


# /api/lgtm/start now lives in periscope/routes/lgtm.py.


# /api/paste-image now lives in periscope/routes/paste_image.py.


# --- Live terminal: WebSocket bridge to a tmux pane ----------------------
#
# Architecture:
#   - tmux pipe-pane -O writes the pane's output stream to a named pipe
#   - we read from the FIFO and forward bytes to the WebSocket
#   - we receive keystroke messages from the WebSocket and pass them through
#     to tmux send-keys -l (literal) so escape sequences (arrow keys, etc.)
#     reach the pane's PTY untouched
#   - on disconnect we stop the pipe-pane and remove the FIFO
#
# pipe-pane duplicates the output, so the user's actual tmux terminal keeps
# rendering normally alongside the browser-side terminal.


# WS /ws/pane now lives in periscope/routes/ws.py.


# prewarm_pr_cache now lives in periscope/git_pr.py.


# Route modules (Peel 8): each one defines an APIRouter that's wired into
# `app` here. Kept above `app.mount("/")` so route paths take precedence
# over the static-files catch-all.
from periscope.routes import auto_rename as _auto_rename_route
from periscope.routes import channel as _channel_route
from periscope.routes import history as _history_route
from periscope.routes import lgtm as _lgtm_route
from periscope.routes import pane as _pane_route
from periscope.routes import paste_image as _paste_image_route
from periscope.routes import prefs as _prefs_route
from periscope.routes import send as _send_route
from periscope.routes import sessions as _sessions_route
from periscope.routes import ws as _ws_route
app.include_router(_auto_rename_route.router)
app.include_router(_channel_route.router)
app.include_router(_history_route.router)
app.include_router(_lgtm_route.router)
app.include_router(_pane_route.router)
app.include_router(_paste_image_route.router)
app.include_router(_prefs_route.router)
app.include_router(_send_route.router)
app.include_router(_sessions_route.router)
app.include_router(_ws_route.router)

# Mounted last so the API/WS routes above take precedence. `html=True` serves
# index.html for `/` (and any directory request) without needing a separate
# route. Asset paths in index.html are root-relative (`/styles.css`, `/app.js`,
# `/vendor/xterm.js`) so they resolve identically here and under Vite's dev
# server on :5174.
app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")


if __name__ == "__main__":
    import uvicorn

    # Reclaim any prior periscope before binding the port. Done here (not in
    # lifespan) because uvicorn binds the socket before lifespan runs — by
    # the time the worker starts up, a port collision has already failed.
    _reclaim_existing_instance()
    _write_pidfile()
    atexit.register(_remove_pidfile)
    # SIGTERM otherwise bypasses atexit; install a handler that logs and
    # exits cleanly so atexit fires and the next start is idempotent.
    def _on_sigterm(signum, _frame):
        log.info("received signal %d; exiting", signum)
        sys.exit(0)
    signal.signal(signal.SIGTERM, _on_sigterm)

    # loop="asyncio" forces the stdlib selector loop instead of uvloop. As of
    # uvloop 0.22.1 + CPython 3.14, uvloop captures `asyncio.iscoroutinefunction`
    # at import time and calls it from `run_in_executor`, which now emits a
    # DeprecationWarning per call (loud during WS resize traffic). Revert this
    # when uvloop ships a 3.14-compatible release.
    #
    # reload=True watches server.py for changes and restarts the worker. It's
    # gated on PERISCOPE_DEV=1 because the reload supervisor adds a second
    # process to the tree (worker + supervisor + multiprocessing helpers),
    # which makes the server hard to kill cleanly and produces orphans when
    # signals don't propagate. dev.sh sets PERISCOPE_DEV=1; bare
    # `uv run server.py` runs as a single process. Needs an import string
    # (not the `app` object) when reload is on so the reloader can re-import
    # the module. reload_dirs is scoped to this file's parent so edits under
    # static/ don't bounce the server — Vite handles frontend reloads in dev,
    # and direct browser hits pick up new static files without a restart.
    dev_mode = os.environ.get("PERISCOPE_DEV") == "1"
    uvicorn.run(
        "server:app",
        host="127.0.0.1",
        port=8765,
        log_level="info",
        loop="asyncio",
        reload=dev_mode,
        reload_dirs=[str(Path(__file__).parent)] if dev_mode else None,
    )
