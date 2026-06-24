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
    argv = bgc._dispatch_argv(session_id="sid-1", text="do the thing")
    assert argv[0] == "/usr/bin/claude"        # the BARE binary, never the multi-word claude_exec() string
    assert "--bg" in argv
    assert argv[argv.index("--session-id") + 1] == "sid-1"
    assert "--strict-mcp-config" in argv
    assert argv[argv.index("--allowedTools") + 1] == bgc.config.BG_COMMANDER_ALLOWED_TOOLS
    assert argv[-1] == "do the thing"          # the command is the trailing positional


def test_dispatch_env_sets_caller_id():
    env = bgc._dispatch_env(session_id="sid-1")
    assert env["PERISCOPE_CALLER_ID"] == "cmdr:sid-1"


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


def test_running_job_ids_excludes_done():
    bgc.insert_job(id="r", text="x", cwd="/tmp", at=1)
    bgc.insert_job(id="d", text="y", cwd="/tmp", at=2)
    bgc._set_status("d", "done")
    assert bgc.running_job_ids() == {"r"}


def test_dispatch_inserts_running_row_then_popens(monkeypatch):
    spawned = {}
    def fake_popen(argv, **kw):
        spawned["argv"], spawned["kw"] = argv, kw
        return object()
    monkeypatch.setattr(bgc.subprocess, "Popen", fake_popen)
    jid = bgc.dispatch("do it", cwd="/tmp")
    # row exists immediately (closes the absent-window race from the write side)
    job = bgc.get_job(jid)
    assert job is not None and job.status == "running" and job.text == "do it"
    assert spawned["kw"]["cwd"] == "/tmp"
    assert spawned["kw"]["env"]["PERISCOPE_CALLER_ID"] == f"cmdr:{jid}"
    assert spawned["argv"][-1] == "do it"


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
