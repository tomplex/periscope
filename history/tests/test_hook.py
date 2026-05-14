import io
import json
from unittest.mock import patch

from history.hook import run_hook


def test_hook_indexes_session_from_stdin(monkeypatch, tmp_path):
    jsonl = tmp_path / "x.jsonl"
    jsonl.write_text('{"type":"permission-mode","permissionMode":"default","sessionId":"x"}\n')
    payload = json.dumps({"transcript_path": str(jsonl), "session_id": "x"})
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))
    calls = []

    def fake_index_one(path: str, **kw):
        calls.append((path, kw))
        return {"status": "trivial", "session_id": "x"}

    with patch("history.hook.index_one", side_effect=fake_index_one):
        rc = run_hook()
    assert rc == 0
    assert calls == [(str(jsonl), {})]


def test_hook_returns_0_on_missing_payload(monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    with patch("history.hook.index_one") as m:
        rc = run_hook()
    assert rc == 0  # silent no-op — never block Claude Code shutdown
    m.assert_not_called()


def test_hook_returns_0_on_missing_file(monkeypatch):
    payload = json.dumps({"transcript_path": "/nope/missing.jsonl"})
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))
    with patch("history.hook.index_one") as m:
        rc = run_hook()
    assert rc == 0
    m.assert_not_called()


def test_hook_swallows_indexer_exception(monkeypatch, tmp_path):
    jsonl = tmp_path / "x.jsonl"
    jsonl.write_text("")
    payload = json.dumps({"transcript_path": str(jsonl)})
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))
    with patch("history.hook.index_one", side_effect=RuntimeError("kaboom")):
        rc = run_hook()
    assert rc == 0  # never propagate errors out of a hook
