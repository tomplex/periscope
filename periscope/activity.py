"""Activity store + read-path merge + the background activity worker.

Owns periscope.db (SQLite): the durable events git cannot reconstruct —
channel alerts, context resets. Git commits and CI runs stay
computed-on-demand in git_pr.py; this module merges them with the
persisted rows at read time for the modal sidebar's Activity section.

Import discipline: this module imports git_pr, panes, rename_ai, config.
git_pr.py must NEVER import activity.py (would create a cycle). No DB work
happens at import time — the connection opens lazily on first use.
"""

import asyncio
import contextlib
import json
import os
import sqlite3
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from periscope import config, session_binding_db
from periscope.git_pr import shared_activity_for
from periscope.log import _bg, log
from periscope.panes import _acted_at, list_windows, parse_pane
from periscope.tmux import tmux

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  scope_kind  TEXT NOT NULL,         -- 'pane' | 'branch'
  scope_key   TEXT NOT NULL,         -- pane_id (%N)  |  repo_path\\x1fbranch
  event_kind  TEXT NOT NULL,         -- 'alert'|'reset'|'rename'|'channel'|'status'
  at          INTEGER NOT NULL,
  text        TEXT NOT NULL,
  detail      TEXT,
  url         TEXT,
  payload     TEXT,
  dedup_key   TEXT UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_events_scope ON events (scope_kind, scope_key, at);
