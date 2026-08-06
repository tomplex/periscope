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
        '{"status": "fixing flaky reconcile test", "name": "fs-build"}')
    assert out == narrator.NarratorResult(
        status="fixing flaky reconcile test", name="fs-build")


def test_parse_response_with_name():
    out = narrator.parse_response('{"status": "s", "name": "fs-liveness"}')
    assert out.name == "fs-liveness"


def test_parse_response_strips_code_fences():
    out = narrator.parse_response('```json\n{"status": "s", "name": null}\n```')
    assert out is not None and out.status == "s"


def test_parse_response_garbage_returns_none():
    assert narrator.parse_response("I think the status is...") is None


def test_parse_response_non_dict_json_returns_none():
    assert narrator.parse_response('["status"]') is None


def test_parse_response_missing_status_returns_none():
    assert narrator.parse_response('{"name": "x"}') is None


def test_parse_response_overlength_status_returns_none():
    long = "x" * (narrator.STATUS_MAX_LEN + 1)
    assert narrator.parse_response(f'{{"status": "{long}", "name": null}}') is None


def test_parse_response_non_string_name_dropped_status_kept():
    out = narrator.parse_response('{"status": "s", "name": 42}')
    assert out is not None and out.name is None


def test_parse_response_rail_accepted_at_limit():
    rail = "x" * narrator.RAIL_MAX_LEN
    out = narrator.parse_response(
        f'{{"status": "s", "rail": "{rail}", "name": null}}')
    assert out.rail == rail


def test_parse_response_rail_over_limit_dropped_status_and_name_kept():
    # A bad rail must never discard a good status or name — the rail is
    # the optional garnish, not the meal.
    rail = "x" * (narrator.RAIL_MAX_LEN + 1)
    out = narrator.parse_response(
        f'{{"status": "s", "rail": "{rail}", "name": "fs-liveness"}}')
    assert out is not None
    assert out.rail is None
    assert out.status == "s"
    assert out.name == "fs-liveness"


def test_parse_response_rail_empty_or_nonstring_dropped():
    assert narrator.parse_response(
        '{"status": "s", "rail": "  ", "name": null}').rail is None
    assert narrator.parse_response(
        '{"status": "s", "rail": 42, "name": null}').rail is None


def test_parse_response_rail_missing_is_none():
    assert narrator.parse_response('{"status": "s", "name": null}').rail is None


def test_parse_response_rail_strips_whitespace():
    out = narrator.parse_response(
        '{"status": "s", "rail": "  comparing rates  ", "name": null}')
    assert out.rail == "comparing rates"


# ---- parse_response: goal ----

def test_parse_response_goal_extracted_and_stripped():
    out = narrator.parse_response(
        '{"status": "s", "goal": "  redesign the rail  ", "name": null}')
    assert out.goal == "redesign the rail"


def test_parse_response_goal_missing_is_none():
    assert narrator.parse_response('{"status": "s", "name": null}').goal is None


def test_parse_response_goal_overlength_or_nonstring_dropped_status_kept():
    # A bad goal must never discard the status — it's carried-forward memory,
    # not the meal.
    long = "x" * (narrator.GOAL_MAX_LEN + 1)
    assert narrator.parse_response(
        f'{{"status": "s", "goal": "{long}", "name": null}}').goal is None
    out = narrator.parse_response('{"status": "s", "goal": 42, "name": null}')
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


def test_rename_decision_locked_blocks_valid_suggestion():
    assert narrator.rename_decision(
        "fs-liveness", current_name="claude", row=_row(), now=NOW,
        locked=True) is None


# ---- is_name_pinned (the name-pin lock) ----

def test_is_name_pinned_true_when_pinned(clean_state):
    from periscope import store
    store.set_window_fields("p1", name_pinned=True)
    assert narrator.is_name_pinned({"pid": "p1", "name": "qa-app-design"})


def test_is_name_pinned_survives_a_later_rename(clean_state):
    """The pin marks the WINDOW as hand-named, not one string. Scoping it to a
    matching name (the old spawn_name lock) meant renaming a pinned tab handed
    it straight back to the narrator — the opposite of what renaming means."""
    from periscope import store
    store.set_window_fields("p1", name_pinned=True)
    assert narrator.is_name_pinned({"pid": "p1", "name": "something-else"})


def test_is_name_pinned_false_when_unpinned(clean_state):
    assert not narrator.is_name_pinned({"pid": "p1", "name": "claude"})


