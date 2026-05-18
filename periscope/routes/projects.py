"""Project CRUD endpoints.

GET    /api/projects               — list all (incl. archived)
POST   /api/projects/adopt         — adopt unmanaged session OR existing worktree
POST   /api/projects/patch         — rename, edit base_branch, set/clear pinned_repo
POST   /api/projects/archive       — set archived_at

We deliberately do NOT use path params for `pinned_dir`. Starlette
rejects URL-encoded `/` in path-converter segments by default, and
pinned_dirs are absolute paths with many slashes. Body-carried
identifiers sidestep the issue entirely.

Phase 1 does NOT include POST /api/projects (create-new); that's phase 2.
"""

import os
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from periscope.log import log
from periscope.panes import list_windows
from periscope.projects import (
    all_projects, archive_project, create_project, get_project,
    update_project, MAIN_KEY,
)
from periscope.tmux import _run, _tmux_mutate, tmux
from periscope.worktree_spawn import spawn_worktree, _detect_default_branch


router = APIRouter()


@router.get("/api/projects")
def projects_list():
    return {"projects": [
        {"pinned_dir": k, **v} for k, v in all_projects().items()
    ]}


class AdoptBody(BaseModel):
    # Exactly one of these must be set.
    pinned_dir: str | None = None
    tmux_session: str | None = None
    # Optional: override the auto-derived name (defaults to pinned_dir basename
    # or tmux_session).
    name: str | None = None


@router.post("/api/projects/adopt")
def projects_adopt(body: AdoptBody):
    if bool(body.pinned_dir) == bool(body.tmux_session):
        raise HTTPException(400, "exactly one of pinned_dir or tmux_session required")

    pinned_dir: str
    tmux_session: str

    if body.pinned_dir:
        # Adopt a worktree on disk as a project.
        if not os.path.isdir(body.pinned_dir):
            raise HTTPException(400, f"pinned_dir does not exist: {body.pinned_dir}")
        # Require a git toplevel so we don't accidentally pin a project to
        # something like /etc. Mirrors the tmux_session branch's invariant.
        code, toplevel = _run(["git", "-C", body.pinned_dir, "rev-parse", "--show-toplevel"])
        if code != 0 or not toplevel:
            raise HTTPException(
                400,
                f"pinned_dir is not inside a git repo: {body.pinned_dir}",
            )
        pinned_dir = os.path.realpath(toplevel)
        # Find matching tmux session, if any (the user may have a session
        # already attached to this directory).
        windows = list_windows()
        matched_session: str | None = None
        for w in windows:
            if os.path.realpath(w.get("cwd") or "") == pinned_dir:
                matched_session = w["session"]
                break
        tmux_session = matched_session or (body.name or os.path.basename(pinned_dir))
    else:
        # Adopt an unmanaged tmux session.
        windows = [w for w in list_windows() if w["session"] == body.tmux_session]
        if not windows:
            raise HTTPException(404, f"no tmux session named {body.tmux_session!r}")
        windows.sort(key=lambda w: w["index"])
        for w in windows:
            cwd = w.get("cwd") or ""
            code, toplevel = _run(["git", "-C", cwd, "rev-parse", "--show-toplevel"])
            if code == 0 and toplevel:
                pinned_dir = os.path.realpath(toplevel)
                break
        else:
            raise HTTPException(
                400,
                f"no window in session {body.tmux_session!r} has a git toplevel; cannot adopt as project",
            )
        tmux_session = body.tmux_session

    # 409 on duplicate per spec.
    if pinned_dir in all_projects():
        raise HTTPException(409, f"project already exists at {pinned_dir!r}")

    # Resolve repo via --git-common-dir (same algorithm as the v2 migration).
    code, common = _run(["git", "-C", pinned_dir, "rev-parse", "--git-common-dir"])
    if code == 0 and common:
        common_abs = common if os.path.isabs(common) else os.path.join(pinned_dir, common)
        repo = os.path.realpath(os.path.dirname(common_abs))
    else:
        repo = pinned_dir

    _, branch = _run(["git", "-C", pinned_dir, "rev-parse", "--abbrev-ref", "HEAD"])
    if branch == "HEAD":
        branch = ""

    try:
        row = create_project(
            pinned_dir,
            name=body.name or tmux_session,
            tmux_session=tmux_session,
            repo=repo,
            base_branch=branch or None,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "pinned_dir": pinned_dir, **row}


