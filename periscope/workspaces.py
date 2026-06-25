"""Workspaces: goal-scoped, persistent top-level rail groups.

A workspace is a named entity in state['workspaces'] (parallel to projects).
Membership is NOT stored here — it is a per-tab tag in the `pane_workspaces`
table (periscope.db, owned by activity.py), keyed on tmux pane_id. This module
owns only the entity (CRUD) and the per-window resolve.
"""
from __future__ import annotations

import re
import time
from typing import TypedDict, cast

from periscope import activity, store


class Workspace(TypedDict, total=False):
    id: str
    name: str
    base_repo: str | None
    base_worktree: str | None
    created_at: int
    archived_at: int | None


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return s or "workspace"


def create_workspace(*, name: str, base_repo: str | None = None,
                     base_worktree: str | None = None) -> Workspace:
    base = f"ws_{_slug(name)}"
    with store._STATE_LOCK:
        existing = store._STATE["workspaces"]
        wid = base
        n = 2
        while wid in existing:
            wid = f"{base}-{n}"
            n += 1
        row: Workspace = {
            "id": wid,
            "name": name,
            "base_repo": base_repo,
            "base_worktree": base_worktree,
            "created_at": int(time.time()),
            "archived_at": None,
        }
        existing[wid] = row
        store._write_state(store._STATE)
        return cast(Workspace, dict(row))


def get_workspace(wid: str) -> Workspace:
    return cast(Workspace, dict(store._STATE["workspaces"].get(wid, {})))


def all_workspaces() -> dict[str, Workspace]:
    return {k: cast(Workspace, dict(v)) for k, v in store._STATE["workspaces"].items()}


def update_workspace(wid: str, **fields) -> bool:
    # Blanket merge, mirroring projects.update_project — the route's PatchBody
    # constrains which fields reach here.
    with store._STATE_LOCK:
        row = store._STATE["workspaces"].get(wid)
        if row is None:
            return False
        row.update(fields)
        store._write_state(store._STATE)
        return True


def archive_workspace(wid: str) -> bool:
    with store._STATE_LOCK:
        row = store._STATE["workspaces"].get(wid)
        if row is None:
            return False
        row["archived_at"] = int(time.time())
        store._write_state(store._STATE)
        return True


def resolve_workspace_for_window(w: dict) -> str | None:
    """The workspace id a window is tagged into, or None.

    Looks up the per-tab tag by tmux pane_id, then validates the workspace
    still exists and is not archived (a stale tag for a deleted/archived
    workspace folds back to normal repo sorting)."""
    pane_id = w.get("pane_id")
    if not pane_id:
        return None
    wid = activity.get_pane_workspace(pane_id)
    if not wid:
        return None
    row = store._STATE["workspaces"].get(wid)
    if row is None or row.get("archived_at"):
        return None
    return wid
