"""GET /api/fs/read — read a file relative to a pane's cwd, with safe-path gating.
POST /api/fs/open?action=reveal — macOS reveal-in-Finder.

Both share the tmux-resolving wrappers in periscope.fs."""
import os

from fastapi import APIRouter, HTTPException

from periscope import fs

router = APIRouter()


_LANGUAGE_BY_EXT = {
    ".py": "python",
    ".js": "javascript", ".jsx": "javascript",
    ".ts": "javascript", ".tsx": "javascript",
    ".json": "json",
    ".md": "markdown",
    ".html": "html", ".htm": "html",
    ".css": "css",
    ".rs": "rust",
    ".go": "go",
    ".toml": "toml",
    ".yaml": "yaml", ".yml": "yaml",
    ".sh": "shell",
    ".sql": "sql",
}


def _language_for(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    return _LANGUAGE_BY_EXT.get(ext, "plain")


@router.get("/api/fs/read")
def fs_read(session: str, index: int, path: str):
    target = f"{session}:{index}"
    resolved, content = fs.safe_read_for_pane(target, path)
    return {"path": resolved, "content": content, "language": _language_for(resolved)}


@router.post("/api/fs/open")
def fs_open(session: str, index: int, path: str, action: str = "reveal"):
    if action != "reveal":
        raise HTTPException(status_code=400, detail=f"unknown action: {action}")
    target = f"{session}:{index}"
    fs.safe_reveal_for_pane(target, path)
    return {"ok": True}
