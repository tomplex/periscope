import sqlite3

from periscope.session_binding_db import (
    AgentHookEvent,
    AgentSessionBinding,
    append_hook_event,
    delete_binding,
    ensure_schema,
    get_binding,
    upsert_binding,
)


def _binding(**overrides):
    values = {
        "pane_id": "%7",
        "provider": "codex",
        "session_id": "session-a",
        "session_path": "/tmp/rollout-a.jsonl",
        "updated_at": 10,
        "evidence": "codex-hook",
    }
    values.update(overrides)
    return AgentSessionBinding(**values)


def test_empty_db_schema_is_idempotent_and_caller_commits():
    conn = sqlite3.connect(":memory:")
    ensure_schema(conn)
    ensure_schema(conn)
    upsert_binding(conn, _binding())

    assert get_binding(conn, "%7") == _binding()
    assert get_binding(conn, "") is None
    assert get_binding(conn, "%missing") is None


def test_mutations_do_not_commit_the_callers_transaction():
    conn = sqlite3.connect(":memory:")
    ensure_schema(conn)
    conn.commit()
    upsert_binding(conn, _binding())
    conn.rollback()

    assert get_binding(conn, "%7") is None


def test_legacy_pane_sessions_is_untouched():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE pane_sessions (pane_id TEXT PRIMARY KEY, "
        "session_id TEXT NOT NULL, updated_at INTEGER NOT NULL)"
    )
    conn.execute("INSERT INTO pane_sessions VALUES ('%7', 'claude-id', 1)")

    ensure_schema(conn)
    upsert_binding(conn, _binding(session_id="codex-id"))

    assert conn.execute("SELECT * FROM pane_sessions").fetchall() == [
        ("%7", "claude-id", 1)
    ]
    assert get_binding(conn, "%7").session_id == "codex-id"


def test_upsert_replaces_all_values_when_pane_is_reused_across_providers():
    conn = sqlite3.connect(":memory:")
    ensure_schema(conn)
    upsert_binding(conn, _binding())
    replacement = _binding(
        provider="claude",
        session_id="session-b",
        session_path=None,
        updated_at=20,
        evidence="claude-hook",
    )
    upsert_binding(conn, replacement)

    assert get_binding(conn, "%7") == replacement
    assert conn.execute("SELECT count(*) FROM agent_sessions").fetchone() == (1,)


def test_delete_binding():
    conn = sqlite3.connect(":memory:")
    ensure_schema(conn)
    upsert_binding(conn, _binding())
    delete_binding(conn, "%7")
    assert get_binding(conn, "%7") is None


def test_append_hook_event_returns_row_id_and_preserves_metadata():
    conn = sqlite3.connect(":memory:")
    ensure_schema(conn)
    event = AgentHookEvent(
        pane_id="%7",
        provider="codex",
        session_id="session-a",
        turn_id="turn-a",
        event="Stop",
        hook_version=1,
        cli_version="0.146.0",
        observed_at=123,
    )

    assert append_hook_event(conn, event) == 1
    assert append_hook_event(conn, event) == 2
    assert conn.execute(
        "SELECT pane_id,provider,session_id,turn_id,event,hook_version,"
        "cli_version,observed_at FROM agent_session_events ORDER BY id"
    ).fetchall() == [
        ("%7", "codex", "session-a", "turn-a", "Stop", 1, "0.146.0", 123),
        ("%7", "codex", "session-a", "turn-a", "Stop", 1, "0.146.0", 123),
    ]
