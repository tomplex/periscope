"""Session and window CRUD endpoints.

DELETE /api/session
POST /api/session/rename
POST /api/window/new            (incl. mode=resume)
POST /api/window/move
DELETE /api/window

window/new's resume mode looks up the original project_path via the
history index, then spawns `claude --resume <id>` in that directory.
The `resumes` sentinel session is auto-created on first use.
"""

import os
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from periscope.channels import dismiss_dev_channels_consent_bg
from periscope.config import CLAUDE_EXEC
from periscope.panes import (
    _acted_at,
    _active_per_session,
    _focused_at,
    _resuming,
    drop_target_focus,
    list_windows,
    note_action,
    note_focus,
)
from periscope.projects import (
    MAIN_KEY,
    get_project,
    placement_kill_set,
    resolve_project_for_window,
)
from periscope.tmux import _run, _tmux_mutate, tmux
from periscope.worktree_spawn import spawn_worktree

router = APIRouter()


class RenameSessionBody(BaseModel):
    name: str


@router.post("/api/session/rename")
def session_rename(session: str, body: RenameSessionBody):
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "empty name")
    if name == session:
        return {"ok": True, "session": name}
    ok, msg = _tmux_mutate("rename-session", "-t", session, name)
    if not ok:
        raise HTTPException(500, msg)
    # Rebind focus/acted timestamps from the old name to the new one so the
    # session's history doesn't get orphaned by the rename.
    old_prefix = f"{session}:"
    new_prefix = f"{name}:"
    for store in (_focused_at, _acted_at):
        for old_t in list(store.keys()):
            if old_t.startswith(old_prefix):
                store[new_prefix + old_t[len(old_prefix):]] = store.pop(old_t)
    if session in _active_per_session:
        _active_per_session[name] = _active_per_session.pop(session)
    return {"ok": True, "session": name}


@router.delete("/api/session")
def session_delete(session: str):
    # Close the worktree row = kill the panes whose rail placement is this
    # project, NOT the whole tmux session. A pane dragged into a workspace has
    # a different placement, so it survives (see the metadata-anchored-rail
    # spec). Contract narrowed: only managed worktree sessions are closable —
    # the sole UI caller is closeWorktree on worktree rows; dev/MAIN_KEY rows
    # use closePane instead.
    project_key = resolve_project_for_window({"session": session})
    if not project_key or project_key == MAIN_KEY:
        raise HTTPException(400, f"session {session!r} is not a closable worktree")
    windows = [w for w in list_windows() if w.get("session") == session]
    kill = placement_kill_set(project_key, windows)
    # Kill by the STABLE pane_id (%N), never the session:index target — with
    # tmux `renumber-windows on`, killing one window renumbers the rest, so
    # index targets captured up front go stale mid-loop and land on the wrong
    # pane (this killed a workspace-tagged pane we'd excluded). pane_id is
    # renumber-immune. `target` is only for the focus-dict key.
    for target, pane_id in kill:
        ok, msg = _tmux_mutate("kill-pane", "-t", pane_id)
        if not ok:
            raise HTTPException(500, msg)
        drop_target_focus(target)
    # Drop the session's active-tracking only once it is actually empty — a
    # surviving workspace-tagged pane keeps the session (and its stamp) alive.
    if not [w for w in list_windows() if w.get("session") == session]:
        _active_per_session.pop(session, None)
    return {"ok": True, "session": session, "killed": [t for t, _ in kill]}


def _send_and_stamp(target: str, cmd: str) -> None:
    """Optionally send `cmd` into `target` — after a 100ms shell-rc settle
    so the command lands as a real prompt entry, not mid-rc echoed text
    (CLAUDE.md "Key invariants" note 5) — then stamp focus + action so the
    window sorts to the top on the next poll. Shared by every window-spawn
    endpoint.

    Claude launched with --dangerously-load-development-channels shows a
    consent dialog that blocks keyboard input until dismissed. Fire a
    background thread to auto-confirm option 1 so the user doesn't have
    to hit Enter before they can type."""
    if cmd:
        time.sleep(0.1)
        tmux("send-keys", "-t", target, cmd, "Enter")
        if "--dangerously-load-development-channels" in cmd:
            dismiss_dev_channels_consent_bg(target)
    note_focus(target)
    note_action(target)


