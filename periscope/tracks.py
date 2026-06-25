"""Track entity + resolution. A track is a metadata container of tabs — the
one organizational primitive (see docs/superpowers/specs/2026-06-25-track-*).
Value-data (TypedDict) + module functions, mirroring workspaces.py/projects.py.
"""
from __future__ import annotations

import os
import time
from typing import TypedDict

from periscope import activity

LOOSE_KEY = "loose"


class Track(TypedDict, total=False):
    id: str
    name: str
    repo: str | None
    created_at: int
    archived_at: int | None


def _slug(s: str) -> str:
    out = "".join(ch if ch.isalnum() else "-" for ch in s.lower()).strip("-")
    while "--" in out:
        out = out.replace("--", "-")
    return out or "track"


def _repo_for_window(window: dict) -> str | None:
    """Repo toplevel path for a window, or None if non-git. Reuses the cached
    git state the view already computes — no new git call (spec §Risks)."""
    cwd = window.get("cwd")
    if not cwd:
        return None
    from periscope import git_pr
    state = git_pr.cached_git_state(cwd)
    # repo_key is the repo toplevel path (resolve_repo(path)) — the SAME value
    # today's project keys use, so repo-default track ids match project keys
    # (byte-identical-to-projects cutover). NOT "toplevel" (no such key).
    return (state or {}).get("repo_key")


def repo_default_track(repo: str | None) -> str:
    """Get-or-create the repo-default track, keyed on the repo path so a fresh
    boot re-derives the SAME id (byte-identical-to-projects cutover)."""
    if not repo:
        return LOOSE_KEY
    if activity.get_track(repo) is None:
        activity.insert_track({"id": repo, "name": os.path.basename(repo.rstrip("/")),
                               "repo": repo, "created_at": int(time.time()),
                               "archived_at": None})
    return repo


def resolve_track_for_window(window: dict) -> str:
    pane_id = window.get("pane_id")
    if pane_id:
        tagged = activity.get_pane_track(pane_id)
        if tagged:
            row = activity.get_track(tagged)
            if row is not None and not row.get("archived_at"):
                return tagged
    return repo_default_track(_repo_for_window(window))


def track_label(track_id: str) -> str:
    """Human display name for a track id — the rail's top-level row label.
    The track row's `name` (goal tracks carry the user-chosen name; repo-default
    tracks carry basename(repo)); falls back to the id's path basename so a
    just-resolved repo-default that hasn't been row-read yet still labels."""
    if track_id == LOOSE_KEY:
        return "loose"
    row = activity.get_track(track_id)
    if row and row.get("name"):
        return row["name"]
    return os.path.basename(track_id.rstrip("/")) or track_id


def create_track(*, name: str, repo: str | None = None) -> Track:
    tid = f"tk_{_slug(name)}"
    base, n = tid, 2
    while activity.get_track(tid) is not None:
        tid = f"{base}-{n}"
        n += 1
    row: Track = {"id": tid, "name": name, "repo": repo,
                  "created_at": int(time.time()), "archived_at": None}
    activity.insert_track(dict(row))
    return row


def rename_track(track_id: str, name: str) -> bool:
    if activity.get_track(track_id) is None:
        return False
    activity.update_track(track_id, name=name)
    return True


def move_pane(pane_id: str, track_id: str) -> None:
    activity.set_pane_track(pane_id, track_id)


def dissolve_track(track_id: str) -> None:
    """Remove the track; its tabs fall back to repo-default/loose on next
    resolve. Nothing is killed. Archived so resolve's stale-tag guard fires."""
    if track_id in (LOOSE_KEY,) or activity.get_track(track_id) is None:
        return
    activity.archive_track(track_id, ts=int(time.time()))


def teardown_targets(track_id: str, windows: list[dict]) -> list[tuple[str, str]]:
    """[(window_target, pane_id)] for kill — panes whose resolved track is
    track_id. Refuses LOOSE and repo-default tracks (catchalls — never mass-kill).
    Moved + retargeted from projects.placement_kill_set (pane_id-based)."""
    if track_id == LOOSE_KEY:
        raise ValueError("refusing to tear down the loose catchall")
    row = activity.get_track(track_id)
    if row is None:
        raise ValueError(f"no such track: {track_id}")
    # INVARIANT: a repo-default track has id == repo (repo_default_track keys the
    # row id on the repo path). Goal tracks have id="tk_<slug>" != repo. So
    # id == repo uniquely identifies the catchall — never mass-kill it. If
    # repo_default_track's id scheme ever changes, revisit this guard.
    if row.get("repo") == track_id:
        raise ValueError("refusing to tear down a repo-default track")
    out: list[tuple[str, str]] = []
    for w in windows:
        pane_id = w.get("pane_id")
        if pane_id and resolve_track_for_window(w) == track_id:
            out.append((f"{w['session']}:{w['index']}", pane_id))
    return out


def seed_tracks(windows: list[dict]) -> int:
    """Migration seed: tag every managed pane with its resolved track. Idempotent
    — skips already-tagged panes (the backfill_pane_projects pattern)."""
    existing = activity.pane_track_map()
    written = 0
    for w in windows:
        pane_id = w.get("pane_id")
        if not pane_id or pane_id in existing:
            continue
        activity.set_pane_track(pane_id, resolve_track_for_window(w))
        written += 1
    return written
