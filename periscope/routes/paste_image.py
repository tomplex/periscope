"""POST /api/paste-image — clipboard image → tmpfile → @path in pane.

xterm.js has no way to carry image bytes through to Claude Code, and tmux
has no image protocol either. So we shortcut: the browser intercepts a
paste event with an image in the clipboard, POSTs the bytes here, we
write them to /tmp, and bracketed-paste "@/tmp/foo.png " into the pane.
Claude Code resolves @-paths against the filesystem on submit.

Files are best-effort GC'd on each paste (anything older than an hour).
Same-machine only by construction — server binds 127.0.0.1.
"""

import time
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from periscope.panes import note_action, note_focus
from periscope.tmux import tmux

router = APIRouter()


_PASTE_IMG_DIR = Path("/tmp")
_PASTE_IMG_PREFIX = "periscope-paste-"
_PASTE_IMG_MAX_AGE_S = 3600.0
_PASTE_IMG_MAX_BYTES = 25 * 1024 * 1024
_EXT_BY_MIME = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/heic": "heic",
}


def _sweep_old_paste_images() -> None:
    cutoff = time.time() - _PASTE_IMG_MAX_AGE_S
    for p in _PASTE_IMG_DIR.glob(f"{_PASTE_IMG_PREFIX}*"):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
        except Exception:
            pass


@router.post("/api/paste-image")
async def paste_image(session: str, index: int, request: Request, deliver: bool = True):
    """Write a clipboard image to /tmp and return its @path. With deliver=True
    (the terminal's path) also bracketed-paste `@path ` straight into the pane.
    With deliver=False (the transcript composer) skip the pane paste — the caller
    splices the @path into the message it's composing instead."""
    target = f"{session}:{index}"
    body = await request.body()
    if not body:
        raise HTTPException(400, "empty body")
    if len(body) > _PASTE_IMG_MAX_BYTES:
        raise HTTPException(400, f"image too large ({len(body)} bytes)")
    mime = (request.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
    ext = _EXT_BY_MIME.get(mime, "png")
    _sweep_old_paste_images()
    path = _PASTE_IMG_DIR / f"{_PASTE_IMG_PREFIX}{uuid.uuid4().hex}.{ext}"
    path.write_bytes(body)
    if deliver:
        # Trailing space so Claude Code commits the @-reference (its file picker
        # closes on whitespace) and the user can keep typing immediately after.
        buf = f"wd-img-{uuid.uuid4().hex[:8]}"
        tmux("set-buffer", "-b", buf, f"@{path} ")
        tmux("paste-buffer", "-d", "-p", "-b", buf, "-t", target)
        note_focus(target)
        note_action(target)
    return {"ok": True, "path": str(path), "bytes": len(body)}
