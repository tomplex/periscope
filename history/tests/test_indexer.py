import json
import time
from unittest.mock import MagicMock, patch

import pytest

from history.indexer import index_one, _row_needs_resummary
from history.db import connect, get_meta


@pytest.fixture
def temp_db(tmp_path):
    db_path = tmp_path / "history.db"
    conn = connect(db_path)
    yield conn, db_path
    conn.close()


def _fake_anthropic(summary="A test summary about X.", tags=("test", "fixture", "stub")):
    client = MagicMock()
    ok_block = MagicMock()
    ok_block.type = "tool_use"
    ok_block.name = "save_session_summary"
    ok_block.input = {"summary": summary, "tags": list(tags)}
    msg = MagicMock()
    msg.content = [ok_block]
    client.messages.create.return_value = msg
    return client


def test_index_one_creates_row_and_fts(temp_db, fixture_dir):
    conn, db_path = temp_db
    client = _fake_anthropic()
    with patch("history.indexer.get_anthropic_client", return_value=client):
        result = index_one(str(fixture_dir / "normal_session.jsonl"), db_path=db_path)
    assert result["status"] == "summarized"
    row = conn.execute("SELECT * FROM sessions WHERE session_id = 'normal-001'").fetchone()
    assert row is not None
    assert row["summary"].startswith("A test summary")
    assert row["tags"] == "test,fixture,stub"
    assert row["summary_model"] == "claude-haiku-4-5"
    assert row["summary_input_hash"] is not None
    fts = conn.execute("SELECT * FROM sessions_fts WHERE session_id = 'normal-001'").fetchone()
    assert fts is not None
    assert "test summary" in fts["summary"]


def test_index_one_is_idempotent_when_content_unchanged(temp_db, fixture_dir):
    conn, db_path = temp_db
    client = _fake_anthropic()
    fixture = fixture_dir / "normal_session.jsonl"
    with patch("history.indexer.get_anthropic_client", return_value=client):
        result1 = index_one(str(fixture), db_path=db_path)
        # Bump mtime but keep it well in the past so live-skip doesn't fire.
        import os
        new_mtime = result1["source_mtime"] + 100
        os.utime(str(fixture), (new_mtime, new_mtime))
        result2 = index_one(str(fixture), db_path=db_path)
    assert result1["status"] == "summarized"
    assert result2["status"] == "hash-cache-hit"
    assert client.messages.create.call_count == 1   # only the first call hit Haiku


def test_index_one_skips_live_session(temp_db, fixture_dir, tmp_path):
    # Copy the fixture into a fresh tmp path; bump mtime to now -> looks live.
    import shutil
    src = fixture_dir / "normal_session.jsonl"
    dst = tmp_path / "live.jsonl"
    shutil.copy(src, dst)
    now = time.time()
    import os
    os.utime(dst, (now, now))
    conn, db_path = temp_db
    client = _fake_anthropic()
    with patch("history.indexer.get_anthropic_client", return_value=client):
        result = index_one(str(dst), db_path=db_path)
    assert result["status"] == "skipped-live"
    row = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    assert row == 0  # nothing indexed
    assert client.messages.create.call_count == 0


def test_index_one_trivial_session_no_haiku(temp_db, fixture_dir):
    conn, db_path = temp_db
    client = _fake_anthropic()
    with patch("history.indexer.get_anthropic_client", return_value=client):
        result = index_one(str(fixture_dir / "short_session.jsonl"), db_path=db_path)
    assert result["status"] == "trivial"
    row = conn.execute("SELECT * FROM sessions WHERE session_id = 'abc-001'").fetchone()
    assert row is not None
    assert "Short session" in row["summary"]
    assert client.messages.create.call_count == 0


def test_index_one_handles_summarizer_failure(temp_db, fixture_dir):
    conn, db_path = temp_db
    # Client that never returns a valid tool_use block
    client = MagicMock()
    bad_block = MagicMock(); bad_block.type = "text"; bad_block.text = "no"
    bad_msg = MagicMock(); bad_msg.content = [bad_block]
    client.messages.create.return_value = bad_msg
    with patch("history.indexer.get_anthropic_client", return_value=client):
        result = index_one(str(fixture_dir / "normal_session.jsonl"), db_path=db_path)
    assert result["status"] == "summary-failed"
    row = conn.execute("SELECT * FROM sessions WHERE session_id = 'normal-001'").fetchone()
    assert row is not None
    assert row["summary"] is None
    # Mechanical fields still landed
    assert json.loads(row["files_touched"]) != []


def test_row_needs_resummary_logic():
    row = {"summary_input_hash": "h1", "summary_model": "claude-haiku-4-5", "summary": "x"}
    assert _row_needs_resummary(row, new_hash="h1", target_model="claude-haiku-4-5") is False
    assert _row_needs_resummary(row, new_hash="h2", target_model="claude-haiku-4-5") is True
    assert _row_needs_resummary(row, new_hash="h1", target_model="claude-haiku-5") is True
    # NULL summary -> always needs resummary
    row_null = {"summary_input_hash": "h1", "summary_model": "claude-haiku-4-5", "summary": None}
    assert _row_needs_resummary(row_null, new_hash="h1", target_model="claude-haiku-4-5") is True
