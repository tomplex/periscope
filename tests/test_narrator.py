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
