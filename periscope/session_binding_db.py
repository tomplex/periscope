"""Provider-aware pane/session persistence shared with standalone hooks.

This module deliberately uses only the Python standard library and never owns
or commits a connection.  Callers choose connection timeouts, locking, and
transaction boundaries.
"""

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class AgentSessionBinding:
    pane_id: str
    provider: str
    session_id: str
    session_path: str | None
    updated_at: int
    evidence: str | None


@dataclass(frozen=True)
class AgentHookEvent:
    pane_id: str
    provider: str
    session_id: str
    turn_id: str | None
    event: str
    hook_version: int
    cli_version: str | None
    observed_at: int


_AGENT_SESSIONS_DDL = """
CREATE TABLE IF NOT EXISTS agent_sessions (
  pane_id      TEXT PRIMARY KEY,
  provider     TEXT NOT NULL,
  session_id   TEXT NOT NULL,
  session_path TEXT,
  updated_at   INTEGER NOT NULL,
  evidence     TEXT
)
"""

_AGENT_SESSION_EVENTS_DDL = """
CREATE TABLE IF NOT EXISTS agent_session_events (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  pane_id      TEXT NOT NULL,
  provider     TEXT NOT NULL,
  session_id   TEXT NOT NULL,
  turn_id      TEXT,
  event        TEXT NOT NULL,
  hook_version INTEGER NOT NULL,
  cli_version  TEXT,
  observed_at  INTEGER NOT NULL
)
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the provider-aware tables, without committing the transaction."""
    # execute(), unlike executescript(), does not implicitly commit an existing
    # caller transaction.
    conn.execute(_AGENT_SESSIONS_DDL)
    conn.execute(_AGENT_SESSION_EVENTS_DDL)


def get_binding(
    conn: sqlite3.Connection, pane_id: str
) -> AgentSessionBinding | None:
    if not pane_id:
        return None
    row = conn.execute(
        "SELECT pane_id, provider, session_id, session_path, updated_at, evidence "
        "FROM agent_sessions WHERE pane_id=?",
        (pane_id,),
    ).fetchone()
    return AgentSessionBinding(*row) if row else None


def upsert_binding(
    conn: sqlite3.Connection, binding: AgentSessionBinding
) -> None:
    conn.execute(
        "INSERT INTO agent_sessions "
        "(pane_id, provider, session_id, session_path, updated_at, evidence) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(pane_id) DO UPDATE SET "
        "provider=excluded.provider, session_id=excluded.session_id, "
        "session_path=excluded.session_path, updated_at=excluded.updated_at, "
        "evidence=excluded.evidence",
        (
            binding.pane_id,
            binding.provider,
            binding.session_id,
            binding.session_path,
            binding.updated_at,
            binding.evidence,
        ),
    )


def delete_binding(conn: sqlite3.Connection, pane_id: str) -> None:
    conn.execute("DELETE FROM agent_sessions WHERE pane_id=?", (pane_id,))


def append_hook_event(
    conn: sqlite3.Connection, event: AgentHookEvent
) -> int:
    cursor = conn.execute(
        "INSERT INTO agent_session_events "
        "(pane_id, provider, session_id, turn_id, event, hook_version, "
        "cli_version, observed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            event.pane_id,
            event.provider,
            event.session_id,
            event.turn_id,
            event.event,
            event.hook_version,
            event.cli_version,
            event.observed_at,
        ),
    )
    if cursor.lastrowid is None:  # pragma: no cover - SQLite always supplies it
        raise sqlite3.DatabaseError("hook event insert returned no row id")
    return cursor.lastrowid
