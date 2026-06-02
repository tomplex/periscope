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


def test_get_turns_for_pane_picks_newest_when_multiple(tmp_path, monkeypatch):
    import os
    import periscope.activity as activity
    cwd = "/Users/tom/dev/turnsproj"
    enc_dir = tmp_path / activity._encode_cwd(cwd)
    enc_dir.mkdir(parents=True)
    older = enc_dir / "sid-old.jsonl"
    newer = enc_dir / "sid-new.jsonl"
    _write_jsonl(older, cwd)
    _write_jsonl(newer, cwd)
    # Force `newer` to have a strictly later mtime regardless of write order.
    os.utime(older, (1_000_000, 1_000_000))
    os.utime(newer, (2_000_000, 2_000_000))
    monkeypatch.setattr(activity, "_PROJECTS_DIR", tmp_path)

    from periscope.turns import get_turns_for_pane
    out = get_turns_for_pane(cwd)
    assert out["session_id"] == "sid-new"


def test_get_turns_for_pane_none_when_no_match(tmp_path, monkeypatch):
    import periscope.activity as activity
    monkeypatch.setattr(activity, "_PROJECTS_DIR", tmp_path)
    from periscope.turns import get_turns_for_pane
    assert get_turns_for_pane("/no/such/cwd") is None