CREATE TABLE IF NOT EXISTS pane_sessions (
  pane_id     TEXT PRIMARY KEY,      -- tmux pane id, e.g. '%56'
  session_id  TEXT NOT NULL,         -- Claude CLAUDE_CODE_SESSION_ID (JSONL stem)
  updated_at  INTEGER NOT NULL
);
-- Baseline commit for the "changes this session" diff scope. Keyed on
-- session_id (NOT pane_id) so /clear — which mints a new session id mid-work —
-- naturally gets its own baseline instead of inheriting the pre-clear one, and
-- so the hook that owns pane_sessions never races a write here.
CREATE TABLE IF NOT EXISTS session_bases (
  session_id  TEXT PRIMARY KEY,      -- Claude session id (JSONL stem)
  repo        TEXT NOT NULL,         -- git toplevel the baseline was taken in
  base_sha    TEXT NOT NULL,         -- `git stash create` snapshot, else HEAD
  created_at  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS pane_workspaces (
  pane_id      TEXT PRIMARY KEY,   -- tmux pane id, e.g. '%56'
  workspace_id TEXT NOT NULL,      -- workspaces[].id (state.json)
  updated_at   INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS tracks (
  id          TEXT PRIMARY KEY,
  name        TEXT NOT NULL,
  repo        TEXT,
  created_at  INTEGER NOT NULL,
  archived_at INTEGER
);
CREATE TABLE IF NOT EXISTS pane_tracks (
  pid        TEXT PRIMARY KEY,     -- @periscope_id; %N rows are pre-re-key
                                   -- leftovers, migrated lazily by resolve
  track_id   TEXT NOT NULL,
  updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS usage_samples (
  at        INTEGER NOT NULL,
  account   TEXT NOT NULL,           -- store.Account id ('default' | 'b' | ...)
  meter     TEXT NOT NULL,           -- 'session' | 'week_all' | 'week_opus' | 'week_sonnet'
  percent   REAL NOT NULL,           -- unrounded utilization
  resets_at INTEGER,
  PRIMARY KEY (account, meter, at)
);
CREATE TABLE IF NOT EXISTS ui_events (
  id     INTEGER PRIMARY KEY AUTOINCREMENT,
  at     INTEGER NOT NULL,
  name   TEXT NOT NULL,
  dev    INTEGER NOT NULL,
  detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_ui_events_name ON ui_events (name, at);
CREATE TABLE IF NOT EXISTS pane_status (
  pane_id      TEXT PRIMARY KEY,   -- tmux %id
  session_id   TEXT,               -- Claude JSONL stem at generation time
  status       TEXT NOT NULL,
  generated_at INTEGER NOT NULL,   -- unix seconds
  jsonl_size   INTEGER NOT NULL,   -- size at generation (change check)
  seen_name    TEXT,               -- window name at last generation
  renamed_at   INTEGER,            -- rename-cooldown stamp (narrator,
                                   -- manual routes, or detected external)
  rail         TEXT,               -- <=28-char rail fragment (nullable)
  goal         TEXT,               -- narrator-curated overarching-thread
                                   -- sentence (the persistent topic memory)
  history      TEXT                -- JSON arc of recent status lines
                                   -- [{"t":<unix>,"s":"..."}], divergence
                                   -- evidence the narrator reasons against
);
DROP TABLE IF EXISTS first_mate;
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
        session_binding_db.ensure_schema(c)
        # pane_status predates the rail/goal/history columns in live DBs, and
        # CREATE TABLE IF NOT EXISTS won't add them. Guarded ALTER (history/db.py
        # pattern) is provably idempotent, so dev/prod schema skew is harmless.
        have = {r[1] for r in c.execute("PRAGMA table_info(pane_status)")}
        for col in ("rail", "goal", "history"):
            if col not in have:
                c.execute(f"ALTER TABLE pane_status ADD COLUMN {col} TEXT")
        # pane_tracks was keyed on tmux %N, which rotates on every restart —
        # renamed so any raw-SQL regression against pane_id fails loudly.
        pt_cols = {r[1] for r in c.execute("PRAGMA table_info(pane_tracks)")}
        if "pane_id" in pt_cols:
            c.execute("ALTER TABLE pane_tracks RENAME COLUMN pane_id TO pid")
        # usage_samples predates multi-account and was keyed (meter, at), which
        # would interleave two subscriptions into one unseparable series. SQLite
        # can't widen a PK in place, so rebuild — existing rows are all from the
        # single account that existed then, i.e. the default one.
        have = {r[1] for r in c.execute("PRAGMA table_info(usage_samples)")}
        if "account" not in have:
            c.executescript("""
              CREATE TABLE usage_samples_new (
                at        INTEGER NOT NULL,
                account   TEXT NOT NULL,
                meter     TEXT NOT NULL,
                percent   REAL NOT NULL,
                resets_at INTEGER,
                PRIMARY KEY (account, meter, at)
              );
              INSERT INTO usage_samples_new (at, account, meter, percent, resets_at)
                SELECT at, 'default', meter, percent, resets_at FROM usage_samples;
              DROP TABLE usage_samples;
              ALTER TABLE usage_samples_new RENAME TO usage_samples;
            """)
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
            # 'status' is the narrator's append-only thread log (every
            # regeneration) — excluded here so it never floods the live
            # timeline; status_log_for() is its dedicated reader.
            "WHERE event_kind != 'status' AND ("
            "       (scope_kind='pane' AND scope_key=?) "
            "    OR (scope_kind='branch' AND scope_key=?)) "
            "ORDER BY at DESC LIMIT ?",
            (pane_id or "\x00", branch_key, limit),
        ).fetchall()
    return [_row_to_event(*r) for r in rows]


def alert_events_since(cutoff_ts: int) -> list[tuple[str, int, str, str]]:
    """(pane_id, at, message, kind) for durable 'alert' events at/after
    `cutoff_ts`, oldest-first. Feeds channel-alert cache rehydration after a
    periscope restart — the in-memory cache is empty on boot but the events
    table (written by every notify()) survived. id/severity aren't persisted."""
    with _LOCK:
        c = _conn()
        rows = c.execute(
            "SELECT scope_key, at, text, detail FROM events "
            "WHERE scope_kind='pane' AND event_kind='alert' AND at >= ? "
            "ORDER BY at ASC",
            (cutoff_ts,),
        ).fetchall()
    return [(r[0], int(r[1] or 0), r[2] or "", r[3] or "info") for r in rows]


def status_log_for(pane_id: str, limit: int = 200) -> list[dict]:
    """Append-only narrator status/goal history for a pane (event_kind=
    'status'), newest-first. Kept out of the live activity timeline
    (events_for) to avoid flooding it; this is the history reader."""
    with _LOCK:
        c = _conn()
        rows = c.execute(
            "SELECT at, text, detail FROM events "
            "WHERE scope_kind='pane' AND scope_key=? AND event_kind='status' "
            "ORDER BY at DESC LIMIT ?",
            (pane_id, limit),
        ).fetchall()
    return [{"at": int(a), "status": t, "goal": d} for a, t, d in rows]


def prune(max_age_days=30):
    """Drop events older than max_age_days. Called once at startup."""
    cutoff = int(time.time()) - max_age_days * 86400
    with _LOCK:
        c = _conn()
        c.execute("DELETE FROM events WHERE at < ?", (cutoff,))
        c.commit()


def checkpoint() -> None:
    """Shrink the WAL file via a TRUNCATE checkpoint. SQLite's default
    auto-checkpoint runs PASSIVE on every 1000-page write, which writes
    the WAL into the main DB but never truncates the WAL file itself —
    so it grows to ~4MB and stays there. TRUNCATE both checkpoints and
    truncates. Best-effort: if a reader holds the file, the truncate
    silently skips and we retry next tick."""
    with _LOCK:
        c = _conn()
        with contextlib.suppress(sqlite3.OperationalError):
            c.execute("PRAGMA wal_checkpoint(TRUNCATE)")


# --- pane_sessions: tmux pane id -> Claude session id mapping ----------
#
# Replaces the legacy ~/.config/periscope/pane_sessions/<pane> file
# layout (one tiny file per pane id, 631+ inodes for a logical k/v map).
# Writer is pane_session_hook.py (out-of-process, opens its own SQLite
# connection on Claude's SessionStart/UserPromptSubmit). Readers are
# periscope.turns and any future code that needs the mapping.

def get_pane_session(pane_id: str) -> str | None:
    """The Claude session id last recorded for this tmux pane, or None."""
    if not pane_id:
        return None
    with _LOCK:
        c = _conn()
        row = c.execute(
            "SELECT session_id FROM pane_sessions WHERE pane_id=?",
            (pane_id,),
        ).fetchone()
    return row[0] if row else None


def get_agent_session(
    pane_id: str,
) -> session_binding_db.AgentSessionBinding | None:
    """The provider-aware session binding for a pane, or None.

    This is intentionally separate from get_pane_session(), whose legacy table
    and return contract remain Claude-only.
    """
    if not pane_id:
        return None
    with _LOCK:
        return session_binding_db.get_binding(_conn(), pane_id)


def get_session_base(session_id: str) -> tuple[str, str] | None:
    """(repo, base_sha) baseline for a session's diff scope, or None if the
    session started before this feature (or outside a git repo)."""
    if not session_id:
        return None
    with _LOCK:
        c = _conn()
        row = c.execute(
            "SELECT repo, base_sha FROM session_bases WHERE session_id=?",
            (session_id,),
        ).fetchone()
    return (row[0], row[1]) if row else None


def set_session_base(session_id: str, repo: str, base_sha: str) -> bool:
    """Record a session's baseline once. INSERT OR IGNORE: the first writer
    wins, so a re-fired SessionStart can't move a baseline out from under a
    session that has already made changes. Returns True if this call set it."""
    if not (session_id and repo and base_sha):
        return False
    with _LOCK:
        c = _conn()
        cur = c.execute(
            "INSERT OR IGNORE INTO session_bases "
            "(session_id, repo, base_sha, created_at) VALUES (?,?,?,?)",
            (session_id, repo, base_sha, int(time.time())),
        )
        c.commit()
        return cur.rowcount > 0


def prune_pane_sessions(alive_pane_ids: set[str]) -> int:
    """Drop pane_sessions rows for panes no longer in tmux. Returns the
    number of rows deleted. Caller passes the live set; we can't safely
    enumerate it here without circular imports."""
    with _LOCK:
        c = _conn()
        existing = {r[0] for r in c.execute("SELECT pane_id FROM pane_sessions")}
        dead = existing - alive_pane_ids
        if not dead:
            return 0
        c.executemany("DELETE FROM pane_sessions WHERE pane_id=?",
                      [(p,) for p in dead])
        c.commit()
        return len(dead)


# --- pane_workspaces: tmux pane id -> workspace id tag -----------------
#
# The per-tab membership tag for workspaces (state['workspaces'] entity).
# Keyed on tmux pane_id exactly like pane_sessions/pane_status, so it reuses
# the dead-pane prune verbatim.

def set_pane_workspace(pane_id: str, workspace_id: str | None) -> None:
    """Tag a tab into a workspace, or clear the tag when workspace_id is None."""
    with _LOCK:
        c = _conn()
        if workspace_id is None:
            c.execute("DELETE FROM pane_workspaces WHERE pane_id=?", (pane_id,))
        else:
            c.execute(
                "INSERT INTO pane_workspaces (pane_id, workspace_id, updated_at) "
                "VALUES (?, ?, ?) ON CONFLICT(pane_id) DO UPDATE SET "
                "workspace_id=excluded.workspace_id, updated_at=excluded.updated_at",
                (pane_id, workspace_id, int(time.time())),
            )
        c.commit()


def get_pane_workspace(pane_id: str) -> str | None:
    """The workspace id this tab is tagged into, or None."""
    if not pane_id:
        return None
    with _LOCK:
        c = _conn()
        row = c.execute(
            "SELECT workspace_id FROM pane_workspaces WHERE pane_id=?",
            (pane_id,),
        ).fetchone()
    return row[0] if row else None


def pane_workspace_map() -> dict[str, str]:
    """All live tags as {pane_id: workspace_id} — one bulk read for the worker
    tick and window_view fan-out."""
    with _LOCK:
        c = _conn()
        return dict(c.execute("SELECT pane_id, workspace_id FROM pane_workspaces"))


def prune_pane_workspaces(alive_pane_ids: set[str]) -> int:
    """Drop tags for tmux pane ids that no longer exist (tab died). Returns
    the number of rows deleted."""
    with _LOCK:
        c = _conn()
        existing = {r[0] for r in c.execute("SELECT pane_id FROM pane_workspaces")}
        dead = existing - alive_pane_ids
        if not dead:
            return 0
        c.executemany("DELETE FROM pane_workspaces WHERE pane_id=?",
                      [(p,) for p in dead])
        c.commit()
        return len(dead)


# --- pane_tracks: @periscope_id -> track id tag (sibling of pane_workspaces) ---
def set_pane_track(pid: str, track_id: str | None) -> None:
    with _LOCK:
        c = _conn()
        if track_id is None:
            c.execute("DELETE FROM pane_tracks WHERE pid=?", (pid,))
        else:
            c.execute(
                "INSERT INTO pane_tracks (pid, track_id, updated_at) "
                "VALUES (?,?,?) ON CONFLICT(pid) DO UPDATE SET "
                "track_id=excluded.track_id, updated_at=excluded.updated_at",
                (pid, track_id, int(time.time())),
            )
        c.commit()


def get_pane_track(pid: str) -> str | None:
    if not pid:
        return None
    with _LOCK:
        row = _conn().execute(
            "SELECT track_id FROM pane_tracks WHERE pid=?", (pid,)
        ).fetchone()
        return row[0] if row else None


def pane_track_map() -> dict[str, str]:
    with _LOCK:
        return dict(_conn().execute("SELECT pid, track_id FROM pane_tracks"))


def prune_pane_tracks(alive_pids: set[str]) -> int:
    with _LOCK:
        c = _conn()
        existing = [r[0] for r in c.execute("SELECT pid FROM pane_tracks")]
        # %N rows predate the pid re-key and are the sweep's business — judged
        # against a pid set they would ALWAYS look dead and lose their tag
        # before resolve's lazy migration could carry it over.
        dead = [p for p in existing
                if not p.startswith("%") and p not in alive_pids]
        c.executemany("DELETE FROM pane_tracks WHERE pid=?",
                      [(p,) for p in dead])
        c.commit()
        return len(dead)


def sweep_legacy_pane_track_rows(live_pane_ids: set[str]) -> int:
    """Delete %-keyed pane_tracks rows whose tmux pane no longer exists. Those
    rows predate the pid re-key; a LIVE pane's row is left for resolve's lazy
    migration to convert — sweeping it would drop the tag it's about to carry
    over."""
    with _LOCK:
        c = _conn()
        # Filter in Python: the table is tens of rows, and a SQL LIKE on a
        # literal '%' needs ESCAPE gymnastics.
        rows = [r[0] for r in c.execute("SELECT pid FROM pane_tracks")
                if r[0].startswith("%") and r[0] not in live_pane_ids]
        c.executemany("DELETE FROM pane_tracks WHERE pid=?",
                      [(p,) for p in rows])
        c.commit()
        return len(rows)


# --- tracks: entity rows (the registry, replacing projects/workspaces) ---
_TRACK_COLS = ("id", "name", "repo", "created_at", "archived_at")


def insert_track(row: dict) -> None:
    with _LOCK:
        c = _conn()
        c.execute(
            "INSERT OR REPLACE INTO tracks (id,name,repo,created_at,archived_at) "
            "VALUES (?,?,?,?,?)",
            (row["id"], row["name"], row.get("repo"),
             row["created_at"], row.get("archived_at")),
        )
        c.commit()


def get_track(track_id: str) -> dict | None:
    with _LOCK:
        r = _conn().execute(
            "SELECT id,name,repo,created_at,archived_at FROM tracks WHERE id=?",
            (track_id,),
        ).fetchone()
        return dict(zip(_TRACK_COLS, r, strict=True)) if r else None


def all_tracks() -> list[dict]:
    with _LOCK:
        rows = _conn().execute(
            "SELECT id,name,repo,created_at,archived_at FROM tracks ORDER BY created_at"
        ).fetchall()
        return [dict(zip(_TRACK_COLS, r, strict=True)) for r in rows]


def update_track(track_id: str, **fields) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    with _LOCK:
        c = _conn()
        c.execute(f"UPDATE tracks SET {cols} WHERE id=?", (*fields.values(), track_id))
        c.commit()


def archive_track(track_id: str, ts: int) -> None:
    update_track(track_id, archived_at=ts)


def delete_track(track_id: str) -> None:
    with _LOCK:
        c = _conn()
        c.execute("DELETE FROM tracks WHERE id=?", (track_id,))
        c.commit()


def migrate_legacy_pane_sessions() -> int:
    """One-shot import of the legacy ~/.config/periscope/pane_sessions/
    file layout into the pane_sessions table. Removes the directory on
    success. Returns the number of rows imported. No-op if the directory
    is absent. Idempotent: rows use INSERT OR REPLACE."""
    legacy = config.config_dir() / "pane_sessions"
    if not legacy.is_dir():
        return 0
    now = int(time.time())
    rows: list[tuple[str, str, int]] = []
    for f in legacy.iterdir():
        if not f.name.startswith("%") or not f.is_file():
            continue
        try:
            sid = f.read_text().strip()
        except OSError:
            continue
        if sid:
            rows.append((f.name, sid, now))
    if rows:
        with _LOCK:
            c = _conn()
            c.executemany(
                "INSERT INTO pane_sessions (pane_id, session_id, updated_at) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(pane_id) DO UPDATE SET "
                "  session_id=excluded.session_id, "
                "  updated_at=excluded.updated_at",
                rows,
            )
            c.commit()
    # Wipe the directory. Use rmtree so partial state doesn't survive —
    # if a hook race left an unreadable file, we still don't want the
    # legacy path to keep coming back on every restart.
    import shutil
    try:
        shutil.rmtree(legacy)
    except OSError:
        log.warning("could not remove legacy pane_sessions dir %s", legacy)
    return len(rows)


# --- pane_status: narrator storage --------------------------------------
#
# One row per Claude pane: the narrator's last generated status line plus
# the bookkeeping it needs to decide when to regenerate (jsonl_size,
# generated_at) and when renaming is allowed (seen_name, renamed_at).
# Writer is periscope/narrator.py (worker tick); readers are the narrator
# and routes/state.py's bulk merge. Statuses survive restarts by design.

_PANE_STATUS_COLS = ("pane_id, session_id, status, generated_at, "
                     "jsonl_size, seen_name, renamed_at, rail, goal, history")


@dataclass(frozen=True)
class PaneStatusRow:
    pane_id: str
    session_id: str | None
    status: str
    generated_at: int
    jsonl_size: int
    seen_name: str | None
    renamed_at: int | None
    rail: str | None = None
    goal: str | None = None
    history: str | None = None    # JSON arc; narrator owns the shape


def get_pane_status(pane_id: str) -> PaneStatusRow | None:
    with _LOCK:
        c = _conn()
        row = c.execute(
            f"SELECT {_PANE_STATUS_COLS} FROM pane_status WHERE pane_id=?",
            (pane_id,),
        ).fetchone()
    return PaneStatusRow(*row) if row else None


def all_pane_statuses() -> list[PaneStatusRow]:
    """Every stored row — the narrator tick's bulk read (one SELECT per
    tick; oldest-first cap selection happens narrator-side)."""
    with _LOCK:
        c = _conn()
        rows = c.execute(f"SELECT {_PANE_STATUS_COLS} FROM pane_status").fetchall()
    return [PaneStatusRow(*r) for r in rows]


def upsert_pane_status(row: PaneStatusRow) -> None:
    with _LOCK:
        c = _conn()
        c.execute(
            f"INSERT INTO pane_status ({_PANE_STATUS_COLS}) VALUES (?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(pane_id) DO UPDATE SET "
            "  session_id=excluded.session_id, status=excluded.status, "
            "  generated_at=excluded.generated_at, jsonl_size=excluded.jsonl_size, "
            "  seen_name=excluded.seen_name, renamed_at=excluded.renamed_at, "
            "  rail=excluded.rail, goal=excluded.goal, history=excluded.history",
            (row.pane_id, row.session_id, row.status, row.generated_at,
             row.jsonl_size, row.seen_name, row.renamed_at, row.rail,
             row.goal, row.history),
        )
        c.commit()


def stamp_pane_rename(pane_id: str, *, name: str, at: int) -> None:
    """Start the narrator's rename cooldown for this pane. Called from the
    manual/auto rename routes. The pane may have no row yet (rename before
    first generation) — insert a placeholder (status='', jsonl_size=0,
    rail=NULL) that the read paths skip and that regenerates promptly
    (size differs)."""
    with _LOCK:
        c = _conn()
        c.execute(
            f"INSERT INTO pane_status ({_PANE_STATUS_COLS}) "
            "VALUES (?, NULL, '', 0, 0, ?, ?, NULL, NULL, NULL) "
            "ON CONFLICT(pane_id) DO UPDATE SET "
            "  seen_name=excluded.seen_name, renamed_at=excluded.renamed_at",
            (pane_id, name, at),
        )
        c.commit()


def pane_status_lines() -> dict[str, tuple[str, int, str | None]]:
    """Bulk read for routes/state.py: pane_id -> (status, generated_at,
    rail). One SELECT per poll — never a per-pane query inside the
    32-thread fan-out (it would serialize on _LOCK). Skips placeholder
    rows."""
    with _LOCK:
        c = _conn()
        rows = c.execute(
            "SELECT pane_id, status, generated_at, rail FROM pane_status "
            "WHERE status != ''"
        ).fetchall()
    return {p: (s, int(g), r) for p, s, g, r in rows}


def prune_pane_status(alive_pane_ids: set[str]) -> int:
    """Drop pane_status rows for panes no longer in tmux. Mirror of
    prune_pane_sessions; caller passes the live set."""
    with _LOCK:
        c = _conn()
        existing = {r[0] for r in c.execute("SELECT pane_id FROM pane_status")}
        dead = existing - alive_pane_ids
        if not dead:
            return 0
        c.executemany("DELETE FROM pane_status WHERE pane_id=?",
                      [(p,) for p in dead])
        c.commit()
        return len(dead)


# --- usage_samples: plan-usage time series ------------------------------
#
# One row per meter per successful OAuth usage fetch (~5 min cadence, from
# periscope/usage.py). Stores the unrounded utilization so burn-rate slopes
# aren't quantized to integer steps. Both prod and a dev instance may write;
# the (account, meter, at) PK + INSERT OR IGNORE makes same-second collisions
# benign. `account` is in the PK because two subscriptions sample the same
# meter names on the same cadence — without it their series interleave into
# one nonsense slope, retroactively unseparable.

def record_usage_samples(
        rows: list[tuple[int, str, str, float, int | None]]) -> None:
    """Bulk-insert (at, account, meter, percent, resets_at) samples."""
    if not rows:
        return
    with _LOCK:
        c = _conn()
        c.executemany(
            "INSERT OR IGNORE INTO usage_samples "
            "(at, account, meter, percent, resets_at) VALUES (?,?,?,?,?)",
            rows,
        )
        c.commit()


def usage_samples_since(account: str, meter: str,
                        since: int) -> list[tuple[int, float]]:
    """(at, percent) samples for one account's meter at/after `since`,
    oldest first."""
    with _LOCK:
        c = _conn()
        rows = c.execute(
            "SELECT at, percent FROM usage_samples "
            "WHERE account=? AND meter=? AND at>=? ORDER BY at",
            (account, meter, since),
        ).fetchall()
    return [(int(a), float(p)) for a, p in rows]


def prune_usage_samples(max_age_days: int = 14) -> None:
    """Drop usage_samples older than max_age_days. Called once at startup."""
    cutoff = int(time.time()) - max_age_days * 86400
    with _LOCK:
        c = _conn()
        c.execute("DELETE FROM usage_samples WHERE at < ?", (cutoff,))
        c.commit()


# --- UI instrumentation ------------------------------------------------
#
# Lightweight usage telemetry: which dashboard actions get used most, so
# UX work is data-driven. The client (static/src/track.js) batches events
# to POST /api/events (routes/events.py), which calls record_ui_events.
# Single-user, low volume; SQLite-by-hand is the readout (no UI). ui_events
# is a separate tenant in periscope.db, like pane_sessions above.

def record_ui_events(events: list, dev: bool) -> int:
    """Bulk-insert UI instrumentation rows. Each event is a dict with keys
    name (str), detail (dict|None), t (int unix seconds, client clock).
    Non-dict elements and rows with no non-empty `name` are skipped.
    `detail` is JSON-serialized (None / empty / non-dict -> NULL). `t` is
    coerced to int, falling back to time.time() when missing/invalid. `dev`
    stamps every row in the batch. Returns the number of rows inserted."""
    now = int(time.time())
    dev_flag = 1 if dev else 0
    rows: list[tuple] = []
    for e in events:
        if not isinstance(e, dict):
            continue
        name = e.get("name")
        if not isinstance(name, str) or not name:
            continue
        try:
            at = int(e.get("t"))
        except (TypeError, ValueError):
            at = now
        detail = e.get("detail")
        detail_json = json.dumps(detail) if isinstance(detail, dict) and detail else None
        rows.append((at, name, dev_flag, detail_json))
    if not rows:
        return 0
    with _LOCK:
        c = _conn()
        c.executemany(
            "INSERT INTO ui_events (at, name, dev, detail) VALUES (?,?,?,?)",
            rows,
        )
        c.commit()
    return len(rows)


def prune_ui_events(max_age_days: int = 90) -> None:
    """Drop ui_events older than max_age_days. Called once at startup."""
    cutoff = int(time.time()) - max_age_days * 86400
    with _LOCK:
        c = _conn()
        c.execute("DELETE FROM ui_events WHERE at < ?", (cutoff,))
        c.commit()


def _row_to_event(event_kind, at, text, detail, url):
    """Map a DB row into the frontend event model (spec §Event model)."""
    if event_kind == "alert":
        # detail holds the alert kind: done / need_human / info.
        return {"src": "alert", "kind": detail or "info", "at": at, "text": text}
    if event_kind == "channel":
        # A message periscope pushed INTO the pane. detail holds the push
        # kind (message / interrupt / ...); text is the full message body.
        return {"src": "channel", "kind": detail or "message", "at": at, "text": text}
    # reset — session-sourced rows.
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
    (stale-while-revalidate cache) + persisted alert/reset events + the
    per-target 'opened in periscope' anchor."""
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
        events.extend({**e, "src": "git"} for e in git_events)
    # Persisted events (alerts, resets).
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
# from the context-% drop).

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
    return (datetime.now(UTC) - when).total_seconds() < 300


def _recent_compact_meta(jsonl_path) -> dict | None:
    """Scan the tail of a transcript for a recent compact_boundary entry;
    return its compactMetadata, or None. Bounded tail read — transcripts
    can be tens of MB."""
    try:
        with jsonl_path.open() as fh:
            tail = deque(fh, maxlen=200)
    except Exception:
        return None
    for line in reversed(tail):
        try:
            d = json.loads(line)
        except Exception:
            continue
        if (
            d.get("type") == "system"
            and d.get("subtype") == "compact_boundary"
            and _compact_is_recent(d.get("timestamp") or "")
        ):
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
# it captures each active Claude pane, runs the context-reset check, and
# drives the narrator (semantic status + auto-rename).

_FD_WARN = 512   # half the launchd SoftResourceLimits NumberOfFiles cap (1024)


def _fd_count() -> int | None:
    """This process's open-fd count via /dev/fd (macOS). None if unreadable."""
    try:
        return len(os.listdir("/dev/fd"))
    except OSError:
        return None


def _worker_tick(last_ctx: dict) -> None:
    """One worker pass. Blocking (tmux + git subprocesses) — run off-loop."""
    panes: list[tuple[dict, dict]] = []
    for w in list_windows():
        target = f"{w['session']}:{w['index']}"
        try:
            content = tmux("capture-pane", "-t", target, "-p", "-e", "-S", "-60")
            parsed = parse_pane(content)
        except Exception:
            continue
        if parsed.get("agent") != "claude":
            continue
        panes.append((w, parsed))
        _check_reset(w.get("pane_id") or "", w.get("cwd") or "",
                     parsed.get("context_pct"), last_ctx)
    # Narrator: one semantic-status pass over the same Claude panes.
    try:
        # Function-level import — narrator imports activity; a top-level
        # import here would be a cycle.
        from periscope import narrator
        narrator.tick(panes)
    except Exception:
        log.exception("narrator tick failed")
    # bg commander job status sync (prod-only, same as the narrator above).
    try:
        from periscope import bg_commander
        bg_commander.sync_jobs()
    except Exception:
        log.exception("bg_commander sync failed")
    # tmux-continuum's save rides on status-line expansion, which never happens
    # when the only attached clients are periscope's control-mode ones. Drive
    # it from here instead; the script self-gates on its own interval + lock.
    try:
        from periscope import resurrect
        resurrect.save_now()
    except Exception:
        log.exception("continuum save failed")
    # How far behind origin this checkout is, for the header's update pill.
    # Self-throttled to hourly; the tick is just a convenient heartbeat.
    try:
        from periscope import updater
        updater.check()
    except Exception:
        log.exception("update check failed")
    # Keep periscope.db-wal bounded — see checkpoint() docstring for why
    # SQLite's default auto-checkpoint isn't enough on its own.
    checkpoint()
    # fd watchdog: EMFILE doesn't crash the server, it wedges it silently
    # (the Jun-2026 bg_commander connection leak). Surface a climbing fd
    # count loudly while there's still headroom under the 1024 soft cap.
    n = _fd_count()
    if n is not None and n >= _FD_WARN:
        log.warning("open fd count high: %d (soft cap 1024)", n)


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
