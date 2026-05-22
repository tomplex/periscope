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
    _, db_path = temp_db
    client = _fake_anthropic()
    fixture = fixture_dir / "normal_session.jsonl"
    with patch("history.indexer.get_anthropic_client", return_value=client):
        result1 = index_one(str(fixture), db_path=db_path)
        # Bump mtime by +100s; OLD_MTIME (1700000000, year 2023) gives years of
        # margin past the 60s LIVE_MTIME_S threshold, so _is_live stays False.
        import os
        new_mtime = result1["source_mtime"] + 100
        os.utime(str(fixture), (new_mtime, new_mtime))
        result2 = index_one(str(fixture), db_path=db_path)
    assert result1["status"] == "summarized"
    assert result2["status"] == "hash-cache-hit"
    assert client.messages.create.call_count == 1


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
    # A default-model change alone does NOT trigger resummary — a valid
    # summary stays cached; a deliberate model switch goes through
    # `resummarize --all`, which NULLs the hash to force re-summary.
    assert _row_needs_resummary(row, new_hash="h1", target_model="claude-haiku-5") is False
    # NULL summary -> always needs resummary
    row_null = {"summary_input_hash": "h1", "summary_model": "claude-haiku-4-5", "summary": None}
    assert _row_needs_resummary(row_null, new_hash="h1", target_model="claude-haiku-4-5") is True


def test_index_one_archives_source_jsonl(temp_db, fixture_dir, tmp_path, monkeypatch):
    """After successful index, source JSONL is copied to the archive dir,
    and the DB row's jsonl_path points to the archive copy."""
    # Redirect the archive to a tmp path so we don't touch ~/.claude/projects-archive
    archive_dir = tmp_path / "archive"
    monkeypatch.setattr("history.indexer.ARCHIVE_DIR", archive_dir)

    conn, db_path = temp_db
    client = _fake_anthropic()
    fixture = fixture_dir / "normal_session.jsonl"
    with patch("history.indexer.get_anthropic_client", return_value=client):
        result = index_one(str(fixture), db_path=db_path)
    assert result["status"] == "summarized"
    # Archive should contain a copy under <encoded-project>/<uuid>.jsonl
    encoded = fixture.parent.name  # the fixture's parent dir name
    archive_copy = archive_dir / encoded / fixture.name
    assert archive_copy.exists()
    assert archive_copy.read_bytes() == fixture.read_bytes()
    # DB row's jsonl_path should point at the archive copy, not the original
    row = conn.execute("SELECT jsonl_path FROM sessions WHERE session_id = 'normal-001'").fetchone()
    assert row["jsonl_path"] == str(archive_copy)


def test_index_one_archive_idempotent(temp_db, fixture_dir, tmp_path, monkeypatch):
    """Re-indexing doesn't re-copy if archive already has matching content."""
    archive_dir = tmp_path / "archive"
    monkeypatch.setattr("history.indexer.ARCHIVE_DIR", archive_dir)

    conn, db_path = temp_db
    client = _fake_anthropic()
    fixture = fixture_dir / "normal_session.jsonl"
    encoded = fixture.parent.name
    archive_copy = archive_dir / encoded / fixture.name

    with patch("history.indexer.get_anthropic_client", return_value=client):
        index_one(str(fixture), db_path=db_path)
    first_mtime = archive_copy.stat().st_mtime
    with patch("history.indexer.get_anthropic_client", return_value=client):
        index_one(str(fixture), db_path=db_path)
    second_mtime = archive_copy.stat().st_mtime
    # No re-copy — mtime preserved
    assert first_mtime == second_mtime


def test_index_one_force_bypasses_live_skip(temp_db, fixture_dir, tmp_path, monkeypatch):
    """SessionEnd hook uses force=True to index a session whose mtime is
    within the live window (mtime check fires for backfill, not for the hook)."""
    archive_dir = tmp_path / "archive"
    monkeypatch.setattr("history.indexer.ARCHIVE_DIR", archive_dir)

    import shutil
    src = fixture_dir / "normal_session.jsonl"
    dst = tmp_path / "live.jsonl"
    shutil.copy(src, dst)
    import os
    import time as _t
    now = _t.time()
    os.utime(dst, (now, now))  # mtime is now → would normally trigger live-skip

    conn, db_path = temp_db
    client = _fake_anthropic()
    # Default behaviour (no force): live-skip fires
    with patch("history.indexer.get_anthropic_client", return_value=client):
        result_no_force = index_one(str(dst), db_path=db_path)
    assert result_no_force["status"] == "skipped-live"
    # With force=True: the live check is bypassed, session gets indexed
    with patch("history.indexer.get_anthropic_client", return_value=client):
        result_force = index_one(str(dst), db_path=db_path, force=True)
    assert result_force["status"] in ("summarized", "trivial")  # depends on content


