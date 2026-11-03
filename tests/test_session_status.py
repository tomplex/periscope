"""Tests for periscope.session_status — authoritative pane state from
~/.claude/sessions/<pid>.json, replacing TUI scraping for the
working/needs-input/idle signal."""

import json
import shutil
import subprocess
import time
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

import periscope.session_status as ss
import periscope.tmux as tmux_mod
from periscope import agent_processes


@pytest.fixture(autouse=True)
def _reset_caches(monkeypatch):
    # Each test seeds its own sessions dir + live-pid set; clear the per-poll
    # caches so state doesn't leak between tests. _config_dirs_cache is SEEDED
    # empty rather than cleared: _build_index consults pane_config_dirs(), and
    # an empty cache miss would fork real tmux/ps inside every index test.
    monkeypatch.setattr(ss, "_index_cache", None)
    monkeypatch.setattr(ss, "_pane_pids_cache", None)
    monkeypatch.setattr(ss, "_config_dirs_cache", (time.time(), {}), raising=False)


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
    d = {"pid": pid, "sessionId": sid, "status": status, "cwd": "/x", "kind": "interactive"}
    if waitingFor is not None:
        d["waitingFor"] = waitingFor
    return d


# ── status → state mapping ───────────────────────────────────────────────


@pytest.mark.parametrize(
    "status,state",
    [
        ("busy", "working"),
        ("waiting", "needs-input"),
        ("idle", "idle"),
    ],
)
def test_maps_trusted_statuses(tmp_path, monkeypatch, status, state):
    _seed(tmp_path, monkeypatch, _sess(100, "sid-a", status))
    out = ss.session_state_for("sid-a")
    assert out == {"state": state, "status": status, "waiting_for": None}


def test_waiting_for_passed_through(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch, _sess(101, "sid-w", "waiting", waitingFor="approve AskUserQuestion"))
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


# ── live claudes and the process table come off process_snapshot ─────────

# Processes aged 01-02:08:58, 22:09:53, 05:33 and 91-10:00:00 (as etime
# would print them) plus a plugin server whose argv
# mentions .claude, as process_snapshot's real `… lstart= state= args=` rows
# read at _PS_NOW. lstart prints in UTC (agent_processes.ps_env's TZ=UTC), so
# both this snapshot's times and _PS_NOW are expressed in UTC — mixing a
# naive-local _PS_NOW with a UTC-parsed lstart would read ages off by the
# host's UTC offset, and wrong by an extra hour across a DST fall-back.
_PS_SNAPSHOT = (
    " 9448     1 Thu Jul 30 10:00:00 2026     S+   /Users/tom/.local/bin/claude --resume 0b6f2a1e-1111-2222-3333-444455556666\n"
    " 9876  9448 Thu Jul 30 13:59:05 2026     S    /Users/tom/.local/share/claude/versions/2.1.161\n"
    "11023     1 Thu Jul 30 14:19:27 2026     S    node /Users/tom/.claude/plugins/cache/lgtm/dist/server/server.js --port 9900\n"
    "  329     1 Fri Jul 31 12:03:25 2026     Ss   /usr/libexec/logd\n"
    "    1     0 Fri May  1 02:08:58 2026     Ss   /sbin/launchd\n"
)
_PS_NOW = datetime(2026, 7, 31, 12, 8, 58, tzinfo=UTC).timestamp()


def _snapshot(monkeypatch, text=_PS_SNAPSHOT):
    monkeypatch.setattr(agent_processes, "process_snapshot", lambda: agent_processes.parse_ps_snapshot(text))
    monkeypatch.setattr(ss, "time", SimpleNamespace(time=lambda: _PS_NOW))


def test_live_claude_pids_matches_claude_paths(monkeypatch):
    # Full-path launcher and versioned binary both count; `node …/.claude/…` doesn't.
    _snapshot(monkeypatch)
    assert ss._live_claude_pids() == {9448, 9876}


