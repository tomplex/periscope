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
    # pid on resolved rows, pid_raw on raw list_windows() rows — track
    # membership keys on the durable @periscope_id, never the rotating %N.
    pid = window.get("pid") or window.get("pid_raw")
    if pid:
        tagged = activity.get_pane_track(pid)
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


def track_kind(track_id: str) -> str:
    """"loose" | "repo" | "goal" — which of the two catchalls a track is, or
    neither. A repo-default track has id == repo (repo_default_track keys the
    row id on the repo path); goal tracks have id="tk_<slug>" != repo. Both
    catchalls refuse dissolve/teardown, so the rail hides their action menu —
    a repo-default row otherwise offered a Tear down that only ever 409'd and
    a Dissolve that archived a row `repo_default_track` immediately resolves
    into again (get_track doesn't filter archived), i.e. a silent no-op.
    An unknown id reads as "repo": hiding a menu is the safe direction."""
    if track_id == LOOSE_KEY:
        return "loose"
    row = activity.get_track(track_id)
    return "goal" if row and row.get("repo") != track_id else "repo"


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


def move_pane(pid: str, track_id: str) -> None:
    activity.set_pane_track(pid, track_id)


def dissolve_track(track_id: str) -> None:
    """Remove the track; its tabs fall back to repo-default/loose on next
    resolve. Nothing is killed. Archived so resolve's stale-tag guard fires.
    Refuses a catchall (same guard as teardown_targets): a repo-default's tabs
    fall back to the repo-default, so archiving it changes nothing visible —
    `repo_default_track` only inserts when `get_track` is None, and that
    doesn't filter archived, so the row keeps resolving. Silent no-op + a
    zombie archived_at."""
    if track_id in (LOOSE_KEY,) or activity.get_track(track_id) is None:
        return
    if track_kind(track_id) == "repo":
        raise ValueError("refusing to dissolve a repo-default track")
    activity.archive_track(track_id, ts=int(time.time()))


def teardown_targets(track_id: str, windows: list[dict]) -> list[tuple[str, str]]:
    """[(window_target, pane_id)] for kill — panes whose resolved track is
    track_id. Refuses LOOSE and repo-default tracks (catchalls — never mass-kill).
    Kills by stable pane_id (renumber-immune)."""
    if track_id == LOOSE_KEY:
        raise ValueError("refusing to tear down the loose catchall")
    row = activity.get_track(track_id)
    if row is None:
        raise ValueError(f"no such track: {track_id}")
    if track_kind(track_id) == "repo":
        raise ValueError("refusing to tear down a repo-default track")
    out: list[tuple[str, str]] = []
    for w in windows:
        pane_id = w.get("pane_id")
        if pane_id and resolve_track_for_window(w) == track_id:
            out.append((f"{w['session']}:{w['index']}", pane_id))
    return out


def seed_tracks(windows: list[dict]) -> int:
    """Migration seed: tag every managed pane with its resolved track. Idempotent
    — skips already-tagged panes. Keys on the @periscope_id; an unstamped window
    has no durable identity to tag yet, so it is skipped (it resolves lazily to
    its repo-default until stamped)."""
    existing = activity.pane_track_map()
    written = 0
    for w in windows:
        pid = w.get("pid") or w.get("pid_raw")
        if not pid or pid in existing:
            continue
        activity.set_pane_track(pid, resolve_track_for_window(w))
        written += 1
    return written


def migrate_workspaces_to_tracks() -> int:
    """Fold legacy workspaces into goal tracks. A workspace WAS a goal track —
    a curated cross-cutting group — so each non-archived workspace becomes a
    track (id == workspace id, so re-runs are idempotent), and its
    pane_workspaces members are tagged into it.

    Skips a pane that ALREADY carries an explicit pane_tracks tag, so a later
    user move/re-tag wins and a second run is a no-op. Every handled row is
    DELETED (folded, preserved, or unresolvable): this runs on every boot and
    tmux restarts reuse %N, so a lingering "%1 → ws" row would later match an
    unrelated brand-new pane that drew the same %N and drag it into the old
    workspace's track. Goes fully inert once pane_workspaces is emptied/dropped
    (the entity fold in T14b). Returns the number of panes newly tagged. Run
    BEFORE seed_tracks so workspace panes get their goal track and only the
    rest fall to repo-default."""
    from periscope import workspaces as _ws

    rows = _ws.all_workspaces()  # {ws_id: Workspace}
    for ws_id, ws in rows.items():
        if ws.get("archived_at"):
            continue
        if activity.get_track(ws_id) is None:
            activity.insert_track({"id": ws_id, "name": ws.get("name") or ws_id,
                                   "repo": ws.get("base_repo"),
                                   "created_at": int(time.time()), "archived_at": None})
    # pane_workspace_map is %N-keyed legacy data; pane_tracks keys on the
    # durable @periscope_id, so convert through a live %N → pid_raw map.
    from periscope.panes import list_windows
    pane_to_pid = {w["pane_id"]: w["pid_raw"] for w in list_windows()
                   if w.get("pane_id") and w.get("pid_raw")}
    already = activity.pane_track_map()
    written = 0
    for pane_id, ws_id in activity.pane_workspace_map().items():
        ws = rows.get(ws_id)
        if not ws or ws.get("archived_at"):
            continue
        pid = pane_to_pid.get(pane_id)
        if not pid:
            # Pane gone or unstamped: the row is unrecoverable, and skipping it
            # forever is the hazard — tmux restarts reuse %N, so a lingering
            # row would later match an unrelated brand-new pane that drew the
            # same %N and drag it into this workspace's track. Delete it.
            activity.set_pane_workspace(pane_id, None)
            continue
        cur = already.get(pid)
        if cur is not None and cur != ws_id:
            # The pane already carries a track tag. Workspace membership
            # OVERRIDES a repo-default tag (id == repo path — just the lazy
            # fallback, e.g. a prior seed), but PRESERVES an explicit goal-track
            # move (the user re-homed it). A repo-default row has repo == its id.
            cur_row = activity.get_track(cur)
            is_repo_default = bool(cur_row and cur_row.get("repo") == cur)
            if not is_repo_default:
                # User moved it to another goal track → leave the tag, but the
                # legacy row is consumed (same %N-reuse hazard as above).
                activity.set_pane_workspace(pane_id, None)
                continue
        elif cur == ws_id:
            activity.set_pane_workspace(pane_id, None)
            continue  # already in this workspace's track → row consumed
        activity.set_pane_track(pid, ws_id)
        activity.set_pane_workspace(pane_id, None)
        written += 1
    return written