def _window_new_resume(session: str, exec_cmd: str, resume_id: str | None, mode: str) -> dict:
    """`mode=resume`: look up the original session's project dir via the
    history index and run `claude --resume <id>` there. The sentinel
    `session` is auto-created on first use. Returns the standard
    window-spawn result dict; raises HTTPException on any guard failure.
    """
    if not resume_id:
        raise HTTPException(400, "resume_id required for mode=resume")
    from history.search import get_session
    resume_sess = get_session(resume_id)
    if resume_sess is None:
        raise HTTPException(404, f"unknown session_id: {resume_id}")
    # Liveness guard: refuse if the jsonl was written to in the last 60s
    # (the session may be currently active in another window/process, and
    # two concurrent appenders would interleave into the same JSONL).
    if resume_sess["jsonl_path"] and os.path.isfile(resume_sess["jsonl_path"]):
        mtime_age = time.time() - os.path.getmtime(resume_sess["jsonl_path"])
        if mtime_age < 60:
            raise HTTPException(409, "session looks live; wait a minute or pick another")
    # Already resumed elsewhere in this periscope process?
    if resume_id in _resuming:
        existing = _resuming[resume_id]
        raise HTTPException(409, f"already resumed in {existing['target']}")
    cwd = resume_sess["project_path"] or os.path.expanduser("~")
    if not os.path.isdir(cwd):
        cwd = os.path.expanduser("~")

    # Resume convention: the frontend always sends `session=resumes` (or
    # any sentinel). If that session doesn't exist yet, create it on first
    # use so the resume button doesn't bounce.
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
            raise HTTPException(500, f"failed to create session '{session}': {msg}")
        try:
            index = int(msg)
        except ValueError:
            raise HTTPException(500, f"tmux returned unexpected index: {msg!r}") from None
        target = f"{session}:{index}"
        _send_and_stamp(target, f"{CLAUDE_EXEC} --resume {resume_id}")
        _resuming[resume_id] = {"target": target, "started_at": int(time.time())}
        return {
            "ok": True,
            "session": session,
            "index": index,
            "target": target,
            "mode": mode,
            "resumed_session_id": resume_id,
        }

    # Session exists — spawn a new window into it.
    ok, msg = _tmux_mutate(
        "new-window", "-t", f"{session}:", "-c", cwd,
        "-P", "-F", "#{window_index}",
    )
    if not ok:
        raise HTTPException(500, msg)
    try:
        index = int(msg)
    except ValueError:
        raise HTTPException(500, f"tmux returned unexpected index: {msg!r}") from None
    target = f"{session}:{index}"

    cmd = exec_cmd.strip()
    _send_and_stamp(target, cmd)
    if resume_id not in _resuming:
        _resuming[resume_id] = {"target": target, "started_at": int(time.time())}
    return {
        "ok": True,
        "session": session,
        "index": index,
        "target": target,
        "mode": mode,
        "exec": cmd,
        "resumed_session_id": resume_id,
    }


def _window_new_plain(session: str, exec_cmd: str, mode: str) -> dict:
    """Non-resume window spawn: resolve cwd, open a new window in `session`,
    optionally run `exec_cmd`."""
    # Project pin always wins over active-pane cwd. If the target session
    # is owned by a non-archived non-main project, new tabs land in the
    # project's pinned_dir — even if the user has cd'd away in the active
    # pane. MAIN_KEY (dev, incl. folded unmanaged sessions) lands in ~/dev;
    # pane-cwd inheritance survives only for archived-project sessions.
    project_key = resolve_project_for_window({"session": session})
    project = get_project(project_key) if project_key else {}
    if project_key == MAIN_KEY:
        cwd = os.path.expanduser("~/dev")
    elif project_key and not project.get("archived_at"):
        cwd = project_key  # the projects dict's key IS the pinned_dir path
        # (see _lookup_key in periscope/projects.py for the realpath normalization).
    else:
        cwd = tmux(
            "display-message", "-t", f"{session}:", "-p", "#{pane_current_path}",
        ).strip() or os.path.expanduser("~")

    # Dev's "+ New tab" can target __main__'s tmux_session while it doesn't
    # exist (dev can be populated purely by folded ad-hoc sessions). Create
    # it instead of letting `new-window -t` error. Gated on MAIN_KEY so a
    # typo'd session= on any other call still errors instead of silently
    # minting a session. Same -P -F rationale as _window_new_resume:
    # base-index 1 makes hardcoded :0 targets no-op.
    code, _ = _run(["tmux", "has-session", "-t", session])
    if code != 0 and project_key == MAIN_KEY:
        ok, msg = _tmux_mutate(
            "new-session", "-d", "-s", session, "-c", cwd,
            "-P", "-F", "#{window_index}",
        )
        if not ok:
            raise HTTPException(500, f"failed to create session '{session}': {msg}")
    else:
        ok, msg = _tmux_mutate(
            "new-window", "-t", f"{session}:", "-c", cwd,
            "-P", "-F", "#{window_index}",
        )
        if not ok:
            raise HTTPException(500, msg)
    try:
        index = int(msg)
    except ValueError:
        raise HTTPException(500, f"tmux returned unexpected index: {msg!r}") from None
    target = f"{session}:{index}"

    cmd = exec_cmd.strip()
    _send_and_stamp(target, cmd)
    return {"ok": True, "session": session, "index": index, "target": target, "mode": mode, "exec": cmd}


