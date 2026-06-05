"""/api/prefs/* — persistent UI prefs + per-window annotations + commands.

Thin route layer over periscope.store's typed accessors. Each mutation
goes through `set_window_fields` / `update_ui` / `add_command` / etc.,
which hold `_STATE_LOCK` internally and persist to state.json.
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from periscope.store import (
    add_command as store_add_command,
    delete_command as store_delete_command,
    get_commands,
    get_window,
    reorder_commands,
    set_window_fields,
    snapshot,
    update_command as store_update_command,
    update_ui,
)

router = APIRouter()


@router.get("/api/prefs")
def get_prefs():
    """Full state blob, for client boot."""
    return snapshot()


class UIPatch(BaseModel):
    session_order: list[str] | None = None
    collapsed_sessions: list[str] | None = None
    view: str | None = None  # "grid" | "stream" | "split"
    alerts_open: bool | None = None  # right-rail alerts feed visibility
    # Rail (split view) state — opaque dicts merged through update_ui's
    # generic merge. The schema is documented in
    # docs/superpowers/specs/2026-06-01-split-view-rail-design.md §Data model.
    repo_order: list[str] | None = None
    worktrees_by_repo: dict[str, list[str]] | None = None
    panes_by_worktree: dict[str, list[str]] | None = None
    rail_collapsed: dict[str, bool] | None = None
    last_selected: dict | None = None
    # Per-pane detail-mode toggle (split view): "terminal" | "transcript".
    # Keyed by periscope pid; entries persist until the pid is reaped.
    detail_mode_by_pid: dict[str, str] | None = None


@router.patch("/api/prefs/ui")
def patch_prefs_ui(body: UIPatch):
    """Merge partial UI prefs. Only fields present in the body get written."""
    patch = body.model_dump(exclude_none=True)
    # `view` is validated against a fixed enum to keep junk out of the file.
    if "view" in patch and patch["view"] not in ("grid", "stream", "split"):
        raise HTTPException(400, f"invalid view: {patch['view']!r}")
    if "detail_mode_by_pid" in patch:
        for pid, mode in patch["detail_mode_by_pid"].items():
            if mode not in ("terminal", "transcript"):
                raise HTTPException(400, f"invalid detail_mode for {pid!r}: {mode!r}")
    update_ui(patch)
    from periscope.store import get_ui
    return {"ok": True, "ui": get_ui()}


class WindowAnnotation(BaseModel):
    notes: str | None = None
    tags: list[str] | None = None
    pinned_files: list[str] | None = None


@router.put("/api/prefs/windows/{pid}")
def put_window_annotation(pid: str, body: WindowAnnotation):
    """Set/replace notes, tags, and pinned_files on a window. `last_seen`
    and other fields are left intact — only the annotation-shaped fields
    are managed via this endpoint."""
    if not pid or not pid.isalnum():
        raise HTTPException(400, "invalid pid")
    patch = body.model_dump(exclude_none=True)
    # Coerce tags to a trimmed unique list, preserving order.
    if "tags" in patch:
        seen: set[str] = set()
        clean: list[str] = []
        for t in patch["tags"]:
            t = (t or "").strip()
            if t and t not in seen:
                seen.add(t)
                clean.append(t)
        patch["tags"] = clean
    # Same dedupe + trim for pinned_files. Paths are stored verbatim
    # (no path canonicalization — frontend pins exactly what filesTouched yields).
    if "pinned_files" in patch:
        seen_p: set[str] = set()
        clean_p: list[str] = []
        for p in patch["pinned_files"]:
            p = (p or "").strip()
            if p and p not in seen_p:
                seen_p.add(p)
                clean_p.append(p)
        patch["pinned_files"] = clean_p
    # Empty value → delete via the None-deletes semantics.
    updates: dict = {}
    if "notes" in patch:
        updates["notes"] = patch["notes"] if patch["notes"] != "" else None
    if "tags" in patch:
        updates["tags"] = patch["tags"] if patch["tags"] != [] else None
    if "pinned_files" in patch:
        updates["pinned_files"] = patch["pinned_files"] if patch["pinned_files"] != [] else None
    set_window_fields(pid, **updates)
    entry = get_window(pid)
    return {"ok": True, "pid": pid, "annotation": {
        "notes": entry.get("notes"),
        "tags": entry.get("tags") or [],
        "pinned_files": entry.get("pinned_files") or [],
    }}


@router.delete("/api/prefs/windows/{pid}")
def delete_window_annotation(pid: str):
    """Remove notes + tags + pinned_files. last_seen is preserved (rebind hint)."""
    if not pid or not pid.isalnum():
        raise HTTPException(400, "invalid pid")
    set_window_fields(pid, notes=None, tags=None, pinned_files=None)
    return {"ok": True, "pid": pid}


class Command(BaseModel):
    label: str
    exec: str = ""


class CommandPatch(BaseModel):
    """For PUT: both fields are optional. Sending only `label` renames
    without clobbering `exec`; sending only `exec` updates the command
    without renaming. The frontend always sends both, but keeping them
    optional protects against curl-from-shell footguns."""
    label: str | None = None
    exec: str | None = None


@router.post("/api/prefs/commands")
def add_command(body: Command):
    label = body.label.strip()
    if not label:
        raise HTTPException(400, "empty label")
    # Single-user dashboard: TOCTOU on duplicate detection is fine.
    if any(c["label"] == label for c in get_commands()):
        raise HTTPException(409, f"duplicate label: {label!r}")
    store_add_command(label, body.exec or "")
    return {"ok": True, "commands": get_commands()}


@router.put("/api/prefs/commands/{label}")
def update_command(label: str, body: CommandPatch):
    new_label = (body.label or label).strip() if body.label is not None else label
    if not new_label:
        raise HTTPException(400, "empty label")
    existing = get_commands()
    if not any(c["label"] == label for c in existing):
        raise HTTPException(404, f"unknown label: {label!r}")
    if new_label != label and any(c["label"] == new_label for c in existing):
        raise HTTPException(409, f"duplicate label: {new_label!r}")
    store_update_command(
        label,
        exec_cmd=body.exec if body.exec is not None else None,
        new_label=new_label if new_label != label else None,
    )
    return {"ok": True, "commands": get_commands()}


@router.delete("/api/prefs/commands/{label}")
def delete_command(label: str):
    if not store_delete_command(label):
        raise HTTPException(404, f"unknown label: {label!r}")
    return {"ok": True, "commands": get_commands()}


class CommandsReorder(BaseModel):
    labels: list[str]


@router.put("/api/prefs/commands")
def reorder_commands_route(body: CommandsReorder):
    """Reorder the commands list to match `labels`. Unknown labels are
    ignored; missing labels stay at the end."""
    reorder_commands(body.labels)
    return {"ok": True, "commands": get_commands()}
