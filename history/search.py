"""FTS5 search over the indexed history."""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from .db import connect

log = logging.getLogger(__name__)


def _build_fts_query(query: str) -> str:
    """Convert a user query into an FTS5 MATCH expression.

    FTS5's default tokenizer handles bare words fine. We escape double quotes
    to avoid syntax errors, and split on whitespace into terms ANDed together.
    """
    cleaned = query.replace('"', '""').strip()
    if not cleaned:
        return ""
    # Wrap each whitespace-separated term as a quoted phrase so apostrophes/etc.
    # don't blow up FTS5's tokenizer. AND-join.
    terms = [f'"{t}"' for t in re.split(r"\s+", cleaned) if t]
    return " AND ".join(terms) if terms else ""


def search(query: str, *,
           db_path: Path | str | None = None,
           project: str | None = None,
           branch: str | None = None,
           since: int | None = None,
           until: int | None = None,
           include_trivial: bool = False,
           rerank: bool = False,
           limit: int = 50) -> list[dict]:
    """FTS5-ranked search across indexed sessions.

    Returns a list of result dicts ordered by relevance, optionally re-ranked
    via Haiku when `rerank=True` and the API key is available.
    """
    fts_q = _build_fts_query(query)
    if not fts_q:
        return []

    conn = connect(db_path)
    try:
        sql = """
            SELECT s.*, bm25(sessions_fts) AS fts_rank
            FROM sessions_fts
            JOIN sessions s ON s.session_id = sessions_fts.session_id
            WHERE sessions_fts MATCH :q
        """
        params: dict = {"q": fts_q}
        if project:
            sql += " AND s.project_path = :project"
            params["project"] = project
        if branch:
            sql += " AND s.branch = :branch"
            params["branch"] = branch
        if since is not None:
            sql += " AND s.started_at >= :since"
            params["since"] = since
        if until is not None:
            sql += " AND s.started_at <= :until"
            params["until"] = until
        if not include_trivial:
            # Trivial heuristic-summary rows have summary_model IS NULL.
            sql += " AND s.summary_model IS NOT NULL"
        # FTS rerank request bounds candidates higher, then we trim.
        sql += " ORDER BY fts_rank ASC LIMIT :limit"
        params["limit"] = min(max(limit, 1), 20 if rerank else 200)
        rows = [dict(r) for r in conn.execute(sql, params)]
    finally:
        conn.close()

    if rerank and len(rows) > 1:
        rows = _rerank(rows, query)

    # Normalize for the API surface: parse JSON columns, drop verbose blobs.
    out = []
    for i, r in enumerate(rows[:limit]):
        out.append({
            "session_id": r["session_id"],
            "jsonl_path": r["jsonl_path"],
            "project_path": r["project_path"],
            "branch": r["branch"],
            "started_at": r["started_at"],
            "duration_s": r["duration_s"],
            "user_msg_count": r["user_msg_count"],
            "summary": r["summary"],
            "tags": (r["tags"] or "").split(",") if r["tags"] else [],
            "first_user_msg": r["first_user_msg"],
            "files_touched": json.loads(r["files_touched"]) if r["files_touched"] else [],
            "rank": i + 1,
            "rerank_reason": r.get("rerank_reason"),
        })
    return out


def _rerank(rows: list[dict], query: str) -> list[dict]:
    """Send candidate rows + the query to Haiku for semantic re-ranking.
    Returns rows reordered, each annotated with `rerank_reason`. If the
    Haiku call fails for any reason, the original FTS order is preserved
    (and a one-line log warning is emitted)."""
    try:
        from .indexer import get_anthropic_client
        client = get_anthropic_client()
    except Exception as e:
        log.warning("rerank: skipping (no client): %s", e)
        return rows
    candidates = [{
        "session_id": r["session_id"],
        "summary": r.get("summary") or r.get("first_user_msg") or "",
        "tags": r.get("tags") or "",
        "project": r.get("project_path") or "",
        "first_user_msg": r.get("first_user_msg") or "",
    } for r in rows]
    try:
        msg = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=1024,
            system=[{
                "type": "text",
                "text": (
                    "You re-rank candidate Claude Code session summaries by their relevance "
                    "to the user's query. Always call rank_search_results."
                ),
                "cache_control": {"type": "ephemeral"},
            }],
            tools=[RERANK_TOOL],
            tool_choice={"type": "tool", "name": RERANK_TOOL["name"]},
            messages=[{
                "role": "user",
                "content": (
                    f"QUERY: {query}\n\n"
                    f"CANDIDATES:\n{json.dumps(candidates, indent=2)}\n\n"
                    "Call rank_search_results."
                ),
            }],
        )
        for block in getattr(msg, "content", []):
            if getattr(block, "type", None) == "tool_use" and getattr(block, "name", None) == RERANK_TOOL["name"]:
                data = getattr(block, "input", None) or {}
                ranked = data.get("results") or []
                by_id = {r["session_id"]: r for r in rows}
                reordered = []
                for entry in ranked:
                    sid = entry.get("session_id")
                    if sid in by_id:
                        row = dict(by_id[sid])
                        row["rerank_reason"] = entry.get("reason")
                        reordered.append(row)
                # Append any candidates the model omitted at the end
                seen = {r["session_id"] for r in reordered}
                for r in rows:
                    if r["session_id"] not in seen:
                        reordered.append(r)
                return reordered
    except Exception as e:
        log.warning("rerank: model call failed (%s) — preserving FTS order", e)
    return rows


RERANK_TOOL = {
    "name": "rank_search_results",
    "description": "Re-rank candidate Claude Code session summaries by relevance to a query.",
    "input_schema": {
        "type": "object",
        "properties": {
            "results": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "session_id": {"type": "string"},
                        "reason": {"type": "string",
                                    "description": "One sentence explaining the match."},
                        "score": {"type": "number",
                                   "description": "0.0 (irrelevant) to 1.0 (perfect match)."},
                    },
                    "required": ["session_id", "reason", "score"],
                },
            },
        },
        "required": ["results"],
    },
}
