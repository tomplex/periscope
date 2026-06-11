"""Tests for periscope/narrator.py — pure decision core (this task) and
the impure tick (Task 4)."""
import logging

import pytest

from periscope import narrator
from periscope.activity import PaneStatusRow


@pytest.fixture(autouse=True)
def narrator_enabled(monkeypatch):
    """Tests control the latch: key present, latch reset per test."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(narrator, "_enabled_checked", None)


def _row(**over):
    base = dict(pane_id="%1", session_id="sid-a", status="doing a thing",
                generated_at=1000, jsonl_size=2048, seen_name="claude",
                renamed_at=None)
    base.update(over)
    return PaneStatusRow(**base)


NOW = 1000 + narrator.MIN_INTERVAL_S  # exactly at the interval gate


# ---- should_regenerate ----

def test_no_row_is_first_sight():
    assert narrator.should_regenerate(
        None, session_id="sid-a", jsonl_size=10, now=NOW) == "first_sight"


def test_unchanged_size_and_session_never_regenerates():
    assert narrator.should_regenerate(
        _row(), session_id="sid-a", jsonl_size=2048, now=NOW + 9999) is None


def test_grown_jsonl_regenerates():
    assert narrator.should_regenerate(
        _row(), session_id="sid-a", jsonl_size=4096, now=NOW) == "grew"


def test_shrunk_jsonl_also_regenerates():
    # Size DIFFERS, not "grew" numerically — covers truncate/rewrite cases.
    assert narrator.should_regenerate(
        _row(), session_id="sid-a", jsonl_size=10, now=NOW) == "grew"


def test_session_switch_detected():
    # /clear mints a new session id with a smaller JSONL; a size-only check
    # could miss it (and pane-id recycling across restarts looks the same).
    assert narrator.should_regenerate(
        _row(), session_id="sid-B", jsonl_size=2048, now=NOW) == "session_switch"


def test_interval_gate_blocks_both_reasons():
    early = 1000 + narrator.MIN_INTERVAL_S - 1
    assert narrator.should_regenerate(
        _row(), session_id="sid-a", jsonl_size=4096, now=early) is None
    assert narrator.should_regenerate(
        _row(), session_id="sid-B", jsonl_size=2048, now=early) is None


def test_placeholder_row_is_not_a_session_switch():
    # stamp_pane_rename inserts session_id=None; treating None→sid as a
    # session switch would reset the renamed_at the stamp just wrote,
    # defeating the manual-rename cooldown. It regenerates via the
    # size-differs path instead.
    placeholder = _row(session_id=None, status="", generated_at=0,
                       jsonl_size=0, renamed_at=5000)
    assert narrator.should_regenerate(
        placeholder, session_id="sid-a", jsonl_size=100, now=NOW) == "grew"


# ---- parse_response ----

def test_parse_response_happy_path():
    out = narrator.parse_response(
        '{"status": "fixing flaky reconcile test", "rename": null}')
    assert out == narrator.NarratorResult(
        status="fixing flaky reconcile test", rename=None)


def test_parse_response_with_rename():
    out = narrator.parse_response('{"status": "s", "rename": "fs-liveness"}')
    assert out.rename == "fs-liveness"


def test_parse_response_strips_code_fences():
    out = narrator.parse_response('```json\n{"status": "s", "rename": null}\n```')
    assert out is not None and out.status == "s"


def test_parse_response_garbage_returns_none():
    assert narrator.parse_response("I think the status is...") is None


def test_parse_response_non_dict_json_returns_none():
    assert narrator.parse_response('["status"]') is None


def test_parse_response_missing_status_returns_none():
    assert narrator.parse_response('{"rename": "x"}') is None


def test_parse_response_overlength_status_returns_none():
    long = "x" * (narrator.STATUS_MAX_LEN + 1)
    assert narrator.parse_response(f'{{"status": "{long}", "rename": null}}') is None


def test_parse_response_non_string_rename_dropped_status_kept():
    out = narrator.parse_response('{"status": "s", "rename": 42}')
    assert out is not None and out.rename is None


# ---- rename_decision ----

def test_rename_decision_passes_valid_suggestion():
    assert narrator.rename_decision(
        "fs-liveness", current_name="claude", row=_row(), now=NOW) == "fs-liveness"


def test_rename_decision_none_suggestion():
    assert narrator.rename_decision(
        None, current_name="claude", row=_row(), now=NOW) is None


def test_rename_decision_drops_equal_to_current():
    assert narrator.rename_decision(
        "claude", current_name="claude", row=_row(), now=NOW) is None


def test_rename_decision_respects_cooldown():
    row = _row(renamed_at=NOW - narrator.RENAME_COOLDOWN_S + 60)
    assert narrator.rename_decision(
        "fs-liveness", current_name="claude", row=row, now=NOW) is None


def test_rename_decision_allows_after_cooldown_expiry():
    row = _row(renamed_at=NOW - narrator.RENAME_COOLDOWN_S - 1)
    assert narrator.rename_decision(
        "fs-liveness", current_name="claude", row=row, now=NOW) == "fs-liveness"


@pytest.mark.parametrize("bad", [
    "a-name-far-too-long-to-accept",   # > 25 chars
    "Has-Uppercase",
    "four-dash-words-here",            # 4 words
    "spaces in name",
    "-leading-dash",
])
def test_rename_decision_format_guards(bad):
    assert narrator.rename_decision(
        bad, current_name="claude", row=_row(), now=NOW) is None


def test_rename_decision_no_row_allows():
    assert narrator.rename_decision(
        "fs-liveness", current_name="claude", row=None, now=NOW) == "fs-liveness"


# ---- is_external_rename ----

def test_external_rename_detected_when_name_differs_from_seen():
    assert narrator.is_external_rename(_row(seen_name="claude"), "human-name") is True


def test_no_external_rename_when_name_matches_seen():
    assert narrator.is_external_rename(_row(seen_name="claude"), "claude") is False


def test_no_external_rename_when_seen_name_never_recorded():
    assert narrator.is_external_rename(_row(seen_name=None), "anything") is False


# ---- pick_regenerations ----

def test_pick_regenerations_caps_oldest_first():
    cands = [(700, "%7"), (100, "%1"), (300, "%3"), (500, "%5"),
             (200, "%2"), (600, "%6"), (400, "%4")]
    assert narrator.pick_regenerations(cands) == ["%1", "%2", "%3", "%4", "%5"]


def test_pick_regenerations_under_cap_returns_all():
    assert narrator.pick_regenerations([(2, "%b"), (1, "%a")]) == ["%a", "%b"]


# ---- build_narrator_prompt ----

def test_build_narrator_prompt_carries_all_signals():
    p = narrator.build_narrator_prompt(
        window_name="claude", branch="tc/foo", pr=123, cwd="/repo",
        signals={
            "recent_user_prompts": ["implement liveness check"],
            "recent_tool_calls": ["Edit anthology/liveness.py"],
            "files_touched": ["anthology/liveness.py"],
        })
    assert "current_name: claude" in p
    assert "tc/foo" in p and "PR #123" in p
    assert "/repo" in p
    assert "implement liveness check" in p
    assert "Edit anthology/liveness.py" in p
    assert str(narrator.STATUS_MAX_LEN) in p     # status length rule in-prompt
    assert '"rename": null' in p                 # the keep-name few-shot
    assert "JSON" in p


def test_build_narrator_prompt_omits_empty_sections():
    p = narrator.build_narrator_prompt(
        window_name="w", branch=None, pr=None, cwd="/repo", signals={})
    assert "branch:" not in p
    assert "recent user prompts" not in p
    assert "files touched" not in p


# ---- disabled latch ----

def test_enabled_true_with_key(monkeypatch):
    assert narrator._enabled() is True


def test_disabled_without_key_logs_once(monkeypatch, caplog):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(narrator, "_enabled_checked", None)
    with caplog.at_level(logging.INFO, logger="periscope"):
        assert narrator._enabled() is False
        assert narrator._enabled() is False
    disable_lines = [r for r in caplog.records if "narrator disabled" in r.message]
    assert len(disable_lines) == 1


# ---- tick (impure shell) ------------------------------------------------
#
# Real SQLite via the fresh_db pattern; only the IO boundaries are
# monkeypatched (claude_complete / tmux / transcript_summary_from_path /
# git caches) — all patched on the narrator namespace, where they're bound.

import json as _json
import time as _time

from periscope import activity, config


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ACTIVITY_DB", tmp_path / "t.db")
    activity._CONN = None
    yield
    if activity._CONN is not None:
        activity._CONN.close()
        activity._CONN = None


@pytest.fixture
def tick_env(fresh_db, tmp_path, monkeypatch):
    """A projects dir with one transcript, a pane mapped to it, and every
    IO boundary stubbed. Returns a dict of knobs the tests adjust."""
    projects = tmp_path / "projects"
    d = projects / "-repo"
    d.mkdir(parents=True)
    jsonl = d / "sid-a.jsonl"
    jsonl.write_text(_json.dumps(
        {"type": "user", "message": {"role": "user", "content": "hi"}}) + "\n")
    monkeypatch.setattr(activity, "_PROJECTS_DIR", projects)

    def set_session(pane_id, sid):
        with activity._LOCK:
            c = activity._conn()
            c.execute("INSERT OR REPLACE INTO pane_sessions "
                      "(pane_id, session_id, updated_at) VALUES (?,?,0)",
                      (pane_id, sid))
            c.commit()
    set_session("%1", "sid-a")

    env = {
        "jsonl": jsonl,
        "set_session": set_session,
        "haiku_calls": [],
        "tmux_calls": [],
        "response": '{"status": "fixing flaky reconcile test", "rename": null}',
    }

    def fake_complete(prompt, model="claude-haiku-4-5"):
        env["haiku_calls"].append(prompt)
        r = env["response"]
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setattr(narrator, "claude_complete", fake_complete)
    monkeypatch.setattr(narrator, "tmux",
                        lambda *a: env["tmux_calls"].append(a) or "")
    monkeypatch.setattr(narrator, "transcript_summary_from_path",
                        lambda p, **kw: {"recent_user_prompts": ["hi"]})
    monkeypatch.setattr(narrator, "cached_git_state", lambda cwd: {"branch": "main"})
    monkeypatch.setattr(narrator, "cached_pr_state", lambda cwd, b: {})
    return env


def _pane(pane_id="%1", name="claude", session="s", index=0, cwd="/repo"):
    return ({"pane_id": pane_id, "name": name, "session": session,
             "index": index, "cwd": cwd}, {"is_claude": True, "state": "idle"})


def test_tick_generates_first_status(tick_env):
    narrator.tick([_pane()])
    assert len(tick_env["haiku_calls"]) == 1
    row = activity.get_pane_status("%1")
    assert row.status == "fixing flaky reconcile test"
    assert row.session_id == "sid-a"
    assert row.jsonl_size == tick_env["jsonl"].stat().st_size
    assert row.seen_name == "claude"
    assert row.renamed_at is None
    assert tick_env["tmux_calls"] == []   # rename: null → no tmux


def test_tick_skips_pane_without_session_mapping(tick_env):
    narrator.tick([_pane(pane_id="%99")])   # no pane_sessions row, NO cwd fallback
    assert tick_env["haiku_calls"] == []
    assert activity.get_pane_status("%99") is None


def test_tick_skips_when_jsonl_missing(tick_env):
    tick_env["set_session"]("%1", "sid-gone")
    narrator.tick([_pane()])
    assert tick_env["haiku_calls"] == []


def test_tick_idle_pane_never_regenerates(tick_env):
    narrator.tick([_pane()])
    tick_env["haiku_calls"].clear()
    # Same size, same session, interval long past — still no call.
    row = activity.get_pane_status("%1")
    activity.upsert_pane_status(
        activity.PaneStatusRow(**{**row.__dict__, "generated_at": 1}))
    narrator.tick([_pane()])
    assert tick_env["haiku_calls"] == []


def test_tick_applies_rename_and_records_event(tick_env):
    tick_env["response"] = '{"status": "s", "rename": "fs-liveness"}'
    narrator.tick([_pane()])
    assert ("rename-window", "-t", "s:0", "fs-liveness") in tick_env["tmux_calls"]
    row = activity.get_pane_status("%1")
    assert row.renamed_at is not None
    assert row.seen_name == "fs-liveness"   # narrator-applied name becomes seen
    events = activity.events_for("%1", None, None)
    assert any(e["kind"] == "rename" and "claude → fs-liveness" in e["text"]
               for e in events)


def test_tick_haiku_exception_keeps_previous_row(tick_env):
    narrator.tick([_pane()])
    before = activity.get_pane_status("%1")
    # Make it a candidate again: bigger file, interval long past.
    tick_env["jsonl"].write_text(tick_env["jsonl"].read_text() + "x" * 100)
    activity.upsert_pane_status(
        activity.PaneStatusRow(**{**before.__dict__, "generated_at": 1}))
    tick_env["response"] = RuntimeError("haiku down")
    narrator.tick([_pane()])   # must not raise
    assert activity.get_pane_status("%1").status == before.status


def test_tick_garbage_response_keeps_previous_row(tick_env):
    narrator.tick([_pane()])
    tick_env["jsonl"].write_text(tick_env["jsonl"].read_text() + "x" * 100)
    row = activity.get_pane_status("%1")
    activity.upsert_pane_status(
        activity.PaneStatusRow(**{**row.__dict__, "generated_at": 1}))
    tick_env["response"] = "not json at all"
    narrator.tick([_pane()])
    assert activity.get_pane_status("%1").status == "fixing flaky reconcile test"


def test_tick_external_rename_starts_cooldown_instead_of_renaming(tick_env):
    size = tick_env["jsonl"].stat().st_size
    activity.upsert_pane_status(activity.PaneStatusRow(
        pane_id="%1", session_id="sid-a", status="old", generated_at=1,
        jsonl_size=size + 1, seen_name="claude", renamed_at=None))
    tick_env["response"] = '{"status": "s", "rename": "fs-liveness"}'
    now = int(_time.time())
    narrator.tick([_pane(name="human-chosen")])   # differs from seen_name
    assert tick_env["tmux_calls"] == []           # never clobber the human
    row = activity.get_pane_status("%1")
    assert row.renamed_at >= now                  # cooldown started
    assert row.seen_name == "human-chosen"        # new name recorded


def test_tick_rename_cooldown_blocks_but_status_updates(tick_env):
    size = tick_env["jsonl"].stat().st_size
    now = int(_time.time())
    activity.upsert_pane_status(activity.PaneStatusRow(
        pane_id="%1", session_id="sid-a", status="old", generated_at=1,
        jsonl_size=size + 1, seen_name="claude", renamed_at=now - 60))
    tick_env["response"] = '{"status": "new status", "rename": "fs-liveness"}'
    narrator.tick([_pane()])
    assert tick_env["tmux_calls"] == []
    row = activity.get_pane_status("%1")
    assert row.status == "new status"
    assert row.renamed_at == now - 60             # cooldown preserved


def test_tick_session_switch_resets_cooldown(tick_env):
    size = tick_env["jsonl"].stat().st_size
    now = int(_time.time())
    activity.upsert_pane_status(activity.PaneStatusRow(
        pane_id="%1", session_id="sid-OLD", status="old", generated_at=1,
        jsonl_size=size, seen_name="claude", renamed_at=now - 60))
    narrator.tick([_pane()])                      # mapped session is sid-a
    row = activity.get_pane_status("%1")
    assert row.session_id == "sid-a"
    assert row.renamed_at is None                 # recycled-pane cooldown gone


def test_tick_caps_regenerations_per_tick(tick_env):
    panes = []
    for i in range(2, 9):                         # %2..%8: 7 candidates
        sid = f"sid-{i}"
        (tick_env["jsonl"].parent / f"{sid}.jsonl").write_text("{}\n")
        tick_env["set_session"](f"%{i}", sid)
        panes.append(_pane(pane_id=f"%{i}", index=i))
    narrator.tick(panes)
    assert len(tick_env["haiku_calls"]) == narrator.MAX_PER_TICK


def test_tick_disabled_makes_no_calls(tick_env, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(narrator, "_enabled_checked", None)
    narrator.tick([_pane()])
    assert tick_env["haiku_calls"] == []