def test_is_name_pinned_false_after_unpin(clean_state):
    from periscope import store
    store.set_window_fields("p1", name_pinned=True)
    store.set_window_fields("p1", name_pinned=None)
    assert not narrator.is_name_pinned({"pid": "p1", "name": "qa-app-design"})


def test_is_name_pinned_false_without_a_pid(clean_state):
    assert not narrator.is_name_pinned({"pid": "", "name": "claude"})


def test_is_name_pinned_reads_pid_raw_from_a_list_windows_shape(clean_state):
    """The narrator's windows come from `list_windows()`, which carries
    `pid_raw` and no `pid` — the shape every other test here skips. Keying on
    `pid` alone silently disabled the lock for every real pane."""
    from periscope import store
    store.set_window_fields("p1", name_pinned=True)
    assert narrator.is_name_pinned({"pid_raw": "p1", "name": "sts2-d2-darv"})


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
    assert '"name"' in p                         # name-the-goal contract
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


def test_prompt_includes_track_and_siblings():
    from periscope.narrator import build_narrator_prompt
    p = build_narrator_prompt(
        window_name="token-store", branch="auth-core", pr=None, cwd="/dev/fdy",
        signals={}, track_name="Auth refactor",
        sibling_names=["rename-flow", "token-store"],
    )
    assert "Auth refactor" in p
    assert "rename-flow" in p
    # The anti-echo contract: the track/branch label is already visible above
    # the tab, so an echoing current_name must NOT count as "describes the
    # goal" (that's how branch-named tabs froze — the KEEP-IT rule locked
    # them in).
    assert "ALREADY VISIBLE" in p
    assert "echo" in p.lower()


# ---- the anti-echo post-filter + sibling self-exclusion ----
#
# Five tabs in one worktree all converged on 'world-model' (the worktree dir
# and its track's label) despite the prompt forbidding exactly that. Prompt
# taste is advisory; these two guards are not.

def test_container_tokens_spans_track_branch_and_worktree_dir():
    from periscope.narrator import container_tokens
    c = container_tokens(track_name="worktree-world-model", branch="world-model",
                         cwd="/Users/tom/dev/x/.claude/worktrees/world-model")
    # 'worktree' is scaffolding and must not pad the set — otherwise a
    # 'worktree-*' track label makes its own echo look like a new token.
    assert c == {"world", "model"}


def test_is_echo_blocks_a_pure_container_echo_and_keeps_a_distinguishing_name():
    from periscope.narrator import container_tokens, is_echo
    c = container_tokens(track_name="worktree-world-model", branch="world-model",
                         cwd="/dev/x/.claude/worktrees/world-model")
    assert is_echo("world-model", c)          # the observed failure
    assert is_echo("model", c)                # an abbreviation of it
    for good in ("c2-ancients", "e1-option-slot", "sts2-d2-darv",
                 "reward-gpu-spec"):          # the spawn names it overwrote
        assert not is_echo(good, c)


def test_is_echo_needs_a_container_to_block_anything():
    """No track/branch/cwd context => nothing is an echo. The guard must never
    reject on an empty container, or a pane with no track loses every rename."""
    from periscope.narrator import is_echo
    assert not is_echo("world-model", set())


def test_rename_decision_rejects_an_echo_of_the_container():
    from periscope.narrator import rename_decision
    kw = {"current_name": "c2-ancients", "row": None, "now": 1000}
    assert rename_decision("world-model", container={"world", "model"}, **kw) is None
    # …and still allows a name that carries something new.
    assert rename_decision("ancient-events", container={"world", "model"},
                           **kw) == "ancient-events"


def test_rename_decision_without_a_container_is_unchanged():
    from periscope.narrator import rename_decision
    assert rename_decision("world-model", current_name="x", row=None,
                           now=1000) == "world-model"


def test_siblings_excluding_drops_self_by_pane_id_not_by_name():
    """Colliding names are the case that matters: excluding by name would
    erase a genuine sibling that happens to share this tab's name."""
    from periscope.narrator import siblings_excluding
    members = [("%54", "world-model"), ("%62", "world-model"),
               ("%66", "world-model-migration")]
    assert siblings_excluding(members, "%54") == ["world-model",
                                                  "world-model-migration"]
    assert siblings_excluding(members, "%66") == ["world-model"]


def test_siblings_excluding_dedupes_and_drops_blanks():
    from periscope.narrator import siblings_excluding
    members = [("%1", "a"), ("%2", "a"), ("%3", ""), ("%4", "b")]
    assert siblings_excluding(members, "%9") == ["a", "b"]


