"""GET /api/fs/read — read a file relative to a pane's cwd, with safe-path gating.
GET /api/fs/render/{token}/{path} — stream a file's raw bytes for in-page rendering.
POST /api/fs/open?action=reveal — macOS reveal-in-Finder.

All three share the tmux-resolving wrappers in periscope.fs."""
import base64
import binascii
import mimetypes
import os

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

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

# Raster/vector formats the browser renders natively. These bypass the
# UTF-8 text read (which 415s on binary) and are shown via /api/fs/render.
# SVG is text but treated as an image so it displays rendered, not as XML.
_IMAGE_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp",
    ".svg", ".bmp", ".ico", ".avif",
}


# Render cap: high enough for typical bundled JS / hero images that pages
# pull in via <script src> / <img src>. The 1MB safe_read cap is too tight
# once we're serving a page's whole sub-resource tree.
_RENDER_MAX_BYTES = 50_000_000


def _language_for(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    return _LANGUAGE_BY_EXT.get(ext, "plain")


def _decode_pane_token(token: str) -> tuple[str, int]:
    """Decode base64url(b"<session>:<index>") → (session, index).

    Pane targets travel as a path segment so the browser resolves
    sibling-asset URLs against the file's directory (see fs_render). A
    raw "session:index" can't go in a path segment because session names
    contain '/' (invariant #6); base64url sidesteps that without giving
    up the path-based prefix relative URLs need.
    """
    # base64url drops trailing '=' padding — re-add to satisfy decoder.
    padded = token + "=" * (-len(token) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        raise HTTPException(status_code=400, detail="invalid pane token") from None
    # Split on the LAST colon — session names may contain colons too.
    session, sep, index_str = raw.rpartition(":")
    if not sep or not session or not index_str.isdigit():
        raise HTTPException(status_code=400, detail="invalid pane token")
    return session, int(index_str)


@router.get("/api/fs/read")
def fs_read(session: str, index: int, path: str):
    target = f"{session}:{index}"
    # Images: resolve the path (safe-root gated) but don't read the bytes —
    # they're not UTF-8 and the client streams them via /api/fs/render.
    if os.path.splitext(path)[1].lower() in _IMAGE_EXTS:
        resolved = fs.safe_resolve_for_pane(target, path)
        return {"path": str(resolved), "content": None, "language": "image"}
    resolved, content = fs.safe_read_for_pane(target, path)
    return {"path": resolved, "content": content, "language": _language_for(resolved)}


@router.get("/api/fs/stat")
def fs_stat(session: str, index: int, path: str):
    """Cheap freshness probe for an open preview tab: `{mtime, size}`.

    Separate from fs_read because it is POLLED. Re-reading a file body every
    couple of seconds to discover it is unchanged is the wasteful shape; one
    os.stat is not. Same safe-root gating as every other fs route — resolution
    goes through safe_resolve_for_pane, so this is not a path-disclosure hole.

    mtime is a float (st_mtime), not an int: editors and generators routinely
    rewrite a file twice inside one second, and truncating to seconds would
    silently drop the second write — the exact case an auto-refresh exists to
    catch.
    """
    resolved = fs.safe_resolve_for_pane(f"{session}:{index}", path)
    try:
        st = resolved.stat()
    except OSError as e:
        # Deleted between open and poll (a regenerated file mid-rewrite is the
        # common case). 404 so the client holds its last good content.
        raise HTTPException(status_code=404, detail=str(e)) from None
    return {"path": str(resolved), "mtime": st.st_mtime, "size": st.st_size}


@router.get("/api/fs/render/{token}/{file_path:path}")
def fs_render(token: str, file_path: str):
    """Stream a file's raw bytes with a Content-Type the browser will render.

    Used by the HTML preview iframe. The pane id is encoded in `token`
    rather than a query string so the iframe URL's path prefix matches
    the file's directory — that's what lets the browser resolve relative
    `<img>`/`<link>`/`<script>` references (which strip the query) into
    URLs that also land on this endpoint, scoped to the same pane.
    """
    session, index = _decode_pane_token(token)
    # FastAPI's :path converter strips the leading '/'; restore it so
    # safe_resolve treats it as absolute.
    abs_path = "/" + file_path if not file_path.startswith("/") else file_path
    resolved = fs.safe_resolve_for_pane(f"{session}:{index}", abs_path)
    size = resolved.stat().st_size
    if size > _RENDER_MAX_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"file too large ({size} > {_RENDER_MAX_BYTES} bytes)",
        )
    media_type, _ = mimetypes.guess_type(str(resolved))
    if media_type is None:
        media_type = "application/octet-stream"
    return FileResponse(str(resolved), media_type=media_type)


@router.post("/api/fs/open")
def fs_open(session: str, index: int, path: str, action: str = "reveal"):
    if action != "reveal":
        raise HTTPException(status_code=400, detail=f"unknown action: {action}")
    target = f"{session}:{index}"
    fs.safe_reveal_for_pane(target, path)
    return {"ok": True}
