"""Activity store + read-path merge + the background activity worker.

Owns periscope.db (SQLite): the durable events git cannot reconstruct —
channel alerts, context resets, Haiku milestones. Git commits and CI runs
stay computed-on-demand in git_pr.py; this module merges them with the
persisted rows at read time for the modal sidebar's Activity section.

Import discipline: this module imports git_pr, panes, rename_ai, config.
git_pr.py must NEVER import activity.py (would create a cycle). No DB work
happens at import time — the connection opens lazily on first use.
"""

import json
import sqlite3
import threading
import time

from periscope import config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  scope_kind  TEXT NOT NULL,         -- 'pane' | 'branch'
  scope_key   TEXT NOT NULL,         -- pane_id (%N)  |  repo_path\\x1fbranch
  event_kind  TEXT NOT NULL,         -- 'alert' | 'milestone' | 'reset'
  at          INTEGER NOT NULL,
  text        TEXT NOT NULL,
  detail      TEXT,
  url         TEXT,
  payload     TEXT,
  dedup_key   TEXT UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_events_scope ON events (scope_kind, scope_key, at);
CREATE TABLE IF NOT EXISTS cursors (key TEXT PRIMARY KEY, value TEXT);
"""

_CONN: sqlite3.Connection | None = None
_LOCK = threading.Lock()


def _conn() -> sqlite3.Connection:
    """Lazily open the SQLite connection. Caller must hold _LOCK."""
    global _CONN
    if _CONN is None:
        config.ACTIVITY_DB.parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(str(config.ACTIVITY_DB), check_same_thread=False)
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript(_SCHEMA)
        c.commit()
        _CONN = c
    return _CONN


def record(scope_kind, scope_key, event_kind, text, *,
           at=None, detail=None, url=None, payload=None, dedup_key=None):
    """Persist one event. INSERT OR IGNORE on dedup_key, so a non-None
    dedup_key already present makes this a no-op. dedup_key=None inserts."""
    row = (
        scope_kind, scope_key, event_kind,
        int(at if at is not None else time.time()),
        text, detail, url,
        json.dumps(payload) if payload is not None else None,
        dedup_key,
    )
    with _LOCK:
        c = _conn()
        c.execute(
            "INSERT OR IGNORE INTO events "
            "(scope_kind,scope_key,event_kind,at,text,detail,url,payload,dedup_key) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            row,
        )
        c.commit()


def events_for(pane_id, repo_path, branch, limit=40):
    """Persisted events for a pane: pane-scoped rows for pane_id plus
    branch-scoped rows for (repo_path, branch), newest-first, mapped into
    the frontend event model."""
    branch_key = f"{repo_path}\x1f{branch}" if repo_path and branch else "\x00"
    with _LOCK:
        c = _conn()
        rows = c.execute(
            "SELECT event_kind,at,text,detail,url FROM events "
            "WHERE (scope_kind='pane' AND scope_key=?) "
            "   OR (scope_kind='branch' AND scope_key=?) "
            "ORDER BY at DESC LIMIT ?",
            (pane_id or "\x00", branch_key, limit),
        ).fetchall()
    return [_row_to_event(*r) for r in rows]


def prune(max_age_days=30):
    """Drop events older than max_age_days. Called once at startup."""
    cutoff = int(time.time()) - max_age_days * 86400
    with _LOCK:
        c = _conn()
        c.execute("DELETE FROM events WHERE at < ?", (cutoff,))
        c.commit()


def _row_to_event(event_kind, at, text, detail, url):
    """Map a DB row into the frontend event model (spec §Event model)."""
    if event_kind == "alert":
        # detail holds the alert kind: done / need_human / info.
        return {"src": "alert", "kind": detail or "info", "at": at, "text": text}
    # reset / milestone — session-sourced rows.
    return {"src": "session", "kind": event_kind, "at": at,
            "text": text, "state": detail, "url": url}
