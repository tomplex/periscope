import shutil
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from history.backfill import backfill, find_jsonl_files


def _fake_anthropic():
    client = MagicMock()
    ok_block = MagicMock()
    ok_block.type = "tool_use"
    ok_block.name = "save_session_summary"
    ok_block.input = {"summary": "fake", "tags": ["a", "b", "c"]}
    msg = MagicMock(); msg.content = [ok_block]
    client.messages.create.return_value = msg
    return client


def test_find_jsonl_files(tmp_path):
    # Create a fake ~/.claude/projects layout
    proj_dir = tmp_path / "projects"
    (proj_dir / "-Users-tom-foo").mkdir(parents=True)
    (proj_dir / "-Users-tom-foo" / "abc.jsonl").write_text("")
    (proj_dir / "-Users-tom-foo" / "def.jsonl").write_text("")
    (proj_dir / "-Users-tom-bar").mkdir(parents=True)
    (proj_dir / "-Users-tom-bar" / "ghi.jsonl").write_text("")
    # Ignore non-jsonl files
    (proj_dir / "-Users-tom-foo" / "notes.md").write_text("")
    found = sorted(find_jsonl_files(projects_dir=proj_dir))
    assert len(found) == 3
    assert all(str(p).endswith(".jsonl") for p in found)


def test_backfill_indexes_each_jsonl(tmp_path, fixture_dir):
    proj_dir = tmp_path / "projects"
    cwd_dir = proj_dir / "-Users-tom-dev-foo"
    cwd_dir.mkdir(parents=True)
    shutil.copy(fixture_dir / "normal_session.jsonl", cwd_dir / "n1.jsonl")
    shutil.copy(fixture_dir / "short_session.jsonl", cwd_dir / "s1.jsonl")
    # Back-date the copied fixtures so live-skip doesn't trigger
    import os
    for p in cwd_dir.glob("*.jsonl"):
        os.utime(p, (1700000000, 1700000000))
    db_path = tmp_path / "h.db"
    client = _fake_anthropic()
    with patch("history.indexer.get_anthropic_client", return_value=client):
        result = backfill(projects_dir=proj_dir, db_path=db_path, workers=2)
    assert result["scanned"] == 2
    # short_session is trivial → no Haiku call. normal_session → 1 Haiku call.
    assert client.messages.create.call_count == 1
    from history.db import connect
    conn = connect(db_path)
    try:
        rows = conn.execute("SELECT session_id FROM sessions ORDER BY session_id").fetchall()
        ids = [r["session_id"] for r in rows]
        assert "abc-001" in ids       # from short_session
        assert "normal-001" in ids
    finally:
        conn.close()


def test_backfill_resumable(tmp_path, fixture_dir):
    proj_dir = tmp_path / "projects"
    cwd_dir = proj_dir / "-Users-tom-dev-foo"
    cwd_dir.mkdir(parents=True)
    shutil.copy(fixture_dir / "normal_session.jsonl", cwd_dir / "n1.jsonl")
    # Back-date
    import os
    os.utime(cwd_dir / "n1.jsonl", (1700000000, 1700000000))
    db_path = tmp_path / "h.db"
    client = _fake_anthropic()
    with patch("history.indexer.get_anthropic_client", return_value=client):
        backfill(projects_dir=proj_dir, db_path=db_path, workers=2)
        result2 = backfill(projects_dir=proj_dir, db_path=db_path, workers=2)
    # Second run: hash-cache-hit, no Haiku call
    assert result2["scanned"] == 1
    assert result2["statuses"].get("hash-cache-hit", 0) == 1
    assert client.messages.create.call_count == 1  # only the first backfill called
