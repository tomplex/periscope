"""Tests for periscope.turns (pane -> session -> transcript) and the
channel_shim producer that records the pane -> session mapping."""
import json

import periscope.activity as activity
import periscope.turns as turns


def _write_jsonl(path, cwd, text="hello transcript"):
    path.write_text(json.dumps({
        "type": "user", "sessionId": path.stem, "cwd": cwd,
        "gitBranch": "main", "timestamp": "2026-06-01T10:00:00.000Z",
        "uuid": "u1", "parentUuid": None,
        "message": {"role": "user", "content": text},
    }) + "\n")


def _seed_projects(tmp_path, monkeypatch, cwd):
    enc = tmp_path / activity._encode_cwd(cwd)
    enc.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(activity, "_PROJECTS_DIR", tmp_path)
    return enc


# ── session_id_for_pane reads the shim-written map ───────────────────────

def test_session_id_for_pane_reads_map_file(tmp_path, monkeypatch):
    monkeypatch.setattr(turns, "PANE_SESSIONS_DIR", tmp_path)
    sid = "c0f5cc37-c50d-47e3-9b10-60bb363e4d10"
    (tmp_path / "%56").write_text(sid + "\n")
    assert turns.session_id_for_pane("%56") == sid
    assert turns.session_id_for_pane("%999") is None   # no file for that pane
    assert turns.session_id_for_pane("") is None


# ── get_turns_for_pane ───────────────────────────────────────────────────

def test_picks_pane_specific_session_when_cwd_shared(tmp_path, monkeypatch):
    """Two panes, one cwd, two sessions — the recorded session id disambiguates."""
    cwd = "/Users/tom/dev/shared"
    enc = _seed_projects(tmp_path, monkeypatch, cwd)
    _write_jsonl(enc / "sid-A.jsonl", cwd, "conversation A")
    _write_jsonl(enc / "sid-B.jsonl", cwd, "conversation B")
    monkeypatch.setattr(turns, "tmux", lambda *a, **k: f"%7\t{cwd}")
    monkeypatch.setattr(turns, "session_id_for_pane", lambda pid: "sid-B")

    out = turns.get_turns_for_pane("main", 4)
    assert out["session_id"] == "sid-B"
    assert out["messages"][0]["text"] == "conversation B"


def test_resolves_session_under_different_cwd_dir(tmp_path, monkeypatch):
    """The session started in /a but the pane has since cd'd to /b, so its JSONL
    is under /a's encoded dir. Glob-by-session-id finds it regardless of the
    pane's current cwd (the worktree-switch case)."""
    start_cwd = "/Users/tom/dev/started-here"
    cur_cwd = "/Users/tom/dev/worktrees/now-here"
    enc = tmp_path / activity._encode_cwd(start_cwd)
    enc.mkdir(parents=True)
    _write_jsonl(enc / "sid-X.jsonl", start_cwd, "moved convo")
    monkeypatch.setattr(activity, "_PROJECTS_DIR", tmp_path)
    monkeypatch.setattr(turns, "tmux", lambda *a, **k: f"%2\t{cur_cwd}")
    monkeypatch.setattr(turns, "session_id_for_pane", lambda pid: "sid-X")

    out = turns.get_turns_for_pane("main", 1)
    assert out["session_id"] == "sid-X"
    assert out["messages"][0]["text"] == "moved convo"


def test_falls_back_to_newest_when_no_session(tmp_path, monkeypatch):
    cwd = "/Users/tom/dev/solo"
    enc = _seed_projects(tmp_path, monkeypatch, cwd)
    _write_jsonl(enc / "only.jsonl", cwd, "solo convo")
    monkeypatch.setattr(turns, "tmux", lambda *a, **k: f"%1\t{cwd}")
    monkeypatch.setattr(turns, "session_id_for_pane", lambda pid: None)

    out = turns.get_turns_for_pane("main", 0)
    assert out["session_id"] == "only"
    assert out["messages"][0]["text"] == "solo convo"


def test_falls_back_when_recorded_session_jsonl_missing(tmp_path, monkeypatch):
    """A recorded session whose JSONL isn't in this cwd (stale after /clear)
    falls back to the pane's current newest transcript, not nothing."""
    cwd = "/Users/tom/dev/rot"
    enc = _seed_projects(tmp_path, monkeypatch, cwd)
    _write_jsonl(enc / "current.jsonl", cwd, "current convo")
    monkeypatch.setattr(turns, "tmux", lambda *a, **k: f"%3\t{cwd}")
    monkeypatch.setattr(turns, "session_id_for_pane", lambda pid: "gone-sid")

    out = turns.get_turns_for_pane("main", 2)
    assert out["session_id"] == "current"


def test_none_when_no_transcript(tmp_path, monkeypatch):
    monkeypatch.setattr(activity, "_PROJECTS_DIR", tmp_path)
    monkeypatch.setattr(turns, "tmux", lambda *a, **k: "%1\t/no/such/cwd")
    monkeypatch.setattr(turns, "session_id_for_pane", lambda pid: None)
    assert turns.get_turns_for_pane("main", 0) is None


# ── pane_session_hook producer (UserPromptSubmit) ─────────────────────────

def test_hook_records_pane_session(tmp_path, monkeypatch):
    import io
    import pane_session_hook as hook
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("TMUX_PANE", "%56")
    # session id comes from the hook payload (authoritative/current), not env.
    monkeypatch.setattr("sys.stdin", io.StringIO('{"session_id":"sess-abc","cwd":"/x"}'))
    hook.record()
    assert (tmp_path / "periscope" / "pane_sessions" / "%56").read_text() == "sess-abc"


def test_hook_noop_without_tmux_pane(tmp_path, monkeypatch):
    import io
    import pane_session_hook as hook
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("TMUX_PANE", raising=False)
    monkeypatch.setattr("sys.stdin", io.StringIO('{"session_id":"x"}'))
    hook.record()
    assert not (tmp_path / "periscope" / "pane_sessions").exists()
