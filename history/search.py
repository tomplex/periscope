"""FTS5 search over the indexed history."""
from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path

from .db import connect

log = logging.getLogger(__name__)

# Periscope's resume guardrails refuse if the JSONL has been written within
# the last `LIVE_MTIME_S` seconds — the session likely has an active
# appender. The search result mirrors that check up-front so the UI can
# render the disabled state without round-tripping.
LIVE_MTIME_S = 60


def _build_fts_query(query: str) -> str:
    """Convert a user query into an FTS5 MATCH expression.

    Each whitespace-separated token becomes a `prefix*` term so partial typing
    matches the full token — "meet" finds "meetingstech", "lookup" finds
    "lookup-api" (porter unicode61 splits on the hyphen). All FTS5 special
    chars (`"`, `:`, `(`, `)`, `-`, etc.) are scrubbed to spaces before
    tokenizing — they'd otherwise either break syntax or be parsed as NEAR /
    NOT / column operators.
    """
    # Replace anything that isn't a word char or whitespace with a space.
    # Hyphens get split too — "lookup-api" → "lookup api" → ("lookup*" AND "api*").
    cleaned = re.sub(r"[^\w\s]", " ", query, flags=re.UNICODE).strip()
    if not cleaned:
        return ""
    # Lowercase tokens to defang FTS5's keyword parser — uppercase AND / OR /
    # NOT / NEAR are operators; lowercased equivalents are content. FTS5's
    # default unicode61 tokenizer lowercases the index too, so this is
    # equivalence-preserving for actual matches.
    terms = [f"{t.lower()}*" for t in re.split(r"\s+", cleaned) if t]
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

    return _normalize_rows(rows[:limit])


def _normalize_rows(rows: list[dict]) -> list[dict]:
    """Shape DB rows into the API surface: parse JSON columns, drop verbose
    blobs, compute `is_live`. `is_resuming` belongs to periscope's in-process
    `_resuming` dict and is added at the API route layer — keep this
    function pure (no periscope coupling)."""
    now = time.time()
    out = []
    for i, r in enumerate(rows):
        jsonl_path = r["jsonl_path"] or ""
        is_live = False
        if jsonl_path:
            try:
                is_live = (now - os.path.getmtime(jsonl_path)) < LIVE_MTIME_S
            except OSError:
                is_live = False
        out.append({
            "session_id": r["session_id"],
            "jsonl_path": jsonl_path,
            "project_path": r["project_path"],
            "branch": r["branch"],
            "started_at": r["started_at"],
            "ended_at": r["ended_at"],
            "duration_s": r["duration_s"],
            "user_msg_count": r["user_msg_count"],
            "asst_msg_count": r["asst_msg_count"],
            "tool_use_count": r["tool_use_count"],
            "was_interrupted": bool(r["was_interrupted"]),
            "ended_cleanly": bool(r["ended_cleanly"]),
            "summary": r["summary"],
            "summary_model": r["summary_model"],
            "tags": (r["tags"] or "").split(",") if r["tags"] else [],
            "first_user_msg": r["first_user_msg"],
            "last_user_msg": r["last_user_msg"],
            "files_touched": json.loads(r["files_touched"]) if r["files_touched"] else [],
            "notable_cmds": json.loads(r["notable_cmds"]) if r["notable_cmds"] else [],
            "tool_use_counts": json.loads(r["tool_use_counts"]) if r["tool_use_counts"] else {},
            "trivial": r["summary_model"] is None,
            "is_live": is_live,
            "rank": i + 1,
            "rerank_reason": r.get("rerank_reason"),
            "rerank_score": r.get("rerank_score"),
        })
    return out


def stats(*, db_path: Path | str | None = None) -> dict:
    """Index summary for the route header + footer: total / summarized /
    heuristic counts, distinct projects, last full scan, DB file size.
    Single round trip; not cached (each call ~1-3ms on a populated index)."""
    from .db import get_meta, DEFAULT_DB_PATH
    path = Path(db_path) if db_path else DEFAULT_DB_PATH
    conn = connect(db_path)
    try:
        total = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        summarized = conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE summary_model IS NOT NULL"
        ).fetchone()[0]
        heuristic = total - summarized
        projects = conn.execute(
            "SELECT COUNT(DISTINCT project_path) FROM sessions"
        ).fetchone()[0]
        last_scan = get_meta(conn, "last_full_scan_at")
        haiku_model = get_meta(conn, "haiku_model")
    finally:
        conn.close()
    try:
        db_bytes = path.stat().st_size
    except OSError:
        db_bytes = 0
    return {
        "total": total,
        "summarized": summarized,
        "heuristic": heuristic,
        "projects": projects,
        "last_scan_at": int(last_scan) if last_scan else None,
        "haiku_model": haiku_model,
        "db_bytes": db_bytes,
    }


def recent(*,
           db_path: Path | str | None = None,
           project: str | None = None,
           since: int | None = None,
           include_trivial: bool = False,
           limit: int = 50) -> list[dict]:
    """List recent sessions by `started_at desc` — what the UI shows on an
    empty query. Same filters as `search()`, no FTS rank."""
    conn = connect(db_path)
    try:
        sql = "SELECT * FROM sessions WHERE 1=1"
        params: dict = {}
        if project:
            sql += " AND project_path = :project"
            params["project"] = project
        if since is not None:
            sql += " AND started_at >= :since"
            params["since"] = since
        if not include_trivial:
            sql += " AND summary_model IS NOT NULL"
        sql += " ORDER BY started_at DESC LIMIT :limit"
        params["limit"] = max(1, min(limit, 200))
        rows = [dict(r) for r in conn.execute(sql, params)]
    finally:
        conn.close()
    return _normalize_rows(rows)