def test_claude_procs_age_from_start_time(monkeypatch):
    # The same ages `ps etime` reported: now minus each process's start.
    _snapshot(monkeypatch)
    assert ss._claude_procs() == {
        9448: 26 * 3600 + 8 * 60 + 58,
        9876: 22 * 3600 + 9 * 60 + 53,
    }


def test_proc_table_matches_the_command_table(monkeypatch):
    # What `ps -axo pid=,ppid=,command=` gave: children by parent in ps order,
    # and each pid's full command line.
    _snapshot(monkeypatch)
    kids, cmds = ss.proc_table()
    assert kids == {1: [9448, 11023, 329], 9448: [9876], 0: [1]}
    assert cmds == {
        9448: "/Users/tom/.local/bin/claude --resume 0b6f2a1e-1111-2222-3333-444455556666",
        9876: "/Users/tom/.local/share/claude/versions/2.1.161",
        11023: "node /Users/tom/.claude/plugins/cache/lgtm/dist/server/server.js --port 9900",
        329: "/usr/libexec/logd",
        1: "/sbin/launchd",
    }


def test_ps_failure_reads_as_no_processes(monkeypatch):
    monkeypatch.setattr(agent_processes, "process_snapshot", lambda: None)
    assert ss._claude_procs() == {}
    assert ss.proc_table() == ({}, {})


def test_one_ps_serves_the_whole_refresh(monkeypatch, mocker):
    monkeypatch.setattr(agent_processes, "_snapshot_cache", None)
    take = mocker.patch.object(
        agent_processes, "_take_snapshot", return_value=agent_processes.parse_ps_snapshot(_PS_SNAPSHOT)
    )
    mocker.patch("subprocess.run", side_effect=AssertionError("session_status forked its own ps"))
    ss.proc_table()
    ss._claude_procs()
    agent_processes.process_snapshot()
    take.assert_called_once()


# ── claude_proc_for: measurement independent of status trust ─────────────


def _seed_procs(tmp_path, monkeypatch, session, procs):
    monkeypatch.setattr(ss, "_SESSIONS_DIR", tmp_path)
    (tmp_path / f"{session['pid']}.json").write_text(json.dumps(session))
    monkeypatch.setattr(ss, "_claude_procs", lambda: procs)


def test_claude_proc_for_returns_stats(tmp_path, monkeypatch):
    # status "shell" is untrusted for STATE, but measurement still applies —
    # a bloated claude deserves the hint regardless of what it's doing.
    _seed_procs(tmp_path, monkeypatch, _sess(300, "sid-m", "shell"), {300: 4 * 86400})
    monkeypatch.setattr(ss.resources, "footprint_mb", {300: 4400}.get)
    assert ss.claude_proc_for("sid-m") == {"pid": 300, "mem_mb": 4400, "age_s": 4 * 86400}


def test_claude_proc_for_unreadable_footprint_none(tmp_path, monkeypatch):
    # Exited since the ps snapshot: the next tick drops it from _claude_procs.
    _seed_procs(tmp_path, monkeypatch, _sess(303, "sid-x", "busy"), {303: 60})
    monkeypatch.setattr(ss.resources, "footprint_mb", lambda pid: None)
    assert ss.claude_proc_for("sid-x") is None


def test_claude_proc_for_dead_pid_none(tmp_path, monkeypatch):
    _seed_procs(tmp_path, monkeypatch, _sess(301, "sid-d", "busy"), {})
    assert ss.claude_proc_for("sid-d") is None


def test_claude_proc_for_unmapped_none(tmp_path, monkeypatch):
    _seed_procs(tmp_path, monkeypatch, _sess(302, "sid-k", "busy"), {302: 1})
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
        {
            10: "-zsh",
            11: "/Users/tom/.local/bin/claude",
            20: "-zsh",
            21: "node script.js",
            22: "/Users/tom/.local/bin/claude",
        },
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
    monkeypatch.setattr(
        ss,
        "proc_table",
        lambda: (_ for _ in ()).throw(AssertionError("proc_table must not fork when there are no panes")),
    )
    assert ss.pane_claude_pids() == {}


