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

@app.get("/api/prefs")
def get_prefs():
    """Full state blob, for client boot. Reads from the in-memory cache —
    every mutation refreshes the cache atomically, so this is safe to call
    without the lock."""
    return _STATE

class UIPatch(BaseModel):
    session_order: list[str] | None = None
    collapsed_sessions: list[str] | None = None
    view: str | None = None  # "grid" or "stream"


@app.patch("/api/prefs/ui")
async def patch_prefs_ui(body: UIPatch):
    """Merge partial UI prefs. Only fields present in the body get written."""
    patch = body.model_dump(exclude_none=True)
    # `view` is validated against a fixed enum to keep junk out of the file.
    if "view" in patch and patch["view"] not in ("grid", "stream"):
        return {"ok": False, "error": f"invalid view: {patch['view']!r}"}
    with _STATE_LOCK:
        _STATE["ui"].update(patch)
        _write_state(_STATE)
    return {"ok": True, "ui": _STATE["ui"]}


class WindowAnnotation(BaseModel):
    notes: str | None = None
    tags: list[str] | None = None


@app.put("/api/prefs/windows/{pid}")
async def put_window_annotation(pid: str, body: WindowAnnotation):
    """Set/replace the annotation fields on a window. `last_seen` is left
    intact — only notes/tags are managed via this endpoint."""
    if not pid or not pid.isalnum():
        return {"ok": False, "error": "invalid pid"}
    patch = body.model_dump(exclude_none=True)
    # Coerce tags to a trimmed unique list, preserving order.
    if "tags" in patch:
        seen: set[str] = set()
        clean: list[str] = []
        for t in patch["tags"]:
            t = (t or "").strip()
            if t and t not in seen:
                seen.add(t)
                clean.append(t)
        patch["tags"] = clean
    with _STATE_LOCK:
        entry = _STATE["windows"].setdefault(pid, {})
        for k in ("notes", "tags"):
            if k in patch:
                entry[k] = patch[k]
        # Drop empty notes / empty tag list to keep the file tidy.
        if entry.get("notes") == "":
            entry.pop("notes", None)
        if entry.get("tags") == []:
            entry.pop("tags", None)
        _write_state(_STATE)
    return {"ok": True, "pid": pid, "annotation": {
        "notes": entry.get("notes"),
        "tags": entry.get("tags") or [],
    }}


@app.delete("/api/prefs/windows/{pid}")
async def delete_window_annotation(pid: str):
    """Remove notes + tags. last_seen is preserved (it's the rebind hint)."""
    if not pid or not pid.isalnum():
        return {"ok": False, "error": "invalid pid"}
    with _STATE_LOCK:
        entry = _STATE["windows"].get(pid)
        if entry:
            entry.pop("notes", None)
            entry.pop("tags", None)
            _write_state(_STATE)
    return {"ok": True, "pid": pid}

class Command(BaseModel):
    label: str
    exec: str = ""


class CommandPatch(BaseModel):
    """For PUT: both fields are optional. Sending only `label` renames
    without clobbering `exec`; sending only `exec` updates the command
    without renaming. The frontend always sends both, but keeping them
    optional protects against curl-from-shell footguns."""
    label: str | None = None
    exec: str | None = None


@app.post("/api/prefs/commands")
async def add_command(body: Command):
    label = body.label.strip()
    if not label:
        return {"ok": False, "error": "empty label"}
    with _STATE_LOCK:
        if any(c["label"] == label for c in _STATE["commands"]):
            return {"ok": False, "error": f"duplicate label: {label!r}"}
        _STATE["commands"].append({"label": label, "exec": body.exec or ""})
        _write_state(_STATE)
    return {"ok": True, "commands": _STATE["commands"]}


@app.put("/api/prefs/commands/{label}")
async def update_command(label: str, body: CommandPatch):
    with _STATE_LOCK:
        for c in _STATE["commands"]:
            if c["label"] == label:
                new_label = (body.label or label).strip()
                if not new_label:
                    return {"ok": False, "error": "empty label"}
                if new_label != label and any(
                    other["label"] == new_label for other in _STATE["commands"] if other is not c
                ):
                    return {"ok": False, "error": f"duplicate label: {new_label!r}"}
                c["label"] = new_label
                if body.exec is not None:
                    c["exec"] = body.exec
                _write_state(_STATE)
                return {"ok": True, "commands": _STATE["commands"]}
    return {"ok": False, "error": f"unknown label: {label!r}"}


