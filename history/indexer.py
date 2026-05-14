"""End-to-end indexing of one JSONL session.

Pipeline:
  parse JSONL -> extract mechanical record -> compute summary_input_hash
  -> decide whether to call Haiku -> UPSERT sessions + sessions_fts in one txn.

Liveness guard: skip if last event < 5 min ago OR mtime < 60 s ago.
Idempotency: if a row already exists with matching hash + model, reuse summary.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from .db import MECHANICAL_VERSION, connect, get_meta
from .extract import (
    SessionRecord, compute_summary_input_hash, extract_record,
    heuristic_summary, is_trivial,
)
from .jsonl import parse_jsonl
from .summarize import SummaryResult, call_summarizer

log = logging.getLogger(__name__)

LIVE_LAST_EVENT_S = 5 * 60      # skip if last event newer than 5 min ago
LIVE_MTIME_S = 60               # skip if jsonl mtime newer than 60 s ago

_anthropic_client = None
_anthropic_lock = threading.Lock()


def get_anthropic_client():
    """Lazy-init the Anthropic client. Raises RuntimeError if API key missing."""
    global _anthropic_client
    with _anthropic_lock:
        if _anthropic_client is None:
            if not os.environ.get("ANTHROPIC_API_KEY"):
                raise RuntimeError("ANTHROPIC_API_KEY is not set")
            from anthropic import Anthropic
            _anthropic_client = Anthropic()
    return _anthropic_client


def _row_needs_resummary(row, *, new_hash: str, target_model: str) -> bool:
    if row is None:
        return True
    if row["summary"] is None:
        return True
    if row["summary_input_hash"] != new_hash:
        return True
    if row["summary_model"] != target_model:
        return True
    return False


def _is_live(jsonl_path: Path, source_mtime: int, last_event_ts: int) -> bool:
    now = time.time()
    if last_event_ts and now - last_event_ts < LIVE_LAST_EVENT_S:
        return True
    if source_mtime and now - source_mtime < LIVE_MTIME_S:
        return True
    return False


def _upsert(conn: sqlite3.Connection, rec: SessionRecord, *,
             summary: str | None, tags: list[str] | None,
             summary_input_hash: str | None, summary_model: str | None) -> None:
    """One transaction: UPSERT sessions + refresh sessions_fts."""
    conn.execute(
        """
        INSERT INTO sessions (
          session_id, jsonl_path, project_path, branch,
          started_at, ended_at, duration_s,
          user_msg_count, asst_msg_count, tool_use_count,
          was_interrupted, ended_cleanly,
          summary, tags, summary_input_hash, summary_model,
          first_user_msg, last_user_msg, final_assistant_msg,
          files_touched, notable_cmds, tool_use_counts,
          indexed_at, mechanical_version, source_mtime, source_size
        )
        VALUES (
          ?, ?, ?, ?,
          ?, ?, ?,
          ?, ?, ?,
          ?, ?,
          ?, ?, ?, ?,
          ?, ?, ?,
          ?, ?, ?,
          ?, ?, ?, ?
        )
        ON CONFLICT(session_id) DO UPDATE SET
          jsonl_path = excluded.jsonl_path,
          project_path = excluded.project_path,
          branch = excluded.branch,
          started_at = excluded.started_at,
          ended_at = excluded.ended_at,
          duration_s = excluded.duration_s,
          user_msg_count = excluded.user_msg_count,
          asst_msg_count = excluded.asst_msg_count,
          tool_use_count = excluded.tool_use_count,
          was_interrupted = excluded.was_interrupted,
          ended_cleanly = excluded.ended_cleanly,
          summary = excluded.summary,
          tags = excluded.tags,
          summary_input_hash = excluded.summary_input_hash,
          summary_model = excluded.summary_model,
          first_user_msg = excluded.first_user_msg,
          last_user_msg = excluded.last_user_msg,
          final_assistant_msg = excluded.final_assistant_msg,
          files_touched = excluded.files_touched,
          notable_cmds = excluded.notable_cmds,
          tool_use_counts = excluded.tool_use_counts,
          indexed_at = excluded.indexed_at,
          mechanical_version = excluded.mechanical_version,
          source_mtime = excluded.source_mtime,
          source_size = excluded.source_size
        """,
        (
            rec.session_id, rec.jsonl_path, rec.project_path, rec.branch,
            rec.started_at, rec.ended_at, rec.duration_s,
            rec.user_msg_count, rec.asst_msg_count, rec.tool_use_count,
            rec.was_interrupted, rec.ended_cleanly,
            summary, ",".join(tags) if tags else None,
            summary_input_hash, summary_model,
            rec.first_user_msg, rec.last_user_msg, rec.final_assistant_msg,
            rec.files_touched, rec.notable_cmds, rec.tool_use_counts,
            int(time.time()), MECHANICAL_VERSION, rec.source_mtime, rec.source_size,
        ),
    )
    conn.execute("DELETE FROM sessions_fts WHERE session_id = ?", (rec.session_id,))
    conn.execute(
        """
        INSERT INTO sessions_fts (
          session_id, summary, tags,
          first_user_msg, last_user_msg, final_assistant_msg,
          user_messages, assistant_text,
          files_touched, notable_cmds
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            rec.session_id, summary or "", ",".join(tags) if tags else "",
            rec.first_user_msg or "", rec.last_user_msg or "",
            rec.final_assistant_msg or "",
            rec.user_messages_blob, rec.assistant_text_blob,
            rec.files_touched, rec.notable_cmds,
        ),
    )


