"""Session and window CRUD endpoints.

POST /api/session/rename
POST /api/window/new            (incl. mode=resume)
POST /api/window/move
POST /api/pane/move-account
DELETE /api/window

window/new's resume mode reads the start directory from Claude's transcript,
then spawns `claude --resume <id>` in that directory.
The `resumes` sentinel session is auto-created on first use.

/api/pane/move-account lives here rather than in routes/pane.py despite the
path: it is a window SPAWN (it drives `_window_new_resume` plus the account
`-e` plumbing that only exists in this module), while routes/pane.py is a
read-mostly aggregator. Homing it there would add a routes→routes import edge
for no gain.
"""

import itertools
import json
import os
import shlex
import sqlite3
import time
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from periscope import (
    activity,
    codex_sessions,
    config,
    idle_close,
    open_ops,
    session_binding_db,
    store,
    tracks,
    usage,
)
from periscope import tmux as tmux_mod
from periscope.channels import _resolve_window_by_pid, dismiss_dev_channels_consent_bg
from periscope.config import CLAUDE_EXEC, MANAGED_SESSION
from periscope.narrator import is_placeholder_name
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
from periscope.pids import stamp_new_window, stamped_pids
from periscope.tmux import _run, _tmux_mutate, tmux
from periscope.turns import jsonl_for_session, resume_refusal, session_id_for_pane
from periscope.worktree_spawn import spawn_worktree

router = APIRouter()

# Sentinel tmux session every resumed pane lands in. `_window_new_resume`
# creates it on first use; the rail never shows it, because membership is
# track-anchored and every resumed pane is re-tagged into a real track.
RESUME_SESSION = "resumes"


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
    for stamps in (_focused_at, _acted_at):
        for old_t in list(stamps.keys()):
            if old_t.startswith(old_prefix):
                stamps[new_prefix + old_t[len(old_prefix) :]] = stamps.pop(old_t)
    if session in _active_per_session:
        _active_per_session[name] = _active_per_session.pop(session)
    return {"ok": True, "session": name}


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


def _start_cwd(transcript: Path) -> str:
    """The first `cwd` in Claude's transcript, or "" when it has none — the
    same rule history.extract._decode_project_path uses. It has to be the START
    cwd, not wherever the pane has since cd'd to: `claude --resume` resolves
    the id against the cwd-encoded project dir, and that encoding is fixed at
    session start."""
    try:
        with open(transcript, encoding="utf-8", errors="replace") as fh:
            # Bounded: cwd rides on the first real event, and an unbounded scan
            # of a transcript with no cwd at all would read a multi-MB file.
            for line in itertools.islice(fh, 50):
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict) and rec.get("cwd"):
                    return str(rec["cwd"])
    except OSError:
        pass
    return ""


def _finish_resume(record: activity.ClosedPane | None, result: dict) -> dict:
    """The spawn result; with a closed record, its pid, and the record is gone."""
    if record is None:
        return result
    activity.delete_closed_pane(record["session_id"])
    return {**result, "pid": record["pid"]}