@app.delete("/api/prefs/commands/{label}")
async def delete_command(label: str):
    with _STATE_LOCK:
        before = len(_STATE["commands"])
        _STATE["commands"] = [c for c in _STATE["commands"] if c["label"] != label]
        if len(_STATE["commands"]) == before:
            return {"ok": False, "error": f"unknown label: {label!r}"}
        _write_state(_STATE)
    return {"ok": True, "commands": _STATE["commands"]}


class CommandsReorder(BaseModel):
    labels: list[str]


@app.put("/api/prefs/commands")
async def reorder_commands(body: CommandsReorder):
    """Reorder the commands list to match `labels`. Unknown labels are
    ignored; missing labels stay in place at the end."""
    with _STATE_LOCK:
        by_label = {c["label"]: c for c in _STATE["commands"]}
        ordered = [by_label[l] for l in body.labels if l in by_label]
        leftover = [c for c in _STATE["commands"] if c["label"] not in {l for l in body.labels if l in by_label}]
        _STATE["commands"] = ordered + leftover
        _write_state(_STATE)
    return {"ok": True, "commands": _STATE["commands"]}


@app.get("/api/pane")
def pane(session: str, index: int, lines: int = 200):
    """Capture last N lines of a pane plus parsed status fields. Session/index
    passed as query params so slash-bearing session names (e.g. 'tc/foo/bar')
    don't conflict with path routing."""
    target = f"{session}:{index}"
    # -e preserves ANSI escape sequences for the modal viewer. parse_pane
    # handles the colored content itself — it strips for content parsing
    # but uses the raw prompt-line color info to filter ghost-text input.
    content = tmux("capture-pane", "-t", target, "-p", "-e", "-S", f"-{lines}")
    parsed = parse_pane(content)
    parsed["spinner"] = smooth_spinner(target, parsed.get("spinner"))
    parsed["is_claude"] = smooth_is_claude(target, parsed.get("is_claude", False))
    if not parsed["is_claude"]:
        parsed["state"] = "shell"
    if parsed.get("is_claude") and parsed.get("spinner") and parsed.get("state") not in ("working", "needs-input"):
        parsed["state"] = "working"
    try:
        meta = tmux(
            "display-message", "-t", target, "-p",
            "#{window_name}\t#{pane_current_path}",
        ).strip()
        window_name, _, cwd = meta.partition("\t")
    except Exception:
        window_name, cwd = "", ""
    git = cached_git_state(cwd) or {}
    one = [{"session": session, "index": index, "name": window_name, "active": False, "cwd": cwd, "pid_raw": ""}]
    _attach_git_then_resolve_pids(one)
    pid = one[0].get("pid")
    pr = cached_pr_state(cwd, git.get("branch")) or {}
    activity = cached_pane_activity(target, cwd, git.get("branch"))
    # Shorten $HOME → ~ for display. Done server-side because the browser
    # doesn't know the user's home dir.
    home = os.path.expanduser("~")
    cwd_display = cwd
    if cwd and (cwd == home or cwd.startswith(home + "/")):
        cwd_display = "~" + cwd[len(home):]
    # Channel data: look up pane_id via list_windows since the route doesn't
    # take it directly. Single iteration is fine — list_windows is cached at
    # tmux's speed (sub-ms) and we already pay it on every state() poll.
    pane_id = ""
    for w in list_windows():
        if w["session"] == session and w["index"] == index:
            pane_id = w.get("pane_id", "")
            break
    with _CHANNELS_LOCK:
        channel_attached = pane_id in _MCP_SESSIONS if pane_id else False
        channel_unread = _CHANNEL_UNREAD.get(pane_id, 0) if pane_id else 0
        channel_replies = list(_CHANNEL_REPLIES.get(pane_id, [])) if pane_id else []
    # Persisted links — same override semantics as /api/state.
    persisted = _STATE.get("windows", {}).get(pid or "", {})
    linked_pr = persisted.get("linked_pr")
    linked_linear = persisted.get("linked_linear")
    if linked_pr:
        pr = dict(pr)
        pr["pr"] = str(linked_pr)
        pr["pr_linked"] = True
        pr.pop("ci", None)
    lgtm = cached_lgtm_state(cwd)
    return {
        "content": content,
        "target": target,
        "name": window_name,
        "cwd": cwd_display,
        "cwd_raw": cwd,
        "session": session,
        "pid": pid,
        "pane_id": pane_id,
        "activity": activity,
        "channel_attached": channel_attached,
        "channel_unread": channel_unread,
        "channel_replies": channel_replies,
        "linked_linear": linked_linear,
        "lgtm": lgtm,
        **parsed,
        **git,
        **pr,
    }