def test_prompt_without_track_has_no_sibling_block():
    from periscope.narrator import build_narrator_prompt
    p = build_narrator_prompt(
        window_name="x", branch=None, pr=None, cwd="/dev/x", signals={},
        track_name=None, sibling_names=[],
    )
    assert "Auth refactor" not in p
    assert "ALREADY VISIBLE" not in p


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


def test_prompt_names_the_goal_not_a_rename_decision():
    # Regression (the brainstorm-skill→normalization case): the OLD "decide
    # whether to rename" framing made Haiku return null every tick even when the
    # name no longer matched the goal — it's loss-averse on the rename decision.
    # The fix reframes it as "name the goal" (the model is good at naming, bad at
    # deciding to rename); a code-side diff turns a changed name into a rename.
    p = narrator.build_narrator_prompt(
        window_name="x", branch=None, pr=None, cwd="/repo", signals={},
        goal="some goal")
    low = p.lower()
    assert "name` " in low or "`name`" in low      # outputs a name field
    assert "not a yes/no rename decision" in low    # the reframe is explicit
    assert "return it unchanged" in low             # keep = echo current_name


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
        "response": '{"status": "fixing flaky reconcile test", "name": null}',
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
             "index": index, "cwd": cwd}, {"agent": "claude", "state": "idle"})


def test_tick_feeds_track_label_and_siblings_into_prompt(tick_env):
    """Track context is wired from the TRACKS registry (pane_tracks tag or
    repo-default fallback), not the legacy pane_workspaces table — with the
    old wiring, track-grouped tabs got no sibling context and Haiku happily
    named every tab after its branch (the attr-worker-phase2 twins)."""
    from periscope import tracks
    tk = tracks.create_track(name="attr worker phase2")
    # Narrator rows are raw list_windows() rows — track tags key on pid_raw
    # (the stamped @periscope_id), never the %N pane id.
    activity.set_pane_track("aaaa0001", tk["id"])
    activity.set_pane_track("aaaa0002", tk["id"])
    p1, p2 = _pane(), _pane(pane_id="%2", name="drift-detection")
    p1[0]["pid_raw"] = "aaaa0001"
    p2[0]["pid_raw"] = "aaaa0002"
    narrator.tick([
        p1,   # %1 — regenerates
        p2,   # sibling, no session mapping
    ])
    prompt = tick_env["haiku_calls"][0]
    assert "attr worker phase2" in prompt     # track label, from the registry
    assert "drift-detection" in prompt        # sibling names from this tick
    assert "ALREADY VISIBLE" in prompt        # anti-echo block engaged


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
                            '"rail": "comparing hit rates", "name": null}')
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
    tick_env["response"] = '{"status": "s", "name": "fs-liveness"}'
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
    tick_env["response"] = '{"status": "s", "name": "fs-liveness"}'
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
    tick_env["response"] = '{"status": "new status", "name": "fs-liveness"}'
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
    tick_env["response"] = '{"status": "new status", "name": "fs-liveness"}'
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


def test_tick_persists_goal_and_arc(tick_env):
    tick_env["response"] = ('{"status": "wiring the filter", '
                            '"goal": "redesign the rail", "name": null}')
    narrator.tick([_pane()])
    row = activity.get_pane_status("%1")
    assert row.goal == "redesign the rail"
    assert narrator.load_arc(row.history) == [
        {"t": _arc_t(row), "s": "wiring the filter"}]


def _arc_t(row):
    return narrator.load_arc(row.history)[0]["t"]


def test_tick_carries_goal_forward_when_response_omits_it(tick_env):
    size = tick_env["jsonl"].stat().st_size
    activity.upsert_pane_status(activity.PaneStatusRow(
        pane_id="%1", session_id="sid-a", status="old", generated_at=1,
        jsonl_size=size - 1, seen_name="claude", renamed_at=None,
        goal="redesign the rail", history='[{"t": 1, "s": "sketching"}]'))
    # default response has no goal field → previous goal must survive
    narrator.tick([_pane()])
    row = activity.get_pane_status("%1")
    assert row.goal == "redesign the rail"            # carried, not wiped
    arc = narrator.load_arc(row.history)
    assert [e["s"] for e in arc] == ["sketching", "fixing flaky reconcile test"]


