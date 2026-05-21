"""Activity store + read-path merge + the background activity worker.

Owns periscope.db (SQLite): the durable events git cannot reconstruct —
channel alerts, context resets, Haiku milestones. Git commits and CI runs
stay computed-on-demand in git_pr.py; this module merges them with the
persisted rows at read time for the modal sidebar's Activity section.

Import discipline: this module imports git_pr, panes, rename_ai, config.
git_pr.py must NEVER import activity.py (would create a cycle). No DB work
happens at import time — the connection opens lazily on first use.
"""

import asyncio
import json
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from periscope import config
from periscope.git_pr import shared_activity_for
from periscope.log import _bg, log
from periscope.panes import _acted_at, list_windows, parse_pane
from periscope.tmux import tmux

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


# --- Live transcript location ------------------------------------------
#
# Claude Code writes transcripts to ~/.claude/projects/<encoded-cwd>/
# <session-uuid>.jsonl. We resolve via the encoded dir ('/' and '.' ->
# '-') as a fast path — scanning all ~3500 transcript dirs every worker
# tick is the wrong cost. The cwd-field check below still guards file
# selection within that dir. If Claude Code ever encodes a character
# differently, that cwd gets no transcript (graceful: resets still fire
# from the context-% drop; milestones still summarize commit messages).

_PROJECTS_DIR = Path.home() / ".claude" / "projects"


def _encode_cwd(cwd: str) -> str:
    return cwd.replace("/", "-").replace(".", "-")


def _transcript_cwd(jsonl_path: Path) -> str | None:
    """The `cwd` recorded in a transcript. Scans the first 15 lines — a
    transcript opens with cwd-less entries (file-history-snapshot,
    queue-operation, last-prompt) before the first real turn."""
    try:
        with jsonl_path.open() as fh:
            for _ in range(15):
                line = fh.readline()
                if not line:
                    break
                try:
                    cwd = json.loads(line).get("cwd")
                except Exception:
                    continue
                if cwd:
                    return cwd
    except Exception:
        return None
    return None


def live_transcript_for(cwd: str) -> Path | None:
    """The live transcript JSONL for a pane at `cwd`: newest-mtime file in
    the encoded projects dir whose recorded `cwd` matches. None if absent."""
    d = _PROJECTS_DIR / _encode_cwd(cwd)
    if not d.is_dir():
        return None
    files = sorted(d.glob("*.jsonl"),
                   key=lambda f: f.stat().st_mtime, reverse=True)
    for f in files:
        if _transcript_cwd(f) == cwd:
            return f
    return None


# --- Context-reset detection -------------------------------------------
#
# Both /clear and a compaction reset Claude's context. /clear leaves no
# transcript marker, so detection keys off the status-line context %,
# which climbs monotonically during a session and drops only on a reset.

def _human_tokens(n) -> str:
    try:
        n = int(n)
    except (TypeError, ValueError):
        return "?"
    return f"{round(n / 1000)}k" if n >= 1000 else str(n)


def _compact_is_recent(ts: str) -> bool:
    """True if an ISO8601 timestamp is within the last 5 minutes."""
    try:
        when = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return False
    return (datetime.now(timezone.utc) - when).total_seconds() < 300


def _recent_compact_meta(jsonl_path) -> dict | None:
    """Scan the tail of a transcript for a recent compact_boundary entry;
    return its compactMetadata, or None."""
    try:
        data = jsonl_path.read_text()
    except Exception:
        return None
    for line in reversed(data.splitlines()[-200:]):
        try:
            d = json.loads(line)
        except Exception:
            continue
        if d.get("type") == "system" and d.get("subtype") == "compact_boundary":
            if _compact_is_recent(d.get("timestamp") or ""):
                return d.get("compactMetadata") or {}
    return None


def _compact_or_clear(cwd: str) -> tuple[str, str]:
    """Best-effort label for a context reset. A recent compact_boundary in
    the live transcript -> ('compacted', text); else ('cleared', text)."""
    try:
        tf = live_transcript_for(cwd)
        if tf:
            meta = _recent_compact_meta(tf)
            if meta is not None:
                trig = meta.get("trigger") or "auto"
                pre = _human_tokens(meta.get("preTokens"))
                post = _human_tokens(meta.get("postTokens"))
                return "compacted", f"context compacted ({trig} · {pre} → {post})"
    except Exception:
        pass
    return "cleared", "context cleared (/clear)"


def _check_reset(pane_id: str, cwd: str, context_pct, last_ctx: dict) -> bool:
    """Compare context_pct to the last reading for pane_id. A drop between
    two non-None readings is a context reset — record it. Returns True if
    a reset was recorded. last_ctx is the worker's per-pane memory."""
    prev = last_ctx.get(pane_id)
    if context_pct is not None:
        last_ctx[pane_id] = context_pct
    if prev is None or context_pct is None or context_pct >= prev:
        return False
    detail, text = _compact_or_clear(cwd)
    record("pane", pane_id, "reset", text, detail=detail)
    return True


# --- Background worker -------------------------------------------------
#
# One lifespan-driven loop (prod instance only — see app.py). Every ~30s
# it captures each active Claude pane and runs the context-reset check.
# Phase 3 extends _worker_tick with the milestone check.

def _worker_tick(last_ctx: dict) -> None:
    """One worker pass. Blocking (tmux subprocesses) — run off-loop."""
    for w in list_windows():
        target = f"{w['session']}:{w['index']}"
        try:
            content = tmux("capture-pane", "-t", target, "-p", "-e", "-S", "-60")
            parsed = parse_pane(content)
        except Exception:
            continue
        if not parsed.get("is_claude"):
            continue
        _check_reset(w.get("pane_id") or "", w.get("cwd") or "",
                     parsed.get("context_pct"), last_ctx)


async def run_worker() -> None:
    """Lifespan task: drive _worker_tick every 30s. The blocking tick runs
    in a thread so it never stalls the event loop."""
    last_ctx: dict = {}
    while True:
        try:
            await asyncio.to_thread(_worker_tick, last_ctx)
        except Exception:
            log.exception("activity worker tick failed")
        await asyncio.sleep(30)
