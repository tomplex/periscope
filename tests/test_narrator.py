"""Tests for periscope/narrator.py — pure decision core (this task) and
the impure tick (Task 4)."""
import json as _json
import logging
import time as _time

import pytest

from periscope import activity, narrator
from periscope.activity import PaneStatusRow


@pytest.fixture(autouse=True)
def narrator_enabled(monkeypatch):
    """Tests control the latch: key present, latch reset per test."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(narrator, "_enabled_checked", None)


def _row(**over):
    base = {"pane_id": "%1", "session_id": "sid-a", "status": "doing a thing",
                "generated_at": 1000, "jsonl_size": 2048, "seen_name": "claude",
                "renamed_at": None}
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
        _row(), session_id="sid-a", jsonl_size=4096, now=NOW) == "size_changed"


def test_shrunk_jsonl_also_regenerates():
    # Size DIFFERS, not grew numerically — covers truncate/rewrite cases,
    # hence the reason is "size_changed".
    assert narrator.should_regenerate(
        _row(), session_id="sid-a", jsonl_size=10, now=NOW) == "size_changed"


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
        placeholder, session_id="sid-a", jsonl_size=100, now=NOW) == "size_changed"


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


def test_parse_response_rail_accepted_at_limit():
    rail = "x" * narrator.RAIL_MAX_LEN
    out = narrator.parse_response(
        f'{{"status": "s", "rail": "{rail}", "rename": null}}')
    assert out.rail == rail


def test_parse_response_rail_over_limit_dropped_status_and_rename_kept():
    # A bad rail must never discard a good status or rename — the rail is
    # the optional garnish, not the meal.
    rail = "x" * (narrator.RAIL_MAX_LEN + 1)
    out = narrator.parse_response(
        f'{{"status": "s", "rail": "{rail}", "rename": "fs-liveness"}}')
    assert out is not None
    assert out.rail is None
    assert out.status == "s"
    assert out.rename == "fs-liveness"


def test_parse_response_rail_empty_or_nonstring_dropped():
    assert narrator.parse_response(
        '{"status": "s", "rail": "  ", "rename": null}').rail is None
    assert narrator.parse_response(
        '{"status": "s", "rail": 42, "rename": null}').rail is None


def test_parse_response_rail_missing_is_none():
    assert narrator.parse_response('{"status": "s", "rename": null}').rail is None


def test_parse_response_rail_strips_whitespace():
    out = narrator.parse_response(
        '{"status": "s", "rail": "  comparing rates  ", "rename": null}')
    assert out.rail == "comparing rates"


# ---- parse_response: goal ----

def test_parse_response_goal_extracted_and_stripped():
    out = narrator.parse_response(
        '{"status": "s", "goal": "  redesign the rail  ", "rename": null}')
    assert out.goal == "redesign the rail"


def test_parse_response_goal_missing_is_none():
    assert narrator.parse_response('{"status": "s", "rename": null}').goal is None


def test_parse_response_goal_overlength_or_nonstring_dropped_status_kept():
    # A bad goal must never discard the status — it's carried-forward memory,
    # not the meal.
    long = "x" * (narrator.GOAL_MAX_LEN + 1)
    assert narrator.parse_response(
        f'{{"status": "s", "goal": "{long}", "rename": null}}').goal is None
    out = narrator.parse_response('{"status": "s", "goal": 42, "rename": null}')
    assert out is not None and out.goal is None and out.status == "s"


# ---- update_arc / load_arc ----

def test_update_arc_appends_newest_last():
    arc = narrator.update_arc([], "sketching tracks", now=100)
    arc = narrator.update_arc(arc, "writing the spec", now=200)
    assert arc == [{"t": 100, "s": "sketching tracks"},
                   {"t": 200, "s": "writing the spec"}]


def test_update_arc_skips_consecutive_duplicate():
    arc = narrator.update_arc([], "same line", now=100)
    arc = narrator.update_arc(arc, "same line", now=200)
    assert arc == [{"t": 100, "s": "same line"}]   # quiet stretch doesn't crowd


def test_update_arc_caps_to_last_n():
    arc: list[dict] = []
    for i in range(narrator.ARC_MAX + 3):
        arc = narrator.update_arc(arc, f"line {i}", now=i)
    assert len(arc) == narrator.ARC_MAX
    assert arc[-1]["s"] == f"line {narrator.ARC_MAX + 2}"   # newest kept
    assert arc[0]["s"] == "line 3"                          # oldest dropped


def test_load_arc_roundtrips_and_degrades_safely():
    raw = _json.dumps([{"t": 1, "s": "a"}])
    assert narrator.load_arc(raw) == [{"t": 1, "s": "a"}]
    assert narrator.load_arc(None) == []
    assert narrator.load_arc("not json") == []
    assert narrator.load_arc('{"not": "a list"}') == []
    # entries missing the status key are dropped
    assert narrator.load_arc('[{"t": 1}, {"t": 2, "s": "ok"}]') == [
        {"t": 2, "s": "ok"}]


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


def test_build_narrator_prompt_includes_rail_rules():
    p = narrator.build_narrator_prompt(
        window_name="f2-post-deploy", branch=None, pr=None, cwd="/repo",
        signals={})
    assert '"rail"' in p                          # in the return-shape line
    assert str(narrator.RAIL_MAX_LEN) in p        # length rule in-prompt
    # The no-overlap rule must reference the CURRENT name inline (in the
    # rules block, i.e. before the `current_name:` data line), not speak
    # abstractly about "the window name".
    rules_block = p.split("current_name:")[0]
    assert "f2-post-deploy" in rules_block


def test_prompt_includes_workspace_and_siblings():
    from periscope.narrator import build_narrator_prompt
    p = build_narrator_prompt(
        window_name="token-store", branch="auth-core", pr=None, cwd="/dev/fdy",
        signals={}, workspace_name="Auth refactor",
        sibling_names=["rename-flow", "token-store"],
    )
    assert "Auth refactor" in p
    assert "rename-flow" in p
    # the don't-repeat-the-goal rule is present
    assert "don't repeat" in p.lower() or "do not repeat" in p.lower()


def test_prompt_without_workspace_unchanged():
    from periscope.narrator import build_narrator_prompt
    p = build_narrator_prompt(
        window_name="x", branch=None, pr=None, cwd="/dev/x", signals={},
        workspace_name=None, sibling_names=[],
    )
    assert "Auth refactor" not in p


# ---- build_narrator_prompt: goal + arc ----

def test_prompt_renders_goal_and_arc():
    p = narrator.build_narrator_prompt(
        window_name="workspace-design", branch=None, pr=None, cwd="/repo",
        signals={}, goal="redesign the rail into track-based organization",
        arc=[{"t": 0, "s": "sketching tracks"},
             {"t": 580, "s": "wiring the filter"}], now=600)
    assert "goal so far: redesign the rail into track-based organization" in p
    assert "thread arc so far" in p
    assert "sketching tracks" in p and "wiring the filter" in p
    assert "10m ago" in p and "just now" in p   # ages 600s and 20s from now=600
    # the goal contract is in the return shape
    assert '"goal"' in p
    # rename rules anchor on the goal, not the current step
    assert "tracks the" in p.lower() or "the goal" in p.lower()


def test_prompt_omits_goal_and_arc_when_absent():
    p = narrator.build_narrator_prompt(
        window_name="x", branch=None, pr=None, cwd="/repo", signals={})
    assert "goal so far:" not in p
    assert "thread arc so far" not in p   # the data header, not the rules prose


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
# Real SQLite via the shared fresh_activity_db fixture; only the IO
# boundaries are monkeypatched (claude_complete / tmux /
# transcript_summary_from_path / git caches) — all patched on the narrator
# namespace, where they're bound.

@pytest.fixture
def tick_env(fresh_activity_db, tmp_path, monkeypatch):
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
        # What the pre-apply display-message recheck sees; defaults to the
        # _pane() default name so the snapshot matches and renames apply.
        "live_name": "claude",
    }

    def fake_complete(prompt, model="claude-haiku-4-5"):
        env["haiku_calls"].append(prompt)
        r = env["response"]
        if isinstance(r, Exception):
            raise r
        return r

    def fake_tmux(*a):
        env["tmux_calls"].append(a)
        return env["live_name"] if a[0] == "display-message" else ""

    monkeypatch.setattr(narrator, "claude_complete", fake_complete)
    monkeypatch.setattr(narrator, "tmux", fake_tmux)
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


def test_tick_persists_rail(tick_env):
    tick_env["response"] = ('{"status": "fixing flaky reconcile test", '
                            '"rail": "comparing hit rates", "rename": null}')
    narrator.tick([_pane()])
    assert activity.get_pane_status("%1").rail == "comparing hit rates"


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


def test_tick_recheck_drops_rename_when_live_name_moved(tick_env):
    # TOCTOU: current_name is a tick-start snapshot and a tick can run many
    # seconds; a human rename landing mid-tick must win. The pre-apply
    # display-message recheck sees a different live name → no rename-window,
    # but the new status still lands and the stored cooldown stamp survives.
    size = tick_env["jsonl"].stat().st_size
    now = int(_time.time())
    old_stamp = now - narrator.RENAME_COOLDOWN_S - 100   # expired: gate passes
    activity.upsert_pane_status(activity.PaneStatusRow(
        pane_id="%1", session_id="sid-a", status="old", generated_at=1,
        jsonl_size=size + 1, seen_name="claude", renamed_at=old_stamp))
    tick_env["response"] = '{"status": "new status", "rename": "fs-liveness"}'
    tick_env["live_name"] = "human-renamed"              # moved mid-tick
    narrator.tick([_pane()])
    assert not any(c[0] == "rename-window" for c in tick_env["tmux_calls"])
    row = activity.get_pane_status("%1")
    assert row.status == "new status"                    # status still upserted
    assert row.renamed_at == old_stamp                   # re-read stamp preserved
    assert row.seen_name == "claude"                     # re-read row's seen_name


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


def test_tick_scan_failure_on_one_pane_doesnt_starve_others(tick_env, monkeypatch):
    # The per-pane guard in the candidate scan: pane A's scan blowing up must
    # not prevent pane B from generating.
    (tick_env["jsonl"].parent / "sid-b.jsonl").write_text("{}\n")
    tick_env["set_session"]("%2", "sid-b")
    real = activity.get_pane_session

    def boom(pane_id):
        if pane_id == "%1":
            raise RuntimeError("db hiccup")
        return real(pane_id)

    monkeypatch.setattr(activity, "get_pane_session", boom)
    narrator.tick([_pane(), _pane(pane_id="%2", index=2)])
    assert len(tick_env["haiku_calls"]) == 1
    assert activity.get_pane_status("%1") is None
    assert activity.get_pane_status("%2") is not None


def test_tick_disabled_makes_no_calls(tick_env, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(narrator, "_enabled_checked", None)
    narrator.tick([_pane()])
    assert tick_env["haiku_calls"] == []
