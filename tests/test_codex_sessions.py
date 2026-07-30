import json

from periscope import codex_sessions


def test_catalog_reads_uuid_metadata_and_ignores_names(monkeypatch, tmp_path):
    home = tmp_path / "codex home"
    sessions = home / "sessions" / "2026" / "07" / "30"
    sessions.mkdir(parents=True)
    sid = "019fb027-5e13-74c3-9ed9-0c69e1914367"
    good = sessions / "rollout-good.jsonl"
    good.write_text(json.dumps({
        "timestamp": "2026-07-30T12:00:00Z",
        "type": "session_meta",
        "payload": {
            "id": sid, "cwd": "/tmp/project", "cli_version": "0.146.0",
        },
    }) + "\n")
    (sessions / "rollout-name.jsonl").write_text(json.dumps({
        "type": "session_meta", "payload": {"id": "friendly-name", "cwd": "/tmp"},
    }) + "\n")
    monkeypatch.setenv("CODEX_HOME", str(home))

    result = codex_sessions.catalog()

    assert list(result) == [sid]
    assert result[sid].path == good
    assert result[sid].cwd == "/tmp/project"
    assert result[sid].cli_version == "0.146.0"


def test_empty_codex_home_uses_default(monkeypatch):
    monkeypatch.setenv("CODEX_HOME", "")
    assert codex_sessions.codex_home() == codex_sessions.Path("~/.codex").expanduser()
