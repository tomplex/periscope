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

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from periscope.log import log
from periscope.panes import list_windows
from periscope.projects import (
    all_projects, archive_project, create_project, get_project,
    update_project, MAIN_KEY,
)
from periscope.tmux import _run, _tmux_mutate


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
        pinned_dir = os.path.realpath(body.pinned_dir)
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
