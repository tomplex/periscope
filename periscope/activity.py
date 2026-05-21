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
from periscope.git_pr import shared_activity_for
from periscope.log import _bg
from periscope.panes import _acted_at

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


# --- Read path: merge persisted events with computed git events --------
#
# shared_activity_for() runs git/gh subprocesses, so its result is held in
# a stale-while-revalidate cache keyed by (path, branch): a hit returns
# instantly, a miss/expiry kicks a background refresh and returns whatever
# is cached (possibly nothing on the very first call). Same pattern the
# PR cache uses in git_pr.py.

_GIT_TTL = 60.0
_git_cache: dict[tuple, tuple[float, list]] = {}
_git_fetching: set = set()
_git_lock = threading.Lock()


def _fetch_git_into_cache(path, branch):
    try:
        events = shared_activity_for(path, branch)
    except Exception:
        events = []
    with _git_lock:
        _git_cache[(path, branch)] = (time.time(), events)
        _git_fetching.discard((path, branch))


def cached_pane_activity(target, pane_id, path, branch, limit=40):
    """Merged Activity stream for a pane, newest-first: git/CI events
    (stale-while-revalidate cache) + persisted alert/reset/milestone
    events + the per-target 'opened in periscope' anchor."""
    events: list[dict] = []
    if path and branch:
        key = (path, branch)
        now = time.time()
        with _git_lock:
            cached = _git_cache.get(key)
            stale = cached is None or (now - cached[0] >= _GIT_TTL)
            if stale and key not in _git_fetching:
                _git_fetching.add(key)
                _bg("activity-git-fetch", _fetch_git_into_cache, path, branch)
            git_events = cached[1] if cached else []
        for e in git_events:
            events.append({**e, "src": "git"})
    # Persisted events (alerts, resets, milestones).
    events.extend(events_for(pane_id, path, branch, limit=limit))
    # Per-target "opened in periscope" anchor.
    opened_at = _acted_at.get(target, 0)
    if opened_at:
        events.append({"src": "git", "kind": "open", "at": opened_at,
                       "text": "opened in periscope"})
    events.sort(key=lambda e: e.get("at", 0), reverse=True)
    return events[:limit]
