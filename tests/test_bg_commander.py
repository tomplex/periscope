import json

import pytest

from periscope import bg_commander as bgc


def test_parse_agents_json_keeps_only_background_sessions():
    raw = json.dumps([
        {"kind": "background", "sessionId": "a", "state": "done"},
        {"kind": "background", "sessionId": "b", "state": "blocked"},
        {"kind": "interactive", "sessionId": "c", "status": "idle"},
    ])
    assert bgc.parse_agents_json(raw) == {"a": "done", "b": "blocked"}


def test_parse_agents_json_tolerates_garbage():
    assert bgc.parse_agents_json("not json") == {}
    assert bgc.parse_agents_json("[]") == {}


def test_map_state_present():
    assert bgc.map_state("done", started_at=0, now=10, present=True) == "done"
    assert bgc.map_state("blocked", started_at=0, now=10, present=True) == "running"
    assert bgc.map_state("running", started_at=0, now=10, present=True) == "running"


def test_map_state_absent_young_stays_running():
    # absent within the grace window => not yet registered, keep running
    assert bgc.map_state(None, started_at=100, now=130, present=False) == "running"


def test_map_state_absent_old_is_done():
    assert bgc.map_state(None, started_at=100, now=200, present=False) == "done"


def test_dispatch_argv_pins_the_security_flags(monkeypatch):
    monkeypatch.setenv("PERISCOPE_CLAUDE_BIN", "/usr/bin/claude")
    argv = bgc._dispatch_argv()
    assert argv[0] == "/usr/bin/claude"        # the BARE binary, never the multi-word claude_exec() string
    assert "--bg" in argv
    assert "-p" in argv                         # REQUIRED — without it --bg never submits the prompt
    assert "--session-id" not in argv          # --bg ignores it; we capture the id it mints instead
    assert "--strict-mcp-config" in argv
    # --allowedTools is the LAST token: it's variadic, so a trailing prompt
    # positional would be swallowed → the prompt goes via stdin instead.
    assert argv[-2] == "--allowedTools"
    assert argv[-1] == bgc.config.BG_COMMANDER_ALLOWED_TOOLS


def test_dispatch_env_sets_caller_id():
    env = bgc._dispatch_env(handle="tok-1")
    assert env["PERISCOPE_CALLER_ID"] == "cmdr:tok-1"


def test_dispatch_env_strips_api_key(monkeypatch):
    # server.py load_dotenv()s ANTHROPIC_API_KEY into os.environ; it must NOT reach
    # the commander or claude bills API credits instead of the subscription.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-leak")
    env = bgc._dispatch_env(handle="tok-1")
    assert "ANTHROPIC_API_KEY" not in env


def test_parse_session_id_from_backgrounded_line():
    assert bgc._parse_session_id("backgrounded · 5bf0d4d5\n  claude agents") == "5bf0d4d5"
    assert bgc._parse_session_id("backgrounded · 5bf0d4d5-4751-4cd7-8c33") == "5bf0d4d5-4751-4cd7-8c33"
    assert bgc._parse_session_id("no id here") is None


@pytest.fixture(autouse=True)
def _db(fresh_activity_db):
    # bg_commander._conn() opens config.ACTIVITY_DB fresh each call; the fixture
    # repoints it at a temp file. No bg_commander-side connection cache to reset.
    yield


def test_insert_then_list_and_get():
    bgc.insert_job(id="j1", text="hello", cwd="/tmp", at=100)
    bgc.insert_job(id="j2", text="world", cwd="/tmp", at=200)
    jobs = bgc.list_jobs()
    assert [j.id for j in jobs] == ["j2", "j1"]          # newest-first
    assert bgc.get_job("j1") == bgc.Job(id="j1", text="hello", cwd="/tmp", status="running", started_at=100)
    assert bgc.get_job("nope") is None


def test_dispatch_captures_claude_id_and_records_job(monkeypatch):
    spawned = {}
    class FakeProc:
        stdout = "backgrounded · 9ab3cd12\n  claude agents   list sessions"
        stderr = ""
    def fake_run(argv, **kw):
        spawned["argv"], spawned["kw"] = argv, kw
        return FakeProc()
    monkeypatch.setattr(bgc.subprocess, "run", fake_run)
    jid = bgc.dispatch("do it", cwd="/tmp")
    # the job id is claude's minted id, parsed from stdout (NOT a pre-mint)
    assert jid == "9ab3cd12"
    job = bgc.get_job(jid)
    assert job is not None and job.status == "running" and job.text == "do it"
    assert spawned["kw"]["cwd"] == "/tmp"
    assert spawned["kw"]["input"] == "do it"     # prompt goes via stdin, not argv
    # the cmdr handle is a fresh token, distinct from claude's session id
    handle = spawned["kw"]["env"]["PERISCOPE_CALLER_ID"]
    assert handle.startswith("cmdr:") and handle != f"cmdr:{jid}"
    assert "do it" not in spawned["argv"]        # prompt is via stdin, not a positional


def test_dispatch_raises_when_no_session_id(monkeypatch):
    class FakeProc:
        stdout = "something went wrong"
        stderr = "error"
    monkeypatch.setattr(bgc.subprocess, "run", lambda *a, **k: FakeProc())
    with pytest.raises(RuntimeError):
        bgc.dispatch("do it", cwd="/tmp")


def test_sync_jobs_marks_done_and_stops_present(monkeypatch):
    bgc.insert_job(id="busy", text="x", cwd="/tmp", at=1000)
    bgc.insert_job(id="fin",  text="y", cwd="/tmp", at=1000)
    raw = json.dumps([
        {"kind": "background", "sessionId": "busy", "state": "blocked"},
        {"kind": "background", "sessionId": "fin",  "state": "done"},
    ])
    stopped = []
    bgc.sync_jobs(now=1001, agents_raw=raw, stop_fn=stopped.append)
    assert bgc.get_job("busy").status == "running"
    assert bgc.get_job("fin").status == "done"
    assert stopped == ["fin"]                      # proactive claude stop on a still-listed done session


def test_sync_jobs_absent_young_stays_running_old_reaped(monkeypatch):
    bgc.insert_job(id="young", text="x", cwd="/tmp", at=1000)
    bgc.insert_job(id="old",   text="y", cwd="/tmp", at=1000)
    stopped = []
    # young: now-started_at < 60 ; old: >= 60 ; neither present in the (empty) list
    bgc.sync_jobs(now=1030, agents_raw="[]", stop_fn=stopped.append)  # young still within grace
    assert bgc.get_job("young").status == "running"
    bgc.sync_jobs(now=1100, agents_raw="[]", stop_fn=stopped.append)  # both now old
    assert bgc.get_job("young").status == "done"
    assert bgc.get_job("old").status == "done"
    assert stopped == []                           # absent/reaped => no stop call (already gone)