class SendBody(BaseModel):
    keys: list[str] = []
    paste: str | None = None  # bracketed-pasted into the pane before `keys`


class SendBulkBody(BaseModel):
    targets: list[str]            # ["session:index", ...]
    keys: list[str] = []
    paste: str | None = None


class RenameBody(BaseModel):
    name: str


class NewSessionBody(BaseModel):
    name: str
    cwd: str | None = None


@app.post("/api/rename")
def rename(session: str, index: int, body: RenameBody):
    target = f"{session}:{index}"
    name = body.name.strip()
    if not name:
        return {"ok": False, "error": "empty name"}
    tmux("rename-window", "-t", target, name)
    note_action(target)
    return {"ok": True, "target": target, "name": name}


@app.post("/api/session/new")
def session_new(body: NewSessionBody):
    name = body.name.strip()
    if not name:
        return {"ok": False, "error": "empty name"}
    cwd = body.cwd or os.path.expanduser("~")
    ok, msg = _tmux_mutate("new-session", "-d", "-s", name, "-c", cwd)
    if not ok:
        return {"ok": False, "error": msg}
    # Stamp focus so the new session sorts to the top on next poll. Stamping
    # `acted_at` too: creating a session through periscope is a user action,
    # so the new window earns a slot in the stream view.
    note_focus(f"{name}:0")
    note_action(f"{name}:0")
    return {"ok": True, "session": name}


@app.delete("/api/session")
def session_delete(session: str):
    ok, msg = _tmux_mutate("kill-session", "-t", session)
    if not ok:
        return {"ok": False, "error": msg}
    prefix = f"{session}:"
    for t in [t for t in _focused_at if t.startswith(prefix)]:
        _focused_at.pop(t, None)
    for t in [t for t in _acted_at if t.startswith(prefix)]:
        _acted_at.pop(t, None)
    _active_per_session.pop(session, None)
    return {"ok": True, "session": session}


