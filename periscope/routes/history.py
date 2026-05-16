"""History endpoints: /api/history/* search/session/stats + /history page.

The history index lives in a sibling top-level `history` package; we
import inside handlers to keep server startup independent of its
optional sqlite/embeddings dependencies.
"""

import time

from fastapi import APIRouter
from fastapi.responses import FileResponse, JSONResponse

from periscope.config import STATIC
from periscope.panes import _resuming

router = APIRouter()


@router.get("/api/history/search")
def history_search(
    q: str,
    project: str | None = None,
    branch: str | None = None,
    since: int | None = None,
    until: int | None = None,
    include_trivial: bool = False,
    rerank: bool = False,
    limit: int = 50,
):
    """FTS5-ranked search across the history index. Empty q falls back to
    `recent()` (newest-first) so the UI can populate before the user types."""
    import history
    started = time.time()
    if q and q.strip():
        results = history.search(
            q,
            project=project,
            branch=branch,
            since=since,
            until=until,
            include_trivial=include_trivial,
            rerank=rerank,
            limit=limit,
        )
    else:
        # branch / until aren't part of recent's filter set (the UI doesn't
        # surface them either); plumb through what the route accepts.
        results = history.recent(
            project=project,
            since=since,
            include_trivial=include_trivial,
            limit=limit,
        )
    # is_resuming belongs to periscope's in-process _resuming dict, not to
    # the history index — apply it here so the frontend can render guards.
    for r in results:
        r["is_resuming"] = r["session_id"] in _resuming
    return {
        "query": q,
        "rerank_used": rerank,
        "results": results,
        "took_ms": int((time.time() - started) * 1000),
    }


@router.get("/api/history/session/{session_id}")
def history_session(session_id: str):
    """Full session record + parsed conversation messages.

    Returns 404 if the session_id is not in the index. The `jsonl_missing`
    field is true if the row exists but the underlying JSONL has been
    deleted (search will hide it until `history clean` removes the row)."""
    from history.search import get_session
    data = get_session(session_id)
    if data is None:
        return JSONResponse({"ok": False, "error": "unknown session_id"}, status_code=404)
    data["is_resuming"] = session_id in _resuming
    return data


@router.get("/api/history/stats")
def history_stats():
    """Index summary for the /history route header + footer."""
    import history
    return history.stats()


@router.get("/history")
def history_page():
    """Serve the /history single-page route. The StaticFiles mount below
    can't resolve /history without a trailing slash + an index.html in a
    subdir; this explicit route keeps the URL clean."""
    return FileResponse(STATIC / "history.html")
