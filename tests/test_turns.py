"""Tests for periscope.turns — pane -> session -> transcript resolution."""
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


# ── session id scan + cache ──────────────────────────────────────────────

def _fake_ps(lines):
    class _R:
        stdout = "\n".join(lines)
    return lambda *a, **k: _R()


def test_scan_session_ids_matches_tmux_pane(monkeypatch):
    sid = "c0f5cc37-c50d-47e3-9b10-60bb363e4d10"
    monkeypatch.setattr(turns.subprocess, "run", _fake_ps([
        "100 zsh TMUX_PANE=%9 ATUIN_SESSION=x",
        f"200 claude TMUX_PANE=%9 CLAUDE_CODE_SESSION_ID={sid} FOO=bar",
        "300 other TMUX_PANE=%42 CLAUDE_CODE_SESSION_ID=ffffffff-0000-0000-0000-000000000000",
    ]))
    assert turns._scan_session_ids("%9") == [sid]
    # %42 has a session id but it's a different pane; %999 isn't present at all.
    assert turns._scan_session_ids("%999") == []
    # Guard against %9 matching %99 etc. (trailing-boundary match).
    assert turns._scan_session_ids("%4") == []


def test_session_id_for_pane_caches(monkeypatch):
    turns._PANE_SESSION.clear()
    calls = {"n": 0}

    def fake_scan(pane_id):
        calls["n"] += 1
        return ["aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"]

    monkeypatch.setattr(turns, "_scan_session_ids", fake_scan)
    assert turns.session_id_for_pane("%5", "/cwd").startswith("aaaa")
    assert turns.session_id_for_pane("%5", "/cwd").startswith("aaaa")
    assert calls["n"] == 1  # resolved once, then cache hit


def test_session_id_for_pane_picks_newest_among_candidates(tmp_path, monkeypatch):
    """Inherited-parent case: a pane's env carries two session ids; pick the one
    whose JSONL is in this cwd and newest (the pane's own live session)."""
    import os
    turns._PANE_SESSION.clear()
    cwd = "/Users/tom/dev/spawned"
    enc = _seed_projects(tmp_path, monkeypatch, cwd)
    _write_jsonl(enc / "parent.jsonl", cwd)
    _write_jsonl(enc / "own.jsonl", cwd)
    os.utime(enc / "parent.jsonl", (1_000_000, 1_000_000))
    os.utime(enc / "own.jsonl", (2_000_000, 2_000_000))   # newer = the pane's own
    monkeypatch.setattr(turns, "_scan_session_ids", lambda pid: ["parent", "own"])
    assert turns.session_id_for_pane("%8", cwd) == "own"


# ── get_turns_for_pane ───────────────────────────────────────────────────

def test_picks_pane_specific_session_when_cwd_shared(tmp_path, monkeypatch):
    """Two panes, one cwd, two sessions — the env signal disambiguates."""
    turns._PANE_SESSION.clear()
    cwd = "/Users/tom/dev/shared"
    enc = _seed_projects(tmp_path, monkeypatch, cwd)
    _write_jsonl(enc / "sid-A.jsonl", cwd, "conversation A")
    _write_jsonl(enc / "sid-B.jsonl", cwd, "conversation B")
    monkeypatch.setattr(turns, "tmux", lambda *a, **k: f"%7\t{cwd}")
    monkeypatch.setattr(turns, "session_id_for_pane", lambda pid, cwd: "sid-B")

    out = turns.get_turns_for_pane("main", 4)
    assert out["session_id"] == "sid-B"
    assert out["messages"][0]["text"] == "conversation B"


def test_falls_back_to_newest_when_no_session(tmp_path, monkeypatch):
    turns._PANE_SESSION.clear()
    cwd = "/Users/tom/dev/solo"
    enc = _seed_projects(tmp_path, monkeypatch, cwd)
    _write_jsonl(enc / "only.jsonl", cwd, "solo convo")
    monkeypatch.setattr(turns, "tmux", lambda *a, **k: f"%1\t{cwd}")
    monkeypatch.setattr(turns, "session_id_for_pane", lambda pid, cwd: None)

    out = turns.get_turns_for_pane("main", 0)
    assert out["session_id"] == "only"
    assert out["messages"][0]["text"] == "solo convo"


def test_none_when_no_transcript(tmp_path, monkeypatch):
    turns._PANE_SESSION.clear()
    monkeypatch.setattr(activity, "_PROJECTS_DIR", tmp_path)
    monkeypatch.setattr(turns, "tmux", lambda *a, **k: "%1\t/no/such/cwd")
    monkeypatch.setattr(turns, "session_id_for_pane", lambda pid, cwd: None)
    assert turns.get_turns_for_pane("main", 0) is None


def test_stale_cached_session_is_evicted(tmp_path, monkeypatch):
    """A cached session whose JSONL has vanished (cleared/rotated) is forgotten,
    and resolution falls back to the pane's current newest transcript."""
    cwd = "/Users/tom/dev/rot"
    enc = _seed_projects(tmp_path, monkeypatch, cwd)
    _write_jsonl(enc / "newsid.jsonl", cwd, "current")
    turns._PANE_SESSION.clear()
    turns._PANE_SESSION["%3"] = "goneSID"   # points at a JSONL that doesn't exist
    monkeypatch.setattr(turns, "tmux", lambda *a, **k: f"%3\t{cwd}")

    out = turns.get_turns_for_pane("main", 2)
    assert out["session_id"] == "newsid"
    assert "%3" not in turns._PANE_SESSION