@app.post("/api/window/new")
def window_new(session: str, exec_cmd: str = Query("", alias="exec"), mode: str = "shell", resume_id: str | None = None):
    """Spawn a window in `session`. `exec` param sends a command to the new window;
    legacy `mode` maps to `exec` for backwards-compat. `mode=resume` runs
    `claude --resume <resume_id>` in the original session's project dir.
    cwd is inherited from the session's active pane — without `-c`,
    tmux would use the periscope server's cwd, which is never what you want."""
    # Legacy `mode` → exec_cmd mapping for callers still on the old contract.
    # `mode=resume` synthesizes the actual command from resume_id; the
    # _resuming registration happens after the spawn (below) so the existing-
    # session fall-through path doesn't lose either side-effect.
    if not exec_cmd:
        if mode in ("claude", "vim", "shell"):
            exec_cmd = {"claude": "claude", "vim": "vim", "shell": ""}.get(mode, "")
        elif mode == "resume" and resume_id:
            exec_cmd = f"claude --resume {resume_id}"
    
    # mode=resume looks up the original project_path and runs claude --resume
    # there; we resolve cwd up front so the rest of the spawn path is shared.
    resume_sess = None
    if mode == "resume":
        if not resume_id:
            return {"ok": False, "error": "resume_id required for mode=resume"}
        from history.search import get_session
        resume_sess = get_session(resume_id)
        if resume_sess is None:
            return {"ok": False, "error": f"unknown session_id: {resume_id}"}
        # Liveness guard: refuse if the jsonl was written to in the last 60s
        # (the session may be currently active in another window/process,
        # and two concurrent appenders would interleave into the same JSONL).
        if resume_sess["jsonl_path"] and os.path.isfile(resume_sess["jsonl_path"]):
            mtime_age = time.time() - os.path.getmtime(resume_sess["jsonl_path"])
            if mtime_age < 60:
                return {"ok": False, "error": "session looks live; wait a minute or pick another"}
        # Already resumed elsewhere in this periscope process?
        if resume_id in _resuming:
            existing = _resuming[resume_id]
            return {"ok": False,
                    "error": f"already resumed in {existing['target']}",
                    "existing_target": existing["target"]}
        cwd = resume_sess["project_path"] or os.path.expanduser("~")
        if not os.path.isdir(cwd):
            cwd = os.path.expanduser("~")
        # Resume convention: the frontend always sends `session=resumes`
        # (or any sentinel). If that session doesn't exist yet, create it
        # on first use so the resume button doesn't bounce. Side-effect-
        # only when actually missing; existing sessions pass through.
        code, _ = _run(["tmux", "has-session", "-t", session])
        if code != 0:
            # `-P -F #{window_index}` is essential: with `base-index 1` in
            # tmux.conf the first window isn't 0, and a hardcoded `:0` target
            # makes the follow-up send-keys silently no-op (tmux() discards
            # stderr) — the user sees the session appear but claude never
            # launches.
            ok, msg = _tmux_mutate(
                "new-session", "-d", "-s", session, "-c", cwd,
                "-P", "-F", "#{window_index}",
            )
            if not ok:
                return {"ok": False, "error": f"failed to create session '{session}': {msg}"}
            try:
                index = int(msg)
            except ValueError:
                return {"ok": False, "error": f"tmux returned unexpected index: {msg!r}"}
            target = f"{session}:{index}"
            time.sleep(0.1)
            tmux("send-keys", "-t", target, f"claude --resume {resume_id}", "Enter")
            _resuming[resume_id] = {"target": target, "started_at": int(time.time())}
            note_focus(target)
            note_action(target)
            return {
                "ok": True,
                "session": session,
                "index": index,
                "target": target,
                "mode": mode,
                "resumed_session_id": resume_id,
            }
    else:
        cwd = tmux(
            "display-message", "-t", f"{session}:", "-p", "#{pane_current_path}",
        ).strip() or os.path.expanduser("~")
    ok, msg = _tmux_mutate(
        "new-window", "-t", f"{session}:", "-c", cwd,
        "-P", "-F", "#{window_index}",
    )
    if not ok:
        return {"ok": False, "error": msg}
    try:
        index = int(msg)
    except ValueError:
        return {"ok": False, "error": f"tmux returned unexpected index: {msg!r}"}
    target = f"{session}:{index}"
    
    # Execute the command if provided via exec or legacy mode mapping.
    cmd = exec_cmd.strip()
    if cmd:
        # Let the shell finish its rc before the command line arrives, so
        # the command runs as a real prompt entry rather than mid-rc
        # echoed text. (See CLAUDE.md "Key invariants" note 5.)
        time.sleep(0.1)
        tmux("send-keys", "-t", target, cmd, "Enter")

    # Resume bookkeeping for the fall-through path (existing `resumes`
    # session). The new-session branch above already set this inline before
    # its early return.
    if mode == "resume" and resume_id and resume_id not in _resuming:
        _resuming[resume_id] = {"target": target, "started_at": int(time.time())}

    note_focus(target)
    note_action(target)
    result = {"ok": True, "session": session, "index": index, "target": target, "mode": mode, "exec": cmd}
    if mode == "resume":
        result["resumed_session_id"] = resume_id
    return result


@app.post("/api/window/move")
def window_move(session: str, index: int, dest: str):
    """Move a window into another session via tmux move-window. The new
    index is whatever slot dest had free; tmux's move-window doesn't print
    it, so we capture the source's stable #{window_id} (e.g. `@42`) up
    front and look up its post-move index by id."""
    src = f"{session}:{index}"
    if not dest or dest == session:
        return {"ok": False, "error": "destination missing or same as source"}
    win_id = tmux("display-message", "-t", src, "-p", "#{window_id}").strip()
    if not win_id.startswith("@"):
        return {"ok": False, "error": f"unknown source window: {src!r}"}
    code, _ = _run(["tmux", "has-session", "-t", dest])
    if code != 0:
        return {"ok": False, "error": f"unknown destination session: {dest!r}"}
    ok, msg = _tmux_mutate("move-window", "-d", "-s", src, "-t", f"{dest}:")
    if not ok:
        return {"ok": False, "error": msg}
    out = tmux("list-windows", "-t", dest, "-F", "#{window_id} #{window_index}")
    new_index = None
    for line in out.splitlines():
        wid, _, idx = line.partition(" ")
        if wid == win_id and idx.isdigit():
            new_index = int(idx)
            break
    if new_index is None:
        return {"ok": False, "error": f"could not locate moved window {win_id}"}
    new_target = f"{dest}:{new_index}"
    # Carry focus / acted bookkeeping over to the new target so the moved
    # window keeps its sort position instead of dropping to the bottom.
    if src in _focused_at:
        _focused_at[new_target] = _focused_at.pop(src)
    if src in _acted_at:
        _acted_at[new_target] = _acted_at.pop(src)
    return {"ok": True, "src": src, "dest": dest, "index": new_index, "target": new_target}