# ── pane -> CLAUDE_CONFIG_DIR scan (moved from resurrect) ────────────────


def test_pane_config_dirs_cached_at_15s(mocker):
    """pane_config_dirs snapshots once per 15s window: env is immutable for a
    process lifetime, so a 1s TTL would add a fork/s for nothing."""
    ss._config_dirs_cache = None
    mocker.patch.object(ss, "pane_claude_pids", return_value={"%1": 111})
    mocker.patch.object(ss, "proc_table", return_value=({}, {111: "claude"}))
    envs = mocker.patch.object(
        ss, "_env_by_pid", return_value={111: "claude CLAUDE_CONFIG_DIR=/Users/t/.claude-b PATH=/x"}
    )
    first = ss.pane_config_dirs()
    second = ss.pane_config_dirs()
    assert first == {"%1": "/Users/t/.claude-b"}
    assert second == first
    assert envs.call_count == 1  # second call served from cache


def test_env_tail_strips_the_command():
    """`ps eww` concatenates command and env with no delimiter. A process whose
    COMMAND merely mentions a variable must not be read as setting it."""
    alt = "/Users/tom/.claude-b"
    cmd = "grep -o :CLAUDE_CONFIG_DIR=[^\\011]*|:claude"
    # command mentions it, environment does not
    assert ss._CONFIG_DIR_RE.search(ss._env_tail(cmd, f"{cmd} PATH=/usr/bin SHELL=/bin/zsh") or "") is None
    # genuine env value is found
    tail = ss._env_tail("claude --resume x", f"claude --resume x PATH=/usr/bin CLAUDE_CONFIG_DIR={alt} TERM=xterm")
    assert ss._CONFIG_DIR_RE.search(tail or "").group(1) == alt
    # command prefix mismatch (ps raced / truncated) yields nothing rather than a guess
    assert ss._env_tail("claude", f"something-else CLAUDE_CONFIG_DIR={alt}") is None


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_env_tail_survives_non_ascii_argv_under_a_utf8_locale(monkeypatch, tmp_path):
    """agent_processes.process_snapshot's ps and _env_by_pid's ps must share
    one locale (agent_processes.ps_env): under en_US.UTF-8 a C-locale ps
    vis-encodes non-ASCII argv bytes (`café` -> `cafM-CM-)`), so a snapshot
    taken in C and an env lookup taken in the server's own locale disagree on
    the command prefix and _env_tail drops the pane. node stands in for a
    long-lived claude here; /bin/sleep won't do — ps can't read a
    SIP-sealed binary's environment at all, locale aside."""
    import os

    script = tmp_path / "sleepy.js"
    script.write_text("setTimeout(() => {}, 30000);\n")
    # LC_ALL and LC_CTYPE override LANG, so without clearing/setting them too
    # this test passes on a runner that exports either as C regardless of
    # whether ps_env() is applied to the ps eww call below.
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    monkeypatch.delenv("LC_CTYPE", raising=False)
    monkeypatch.setenv("LC_ALL", "en_US.UTF-8")
    agent_processes.clear_caches()
    proc = subprocess.Popen(
        ["/Users/tom/.local/bin/café/claude", str(script)],
        executable=shutil.which("node"),
        env={**os.environ, "LANG": "en_US.UTF-8", "CLAUDE_CONFIG_DIR": "/tmp/x"},
    )
    try:
        cmd = agent_processes.process_snapshot()[proc.pid].argv
        tail = ss._env_tail(cmd, ss._env_by_pid([proc.pid])[proc.pid])
    finally:
        proc.kill()
        proc.wait()
    assert tail is not None
    assert "CLAUDE_CONFIG_DIR=/tmp/x" in tail


