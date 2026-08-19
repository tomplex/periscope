"""POST /api/editor/open — open a pane's worktree in the preferred editor."""

from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from periscope.editors import open_in_editor
from periscope.fs import _git_repo_root
from periscope.panes import list_windows
from periscope.store import get_settings

router = APIRouter()


class OpenEditorBody(BaseModel):
    # The @periscope_id, not session:index — indices renumber under
    # move-window (invariant 11), and the rail already keys on the pid.
    pid: str


@router.post("/api/editor/open")
def editor_open(body: OpenEditorBody):
    editor = (get_settings().get("editor") or "").strip()
    if not editor:
        raise HTTPException(400, "no preferred editor set — pick one in Settings")

    win = next(
        (w for w in list_windows() if (w.get("pid_raw") or "") == body.pid), None
    )
    if win is None:
        raise HTTPException(404, f"no live pane with id {body.pid}")

    cwd = win.get("cwd") or ""
    if not cwd:
        raise HTTPException(400, "pane has no cwd")
    # Always the worktree root, never the pane's cwd: editors treat the opened
    # folder as the project, so a pane sitting in a subdirectory would get a
    # fragment of the tree with no useful git integration. _git_repo_root walks
    # up for `.git` — a file in a linked worktree, a dir in the main checkout,
    # and .exists() covers both.
    root = _git_repo_root(Path(cwd))
    if root is None:
        raise HTTPException(400, f"pane is not inside a git repo: {cwd}")

    try:
        open_in_editor(editor, str(root))
    except ValueError as e:
        raise HTTPException(500, str(e)) from e
    return {"ok": True, "path": str(root), "editor": editor}
