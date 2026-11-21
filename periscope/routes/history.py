"""History endpoints: /api/history/* search/session/stats + /history page.

The history index lives in a sibling top-level `history` package; we
import inside handlers to keep server startup independent of its
optional sqlite/embeddings dependencies.
"""

import time

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from periscope import store
from periscope.config import STATIC
from periscope.git_pr import linear_ticket_fields
from periscope.narrator import is_placeholder_name
from periscope.turns import jsonl_by_session, resume_refusal

router = APIRouter()


def _panes_by_session() -> dict[str, dict]:
    """Each session id's state.json `windows` entry: the pane that last ran it.
    An entry outlives its pane for 30 days, and longer when it holds a link.
    Two entries can carry one sid (31 in prod on 2026-11-19); the newest
    sighting wins."""
    newest: dict[str, tuple[int, dict]] = {}
    for ann in store.all_windows().values():
        seen = ann.get("last_seen") or {}
        sid, ts = seen.get("sid"), seen.get("ts") or 0
        if sid and ts >= newest.get(sid, (-1, {}))[0]:
            newest[sid] = (ts, dict(ann))
    return {sid: ann for sid, (_ts, ann) in newest.items()}


def _annotate(rows: list[dict]) -> None:
    """Add what periscope knows and the history index does not: why resume
    would refuse the session (null when it would resume), and the name, PR
    and ticket of the pane that last ran it. A placeholder name is null, so
    the page titles the row with the summary."""
    panes = _panes_by_session()
    team_keys = store.linear_team_keys()
    transcripts = jsonl_by_session()
    for r in rows:
        sid = r["session_id"]
        refusal = resume_refusal(sid, transcripts.get(sid))
        r["resume_refusal"] = {"status": refusal[0], "detail": refusal[1]} if refusal else None
        pane = panes.get(sid) or {}
        name = (pane.get("last_seen") or {}).get("name")
        r["pane_name"] = None if is_placeholder_name(name) else name
        r["linked_pr"] = pane.get("linked_pr")
        r.update(linear_ticket_fields(pane, r.get("branch"), None, team_keys))


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
    `recent()` (newest last turn first) so the UI can populate before the
    user types. `limit` in the answer is the cap that applied: a rerank
    returns fewer rows than asked for."""
    import history
    from history.search import row_limit

    started = time.time()
    searching = bool(q and q.strip())
    if searching:
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
    _annotate(results)
    # A query that nothing summarized matches: how many unsummarized sessions
    # match it, so the empty state offers to include them only when one does.
    unsummarized_hidden = None
    if searching and not results and not include_trivial:
        unsummarized_hidden = history.count_unsummarized(q, project=project, branch=branch, since=since, until=until)
    return {
        "query": q,
        # A failed Haiku call keeps the FTS order and scores nothing.
        "rerank_used": any(r.get("rerank_score") is not None for r in results),
        "limit": row_limit(limit, rerank=rerank and searching),
        "results": results,
        "unsummarized_hidden": unsummarized_hidden,
        "took_ms": int((time.time() - started) * 1000),
    }


@router.get("/api/history/session/{session_id}")
def history_session(session_id: str):
    """Full session record + parsed conversation messages.

    Returns 404 if the session_id is not in the index. The `jsonl_missing`
    field is true if the row exists but its archived JSONL is gone, so
    there are no messages to show."""
    from history.search import get_session

    data = get_session(session_id)
    if data is None:
        raise HTTPException(404, "unknown session_id")
    return data


@router.get("/api/history/stats")
def history_stats():
    """Index summary for the /history route header and project filter."""
    import history

    return history.stats()


@router.get("/history")
def history_page():
    """Serve the /history single-page route. The StaticFiles mount below
    can't resolve /history without a trailing slash + an index.html in a
    subdir; this explicit route keeps the URL clean."""
    return FileResponse(STATIC / "history.html")