def index_one(jsonl_path: str, *, db_path: Path | str | None = None,
              force_summary: bool = False) -> dict[str, Any]:
    """Index (or re-index) one session. Returns a status dict."""
    p = Path(jsonl_path)
    if not p.is_file():
        return {"status": "missing", "jsonl_path": str(p)}
    stat = p.stat()

    # Parse + extract first; if we end up skipping, no DB write.
    events = list(parse_jsonl(str(p)))
    rec = extract_record(str(p), events,
                         source_mtime=int(stat.st_mtime),
                         source_size=stat.st_size)

    if _is_live(p, rec.source_mtime, rec.ended_at):
        return {"status": "skipped-live", "session_id": rec.session_id}

    new_hash = compute_summary_input_hash(rec)

    conn = connect(db_path)
    try:
        target_model = get_meta(conn, "haiku_model") or "claude-haiku-4-5"
        row = conn.execute(
            "SELECT summary, tags, summary_input_hash, summary_model FROM sessions WHERE session_id = ?",
            (rec.session_id,),
        ).fetchone()

        # Decide summary path
        if is_trivial(rec):
            _upsert(conn, rec,
                    summary=heuristic_summary(rec), tags=None,
                    summary_input_hash=new_hash, summary_model=None)
            conn.commit()
            return {"status": "trivial", "session_id": rec.session_id,
                    "source_mtime": rec.source_mtime}

        if not force_summary and not _row_needs_resummary(
                row, new_hash=new_hash, target_model=target_model):
            # Re-extract but reuse existing summary.
            _upsert(conn, rec,
                    summary=row["summary"], tags=(row["tags"] or "").split(",") if row["tags"] else None,
                    summary_input_hash=new_hash, summary_model=row["summary_model"])
            conn.commit()
            return {"status": "hash-cache-hit", "session_id": rec.session_id,
                    "source_mtime": rec.source_mtime}

        # Need to call Haiku
        try:
            client = get_anthropic_client()
        except RuntimeError as e:
            log.warning("summarize: %s - storing mechanical fields only", e)
            _upsert(conn, rec,
                    summary=None, tags=None,
                    summary_input_hash=new_hash, summary_model=None)
            conn.commit()
            return {"status": "no-api-key", "session_id": rec.session_id,
                    "source_mtime": rec.source_mtime}

        result: SummaryResult | None = call_summarizer(client, rec, model=target_model)
        if result is None:
            _upsert(conn, rec,
                    summary=None, tags=None,
                    summary_input_hash=new_hash, summary_model=None)
            conn.commit()
            return {"status": "summary-failed", "session_id": rec.session_id,
                    "source_mtime": rec.source_mtime}

        _upsert(conn, rec,
                summary=result.summary, tags=result.tags,
                summary_input_hash=new_hash, summary_model=result.model)
        conn.commit()
        return {"status": "summarized", "session_id": rec.session_id,
                "source_mtime": rec.source_mtime}
    finally:
        conn.close()