class PatchBody(BaseModel):
    # `pinned_dir` is REQUIRED — body-carried because URL-encoded slashes
    # in path params are broken in Starlette.
    pinned_dir: str
    # All other fields optional. Pydantic distinguishes "not sent" from
    # "sent as null" via model_fields_set — fields the client explicitly
    # omitted don't appear there. Sending `null` for base_branch or
    # pinned_repo CLEARS those fields; sending `null` for name or
    # tmux_session is rejected (those have no meaningful empty state).
    name: str | None = None
    base_branch: str | None = None
    pinned_repo: str | None = None
    tmux_session: str | None = None


@router.post("/api/projects/patch")
def projects_patch(body: PatchBody):
    key = body.pinned_dir
    sent = body.model_fields_set - {"pinned_dir"}  # exclude the identity field

    if key == MAIN_KEY:
        # __main__ is restricted; only tmux_session is mutable.
        if not sent.issubset({"tmux_session"}):
            raise HTTPException(400, "only tmux_session is mutable on main")
        if "tmux_session" not in sent or body.tmux_session is None:
            return {"ok": True, "pinned_dir": key, **get_project(key)}
        if not update_project(key, tmux_session=body.tmux_session):
            raise HTTPException(404, "main not found")
        return {"ok": True, "pinned_dir": key, **get_project(key)}

    existing = get_project(key)
    if not existing:
        raise HTTPException(404, f"no project at {key!r}")

    fields: dict = {}
    for k in sent:
        v = getattr(body, k)
        if k in ("name", "tmux_session") and v is None:
            raise HTTPException(400, f"{k} cannot be null")
        # base_branch and pinned_repo accept None as "clear."
        fields[k] = v

    # If tmux_session changes, run `tmux rename-session` FIRST. If tmux
    # fails, abort without touching state — drift between state and tmux
    # would break addressing on the next poll (resolve_project_for_window
    # matches on `tmux_session` literal). If tmux succeeds, state is then
    # updated; a state-write failure after that leaves us with a renamed
    # tmux session and stale state — recoverable via the user re-issuing
    # the rename, while the inverse drift is not.
    new_tmux = fields.get("tmux_session")
    if new_tmux and new_tmux != existing.get("tmux_session"):
        ok, msg = _tmux_mutate(
            "rename-session", "-t", existing["tmux_session"], new_tmux
        )
        if not ok:
            raise HTTPException(
                500, f"tmux rename-session failed: {msg}"
            )
        log.info(
            "renamed tmux session %r → %r for project %r",
            existing["tmux_session"], new_tmux, key,
        )

    update_project(key, **fields)
    return {"ok": True, "pinned_dir": key, **get_project(key)}


class ArchiveBody(BaseModel):
    pinned_dir: str


@router.post("/api/projects/archive")
def projects_archive(body: ArchiveBody):
    key = body.pinned_dir
    if key == MAIN_KEY:
        raise HTTPException(400, "cannot archive __main__")
    if not archive_project(key):
        raise HTTPException(404, f"no project at {key!r}")
    return {"ok": True, "pinned_dir": key, **get_project(key)}


class CreateBody(BaseModel):
    repo: str
    branch: str
    name: str | None = None  # auto-fills to branch if absent


def _layout_two_window(tmux_session: str, pinned_dir: str) -> None:
    """Apply the trellis-style 2-window layout: window 1 'claude',
    window 2 'shell'. tmux session is created from scratch and ends with
    window 1 active. The user is NOT attached — periscope is a dashboard,
    not a terminal client.

    The 100ms sleep before each send-keys lets the shell finish loading
    its rc file before the command lands (see CLAUDE.md "Key invariants"
    note 5). Without it, `claude` can land mid-rc and either get echoed
    as text or fail silently.
    """
    import time
    from periscope.panes import note_focus, note_action

    # new-session creates window 0 (or whatever base-index is) with a bare
    # shell at cwd = pinned_dir.
    ok, msg = _tmux_mutate(
        "new-session", "-d", "-s", tmux_session, "-c", pinned_dir,
        "-n", "claude",
    )
    if not ok:
        raise HTTPException(500, f"tmux new-session failed: {msg}")

    # Send `claude` into window 1.
    time.sleep(0.1)
    _tmux_mutate(
        "send-keys", "-t", f"{tmux_session}:claude", "claude", "Enter",
    )

    # Window 2: shell.
    ok, msg = _tmux_mutate(
        "new-window", "-t", f"{tmux_session}:", "-c", pinned_dir,
        "-n", "shell",
    )
    if not ok:
        # Worktree + session + window 1 already exist; don't roll back.
        log.warning("new-project: failed to create shell window: %s", msg)

    # Park focus on window 1 (claude).
    _tmux_mutate("select-window", "-t", f"{tmux_session}:claude")

    # Stamp focus + action so the new project sorts to the top of the
    # grid + stream views on the next poll. Match the pattern in
    # routes/sessions.py:46-47 for `+ session`.
    # The claude window is the first one created; its tmux window index
    # depends on base-index. Resolve it by looking up the window-id.
    idx_out = tmux(
        "display-message", "-t", f"{tmux_session}:claude",
        "-p", "#{window_index}",
    ).strip()
    if idx_out.isdigit():
        target = f"{tmux_session}:{idx_out}"
        note_focus(target)
        note_action(target)


