import io
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import codex_pane_session_hook as hook
from periscope import session_binding_db


def _rollout(home: Path, sid: str, *, originator: str = "codex-tui") -> Path:
    path = home / "sessions" / "2026" / "07" / "30" / "rollout.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "type": "session_meta",
        "payload": {
            "session_id": sid,
            "originator": originator,
            "cli_version": "0.146.0",
        },
    }) + "\n")
    return path


def _payload(event: str, sid: str, path: Path) -> str:
    return json.dumps({
        "hook_event_name": event,
        "session_id": sid,
        "transcript_path": str(path),
        "turn_id": "turn-1",
        "cli_version": "0.146.0",
        "prompt": "must not be stored",
    })


def test_all_events_bind_and_append_sanitized_metadata(tmp_path, monkeypatch):
    home = tmp_path / "codex"
    monkeypatch.setenv("CODEX_HOME", str(home))
    path = _rollout(home, "root")
    db = tmp_path / "periscope.db"
    for event_name in hook.EVENTS:
        event = hook.parse_payload(io.StringIO(_payload(event_name, "root", path)))
        assert event is not None
        hook.record(event, pane_id="%4", db_path=db)

    with sqlite3.connect(db) as conn:
        binding = session_binding_db.get_binding(conn, "%4")
        assert binding is not None
        assert (binding.provider, binding.session_id) == ("codex", "root")
        rows = conn.execute(
            "SELECT event, turn_id FROM agent_session_events ORDER BY id"
        ).fetchall()
        assert {row[0] for row in rows} == hook.EVENTS
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(agent_session_events)")
        }
        assert "prompt" not in columns


def test_non_root_rollout_is_rejected_and_cannot_overwrite(tmp_path, monkeypatch):
    home = tmp_path / "codex"
    monkeypatch.setenv("CODEX_HOME", str(home))
    root_path = _rollout(home, "root")
    root = hook.parse_payload(io.StringIO(_payload("SessionStart", "root", root_path)))
    assert root is not None
    db = tmp_path / "db.sqlite"
    hook.record(root, pane_id="%1", db_path=db)

    root_path.write_text(json.dumps({
        "type": "session_meta",
        "payload": {"session_id": "child", "originator": "codex-subagent"},
    }) + "\n")
    child = hook.parse_payload(
        io.StringIO(_payload("SessionStart", "child", root_path))
    )
    assert child is None
    with sqlite3.connect(db) as conn:
        assert session_binding_db.get_binding(conn, "%1").session_id == "root"


def test_even_valid_different_session_cannot_overwrite_before_evidence_gate(
    tmp_path, monkeypatch
):
    home = tmp_path / "codex"
    monkeypatch.setenv("CODEX_HOME", str(home))
    first_path = _rollout(home, "first")
    db = tmp_path / "db.sqlite"
    first = hook.parse_payload(
        io.StringIO(_payload("SessionStart", "first", first_path))
    )
    assert first is not None
    hook.record(first, pane_id="%1", db_path=db)
    first_path.unlink()
    second_path = _rollout(home, "second")
    second = hook.parse_payload(
        io.StringIO(_payload("SessionStart", "second", second_path))
    )
    assert second is not None
    hook.record(second, pane_id="%1", db_path=db)
    with sqlite3.connect(db) as conn:
        binding = session_binding_db.get_binding(conn, "%1")
        assert binding.session_id == "first"
        assert binding.evidence == "codex-hook-unverified"


def test_subprocess_is_silent_always_zero_and_bootstraps_legacy_db(tmp_path):
    repo = Path(__file__).resolve().parent.parent
    home = tmp_path / "codex"
    rollout = _rollout(home, "session")
    xdg = tmp_path / "config"
    db = xdg / "periscope" / "periscope.db"
    db.parent.mkdir(parents=True)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE pane_sessions "
            "(pane_id TEXT PRIMARY KEY, session_id TEXT, updated_at INTEGER)"
        )
    env = {
        **os.environ,
        "TMUX_PANE": "%9",
        "CODEX_HOME": str(home),
        "XDG_CONFIG_HOME": str(xdg),
    }
    result = subprocess.run(
        [sys.executable, str(repo / "codex_pane_session_hook.py")],
        input=_payload("Stop", "session", rollout),
        text=True,
        capture_output=True,
        cwd=tmp_path,
        env=env,
    )
    assert result.returncode == 0
    assert result.stdout == result.stderr == ""
    with sqlite3.connect(db) as conn:
        assert session_binding_db.get_binding(conn, "%9").session_id == "session"


def test_bad_inputs_are_silent_and_successful(tmp_path):
    script = Path(__file__).resolve().parent.parent / "codex_pane_session_hook.py"
    for payload, pane in (("{", "%1"), ("{}", "%1"), ("{}", ""),):
        result = subprocess.run(
            [sys.executable, str(script)],
            input=payload,
            text=True,
            capture_output=True,
            cwd=tmp_path,
            env={**os.environ, "TMUX_PANE": pane},
        )
        assert result.returncode == 0
        assert result.stdout == result.stderr == ""