def test_pane_profiles_reads_the_wrapper_profile_from_env(mocker):
    """The profile is observable ONLY in env: the wrapper consumes the typed
    `claude full` word and execs `command claude --settings ...`, so it never
    reaches argv."""
    ss._config_dirs_cache = None
    mocker.patch.object(ss, "pane_claude_pids", return_value={"%1": 111, "%2": 222})
    mocker.patch.object(ss, "proc_table", return_value=({}, {111: "claude", 222: "claude"}))
    mocker.patch.object(
        ss,
        "_env_by_pid",
        return_value={
            111: "claude PATH=/x CLAUDE_WRAPPER_PROFILE=full TERM=xterm",
            222: "claude PATH=/x TERM=xterm",
        },
    )
    # Only the full pane gets an entry; a default-profile pane has none.
    assert ss.pane_profiles() == {"%1": "full"}


def test_pane_models_reads_the_spawn_model_from_env(mocker):
    ss._config_dirs_cache = None
    mocker.patch.object(ss, "pane_claude_pids", return_value={"%1": 111, "%2": 222})
    mocker.patch.object(ss, "proc_table", return_value=({}, {111: "claude", 222: "claude"}))
    mocker.patch.object(
        ss,
        "_env_by_pid",
        return_value={
            111: "claude PATH=/x ANTHROPIC_MODEL=opus TERM=xterm",
            222: "claude PATH=/x TERM=xterm",
        },
    )
    assert ss.pane_models() == {"%1": "opus"}


def test_pane_profiles_and_config_dirs_share_one_ps_fork(mocker):
    """Both bindings come off ONE `ps eww` snapshot. Caching parsed values
    per-variable instead of the env tail would fork once per variable on every
    poll, on the /api/state hot path."""
    ss._config_dirs_cache = None
    mocker.patch.object(ss, "pane_claude_pids", return_value={"%1": 111})
    mocker.patch.object(ss, "proc_table", return_value=({}, {111: "claude"}))
    envs = mocker.patch.object(
        ss, "_env_by_pid", return_value={111: "claude CLAUDE_CONFIG_DIR=/Users/t/.claude-b CLAUDE_WRAPPER_PROFILE=full"}
    )
    assert ss.pane_config_dirs() == {"%1": "/Users/t/.claude-b"}
    assert ss.pane_profiles() == {"%1": "full"}
    assert envs.call_count == 1


def test_pane_profiles_ignores_a_command_that_merely_mentions_the_var(mocker):
    """The `ps eww` no-delimiter trap, for the profile variable specifically."""
    ss._config_dirs_cache = None
    cmd = "rg CLAUDE_WRAPPER_PROFILE=full"
    mocker.patch.object(ss, "pane_claude_pids", return_value={"%1": 111})
    mocker.patch.object(ss, "proc_table", return_value=({}, {111: cmd}))
    mocker.patch.object(ss, "_env_by_pid", return_value={111: f"{cmd} PATH=/x"})
    assert ss.pane_profiles() == {}


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


# ── pane_session_activity: which of several panes is using its session ──


def test_pane_session_activity_takes_each_live_panes_newest_file(tmp_path, monkeypatch):
    # resurrect's keeper among panes resuming one session: a dead pid (502, a
    # leftover file) or a pane with no session file (503) must not compete.
    import os

    _seed(
        tmp_path,
        monkeypatch,
        _sess(500, "sid-a", "busy"),
        _sess(501, "sid-b", "idle"),
        _sess(502, "sid-c", "idle"),
        live_pids={500, 501},
    )
    os.utime(tmp_path / "500.json", (1000, 1000))
    os.utime(tmp_path / "501.json", (2000, 2000))
    monkeypatch.setattr(ss, "pane_claude_pids", lambda: {"%3": 500, "%4": 501, "%5": 502, "%6": 503})
    assert ss.pane_session_activity() == {"%3": ("sid-a", 1000.0), "%4": ("sid-b", 2000.0)}


