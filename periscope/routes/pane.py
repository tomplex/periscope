"""GET /api/pane (single-pane detail) and POST /api/rename (rename window).

Pane returns the captured screen + every derived field the modal needs
(git state, PR badge, activity timeline, channel reply queue, LGTM
mirror). Mostly an aggregator over already-cached subsystems.
"""

import os

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from periscope.channels import channel_state_for
from periscope.git_pr import cached_git_state, cached_pr_state
from periscope.activity import cached_pane_activity
from periscope.lgtm import cached_lgtm_state
from periscope.panes import (
    list_windows, note_action, parse_pane, smooth_is_claude, smooth_spinner,
)
from periscope.pids import _attach_git_then_resolve_pids
from periscope.store import get_window
from periscope.tabs import activate_tab, close_tab, open_tab
from periscope.tmux import pane_meta, tmux
from periscope.turns import get_turns_for_pane

router = APIRouter()


class RenameBody(BaseModel):
    name: str


class TabOpenBody(BaseModel):
    pid: str
    path: str
    line: int | None = None


class TabCloseBody(BaseModel):
    pid: str
    path: str


class TabActivateBody(BaseModel):
    pid: str
    tab: str


@router.get("/api/pane")
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
        window_name, cwd = pane_meta(target)
    except Exception:
        window_name, cwd = "", ""
    git = cached_git_state(cwd) or {}
    one = [{"session": session, "index": index, "name": window_name, "active": False, "cwd": cwd, "pid_raw": ""}]
    _attach_git_then_resolve_pids(one)
    pid = one[0].get("pid")
    pr = cached_pr_state(cwd, git.get("branch")) or {}
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
    activity = cached_pane_activity(target, pane_id, cwd, git.get("branch"))
    channel = channel_state_for(pane_id)
    # Persisted links — same override semantics as /api/state.
    persisted = get_window(str(pid) if pid else "")
    linked_pr = persisted.get("linked_pr")
    linked_linear = persisted.get("linked_linear")
    linked_linear_title = persisted.get("linked_linear_title")
    linked_linear_status = persisted.get("linked_linear_status")
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
        "channel_attached": channel["attached"],
        "channel_unread": channel["unread"],
        "linked_linear": linked_linear,
        "linked_linear_title": linked_linear_title,
        "linked_linear_status": linked_linear_status,
        "lgtm": lgtm,
        **parsed,
        **git,
        **pr,
    }


@router.get("/api/pane/turns")
def pane_turns(session: str, index: int):
    """Structured turn transcript for a Claude pane. Full message list per call
    (the client reconciles by uuid). Resolves the pane's SPECIFIC Claude session
    (not just newest-in-cwd — many panes share a cwd). Returns {turns: null} when
    the pane has no live transcript. Session/index are query params so
    slash-bearing session names don't collide with path routing (invariant 6)."""
    out = get_turns_for_pane(session, index)
    return out if out is not None else {"turns": None}


# Tab mutations are granular (open/close/activate) rather than a whole-state
# PUT so a browser action and an MCP open_document landing in the same poll
# window can't clobber each other — the server merges per-operation.
@router.post("/api/pane/tabs/open")
def pane_tabs_open(body: TabOpenBody):
    if not body.pid.strip() or not body.path.strip():
        raise HTTPException(400, "pid and path must be non-empty")
    return {"ok": True, **open_tab(body.pid, body.path, body.line)}


@router.post("/api/pane/tabs/close")
def pane_tabs_close(body: TabCloseBody):
    if not body.pid.strip() or not body.path.strip():
        raise HTTPException(400, "pid and path must be non-empty")
    return {"ok": True, **close_tab(body.pid, body.path)}


@router.post("/api/pane/tabs/activate")
def pane_tabs_activate(body: TabActivateBody):
    if not body.pid.strip() or not body.tab.strip():
        raise HTTPException(400, "pid and tab must be non-empty")
    return {"ok": True, **activate_tab(body.pid, body.tab)}


@router.post("/api/rename")
def rename(session: str, index: int, body: RenameBody):
    target = f"{session}:{index}"
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "empty name")
    tmux("rename-window", "-t", target, name)
    note_action(target)
    return {"ok": True, "target": target, "name": name}