@router.post("/api/window/new")
def window_new(session: str, exec_cmd: str = Query("", alias="exec"), mode: str = "shell", resume_id: str | None = None):
    """Spawn a window in `session`. `exec` param sends a command to the new
    window; legacy `mode` maps to `exec` for backwards-compat. `mode=resume`
    runs `claude --resume <resume_id>` in the original session's project
    dir. cwd is inherited from the session's active pane — without `-c`,
    tmux would use the periscope server's cwd, which is never what you
    want."""
    # Legacy `mode` → exec_cmd mapping for callers still on the old
    # contract. `mode=resume` synthesizes the command from resume_id.
    if not exec_cmd:
        if mode in ("claude", "vim", "shell"):
            exec_cmd = {"claude": CLAUDE_EXEC, "vim": "vim", "shell": ""}.get(mode, "")
        elif mode == "resume" and resume_id:
            exec_cmd = f"{CLAUDE_EXEC} --resume {resume_id}"

    if mode == "resume":
        return _window_new_resume(session, exec_cmd, resume_id, mode)
    return _window_new_plain(session, exec_cmd, mode)


@router.post("/api/window/new-worktree")
def window_new_worktree(
    session: str,
    branch: str,
    exec_cmd: str = Query(CLAUDE_EXEC, alias="exec"),
):
    """Spawn a new worktree-tab in `session`'s owning project.

    Forks a sub-worktree off the project's `base_branch` (local ref,
    no fetch), opens a new tmux window in it, and optionally runs
    `exec_cmd` (defaults to `claude` — matches trellis's `t` hotkey).

    Body shape mirrors `/api/window/new`: session + exec are query
    params; `branch` is the new sub-branch name. Slugging for the
    on-disk worktree path is handled by `spawn_worktree`.

    Errors:
      400 — session not owned by a project, or branch invalid, or
            worktree-add failed.
      404 — session doesn't exist in tmux.
      409 — sub-worktree path or branch already exists.
    """
    branch = branch.strip()
    if not branch:
        raise HTTPException(400, "branch is required")
    if branch.startswith("-"):
        raise HTTPException(400, f"branch name cannot start with '-': {branch!r}")

    # Confirm the tmux session exists. The `has-session` invariant
    # mirrors the phase-2 create endpoint's pre-check.
    code, _ = _run(["tmux", "has-session", "-t", session])
    if code != 0:
        raise HTTPException(404, f"tmux session {session!r} not found")

    # Resolve project. The session must be owned by a non-main project
    # (the worktree-tab verb doesn't apply to __main__ — there's no
    # base_branch to fork from).
    project_key = resolve_project_for_window({"session": session})
    if not project_key or project_key == MAIN_KEY:
        raise HTTPException(
            400,
            f"worktree-tab requires a session owned by a pinned project; "
            f"{session!r} is unmanaged or main",
        )
    project = get_project(project_key)
    if not project.get("repo"):
        raise HTTPException(
            400, f"project at {project_key!r} has no repo recorded"
        )

    repo = project["repo"]
    assert repo is not None  # guarded by the `not project.get("repo")` 400 above
    base_branch = project.get("base_branch")
    # Two paths based on base_branch presence:
    #   - base_branch set (typical): fork from LOCAL ref (no fetch).
    #     The user's unpushed work on the project's branch is included.
    #   - base_branch null (legacy projects): fall back to repo default
    #     branch with fetch=True (defaults are pushed, fetch is safe).
    # base_branch set → fork from local ref (fetch=False); null → detected default + fetch=True.
    spawn_kwargs: dict[str, Any] = {"base_branch": base_branch, "fetch": False} if base_branch else {}

    try:
        res = spawn_worktree(repo, branch, **spawn_kwargs)
    except ValueError as e:
        # spawn_worktree raises ValueError in these cases (see
        # periscope/worktree_spawn.py):
        #   - "branch name cannot start with '-'"        → 400 (caught above too)
        #   - "not a git repo: <path>"                    → 400
        #   - "worktree path already exists: <path>"      → 409
        #   - "git worktree add failed: <git stderr>"     → 409 if stderr
        #     contains "already exists" (branch collision from git), else 400
        # The "already exists" substring catches both the path-collision
        # path AND the branch-collision-from-git path. Other failures
        # (network, disk full, etc.) fall through to 400.
        msg = str(e)
        status = 409 if "already exists" in msg else 400
        raise HTTPException(status, msg) from e
    wt_path = res["path"]
    warning = res.get("warning")

    # Spawn the new window in the project's tmux session, rooted at the
    # new worktree's path. -P -F captures the freshly-created window's
    # index so we know the target for note_focus / send-keys.
    ok, msg = _tmux_mutate(
        "new-window", "-t", f"{session}:",
        "-c", wt_path,
        "-P", "-F", "#{window_index}",
    )
    if not ok:
        # The worktree is on disk but the window failed. Leave the
        # worktree — the user can try again or `tmux new-window` manually.
        raise HTTPException(500, f"tmux new-window failed: {msg}")
    try:
        index = int(msg)
    except ValueError:
        raise HTTPException(500, f"tmux returned unexpected index: {msg!r}") from None
    target = f"{session}:{index}"

    cmd = exec_cmd.strip()
    _send_and_stamp(target, cmd)

    result = {
        "ok": True,
        "session": session,
        "index": index,
        "target": target,
        "worktree_path": wt_path,
        "branch": branch,
        "base_branch": res["base_branch"],
        "exec": cmd,
    }
    if warning:
        result["warning"] = warning
    return result


