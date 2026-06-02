"""Tests for periscope.turns.get_turns_for_pane (stateless cwd -> messages)."""
import json


def _write_jsonl(path, cwd):
    line = {
        "type": "user", "sessionId": path.stem, "cwd": cwd,
        "gitBranch": "main", "timestamp": "2026-06-01T10:00:00.000Z",
        "uuid": "u1", "parentUuid": None,
        "message": {"role": "user", "content": "hello transcript"},
    }
    path.write_text(json.dumps(line) + "\n")


def test_get_turns_for_pane_resolves_messages(tmp_path, monkeypatch):
    import periscope.activity as activity
    cwd = "/Users/tom/dev/turnsproj"
    enc_dir = tmp_path / activity._encode_cwd(cwd)
    enc_dir.mkdir(parents=True)
    _write_jsonl(enc_dir / "sid-123.jsonl", cwd)
    monkeypatch.setattr(activity, "_PROJECTS_DIR", tmp_path)

    from periscope.turns import get_turns_for_pane
    out = get_turns_for_pane(cwd)
    assert out is not None
    assert out["session_id"] == "sid-123"
    assert out["jsonl_path"].endswith("sid-123.jsonl")
    assert out["messages"][0]["role"] == "user"
    assert out["messages"][0]["text"] == "hello transcript"


def test_get_turns_for_pane_none_when_no_match(tmp_path, monkeypatch):
    import periscope.activity as activity
    monkeypatch.setattr(activity, "_PROJECTS_DIR", tmp_path)
    from periscope.turns import get_turns_for_pane
    assert get_turns_for_pane("/no/such/cwd") is None