def test_index_one_skips_scrape_session(tmp_path):
    """A transcript with no assistant turn (a /usage scrape) is not indexed."""
    from history import indexer
    # A minimal transcript: one user line, zero assistant lines.
    jp = tmp_path / "scrape.jsonl"
    jp.write_text(
        '{"type":"user","cwd":"/repo","sessionId":"sc1",'
        '"timestamp":"2026-05-20T10:00:00.000Z",'
        '"message":{"role":"user","content":"hi"}}\n'
    )
    db = tmp_path / "h.db"
    res = indexer.index_one(str(jp), db_path=db, force=True)
    assert res["status"] == "skipped-scrape"
    from history.db import connect
    conn = connect(db)
    assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
    conn.close()


def test_index_one_persists_facets(tmp_path, monkeypatch):
    from history import indexer
    from history.summarize import SummaryResult

    # Stub the Anthropic client + the summarizer call.
    monkeypatch.setattr(indexer, "get_anthropic_client", lambda: object())
    monkeypatch.setattr(indexer, "call_summarizer", lambda client, rec, model=None:
        SummaryResult(summary="s", tags=["t1", "t2", "t3"], model="m",
                      outcome="shipped", category="bugfix",
                      notable=True, topics=["periscope"]))

    # A non-trivial transcript: >=2 user msgs, an assistant turn, >60s span.
    jp = tmp_path / "real.jsonl"
    jp.write_text("\n".join([
        '{"type":"user","cwd":"/repo","sessionId":"r1","timestamp":"2026-05-20T10:00:00.000Z","message":{"role":"user","content":"first request please"}}',
        '{"type":"assistant","sessionId":"r1","timestamp":"2026-05-20T10:01:00.000Z","message":{"role":"assistant","content":"working on it"}}',
        '{"type":"user","cwd":"/repo","sessionId":"r1","timestamp":"2026-05-20T10:02:00.000Z","message":{"role":"user","content":"second request please"}}',
        '{"type":"assistant","sessionId":"r1","timestamp":"2026-05-20T10:03:00.000Z","message":{"role":"assistant","content":"done"}}',
    ]) + "\n")
    db = tmp_path / "h.db"
    res = indexer.index_one(str(jp), db_path=db, force=True)
    assert res["status"] == "summarized"

    from history.db import connect
    conn = connect(db)
    row = conn.execute(
        "SELECT outcome, category, notable, topics FROM sessions WHERE session_id='r1'"
    ).fetchone()
    conn.close()
    assert row["outcome"] == "shipped"
    assert row["category"] == "bugfix"
    assert row["notable"] == 1
    import json as _j
    assert _j.loads(row["topics"]) == ["periscope"]


def test_index_one_model_override_beats_meta_default(tmp_path, monkeypatch):
    from history import indexer
    from history.summarize import SummaryResult

    seen = {}
    monkeypatch.setattr(indexer, "get_anthropic_client", lambda: object())
    def _fake(client, rec, model=None):
        seen["model"] = model
        return SummaryResult(summary="s", tags=["a", "b", "c"], model=model or "?",
                             outcome="explored", category="research",
                             notable=False, topics=["x"])
    monkeypatch.setattr(indexer, "call_summarizer", _fake)

    jp = tmp_path / "real.jsonl"
    jp.write_text("\n".join([
        '{"type":"user","cwd":"/r","sessionId":"m1","timestamp":"2026-05-20T10:00:00.000Z","message":{"role":"user","content":"first request please"}}',
        '{"type":"assistant","sessionId":"m1","timestamp":"2026-05-20T10:01:00.000Z","message":{"role":"assistant","content":"ok"}}',
        '{"type":"user","cwd":"/r","sessionId":"m1","timestamp":"2026-05-20T10:02:00.000Z","message":{"role":"user","content":"second request please"}}',
        '{"type":"assistant","sessionId":"m1","timestamp":"2026-05-20T10:03:00.000Z","message":{"role":"assistant","content":"done"}}',
    ]) + "\n")
    indexer.index_one(str(jp), db_path=tmp_path / "h.db", force=True,
                      model="claude-sonnet-test")
    assert seen["model"] == "claude-sonnet-test"
