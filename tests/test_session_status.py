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
    monkeypatch.setattr(ss, "_claude_procs_cache", None)
    monkeypatch.setattr(ss, "_proc_table_cache", None)
    monkeypatch.setattr(ss, "_pane_pids_cache", None)
    monkeypatch.setattr(ss, "_config_dirs_cache", None, raising=False)


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

_PS_SAMPLE = (
    " 9448  473120     01-02:08:58 /Users/tom/.local/bin/claude\n"
    " 9876  605744        22:09:53 /Users/tom/.local/share/claude/versions/2.1.161\n"
    "  329    1024        05:33 /usr/libexec/logd\n"
    "    1     512     91-10:00:00 /sbin/launchd\n"
)


def test_live_claude_pids_matches_claude_paths(monkeypatch):
    class _R:
        stdout = _PS_SAMPLE

    monkeypatch.setattr(ss, "_claude_procs_cache", None)
    monkeypatch.setattr(ss.subprocess, "run", lambda *a, **k: _R())
    assert ss._live_claude_pids() == {9448, 9876}


def test_claude_procs_carry_rss_and_age(monkeypatch):
    class _R:
        stdout = _PS_SAMPLE

    monkeypatch.setattr(ss, "_claude_procs_cache", None)
    monkeypatch.setattr(ss.subprocess, "run", lambda *a, **k: _R())
    assert ss._claude_procs() == {
        9448: (473120, 26 * 3600 + 8 * 60 + 58),
        9876: (605744, 22 * 3600 + 9 * 60 + 53),
    }


# ── etime parsing ([[dd-]hh:]mm:ss) ──────────────────────────────────────

@pytest.mark.parametrize("s,secs", [
    ("05:33", 5 * 60 + 33),
    ("22:09:53", 22 * 3600 + 9 * 60 + 53),
    ("01-02:08:58", 86400 + 2 * 3600 + 8 * 60 + 58),
    ("3-00:00:01", 3 * 86400 + 1),
    ("", 0),
    ("garbage", 0),
    ("1:2:3:4", 0),
])
def test_parse_etime(s, secs):
    assert ss.parse_etime(s) == secs


# ── claude_proc_for: measurement independent of status trust ─────────────

def _seed_procs(tmp_path, monkeypatch, session, procs):
    monkeypatch.setattr(ss, "_SESSIONS_DIR", tmp_path)
    (tmp_path / f"{session['pid']}.json").write_text(json.dumps(session))
    monkeypatch.setattr(ss, "_claude_procs", lambda: procs)


def test_claude_proc_for_returns_stats(tmp_path, monkeypatch):
    # status "shell" is untrusted for STATE, but measurement still applies —
    # a bloated claude deserves the hint regardless of what it's doing.
    _seed_procs(tmp_path, monkeypatch, _sess(300, "sid-m", "shell"),
                {300: (4_500_000, 4 * 86400)})
    assert ss.claude_proc_for("sid-m") == {
        "pid": 300, "rss_kb": 4_500_000, "age_s": 4 * 86400}


def test_claude_proc_for_dead_pid_none(tmp_path, monkeypatch):
    _seed_procs(tmp_path, monkeypatch, _sess(301, "sid-d", "busy"), {})
    assert ss.claude_proc_for("sid-d") is None


def test_claude_proc_for_unmapped_none(tmp_path, monkeypatch):
    _seed_procs(tmp_path, monkeypatch, _sess(302, "sid-k", "busy"), {302: (1, 1)})
    assert ss.claude_proc_for("sid-nope") is None
    assert ss.claude_proc_for(None) is None


# ── pane -> claude pid walk ──────────────────────────────────────────────

def _seed_walk(monkeypatch, panes, kids, cmds):
    monkeypatch.setattr(ss, "_tmux_pane_pids", lambda: panes)
    monkeypatch.setattr(ss, "proc_table", lambda: (kids, cmds))


def test_pane_claude_pids_finds_first_claude_descendant(monkeypatch):
    # %1: shell(10) -> claude(11); %2: shell(20) -> node(21) -> claude(22)
    _seed_walk(
        monkeypatch,
        {"%1": 10, "%2": 20},
        {10: [11], 20: [21], 21: [22]},
        {10: "-zsh", 11: "/Users/tom/.local/bin/claude", 20: "-zsh",
         21: "node script.js", 22: "/Users/tom/.local/bin/claude"},
    )
    assert ss.pane_claude_pids() == {"%1": 11, "%2": 22}