@app.delete("/api/window")
def window_delete(session: str, index: int):
    target = f"{session}:{index}"
    ok, msg = _tmux_mutate("kill-window", "-t", target)
    if not ok:
        return {"ok": False, "error": msg}
    _focused_at.pop(target, None)
    _acted_at.pop(target, None)
    return {"ok": True, "target": target}


# --- history index API ----------------------------------------------------


# /api/history/* + /history page now live in periscope/routes/history.py.


# Anthropic-SDK helpers for auto-rename now live in periscope/rename_ai.py.


# /api/auto-rename-{session,window} now live in periscope/routes/auto_rename.py.


def _send_to_target(target: str, paste: str | None, keys: list[str]) -> dict:
    """Core paste-buffer + send-keys logic. Used by `/api/send` and the bulk
    variant; both bump focus + acted_at on the target. Returns a result dict
    suitable for inclusion in the endpoint response."""
    if not keys and (paste is None or paste == ""):
        return {"target": target, "ok": False, "error": "no keys or paste"}
    try:
        if paste is not None and paste != "":
            # Unique buffer name so concurrent calls (including bulk fan-out)
            # never trample each other.
            import uuid
            buf = f"wd-{uuid.uuid4().hex[:8]}"
            tmux("set-buffer", "-b", buf, paste)
            tmux("paste-buffer", "-d", "-p", "-b", buf, "-t", target)
            # Give the receiving TUI (especially Claude Code) time to apply
            # state for the paste before the submit key arrives. Without this,
            # Enter can land before React renders and submits empty input,
            # leaving the pasted text visibly stranded in the prompt area.
            if keys:
                time.sleep(0.10)
        if keys:
            tmux("send-keys", "-t", target, *keys)
    except subprocess.CalledProcessError as e:
        return {"target": target, "ok": False, "error": (e.stderr or str(e)).strip()}
    except Exception as e:
        return {"target": target, "ok": False, "error": str(e)}
    note_focus(target)
    note_action(target)
    return {"target": target, "ok": True}


@app.post("/api/send")
def send(session: str, index: int, body: SendBody):
    """Send input to a tmux pane.

    `paste`, if set, is sent first via tmux's bracketed-paste mechanism — this
    is the only reliable way to deliver multi-line text, since tmux send-keys
    silently strips embedded newlines.

    `keys` is then sent via send-keys. Each item is either a tmux key name
    (Enter, Escape, C-c, S-Tab, Up, F1, …) or a literal string.
    """
    target = f"{session}:{index}"
    result = _send_to_target(target, body.paste, body.keys)
    if not result["ok"]:
        return result
    return {"ok": True, "target": target}


@app.post("/api/send-bulk")
def send_bulk(body: SendBulkBody):
    """Fan out the same paste/keys to multiple panes concurrently.

    Each target is processed in its own thread so the per-pane 100ms
    bracketed-paste delay overlaps across panes — broadcasting `/reload-plugins`
    to 30 claudes finishes in ~100ms wall time instead of 3s sequential.

    Buffer-name collisions are avoided by `_send_to_target` minting a fresh
    uuid'd buf per call.
    """
    if not body.targets:
        return {"ok": False, "error": "no targets"}
    if not body.keys and (body.paste is None or body.paste == ""):
        return {"ok": False, "error": "no keys or paste"}
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=min(32, len(body.targets))) as pool:
        results = list(
            pool.map(
                lambda t: _send_to_target(t, body.paste, body.keys),
                body.targets,
            )
        )
    ok_count = sum(1 for r in results if r["ok"])
    return {"ok": True, "sent": ok_count, "total": len(results), "results": results}


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
from periscope.routes import paste_image as _paste_image_route
from periscope.routes import ws as _ws_route
app.include_router(_auto_rename_route.router)
app.include_router(_channel_route.router)
app.include_router(_history_route.router)
app.include_router(_lgtm_route.router)
app.include_router(_paste_image_route.router)
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