def test_pane_session_activity_reads_the_file_live_session_id_reads(tmp_path, mocker):
    """Newest-across-dirs picked a recycled pid's stale file in another
    account's dir: its mtime and sid were not the pane's — and disagreed with
    what live_session_id_for_pane (the rest of periscope) reports."""
    import os

    a, b = tmp_path / "a", tmp_path / "b"
    (a / "sessions").mkdir(parents=True)
    (b / "sessions").mkdir(parents=True)
    stale, live = a / "sessions" / "300.json", b / "sessions" / "300.json"
    stale.write_text('{"sessionId": "stale-sid", "pid": 300}')
    live.write_text('{"sessionId": "live-sid", "pid": 300}')
    os.utime(stale, (2000, 2000))
    os.utime(live, (1000, 1000))
    mocker.patch.object(ss, "_SESSIONS_DIR", a / "sessions")
    mocker.patch.object(ss, "pane_config_dirs", return_value={"%7": str(b)})
    mocker.patch.object(ss, "pane_claude_pids", return_value={"%7": 300})
    mocker.patch.object(ss, "_live_claude_pids", return_value={300})
    assert ss.pane_session_activity() == {"%7": ("live-sid", 1000.0)}
    assert ss.live_session_id_for_pane("%7") == "live-sid"


def test_pane_session_activity_without_panes_scans_nothing(monkeypatch):
    # rewrite_save_file always calls this; with no claude panes (conftest's tmux
    # stub) it must not fork ps or index the real ~/.claude/sessions first.
    def scanned():
        raise AssertionError("scanned the host")

    monkeypatch.setattr(ss, "pane_claude_pids", dict)
    monkeypatch.setattr(ss, "_live_claude_pids", scanned)
    monkeypatch.setattr(ss, "_indexes", scanned)
    assert ss.pane_session_activity() == {}


# ── multi-account sessions-dir scan ──────────────────────────────────────


def test_build_index_scans_every_live_config_dir(tmp_path, mocker):
    """A pane on CLAUDE_CONFIG_DIR=~/.claude-b writes sessions/<pid>.json
    there; indexing only ~/.claude made the authoritative sid source blind on
    exactly the panes an account swap creates."""
    a, b = tmp_path / "a", tmp_path / "b"
    (a / "sessions").mkdir(parents=True)
    (b / "sessions").mkdir(parents=True)
    (a / "sessions" / "100.json").write_text('{"sessionId": "sid-a", "pid": 100}')
    (b / "sessions" / "200.json").write_text('{"sessionId": "sid-b", "pid": 200}')
    mocker.patch.object(ss, "_SESSIONS_DIR", a / "sessions")
    mocker.patch.object(ss, "pane_config_dirs", return_value={"%9": str(b)})
    by_sid, by_pid = ss._build_index()
    assert "sid-a" in by_sid and "sid-b" in by_sid
    assert {c["sessionId"] for c in by_pid[200]} == {"sid-b"}


def test_by_pid_prefers_the_panes_own_config_dir(tmp_path, mocker):
    """A recycled pid can leave a STALE <pid>.json in account A's dir while
    account B's live claude wrote the real one — last-insert-wins handed back
    a wrong sid, which would feed the TTL-exempt rebind pass 0 and reattach a
    month-old entry's immunity fields."""
    a, b = tmp_path / "a", tmp_path / "b"
    (a / "sessions").mkdir(parents=True)
    (b / "sessions").mkdir(parents=True)
    (a / "sessions" / "300.json").write_text('{"sessionId": "stale-sid", "pid": 300}')
    (b / "sessions" / "300.json").write_text('{"sessionId": "live-sid", "pid": 300}')
    mocker.patch.object(ss, "_SESSIONS_DIR", a / "sessions")
    mocker.patch.object(ss, "pane_config_dirs", return_value={"%7": str(b)})
    mocker.patch.object(ss, "pane_claude_pids", return_value={"%7": 300})
    mocker.patch.object(ss, "_live_claude_pids", return_value={300})
    assert ss.live_session_id_for_pane("%7") == "live-sid"