def test_pane_claude_pids_stops_at_first_claude(monkeypatch):
    # A subagent/tool child below the pane's own claude must not win.
    _seed_walk(
        monkeypatch,
        {"%1": 10},
        {10: [11], 11: [12]},
        {10: "-zsh", 11: "claude", 12: "claude --subagent"},
    )
    assert ss.pane_claude_pids() == {"%1": 11}


def test_pane_claude_pids_skips_shell_only_panes(monkeypatch):
    _seed_walk(monkeypatch, {"%1": 10}, {10: [11]}, {10: "-zsh", 11: "vim"})
    assert ss.pane_claude_pids() == {}


def test_pane_claude_pids_empty_without_tmux(monkeypatch):
    monkeypatch.setattr(ss, "_tmux_pane_pids", dict)
    monkeypatch.setattr(ss, "proc_table", lambda: (_ for _ in ()).throw(
        AssertionError("proc_table must not fork when there are no panes")))
    assert ss.pane_claude_pids() == {}


# ── pane -> CLAUDE_CONFIG_DIR scan (moved from resurrect) ────────────────

def test_pane_config_dirs_cached_at_15s(mocker):
    """pane_config_dirs snapshots once per 15s window: env is immutable for a
    process lifetime, so a 1s TTL would add a fork/s for nothing."""
    ss._config_dirs_cache = None
    mocker.patch.object(ss, "pane_claude_pids", return_value={"%1": 111})
    mocker.patch.object(ss, "proc_table", return_value=({}, {111: "claude"}))
    envs = mocker.patch.object(
        ss, "_env_by_pid",
        return_value={111: "claude CLAUDE_CONFIG_DIR=/Users/t/.claude-b PATH=/x"})
    first = ss.pane_config_dirs()
    second = ss.pane_config_dirs()
    assert first == {"%1": "/Users/t/.claude-b"}
    assert second == first
    assert envs.call_count == 1  # second call served from cache


def test_config_dir_from_ps_reads_only_the_env_tail():
    """`ps eww` concatenates command and env with no delimiter. A process whose
    COMMAND merely mentions the variable must not be read as setting it."""
    alt = "/Users/tom/.claude-b"
    cmd = "grep -o :CLAUDE_CONFIG_DIR=[^\\011]*|:claude"
    # command mentions it, environment does not
    assert ss._config_dir_from_ps(cmd, f"{cmd} PATH=/usr/bin SHELL=/bin/zsh") is None
    # genuine env value is found
    assert ss._config_dir_from_ps(
        "claude --resume x", f"claude --resume x PATH=/usr/bin CLAUDE_CONFIG_DIR={alt} TERM=xterm"
    ) == alt
    # command prefix mismatch (ps raced / truncated) yields nothing rather than a guess
    assert ss._config_dir_from_ps("claude", f"something-else CLAUDE_CONFIG_DIR={alt}") is None


# ── live_session_id_for_pane: the pane's CURRENT session id ──────────────

def test_live_session_id_for_pane(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch, _sess(400, "sid-live", "busy"))
    monkeypatch.setattr(ss, "pane_claude_pids", lambda: {"%7": 400})
    assert ss.live_session_id_for_pane("%7") == "sid-live"


def test_live_session_id_for_pane_unknown_pane(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch, _sess(401, "sid-live", "busy"))
    monkeypatch.setattr(ss, "pane_claude_pids", lambda: {"%7": 401})
    assert ss.live_session_id_for_pane("%9") is None
    assert ss.live_session_id_for_pane("") is None


def test_live_session_id_for_pane_no_file(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch, live_pids={402})
    monkeypatch.setattr(ss, "pane_claude_pids", lambda: {"%7": 402})
    assert ss.live_session_id_for_pane("%7") is None


def test_live_session_id_for_pane_ignores_dead_pid(tmp_path, monkeypatch):
    # Stale <pid>.json left by an exited claude whose pid has been recycled:
    # the file is present but the pid is not a live claude.
    _seed(tmp_path, monkeypatch, _sess(403, "sid-stale", "busy"), live_pids=set())
    monkeypatch.setattr(ss, "pane_claude_pids", lambda: {"%7": 403})
    assert ss.live_session_id_for_pane("%7") is None
