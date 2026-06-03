"""Tests for periscope.session_status — authoritative pane state from
~/.claude/sessions/<pid>.json, replacing TUI scraping for the
working/needs-input/idle signal."""
import json

import pytest

import periscope.session_status as ss


@pytest.fixture(autouse=True)
def _reset_caches(monkeypatch):
    # Each test seeds its own sessions dir + live-pid set; clear the per-poll
    # caches so state doesn't leak between tests.
    monkeypatch.setattr(ss, "_index_cache", None)
    monkeypatch.setattr(ss, "_claude_pids_cache", None)


def _seed(tmp_path, monkeypatch, *sessions, live_pids=None):
    """Write one sessions/<pid>.json per dict and point the module at them.
    `live_pids` defaults to every seeded pid being a live claude."""
    monkeypatch.setattr(ss, "_SESSIONS_DIR", tmp_path)
    pids = set()
    for s in sessions:
        (tmp_path / f"{s['pid']}.json").write_text(json.dumps(s))
        pids.add(int(s["pid"]))
    if live_pids is None:
        live_pids = pids
    monkeypatch.setattr(ss, "_live_claude_pids", lambda: set(live_pids))


def _sess(pid, sid, status, waitingFor=None):
    d = {"pid": pid, "sessionId": sid, "status": status,
         "cwd": "/x", "kind": "interactive"}
    if waitingFor is not None:
        d["waitingFor"] = waitingFor
    return d


# ── status → state mapping ───────────────────────────────────────────────

@pytest.mark.parametrize("status,state", [
    ("busy", "working"),
    ("waiting", "needs-input"),
    ("idle", "idle"),
])
def test_maps_trusted_statuses(tmp_path, monkeypatch, status, state):
    _seed(tmp_path, monkeypatch, _sess(100, "sid-a", status))
    out = ss.session_state_for("sid-a")
    assert out == {"state": state, "status": status, "waiting_for": None}


def test_waiting_for_passed_through(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch,
          _sess(101, "sid-w", "waiting", waitingFor="approve AskUserQuestion"))
    out = ss.session_state_for("sid-w")
    assert out["state"] == "needs-input"
    assert out["waiting_for"] == "approve AskUserQuestion"


# ── statuses we don't trust → None (caller falls back to scraping) ───────

@pytest.mark.parametrize("status", ["shell", "spinning", "", "weird"])
def test_untrusted_status_returns_none(tmp_path, monkeypatch, status):
    _seed(tmp_path, monkeypatch, _sess(102, "sid-s", status))
    assert ss.session_state_for("sid-s") is None


# ── resolution guards ────────────────────────────────────────────────────

def test_unmapped_sid_returns_none(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch, _sess(103, "sid-known", "busy"))
    assert ss.session_state_for("sid-missing") is None


def test_none_sid_returns_none(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch, _sess(104, "sid-x", "busy"))
    assert ss.session_state_for(None) is None


def test_dead_pid_returns_none(tmp_path, monkeypatch):
    # File present + trusted status, but the pid is not a live claude process.
    _seed(tmp_path, monkeypatch, _sess(105, "sid-dead", "busy"), live_pids=set())
    assert ss.session_state_for("sid-dead") is None


def test_recycled_pid_excluded(tmp_path, monkeypatch):
    # pid alive but belongs to some other (non-claude) process.
    _seed(tmp_path, monkeypatch, _sess(106, "sid-rec", "working"), live_pids={999})
    assert ss.session_state_for("sid-rec") is None


# ── robustness ───────────────────────────────────────────────────────────

def test_malformed_file_skipped(tmp_path, monkeypatch):
    monkeypatch.setattr(ss, "_SESSIONS_DIR", tmp_path)
    monkeypatch.setattr(ss, "_live_claude_pids", lambda: {200})
    (tmp_path / "200.json").write_text("{not json")
    (tmp_path / "201.json").write_text(json.dumps(_sess(201, "sid-ok", "busy")))
    monkeypatch.setattr(ss, "_live_claude_pids", lambda: {200, 201})
    assert ss.session_state_for("sid-ok") == {"state": "working", "status": "busy", "waiting_for": None}


def test_missing_sessions_dir_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(ss, "_SESSIONS_DIR", tmp_path / "nope")
    monkeypatch.setattr(ss, "_live_claude_pids", lambda: set())
    assert ss.session_state_for("sid-any") is None


# ── live-claude pid detection parses ps output ───────────────────────────

def test_live_claude_pids_matches_claude_paths(monkeypatch):
    sample = (
        " 9448 /Users/tom/.local/bin/claude\n"
        " 9876 /Users/tom/.local/share/claude/versions/2.1.161\n"
        "  329 /usr/libexec/logd\n"
        "    1 /sbin/launchd\n"
    )

    class _R:
        stdout = sample

    monkeypatch.setattr(ss, "_claude_pids_cache", None)
    monkeypatch.setattr(ss.subprocess, "run", lambda *a, **k: _R())
    assert ss._live_claude_pids() == {9448, 9876}
