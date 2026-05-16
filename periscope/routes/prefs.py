"""/api/prefs/* — persistent UI prefs + per-window annotations + commands.

Every mutation writes the full state dict back to disk through
`_write_state`. Reads return the in-memory cache without locking
(mutations refresh the cache atomically, so a torn read isn't possible).
"""

from fastapi import APIRouter
from pydantic import BaseModel

from periscope.store import _STATE, _STATE_LOCK, _write_state

router = APIRouter()


@router.get("/api/prefs")
def get_prefs():
    """Full state blob, for client boot. Reads from the in-memory cache —
    every mutation refreshes the cache atomically, so this is safe to call
    without the lock."""
    return _STATE


class UIPatch(BaseModel):
    session_order: list[str] | None = None
    collapsed_sessions: list[str] | None = None
    view: str | None = None  # "grid" or "stream"


@router.patch("/api/prefs/ui")
async def patch_prefs_ui(body: UIPatch):
    """Merge partial UI prefs. Only fields present in the body get written."""
    patch = body.model_dump(exclude_none=True)
    # `view` is validated against a fixed enum to keep junk out of the file.
    if "view" in patch and patch["view"] not in ("grid", "stream"):
        return {"ok": False, "error": f"invalid view: {patch['view']!r}"}
    with _STATE_LOCK:
        _STATE["ui"].update(patch)
        _write_state(_STATE)
    return {"ok": True, "ui": _STATE["ui"]}


class WindowAnnotation(BaseModel):
    notes: str | None = None
    tags: list[str] | None = None


@router.put("/api/prefs/windows/{pid}")
async def put_window_annotation(pid: str, body: WindowAnnotation):
    """Set/replace the annotation fields on a window. `last_seen` is left
    intact — only notes/tags are managed via this endpoint."""
    if not pid or not pid.isalnum():
        return {"ok": False, "error": "invalid pid"}
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
    with _STATE_LOCK:
        entry = _STATE["windows"].setdefault(pid, {})
        for k in ("notes", "tags"):
            if k in patch:
                entry[k] = patch[k]
        # Drop empty notes / empty tag list to keep the file tidy.
        if entry.get("notes") == "":
            entry.pop("notes", None)
        if entry.get("tags") == []:
            entry.pop("tags", None)
        _write_state(_STATE)
    return {"ok": True, "pid": pid, "annotation": {
        "notes": entry.get("notes"),
        "tags": entry.get("tags") or [],
    }}


@router.delete("/api/prefs/windows/{pid}")
async def delete_window_annotation(pid: str):
    """Remove notes + tags. last_seen is preserved (it's the rebind hint)."""
    if not pid or not pid.isalnum():
        return {"ok": False, "error": "invalid pid"}
    with _STATE_LOCK:
        entry = _STATE["windows"].get(pid)
        if entry:
            entry.pop("notes", None)
            entry.pop("tags", None)
            _write_state(_STATE)
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
async def add_command(body: Command):
    label = body.label.strip()
    if not label:
        return {"ok": False, "error": "empty label"}
    with _STATE_LOCK:
        if any(c["label"] == label for c in _STATE["commands"]):
            return {"ok": False, "error": f"duplicate label: {label!r}"}
        _STATE["commands"].append({"label": label, "exec": body.exec or ""})
        _write_state(_STATE)
    return {"ok": True, "commands": _STATE["commands"]}


@router.put("/api/prefs/commands/{label}")
async def update_command(label: str, body: CommandPatch):
    with _STATE_LOCK:
        for c in _STATE["commands"]:
            if c["label"] == label:
                new_label = (body.label or label).strip()
                if not new_label:
                    return {"ok": False, "error": "empty label"}
                if new_label != label and any(
                    other["label"] == new_label for other in _STATE["commands"] if other is not c
                ):
                    return {"ok": False, "error": f"duplicate label: {new_label!r}"}
                c["label"] = new_label
                if body.exec is not None:
                    c["exec"] = body.exec
                _write_state(_STATE)
                return {"ok": True, "commands": _STATE["commands"]}
    return {"ok": False, "error": f"unknown label: {label!r}"}


@router.delete("/api/prefs/commands/{label}")
async def delete_command(label: str):
    with _STATE_LOCK:
        before = len(_STATE["commands"])
        _STATE["commands"] = [c for c in _STATE["commands"] if c["label"] != label]
        if len(_STATE["commands"]) == before:
            return {"ok": False, "error": f"unknown label: {label!r}"}
        _write_state(_STATE)
    return {"ok": True, "commands": _STATE["commands"]}


class CommandsReorder(BaseModel):
    labels: list[str]


@router.put("/api/prefs/commands")
async def reorder_commands(body: CommandsReorder):
    """Reorder the commands list to match `labels`. Unknown labels are
    ignored; missing labels stay in place at the end."""
    with _STATE_LOCK:
        by_label = {c["label"]: c for c in _STATE["commands"]}
        ordered = [by_label[l] for l in body.labels if l in by_label]
        leftover = [c for c in _STATE["commands"] if c["label"] not in {l for l in body.labels if l in by_label}]
        _STATE["commands"] = ordered + leftover
        _write_state(_STATE)
    return {"ok": True, "commands": _STATE["commands"]}