def test_by_pid_mtime_fallback_when_pane_dir_unknown(tmp_path, mocker):
    """When the ps scan missed the pane's config dir, prefer the newest file —
    a live claude rewrites its session file; a stale one never does."""
    import os

    a, b = tmp_path / "a", tmp_path / "b"
    (a / "sessions").mkdir(parents=True)
    (b / "sessions").mkdir(parents=True)
    old, new = a / "sessions" / "400.json", b / "sessions" / "400.json"
    old.write_text('{"sessionId": "old-sid", "pid": 400}')
    new.write_text('{"sessionId": "new-sid", "pid": 400}')
    os.utime(old, (1000, 1000))
    os.utime(new, (2000, 2000))
    mocker.patch.object(ss, "_SESSIONS_DIR", a / "sessions")
    mocker.patch.object(ss, "pane_config_dirs", return_value={"%1": str(b)})  # dirs to scan
    mocker.patch.object(ss, "pane_claude_pids", return_value={"%5": 400})  # %5's dir unknown
    mocker.patch.object(ss, "_live_claude_pids", return_value={400})
    assert ss.live_session_id_for_pane("%5") == "new-sid"


# ── resume lineage from argv ─────────────────────────────────────────────


def test_pane_resume_ids_reads_resume_uuid_from_argv(mocker):
    """`--resume <uuid>` in the claude argv names the session lineage even if
    the live sid rotates — the safety net for the documented rotation-on-resume
    case (CLAUDE.md pane→session section)."""
    mocker.patch.object(ss, "pane_claude_pids", return_value={"%3": 500, "%4": 501})
    mocker.patch.object(
        ss,
        "proc_table",
        return_value=(
            {},
            {
                500: "claude --resume 828e314f-76fe-42af-a750-e48d1f0a5316",
                501: "claude --system-prompt xyz",
            },
        ),
    )
    assert ss.pane_resume_ids() == {"%3": "828e314f-76fe-42af-a750-e48d1f0a5316"}


def test_pane_efforts_reads_the_launch_effort_from_argv(mocker):
    mocker.patch.object(ss, "pane_claude_pids", return_value={"%1": 111, "%2": 222, "%3": 333})
    mocker.patch.object(
        ss,
        "proc_table",
        return_value=(
            {},
            {
                111: "claude --dangerously-load-development-channels server:periscope --effort xhigh",
                222: "claude --dangerously-load-development-channels server:periscope",
                333: "claude --effort=low",
            },
        ),
    )
    assert ss.pane_efforts() == {"%1": "xhigh", "%3": "low"}


# ── pane -> root pid comes off the list-panes snapshot ───────────────────


def test_tmux_pane_pids_read_the_snapshot(monkeypatch):
    rows = [{"pane_id": "%5", "pane_pid": "4241"}, {"pane_id": "%6", "pane_pid": "4242"}]
    monkeypatch.setattr(tmux_mod, "_panes_snapshot", (time.monotonic(), rows))
    assert ss._tmux_pane_pids() == {"%5": 4241, "%6": 4242}


def test_tmux_pane_pids_empty_without_tmux(monkeypatch):
    def no_tmux(*args):
        raise FileNotFoundError("tmux")

    monkeypatch.setattr(tmux_mod, "_panes_snapshot", None)
    monkeypatch.setattr(tmux_mod, "tmux", no_tmux)
    assert ss._tmux_pane_pids() == {}


def test_tmux_pane_pids_use_the_configured_socket(monkeypatch, mocker):
    # The old bare `tmux list-panes` ignored PERISCOPE_TMUX_SOCKET.
    monkeypatch.setenv("PERISCOPE_TMUX_SOCKET", "periscope-x")
    monkeypatch.setattr(tmux_mod, "_panes_snapshot", None)
    run = mocker.patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, stdout="", stderr=""))
    ss._tmux_pane_pids()
    assert run.call_args.args[0][:3] == ["tmux", "-L", "periscope-x"]