def messages_from_jsonl(jsonl_path: str) -> list[dict]:
    """Parse a Claude JSONL into structured turn messages (full file, two-pass).

    User/assistant turns in JSONL order. Assistant tool_use blocks are
    back-patched with their paired tool_result content (tool_use.id ==
    tool_result.tool_use_id from later user-role events); unpaired (in-flight)
    tool_uses get result=None. Each message carries `uuid` (the client's stable
    reconciliation key). compact_boundary system events become divider markers.

    Filters read `ev.raw` — isMeta/isSidechain/subtype are NOT lifted onto the
    Event by `_classify` (history/jsonl.py); reaching for ev.is_meta would be an
    AttributeError. Skip the event if any of:
      - ev.raw.get("isMeta") is True
      - ev.raw.get("isSidechain") is True
      - ev.type == "system" and ev.raw.get("subtype") != "compact_boundary"
      - the event carries no user_text, assistant_text, tool_uses, or tool_results
    """
    from .jsonl import parse_jsonl

    events = list(parse_jsonl(jsonl_path))

    # Pass 1: tool_use_id -> result content (full-file; a result can pair with
    # a tool_use emitted in an earlier event).
    results: dict[str, str] = {}
    for ev in events:
        for tr in ev.tool_results:
            tuid = tr.get("tool_use_id")
            if tuid is not None:
                results[tuid] = tr.get("content", "")

    # Pass 2: emit messages.
    messages: list[dict] = []
    for ev in events:
        raw = ev.raw
        if raw.get("isMeta") is True or raw.get("isSidechain") is True:
            continue
        if ev.type == "system":
            if raw.get("subtype") == "compact_boundary":
                messages.append({
                    "role": "system",
                    "kind": "compact",
                    "uuid": ev.uuid,
                    "ts_ms": ev.ts_ms,
                })
            continue
        if not (ev.user_text or ev.assistant_text or ev.tool_uses or ev.tool_results):
            continue
        if ev.type == "user" and ev.user_text:
            messages.append({
                "role": "user",
                "uuid": ev.uuid,
                "ts_ms": ev.ts_ms,
                "text": ev.user_text,
            })
        elif ev.type == "assistant":
            tool_uses = [{
                "id": tu.get("id"),
                "name": tu.get("name"),
                "input": tu.get("input") or {},
                "result": results.get(tu.get("id")),
            } for tu in ev.tool_uses]
            messages.append({
                "role": "assistant",
                "uuid": ev.uuid,
                "ts_ms": ev.ts_ms,
                "text": ev.assistant_text or "",
                "tool_uses": tool_uses,
            })
    return messages


def get_session(session_id: str, *,
                db_path: Path | str | None = None) -> dict | None:
    """Return the full session row + parsed conversation messages.

    Reads the JSONL on demand and shapes it for a UI detail view. Returns
    None if no row matches session_id, or if the JSONL file is missing on
    disk (the row exists but is orphaned; `clean` would remove it)."""
    import os

    conn = connect(db_path)
    try:
        row = conn.execute("SELECT * FROM sessions WHERE session_id = ?",
                           (session_id,)).fetchone()
        if row is None:
            return None
        record = dict(row)
    finally:
        conn.close()

    jsonl_path = record.get("jsonl_path") or ""
    jsonl_missing = not os.path.isfile(jsonl_path)
    messages = [] if jsonl_missing else messages_from_jsonl(jsonl_path)

    # Normalize JSON columns + decode tags
    return {
        "session_id": record["session_id"],
        "jsonl_path": record["jsonl_path"],
        "project_path": record["project_path"],
        "branch": record["branch"],
        "started_at": record["started_at"],
        "ended_at": record["ended_at"],
        "duration_s": record["duration_s"],
        "user_msg_count": record["user_msg_count"],
        "asst_msg_count": record["asst_msg_count"],
        "tool_use_count": record["tool_use_count"],
        "was_interrupted": bool(record["was_interrupted"]),
        "ended_cleanly": bool(record["ended_cleanly"]),
        "summary": record["summary"],
        "tags": (record["tags"] or "").split(",") if record["tags"] else [],
        "first_user_msg": record["first_user_msg"],
        "last_user_msg": record["last_user_msg"],
        "final_assistant_msg": record["final_assistant_msg"],
        "files_touched": json.loads(record["files_touched"]) if record["files_touched"] else [],
        "notable_cmds": json.loads(record["notable_cmds"]) if record["notable_cmds"] else [],
        "tool_use_counts": json.loads(record["tool_use_counts"]) if record["tool_use_counts"] else {},
        "messages": messages,
        "jsonl_missing": jsonl_missing,
    }


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
                    "to the user's query. For each candidate, assign a score from 0.0 "
                    "(irrelevant) to 1.0 (perfect match). Always call rank_search_results."
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
                ranked = list(data.get("results") or [])
                # Haiku tends to preserve input order in the array; sort by
                # score descending so the per-item scores actually drive the
                # final ordering. Missing scores fall through to bottom.
                ranked.sort(key=lambda e: float(e.get("score") or 0.0), reverse=True)
                by_id = {r["session_id"]: r for r in rows}
                reordered = []
                for entry in ranked:
                    sid = entry.get("session_id")
                    if sid in by_id:
                        row = dict(by_id[sid])
                        row["rerank_reason"] = entry.get("reason")
                        row["rerank_score"] = entry.get("score")
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