@router.post("/api/projects")
def projects_create(body: CreateBody):
    """Create a new project: spawn worktree if branch != default,
    create tmux session, apply 2-window layout, register project."""
    repo = os.path.realpath(body.repo)
    if not os.path.isdir(repo):
        raise HTTPException(400, f"repo does not exist: {body.repo}")
    code, toplevel = _run(["git", "-C", repo, "rev-parse", "--show-toplevel"])
    if code != 0 or not toplevel:
        raise HTTPException(400, f"not a git repo: {body.repo}")
    repo = os.path.realpath(toplevel)

    branch = body.branch.strip()
    if not branch:
        raise HTTPException(400, "branch is required")
    if branch.startswith("-"):
        raise HTTPException(400, f"branch name cannot start with '-': {branch!r}")

    default = _detect_default_branch(repo)

    # Pre-flight collision checks before ANY filesystem/tmux mutation.
    # Pre-checking up here means a 409 leaves the user's state untouched —
    # no orphan worktree on disk, no half-created tmux session.
    name = (body.name or branch).strip()
    tmux_session = name

    if branch == default and repo in all_projects():
        raise HTTPException(409, f"project already exists at {repo!r}")
    has_session_code, _ = _run(["tmux", "has-session", "-t", tmux_session])
    if has_session_code == 0:
        raise HTTPException(
            409, f"tmux session {tmux_session!r} already exists; pick a different name",
        )

    pinned_dir: str
    warning: str | None = None
    if branch == default:
        # No worktree — project pins to repo root.
        pinned_dir = repo
    else:
        try:
            res = spawn_worktree(repo, branch)
        except ValueError as e:
            raise HTTPException(400, str(e))
        pinned_dir = res["path"]
        warning = res.get("warning")

        # Belt-and-suspenders: after spawn, re-check the pinned_dir isn't
        # already adopted. spawn_worktree already rejected if the path
        # exists on disk, so this is mostly defensive against a racy
        # adoption during the fetch+add window.
        if pinned_dir in all_projects():
            raise HTTPException(
                409, f"project already exists at {pinned_dir!r}"
            )

    try:
        _layout_two_window(tmux_session, pinned_dir)
    except HTTPException:
        # tmux failed mid-layout — leave the worktree on disk so the user
        # can retry adoption or clean up manually. Don't rollback git.
        raise

    try:
        row = create_project(
            pinned_dir,
            name=name,
            tmux_session=tmux_session,
            repo=repo,
            base_branch=branch,
        )
    except ValueError as e:
        # Race: someone adopted the same pinned_dir between our 409-check
        # and here. Roll back tmux so the orphan session doesn't shadow
        # the existing project on next poll.
        _run(["tmux", "kill-session", "-t", tmux_session])
        raise HTTPException(409, str(e))

    result = {"ok": True, "pinned_dir": pinned_dir, **row}
    if warning:
        result["warning"] = warning
    return result


class PromoteBody(BaseModel):
    # The window to promote (tmux addressing).
    session: str
    index: int
    # Optional override of the auto-derived name.
    name: str | None = None


