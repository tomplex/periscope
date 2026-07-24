"""Track lifecycle REST routes.

Create / rename / move-tab (re-tag) / dissolve (kills nothing — tabs survive)
/ teardown (the destructive confirm path — kills the track's panes and,
opt-in, removes their git worktrees). The entity lives in periscope.tracks;
the per-tab tag map lives in periscope.activity (pane_tracks table). Mirrors
routes/workspaces.py + the destructive bits of routes/cleanup.py.
"""
import os

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from periscope import tracks
from periscope.log import log
from periscope.panes import drop_target_focus, list_windows
from periscope.tmux import _run, _tmux_mutate

router = APIRouter()


class CreateBody(BaseModel):
    name: str
    repo: str | None = None


@router.post("/api/tracks")
def tracks_create(body: CreateBody):
    return tracks.create_track(name=body.name, repo=body.repo)


class RenameBody(BaseModel):
    name: str


@router.patch("/api/tracks/{track_id}")
def tracks_rename(track_id: str, body: RenameBody):
    if not tracks.rename_track(track_id, body.name):
        raise HTTPException(404, f"no track {track_id!r}")
    from periscope import activity
    return activity.get_track(track_id)


class MoveTabBody(BaseModel):
    pane_id: str


@router.post("/api/tracks/{track_id}/move-tab")
def tracks_move_tab(track_id: str, body: MoveTabBody):
    from periscope import activity
    if activity.get_track(track_id) is None:
        raise HTTPException(404, f"no track {track_id!r}")
    tracks.move_pane(body.pane_id, track_id)
    return {"ok": True}


@router.post("/api/tracks/{track_id}/dissolve")
def tracks_dissolve(track_id: str):
    """Archive the track; its tabs survive and fall back to repo-default/loose
    on the next resolve. Kills nothing. Refuses the loose/repo-default
    catchalls (409), matching teardown."""
    from periscope import activity
    if track_id == tracks.LOOSE_KEY or activity.get_track(track_id) is None:
        raise HTTPException(404, f"no track {track_id!r}")
    try:
        tracks.dissolve_track(track_id)
    except ValueError as e:
        raise HTTPException(409, str(e)) from e
    return activity.get_track(track_id)


class TeardownBody(BaseModel):
    delete_worktrees: bool = False


@router.post("/api/tracks/{track_id}/teardown")
def tracks_teardown(track_id: str, body: TeardownBody):
    """Kill every pane resolving to this track (by stable pane_id, reusing
    cleanup's kill path), optionally removing each killed pane's git worktree.
    Refuses the loose/repo-default catchalls (409) — teardown_targets raises."""
    try:
        targets = tracks.teardown_targets(track_id, list_windows())
    except ValueError as e:
        # No such track → 404; refused catchall (loose/repo-default) → 409.
        if "no such track" in str(e):
            raise HTTPException(404, str(e)) from e
        raise HTTPException(409, str(e)) from e

    # Capture each pane's worktree path BEFORE killing (cwd is the worktree
    # for these panes) so we can remove it after, if requested.
    cwd_by_pane = {
        w["pane_id"]: w.get("cwd")
        for w in list_windows()
        if w.get("pane_id")
    }

    killed: list[tuple[str, str]] = []
    for target, pane_id in targets:
        # Kill by stable pane_id, not session:index — renumber-windows shifts
        # indices mid-loop (see cleanup_archive / session_delete).
        _tmux_mutate("kill-pane", "-t", pane_id)
        drop_target_focus(target)
        killed.append((target, pane_id))

    if body.delete_worktrees:
        _remove_worktrees([cwd_by_pane.get(pid) for _, pid in killed])

    return {"killed": killed}


def _remove_worktrees(paths: list[str | None]) -> None:
    """`git worktree remove --force` each path, deduped. Reuses cleanup's
    git-worktree removal (there's no single helper in periscope.worktrees —
    only the cache `invalidate`). Failures are logged, not fatal: teardown
    already killed the panes, so a stuck worktree shouldn't 500 the request."""
    from periscope import worktrees

    seen: set[str] = set()
    for path in paths:
        if not path or path in seen:
            continue
        seen.add(path)
        # Resolve the repo (worktree's main checkout) to run `git -C repo`.
        code, common = _run(
            ["git", "-C", path, "rev-parse", "--git-common-dir"], timeout=3.0,
        )
        if code != 0 or not common:
            log.warning("teardown: not a git worktree, skipping removal: %s", path)
            continue
        common_abs = common if os.path.isabs(common) else os.path.join(path, common)
        repo = os.path.realpath(os.path.dirname(common_abs))
        code, out = _run(
            ["git", "-C", repo, "worktree", "remove", "--force", path], timeout=10.0,
        )
        if code != 0:
            log.warning("teardown: worktree remove %s failed: %s", path, out)
            continue
        worktrees.invalidate(repo)
