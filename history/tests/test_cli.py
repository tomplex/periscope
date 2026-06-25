import shutil
from unittest.mock import MagicMock, patch

from history.cli import main


def _fake_anthropic():
    client = MagicMock()
    blk = MagicMock(); blk.type = "tool_use"; blk.name = "save_session_summary"
    blk.input = {"summary": "x", "tags": ["a", "b", "c"]}
    m = MagicMock(); m.content = [blk]
    client.messages.create.return_value = m
    return client


def test_cli_help_no_verb(capsys):
    rc = main([])
    out = capsys.readouterr().out
    assert rc == 2
    assert "verbs:" in out


def test_cli_unknown_verb(capsys):
    rc = main(["whoami"])
    out = capsys.readouterr().out
    assert rc != 0
    assert "unknown verb" in out.lower()


def test_cli_backfill(tmp_path, fixture_dir, capsys):
    proj_dir = tmp_path / "projects"
    cwd_dir = proj_dir / "-Users-tom-dev-foo"; cwd_dir.mkdir(parents=True)
    shutil.copy(fixture_dir / "short_session.jsonl", cwd_dir / "s1.jsonl")
    # Back-date so live-skip doesn't fire
    import os
    os.utime(cwd_dir / "s1.jsonl", (1700000000, 1700000000))
    db_path = tmp_path / "h.db"
    with patch("history.indexer.get_anthropic_client", return_value=_fake_anthropic()):
        rc = main(["backfill",
                   "--projects-dir", str(proj_dir),
                   "--db-path", str(db_path),
                   "--workers", "1"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "scanned 1" in out.lower() or '"scanned": 1' in out


def test_cli_search(tmp_path, fixture_dir, capsys):
    proj_dir = tmp_path / "projects"
    cwd_dir = proj_dir / "-Users-tom-dev-foo"; cwd_dir.mkdir(parents=True)
    shutil.copy(fixture_dir / "normal_session.jsonl", cwd_dir / "n1.jsonl")
    import os
    os.utime(cwd_dir / "n1.jsonl", (1700000000, 1700000000))
    db_path = tmp_path / "h.db"
    with patch("history.indexer.get_anthropic_client", return_value=_fake_anthropic()):
        main(["backfill",
              "--projects-dir", str(proj_dir),
              "--db-path", str(db_path),
              "--workers", "1"])
    capsys.readouterr()  # discard backfill output
    rc = main(["search", "investigate", "--db-path", str(db_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "normal-001" in out


def test_cli_stats(tmp_path, capsys):
    db_path = tmp_path / "h.db"
    rc = main(["stats", "--db-path", str(db_path)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "sessions" in out.lower()


def test_cli_hook_delegates_to_run_hook(monkeypatch):
    import io
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    rc = main(["hook"])
    assert rc == 0


def test_cli_clean_removes_missing_files(tmp_path, capsys):
    from history.db import connect
    db_path = tmp_path / "h.db"
    conn = connect(db_path)
    conn.execute(
        "INSERT INTO sessions(session_id, jsonl_path, project_path, started_at, ended_at, "
        "duration_s, user_msg_count, asst_msg_count, tool_use_count, indexed_at, "
        "mechanical_version, source_mtime, source_size, files_touched, notable_cmds, "
        "tool_use_counts) "
        "VALUES ('orphan', '/nope/missing.jsonl', '/p', 0, 0, 0, 0, 0, 0, 0, 1, 0, 0, '[]', '[]', '{}')"
    )
    conn.commit()
    conn.close()
    rc = main(["clean", "--db-path", str(db_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "1 row" in out.lower() or "removed 1" in out.lower()
    conn = connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
    conn.close()


def test_cli_backfill_dry_run_without_projects_dir(tmp_path, capsys):
    """Regression: --dry-run with no --projects-dir crashed with AttributeError
    because find_jsonl_files(None) doesn't work; we now resolve to the default."""
    # Point at a real (empty) dir so the test is deterministic
    proj_dir = tmp_path / "empty-projects"
    proj_dir.mkdir()
    rc = main(["backfill", "--dry-run", "--projects-dir", str(proj_dir)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "would scan 0 jsonl files" in out
    # Now check the no-projects-dir path doesn't crash. We can't predict the
    # count (depends on the real user's ~/.claude/projects), so just assert
    # no exception and rc=0.
    rc2 = main(["backfill", "--dry-run"])
    assert rc2 == 0