@router.post("/api/projects/promote")
def projects_promote(body: PromoteBody):
    """Promote a tab in the main project to its own project. Resolves
    the tab's cwd to a git toplevel, creates a project pinned there
    (409 if one exists), creates a tmux session named after the project,
    and moves the window in via `tmux move-window`.
    """
    target = f"{body.session}:{body.index}"
    # Look up the window's cwd via tmux. Use `_run` instead of `tmux()`
    # so we can distinguish "window not found" (non-zero exit) from
    # "legitimately empty cwd" (zero exit, empty stdout — rare but
    # possible mid-tmux-startup).
    code, cwd_out = _run(
        ["tmux", "display-message", "-t", target, "-p", "#{pane_current_path}"]
    )
    if code != 0:
        raise HTTPException(404, f"window not found: {target}")
    cwd_out = cwd_out.strip()
    if not cwd_out:
        raise HTTPException(400, f"window {target} has empty cwd")

    code, toplevel = _run(
        ["git", "-C", cwd_out, "rev-parse", "--show-toplevel"]
    )
    if code != 0 or not toplevel:
        raise HTTPException(
            400, f"tab cwd is not inside a git repo: {cwd_out}"
        )
    pinned_dir = os.path.realpath(toplevel)

    if pinned_dir in all_projects():
        raise HTTPException(
            409, f"project already exists at {pinned_dir!r}"
        )

    # Resolve repo via --git-common-dir (matches Task 1's migration +
    # adopt endpoints).
    code, common = _run(
        ["git", "-C", pinned_dir, "rev-parse", "--git-common-dir"]
    )
    if code == 0 and common:
        common_abs = (
            common if os.path.isabs(common) else os.path.join(pinned_dir, common)
        )
        repo = os.path.realpath(os.path.dirname(common_abs))
    else:
        repo = pinned_dir

    _, branch = _run(
        ["git", "-C", pinned_dir, "rev-parse", "--abbrev-ref", "HEAD"]
    )
    if branch == "HEAD":
        branch = ""

    name = (body.name or os.path.basename(pinned_dir)).strip()
    tmux_session = name

    # Create the new tmux session and capture the auto-created window's
    # id so we can kill it after the move (rather than guessing by index,
    # which would break under `renumber-windows`). `-P -F '#{window_id}'`
    # matches the existing pattern in routes/sessions.py:121-122.
    ok, msg = _tmux_mutate(
        "new-session", "-d", "-s", tmux_session, "-c", pinned_dir,
        "-P", "-F", "#{window_id}",
    )
    if not ok:
        raise HTTPException(500, f"tmux new-session failed: {msg}")
    auto_window_id = msg.strip()  # e.g. "@42"

    # Move the source window in. Without a -t index, tmux picks the next
    # free slot.
    ok, msg = _tmux_mutate(
        "move-window", "-s", target, "-t", f"{tmux_session}:",
    )
    if not ok:
        # Rollback the empty session.
        _tmux_mutate("kill-session", "-t", tmux_session)
        raise HTTPException(500, f"tmux move-window failed: {msg}")

    # Kill the auto-created blank window by its window-id (NOT by index —
    # safe against `renumber-windows`).
    if auto_window_id:
        _tmux_mutate("kill-window", "-t", auto_window_id)

    try:
        row = create_project(
            pinned_dir,
            name=name,
            tmux_session=tmux_session,
            repo=repo,
            base_branch=branch or None,
        )
    except ValueError as e:
        raise HTTPException(409, str(e))

    return {"ok": True, "pinned_dir": pinned_dir, **row}


@router.get("/api/projects/discoverable")
def projects_discoverable():
    """Return the union of (a) currently-known project repos and (b) git
    repos discovered under ~/dev (one level deep). Plus the local branch
    list per repo, capped at 100 each for sanity.

    Frontend uses this to populate the new-project modal's repo/branch
    pickers.
    """
    repos: set[str] = set()

    for p in all_projects().values():
        if p.get("repo"):
            repos.add(os.path.realpath(p["repo"]))

    dev = Path.home() / "dev"
    if dev.is_dir():
        for child in dev.iterdir():
            if not child.is_dir():
                continue
            # Skip hidden and the worktrees container itself.
            if child.name.startswith(".") or child.name == "worktrees":
                continue
            if (child / ".git").exists():
                repos.add(str(child.resolve()))

    branches_by_repo: dict[str, list[str]] = {}
    for repo in sorted(repos):
        code, out = _run(
            ["git", "-C", repo, "branch", "--format=%(refname:short)"],
            timeout=3.0,
        )
        if code == 0:
            branches_by_repo[repo] = out.split("\n")[:100] if out else []
        else:
            branches_by_repo[repo] = []

    return {
        "repos": sorted(repos),
        "branches_by_repo": branches_by_repo,
    }