def test_tick_feeds_prev_goal_and_arc_into_prompt(tick_env):
    size = tick_env["jsonl"].stat().st_size
    activity.upsert_pane_status(activity.PaneStatusRow(
        pane_id="%1", session_id="sid-a", status="old", generated_at=1,
        jsonl_size=size - 1, seen_name="claude", renamed_at=None,
        goal="redesign the rail", history='[{"t": 1, "s": "sketching tracks"}]'))
    narrator.tick([_pane()])
    prompt = tick_env["haiku_calls"][0]
    assert "goal so far: redesign the rail" in prompt
    assert "sketching tracks" in prompt


def test_tick_session_switch_resets_goal_and_arc(tick_env):
    size = tick_env["jsonl"].stat().st_size
    activity.upsert_pane_status(activity.PaneStatusRow(
        pane_id="%1", session_id="sid-OLD", status="old", generated_at=1,
        jsonl_size=size, seen_name="claude", renamed_at=None,
        goal="old thread goal", history='[{"t": 1, "s": "old phase"}]'))
    narrator.tick([_pane()])                          # mapped session is sid-a
    prompt = tick_env["haiku_calls"][0]
    assert "old thread goal" not in prompt            # prior thread not fed in
    assert "old phase" not in prompt
    row = activity.get_pane_status("%1")
    assert row.goal is None                           # default response: no goal
    assert [e["s"] for e in narrator.load_arc(row.history)] == [
        "fixing flaky reconcile test"]                # arc starts fresh


def test_tick_records_status_event_with_goal(tick_env):
    tick_env["response"] = ('{"status": "wiring the filter", '
                            '"goal": "redesign the rail", "name": null}')
    narrator.tick([_pane()])
    log = activity.status_log_for("%1")
    assert len(log) == 1
    assert log[0]["status"] == "wiring the filter"
    assert log[0]["goal"] == "redesign the rail"
    # and it stays OUT of the live activity timeline
    assert "status" not in {e["kind"] for e in activity.events_for("%1", None, None)}


def test_tick_status_event_deduped_when_unchanged(tick_env):
    narrator.tick([_pane()])                       # first status recorded
    assert len(activity.status_log_for("%1")) == 1
    before = activity.get_pane_status("%1")
    # Make it a candidate again (bigger file, interval past) with an IDENTICAL
    # model response → no status/goal change → no new log row.
    tick_env["jsonl"].write_text(tick_env["jsonl"].read_text() + "x" * 50)
    activity.upsert_pane_status(
        activity.PaneStatusRow(**{**before.__dict__, "generated_at": 1}))
    narrator.tick([_pane()])
    assert len(activity.status_log_for("%1")) == 1   # unchanged → not re-logged


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


def test_pane_with_only_a_live_session_is_narrated(tick_env, monkeypatch):
    """A pane with a live claude but NO pane_sessions row still gets a status.

    Claude mints a new session id on resume/compaction and the hook does not
    always record the successor, so the row is the FALLBACK, not the authority
    (same defect that made move-account resume an 18h-stale transcript).
    Reading the row directly left such panes permanently unnarrated.
    """
    monkeypatch.setattr(
        "periscope.session_status.live_session_id_for_pane",
        lambda pane_id: "sid-a" if pane_id == "%9" else None,
    )
    narrator.tick([_pane(pane_id="%9")])
    assert tick_env["haiku_calls"], "pane with a live session was never narrated"


def test_echo_guard_yields_to_a_placeholder_name():
    """An echoing name beats a version string.

    tmux `automatic-rename` labels a window with its running command — for
    Claude that is the VERSION ("2.1.220"). Every guard here errs toward None
    on the assumption the current name is worth keeping; against a placeholder
    that inverts, and a pane whose work genuinely matches its branch (so every
    good name reads as an echo) stays labelled with a version number forever.
    """
    container = {"saved", "searches"}
    # real name -> the echo guard still protects it
    assert narrator.rename_decision(
        "saved-searches", current_name="button-polish", row=None, now=NOW,
        container=container) is None
    # placeholder -> take the echo, it carries strictly more information
    assert narrator.rename_decision(
        "saved-searches", current_name="2.1.220", row=None, now=NOW,
        container=container) == "saved-searches"


def test_placeholder_name_detection():
    assert narrator.is_placeholder_name("2.1.220")
    assert narrator.is_placeholder_name("claude")
    assert narrator.is_placeholder_name("")
    assert not narrator.is_placeholder_name("pit-join-migration")
    assert not narrator.is_placeholder_name("v2-planning")