@router.post("/api/window/move")
def window_move(session: str, index: int, dest: str):
    """Move a window into another session via tmux move-window. The new
    index is whatever slot dest had free; tmux's move-window doesn't print
    it, so we capture the source's stable #{window_id} (e.g. `@42`) up
    front and look up its post-move index by id."""
    src = f"{session}:{index}"
    if not dest or dest == session:
        raise HTTPException(400, "destination missing or same as source")
    win_id = tmux("display-message", "-t", src, "-p", "#{window_id}").strip()
    if not win_id.startswith("@"):
        raise HTTPException(404, f"unknown source window: {src!r}")
    code, _ = _run(["tmux", "has-session", "-t", dest])
    if code != 0:
        raise HTTPException(404, f"unknown destination session: {dest!r}")
    ok, msg = _tmux_mutate("move-window", "-d", "-s", src, "-t", f"{dest}:")
    if not ok:
        raise HTTPException(500, msg)
    out = tmux("list-windows", "-t", dest, "-F", "#{window_id} #{window_index}")
    new_index = None
    for line in out.splitlines():
        wid, _, idx = line.partition(" ")
        if wid == win_id and idx.isdigit():
            new_index = int(idx)
            break
    if new_index is None:
        raise HTTPException(500, f"could not locate moved window {win_id}")
    new_target = f"{dest}:{new_index}"
    # Carry focus / acted bookkeeping over to the new target so the moved
    # window keeps its sort position instead of dropping to the bottom.
    if src in _focused_at:
        _focused_at[new_target] = _focused_at[src]
    if src in _acted_at:
        _acted_at[new_target] = _acted_at[src]
    drop_target_focus(src)
    return {"ok": True, "src": src, "dest": dest, "index": new_index, "target": new_target}


@router.delete("/api/window")
def window_delete(session: str, index: int):
    target = f"{session}:{index}"
    ok, msg = _tmux_mutate("kill-window", "-t", target)
    if not ok:
        raise HTTPException(500, msg)
    drop_target_focus(target)
    return {"ok": True, "target": target}
