"""Session and window CRUD endpoints.

POST /api/session/new
DELETE /api/session
POST /api/window/new            (incl. mode=resume)
POST /api/window/move
DELETE /api/window

window/new's resume mode looks up the original project_path via the
history index, then spawns `claude --resume <id>` in that directory.
The `resumes` sentinel session is auto-created on first use.
"""

import os
import time

from fastapi import APIRouter, Query
from pydantic import BaseModel

from periscope.panes import (
    _acted_at, _active_per_session, _focused_at, _resuming,
    note_action, note_focus,
)
from periscope.tmux import _run, _tmux_mutate, tmux

router = APIRouter()


class NewSessionBody(BaseModel):
    name: str
    cwd: str | None = None


@router.post("/api/session/new")
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


@router.delete("/api/session")
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


@router.post("/api/window/new")
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


@router.post("/api/window/move")
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


@router.delete("/api/window")
def window_delete(session: str, index: int):
    target = f"{session}:{index}"
    ok, msg = _tmux_mutate("kill-window", "-t", target)
    if not ok:
        return {"ok": False, "error": msg}
    _focused_at.pop(target, None)
    _acted_at.pop(target, None)
    return {"ok": True, "target": target}
