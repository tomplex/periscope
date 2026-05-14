import sqlite3
from history.db import apply_schema, SCHEMA_VERSION, MECHANICAL_VERSION


def test_apply_schema_creates_expected_tables():
    conn = sqlite3.connect(":memory:")
    apply_schema(conn)
    tables = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','virtual','view')"
    )}
    assert "sessions" in tables
    assert "sessions_fts" in tables
    assert "meta" in tables


def test_apply_schema_seeds_versions():
    conn = sqlite3.connect(":memory:")
    apply_schema(conn)
    rows = {k: v for k, v in conn.execute("SELECT key, value FROM meta")}
    assert rows["schema_version"] == str(SCHEMA_VERSION)
    assert rows["mechanical_version"] == str(MECHANICAL_VERSION)
    assert "haiku_model" in rows


def test_apply_schema_idempotent():
    conn = sqlite3.connect(":memory:")
    apply_schema(conn)
    apply_schema(conn)  # second call must not error or change meta
    rows = {k: v for k, v in conn.execute("SELECT key, value FROM meta")}
    assert rows["schema_version"] == str(SCHEMA_VERSION)


def test_fts_delete_trigger_fires():
    conn = sqlite3.connect(":memory:")
    apply_schema(conn)
    conn.execute(
        "INSERT INTO sessions(session_id, jsonl_path, project_path, started_at, ended_at, "
        "duration_s, user_msg_count, asst_msg_count, tool_use_count, indexed_at, "
        "mechanical_version, source_mtime, source_size) "
        "VALUES ('s1', '/p.jsonl', '/p', 0, 0, 0, 0, 0, 0, 0, 1, 0, 0)"
    )
    conn.execute("INSERT INTO sessions_fts(session_id, summary) VALUES ('s1', 'foo')")
    conn.execute("DELETE FROM sessions WHERE session_id = 's1'")
    assert conn.execute("SELECT COUNT(*) FROM sessions_fts WHERE session_id='s1'").fetchone()[0] == 0