def _window_new_resume(
    session: str,
    exec_cmd: str,
    resume_id: str | None,
    mode: str,
    account: str | None = None,
    force: bool = False,
    moving: bool = False,
) -> dict:
    """`mode=resume`: run `claude --resume <id>` in the directory the session
    started in. The sentinel `session` is auto-created on first use. Returns
    the standard window-spawn result dict; raises HTTPException with
    `turns.resume_refusal`'s answer when it refuses.

    Claude's transcript is the only source, never the history index: Claude
    resumes from its own file, so an indexed session whose transcript Claude
    deleted cannot resume, and a LIVE session (what move-account resumes) has
    no history row until SessionEnd.

    `account` binds the pane to a Claude subscription via `-e
    CLAUDE_CONFIG_DIR=…` on the tmux window (see `tmux.env_args`) — process
    env, not a command-string prefix, so a user who exits and re-runs `claude`
    by hand in that pane stays on the same account.

    A session whose pane idle close closed has a record: the new window takes
    back its pid, name and wrapper profile, and the result has that `pid`.

    `force` skips ONLY the transcript-mtime liveness guard. Claude writes the
    limit-reached message into the JSONL, so a walled pane — the one the user
    most wants to move off an exhausted subscription — always looks live to
    that guard. The already-resumed-elsewhere guard is never skipped: two
    concurrent appenders interleaving into one JSONL is a different failure.

    `moving` (move-account) skips ONLY the check that a live Claude runs the
    session: the pane it moves still does.
    """
    if not resume_id:
        raise HTTPException(400, "resume_id required for mode=resume")
    config_dir = store.account_config_dir(account)
    transcript = jsonl_for_session(resume_id)
    if refusal := resume_refusal(resume_id, transcript, force=force, moving=moving):
        raise HTTPException(*refusal)
    assert transcript is not None  # resume_refusal refuses a missing transcript
    cwd = _start_cwd(transcript)
    if not os.path.isdir(cwd):
        cwd = os.path.expanduser("~")

    record = activity.get_closed_pane(resume_id)
    if record and idle_close.came_back(record, stamped_pids(list_windows())):
        # A second window with the record's pid would take its PR, ticket and name pin.
        activity.delete_closed_pane(resume_id)
        record = None
    name_args: list[str] = []
    stamp_args: list[str] = []
    profile = None
    if record:
        # Read before the spawn: the next poll writes the new window's name over it.
        name = (store.get_window(record["pid"]).get("last_seen") or {}).get("name")
        if name and not is_placeholder_name(name):
            name_args = ["-n", name]
        # In the spawn's own tmux command: a poll that lists the window unstamped
        # mints an id for it, and the PR, ticket and track stay on the old one.
        stamp_args = [";", "set-option", "-w", "@periscope_id", record["pid"]]
        profile = record["profile"]

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
            "new-session",
            "-d",
            "-s",
            session,
            "-c",
            cwd,
            *tmux_mod.env_args(config_dir, profile),
            *name_args,
            "-P",
            "-F",
            "#{window_index}",
            *stamp_args,
        )
        if not ok:
            raise HTTPException(500, f"failed to create session '{session}': {msg}")
        # `new-session -e` sets the SESSION env, so without this every later
        # resume into this sentinel inherits this account — silently billing
        # them to the wrong subscription. This window already forked with the
        # value; `new-window -e` has no such spillover.
        if config_dir or profile:
            tmux_mod.scrub_session_env(session)
        try:
            index = int(msg)
        except ValueError:
            raise HTTPException(500, f"tmux returned unexpected index: {msg!r}") from None
        target = f"{session}:{index}"
        # Honour the caller's command. This branch used to rebuild it from
        # CLAUDE_EXEC, silently discarding whatever the caller passed — while
        # the existing-session branch below honoured it, so the same call
        # behaved differently depending on whether the sentinel session
        # happened to exist yet — flags and resume ids a caller composed were
        # silently dropped on exactly the branch that runs after a tmux
        # restart. The fallback only covers callers passing nothing at all;
        # the branch below
        # deliberately keeps sending nothing for an empty exec_cmd
        # (channels.py relies on that).
        cmd = exec_cmd.strip() or shlex.join(config.build_agent_command("claude", cwd=cwd, resume_id=resume_id))
        _send_and_stamp(target, cmd)
        _resuming[resume_id] = {"target": target, "started_at": int(time.time())}
        return _finish_resume(
            record,
            {
                "ok": True,
                "session": session,
                "index": index,
                "target": target,
                "mode": mode,
                "resumed_session_id": resume_id,
            },
        )

    # Session exists — spawn a new window into it.
    ok, msg = _tmux_mutate(
        "new-window",
        "-t",
        f"{session}:",
        "-c",
        cwd,
        *tmux_mod.env_args(config_dir, profile),
        *name_args,
        "-P",
        "-F",
        "#{window_index}",
        *stamp_args,
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
    return _finish_resume(
        record,
        {
            "ok": True,
            "session": session,
            "index": index,
            "target": target,
            "mode": mode,
            "exec": cmd,
            "resumed_session_id": resume_id,
        },
    )


def _window_new_plain(
    track_id: str,
    exec_cmd: str,
    mode: str,
    cwd_param: str | None = None,
    branch: str | None = None,
    agent: Literal["claude", "codex"] = "claude",
    account: str | None = None,
    profile: str | None = None,
    model: str | None = None,
    effort: str | None = None,
) -> dict:
    """Non-resume "+ New tab": open a window in the one shared MANAGED_SESSION
    and tag the new pane into `track_id` (the `session` query param now carries
    a track id, not a tmux session name — the rail groups by track).

    cwd resolution precedence (the launcher's branch picker drives the first
    two):
      1. `branch` set AND the track has a repo → resolve it to a worktree: an
         existing one if the branch already has it, else create one (checking
         the branch out when it exists, forking a new branch when it doesn't).
         This is what lets the launcher open a branch that isn't running.
      2. else `cwd_param` set AND it's a real dir → use it (a worktree path the
         client already knows, e.g. straight from the open catalog).
      3. else → the track's repo (repo-default track id == repo path), or
         the first repo root for a goal/loose track (repo None) or an
         unknown id.

    `account` and `model` arrive RESOLVED (window_new ran them through
    `usage.choose_launch`); `model` is the ANTHROPIC_MODEL value or None for
    no override, and `effort` the header's effort pin (Launch.effort).
    """
    row = activity.get_track(track_id)
    repo = row["repo"] if row and row.get("repo") else None
    config_dir = store.account_config_dir(account)
    profile_env = config.profile_env(profile)
    model_env = config.model_env(model)
    effort_level = config.effort_level(effort)

    branch = (branch or "").strip()
    if branch and repo:
        existing = open_ops.worktree_for_branch(repo, branch)
        if existing:
            cwd = existing
        else:
            try:
                wt = spawn_worktree(repo, branch)
            except ValueError as e:
                msg = str(e)
                raise HTTPException(409 if "already exists" in msg else 400, msg) from e
            cwd = wt["path"]
    elif cwd_param and os.path.isdir(cwd_param):
        cwd = cwd_param
    else:
        roots = store.repo_roots()
        cwd = repo or (roots[0] if roots else os.path.expanduser("~"))

    # Everything lives in one session. Create it lazily, else add a window.
    # Capture the STABLE #{window_id} (-P -F), never the index — with one
    # session under `renumber-windows on`, indices drift the moment any window
    # closes. Target MANAGED_SESSION with `=` (exact match): a bare `-t periscope`
    # PREFIX-matches sibling sessions (e.g. periscope-input).
    code, _ = _run(["tmux", "has-session", "-t", f"={MANAGED_SESSION}"])
    if code != 0:
        ok, msg = _tmux_mutate(
            "new-session",
            "-d",
            "-s",
            MANAGED_SESSION,
            "-c",
            cwd,
            *tmux_mod.env_args(config_dir, profile_env, model_env),
            "-P",
            "-F",
            "#{window_id}",
        )
        if not ok:
            raise HTTPException(500, f"failed to create session '{MANAGED_SESSION}': {msg}")
        # `new-session -e` sets the SESSION env, so without this every later
        # window in the one shared session inherits this account — silently
        # billing account-A panes to account B — and this profile. This window
        # already forked with the values; `new-window -e` has no such spillover.
        if config_dir or profile_env or model_env:
            tmux_mod.scrub_session_env(MANAGED_SESSION)
    else:
        ok, msg = _tmux_mutate(
            "new-window",
            "-t",
            f"={MANAGED_SESSION}:",
            "-c",
            cwd,
            *tmux_mod.env_args(config_dir, profile_env, model_env),
            "-P",
            "-F",
            "#{window_id}",
        )
        if not ok:
            raise HTTPException(500, msg)
    window_id = msg.strip()
    if not window_id.startswith("@"):
        raise HTTPException(500, f"tmux returned unexpected window id: {msg!r}")

    # Resolve the new window's index (for the recency stamp, which is keyed by
    # session:index in window_view).
    index_s = tmux("display-message", "-t", window_id, "-p", "#{window_index}").strip()
    try:
        index = int(index_s)
    except ValueError:
        raise HTTPException(500, f"tmux returned unexpected index: {index_s!r}") from None
    # Tag by the durable @periscope_id — stamp the brand-new window now rather
    # than waiting for the next poll's resolve_pids.
    new_pid = stamp_new_window(window_id)
    tracks.move_pane(new_pid, track_id)

    target = f"{MANAGED_SESSION}:{index}"
    cmd = exec_cmd.strip()
    if mode in {"claude", "codex", "agent"}:
        cmd = shlex.join(config.build_agent_command(agent, cwd=cwd, effort=effort_level))
    _send_and_stamp(target, cmd)
    return {
        "ok": True,
        "session": MANAGED_SESSION,
        "index": index,
        "target": target,
        "mode": mode,
        "agent": agent,
        "exec": cmd,
        "cwd": cwd,
        "profile": profile_env or "default",
        "model": model_env or "default",
        "effort": effort_level if agent == "claude" and effort_level else "default",
    }


def _codex_binding(session_id: str):
    with sqlite3.connect(str(config.ACTIVITY_DB), timeout=2.0) as conn:
        session_binding_db.ensure_schema(conn)
        conn.execute(
            "DELETE FROM agent_sessions WHERE provider='codex' AND evidence='launch-pending' AND updated_at < ?",
            (int(time.time()) - 30,),
        )
        row = conn.execute(
            "SELECT pane_id FROM agent_sessions WHERE provider='codex' AND session_id=? AND evidence='resume-explicit'",
            (session_id,),
        ).fetchone()
    if not row:
        return None
    pane_id = row[0]
    live = next(
        (w for w in list_windows() if w.get("pane_id") == pane_id),
        None,
    )
    return pane_id if live and live.get("agent") == "codex" else None


def _write_resume_binding(pane_id: str, session_id: str, path: str) -> None:
    with sqlite3.connect(str(config.ACTIVITY_DB), timeout=2.0) as conn:
        session_binding_db.ensure_schema(conn)
        session_binding_db.upsert_binding(
            conn,
            session_binding_db.AgentSessionBinding(
                pane_id=pane_id,
                provider="codex",
                session_id=session_id,
                session_path=path,
                updated_at=int(time.time()),
                evidence="resume-explicit",
            ),
        )


def _delete_resume_binding(pane_id: str) -> None:
    with sqlite3.connect(str(config.ACTIVITY_DB), timeout=2.0) as conn:
        session_binding_db.ensure_schema(conn)
        current = session_binding_db.get_binding(conn, pane_id)
        if current and current.evidence == "resume-explicit":
            session_binding_db.delete_binding(conn, pane_id)


def _window_new_codex_resume(
    track_id: str,
    resume_id: str | None,
    cwd_param: str | None,
    branch: str | None,
) -> dict:
    if not resume_id:
        raise HTTPException(400, "resume_id required for mode=resume")
    meta = codex_sessions.catalog().get(resume_id)
    if meta is None:
        raise HTTPException(404, f"unknown Codex session_id: {resume_id}")
    if _codex_binding(resume_id):
        raise HTTPException(409, "Codex session is already live")

    requested = meta.cwd if meta.cwd and os.path.isdir(meta.cwd) else cwd_param
    result = _window_new_plain(
        track_id,
        "",
        "shell",
        cwd_param=requested,
        branch=branch,
        agent="codex",
    )
    pane_id = tmux("display-message", "-t", result["target"], "-p", "#{pane_id}").strip()
    if not pane_id:
        raise HTTPException(500, "could not resolve pane for Codex resume")
    _write_resume_binding(pane_id, resume_id, str(meta.path))
    cmd = shlex.join(config.build_agent_command("codex", cwd=result["cwd"], resume_id=resume_id))
    time.sleep(0.1)
    ok, message = _tmux_mutate("send-keys", "-t", result["target"], cmd, "Enter")
    if not ok:
        _delete_resume_binding(pane_id)
        raise HTTPException(500, f"failed to launch Codex resume: {message}")
    note_focus(result["target"])
    note_action(result["target"])
    return {
        **result,
        "mode": "resume",
        "agent": "codex",
        "exec": cmd,
        "resumed_session_id": resume_id,
        "cwd_fallback": requested != meta.cwd,
    }


@router.post("/api/window/new")
def window_new(
    session: str,
    exec_cmd: str = Query("", alias="exec"),
    mode: str = "shell",
    resume_id: str | None = None,
    cwd: str | None = None,
    branch: str | None = None,
    agent: Literal["claude", "codex"] = "claude",
    account: str | None = None,
    profile: str | None = None,
    model: str | None = None,
    effort: str | None = None,
):
    """Spawn a window in `session`. `exec` param sends a command to the new
    window; legacy `mode` maps to `exec` for backwards-compat. `mode=resume`
    runs `claude --resume <resume_id>` in the original session's project
    dir. cwd is inherited from the session's active pane — without `-c`,
    tmux would use the periscope server's cwd, which is never what you
    want.

    The plain (non-resume) path takes two optional cwd hints from the
    launcher's branch picker: `cwd` (land the tab in a worktree path the
    client already knows) and `branch` (resolve the branch to a worktree,
    creating one if it has none). See `_window_new_plain`."""
    # Legacy `mode` → exec_cmd mapping for callers still on the old
    # contract. `mode=resume` synthesizes the command from resume_id.
    if mode == "resume" and agent == "codex":
        return _window_new_codex_resume(session, resume_id, cwd, branch)

    if not exec_cmd:
        if mode in ("claude", "vim", "shell"):
            exec_cmd = {"claude": CLAUDE_EXEC, "vim": "vim", "shell": ""}.get(mode, "")
        elif mode == "resume" and resume_id:
            exec_cmd = f"{CLAUDE_EXEC} --resume {resume_id}"

    if mode == "resume":
        # The dashboard's own resume button. Account only (a resumed session
        # keeps its own model); before this it passed nothing and billed the
        # default account regardless of headroom.
        result = _window_new_resume(
            session,
            exec_cmd,
            resume_id,
            mode,
            account=usage.choose_launch(account).account,
        )
        return {**result, "agent": "claude"}
    if agent == "claude":
        launch = usage.choose_launch(account, model, effort)
        account, model, effort = launch.account, launch.model, launch.effort
    return _window_new_plain(
        session,
        exec_cmd,
        mode,
        cwd_param=cwd,
        branch=branch,
        agent=agent,
        account=account,
        profile=profile,
        model=model,
        effort=effort,
    )


@router.post("/api/pane/move-account")
def pane_move_account(pid: str, account: str, force: bool = False):
    """Re-open this pane's Claude session on another subscription.

    NOT a live migration. `~/.claude-b/projects` symlinks to
    `~/.claude/projects`, so both accounts read one transcript tree and a
    session started on A resumes on B. The move spawns a SECOND pane running
    `claude --resume <id>` under the target account's CLAUDE_CONFIG_DIR and
    leaves the original running: it is the only fallback if the resume doesn't
    take, and killing a pane the user hasn't finished reading is unrecoverable.

    `_window_new_resume`'s guards apply, except the one on a live Claude that
    runs the session (the original pane's does) — a transcript written
    to in the last 60s 409s (two concurrent appenders would interleave into the
    same JSONL), as does a session already resumed elsewhere. `force=1` bypasses
    the mtime guard only — the client sends it after the user confirms "move
    anyway" on that 409.
    """
    # `store.account_config_dir` fails OPEN to the DEFAULT account on an id no
    # registered account claims — correct at spawn time (an unauthenticated
    # pane beats a mis-billed one) and exactly wrong here. This endpoint exists
    # to move work OFF an exhausted subscription, so a typo silently landing
    # the pane back on account A is the one outcome it must never produce.
    if account not in {a["id"] for a in store.get_accounts()}:
        raise HTTPException(400, f"unknown account: {account!r}")
    _pid, pane_id, window = _resolve_window_by_pid(pid)
    if not pane_id:
        raise HTTPException(404, f"no live pane for pid {pid!r}")
    session_id = session_id_for_pane(pane_id)
    if not session_id:
        # No pane_sessions row: the SessionStart hook hasn't fired for this pane
        # yet (or isn't installed). There is deliberately no cwd fallback —
        # panes share a cwd, and resuming the WRONG session onto a second
        # account is worse than telling the user to try again after a prompt.
        raise HTTPException(404, f"pane {pid} has no recorded Claude session yet")
    track_id = tracks.resolve_track_for_window(window)

    result = _window_new_resume(
        RESUME_SESSION,
        f"{CLAUDE_EXEC} --resume {session_id}",
        session_id,
        "resume",
        account=account,
        force=force,
        moving=True,
    )
    target = result["target"]
    new_pane_id = tmux("display-message", "-t", target, "-p", "#{pane_id}").strip()
    # Mint a guaranteed-unique id rather than resolving: a brand-new window has
    # no stamp, and resolving one window with an empty taken-set lets the rebind
    # pass match it to the ORIGINAL pane's entry on (branch, cwd) — which here
    # is always the same pair — stealing that pid. Same reasoning as
    # channels._do_spawn_claude_tool.
    new_pid = stamp_new_window(target)
    if new_pid:
        # Land it next to where it came from. `resumes` is plumbing and the rail
        # is track-anchored, so without the re-tag the moved session shows up in
        # the repo-default bucket instead of beside the pane it replaced.
        tracks.move_pane(new_pid, track_id)
    return {
        **result,
        "pid": new_pid,
        "pane_id": new_pane_id,
        "account": account,
        "track_id": track_id,
        "moved_from": pid,
    }


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
