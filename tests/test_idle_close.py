"""Idle close: the rules (pure), the sweep (I/O mocked) and retention.

Nothing here spawns a thread: conftest neuters idle_close._bg, and `sweep` is
called directly."""

import os
from datetime import datetime

import pytest

from periscope import activity, idle_close

RECORD: activity.ClosedPane = {
    "session_id": "s1",
    "pid": "dddd4444",
    "mem_mb": 812,
    "profile": None,
    "idle_since": 1000,
    "closed_at": 2000,
}


@pytest.fixture
def no_live_claude(monkeypatch):
    monkeypatch.setattr(idle_close.session_status, "claude_proc_for", lambda sid: None)


def test_came_back_when_a_live_window_wears_the_records_pid(no_live_claude):
    assert idle_close.came_back(RECORD, {"dddd4444"})
    assert not idle_close.came_back(RECORD, {"aaaa1111"})


def test_came_back_when_a_live_claude_runs_the_session(monkeypatch):
    monkeypatch.setattr(idle_close.session_status, "claude_proc_for", lambda sid: {"pid": 1} if sid == "s1" else None)
    assert idle_close.came_back(RECORD, set())


def test_prune_closed_drops_a_row_with_no_transcript_or_whose_session_came_back(monkeypatch, no_live_claude, tmp_path):
    for sid, pid in [("kept", "aaaa1111"), ("no-file", "bbbb2222"), ("worn", "cccc3333")]:
        activity.record_closed_pane({**RECORD, "session_id": sid, "pid": pid})
    transcripts = {"kept": tmp_path / "kept.jsonl", "worn": tmp_path / "worn.jsonl"}
    monkeypatch.setattr(idle_close, "jsonl_for_session", transcripts.get)

    assert idle_close.prune_closed({"cccc3333"}) == 2
    assert [r["session_id"] for r in activity.closed_panes()] == ["kept"]


def test_prune_closed_drops_a_row_whose_session_a_live_claude_runs(monkeypatch, tmp_path):
    activity.record_closed_pane(RECORD)
    monkeypatch.setattr(idle_close, "jsonl_for_session", lambda sid: tmp_path / "s1.jsonl")
    monkeypatch.setattr(idle_close.session_status, "claude_proc_for", lambda sid: {"pid": 1})
    assert idle_close.prune_closed(set()) == 1
    assert activity.closed_panes() == []


# --- the rules ----------------------------------------------------------------

MON, FRI = datetime(2026, 11, 16, 17), datetime(2026, 11, 20, 4)
DAY = 24 * 3600


def ts(at: datetime) -> int:
    return int(at.timestamp())


@pytest.mark.parametrize(
    "start,end,hours",
    [
        (datetime(2026, 11, 18, 10), datetime(2026, 11, 18, 13), 3),  # same day
        (MON, datetime(2026, 11, 19, 4), 59),  # the spec's example: Thursday 04:00
        (MON, FRI, 83),  # Friday 04:00
        (datetime(2026, 11, 20, 17), datetime(2026, 11, 23, 4), 11),  # Friday to Monday
        (datetime(2026, 11, 21, 9), datetime(2026, 11, 23, 4), 4),  # starts on Saturday
        (datetime(2026, 11, 19, 4), datetime(2026, 11, 22, 12), 44),  # ends on Sunday
    ],
)
def test_weekday_hours_skip_saturday_and_sunday(start, end, hours):
    assert idle_close.weekday_hours(start, end) == hours


def test_idle_since_is_the_latest_time_the_pane_was_used():
    assert idle_close.idle_since(300.9, 200, 100) == 300
    assert idle_close.idle_since(100.0, 0, 250) == 250
    assert idle_close.idle_since(None, 0, 0) is None


def pane(**over):
    base = {
        "pid": "aaaa1111",
        "pane_id": "%1",
        "name": "notify-buttons",
        "session_id": "s1",
        "state": "idle",
        "panes": 1,
        "idle_since": ts(MON),
        "transcript_mtime": float(ts(MON)),
        "mem_mb": 812,
        "profile": None,
    }
    return {**base, **over}


def verdict(at=FRI, weekdays=3, pinned=frozenset(), **over):
    return idle_close.verdict(pane(**over), at, weekdays, pinned)


def test_verdict_closes_a_pane_at_exactly_the_limit():
    # 72 weekday hours: Monday 17:00 to Thursday 17:00.
    assert verdict(at=datetime(2026, 11, 19, 17)) == "closes"
    assert verdict(at=datetime(2026, 11, 19, 16, 59)) is None


def test_verdict_leaves_out_a_pane_under_the_limit_or_with_no_stamp():
    assert verdict(at=datetime(2026, 11, 19, 4)) is None
    assert verdict(idle_since=None) is None


@pytest.mark.parametrize(
    "over,code",
    [
        ({"session_id": None}, "no-transcript"),
        ({"transcript_mtime": None}, "no-transcript"),
        ({"panes": 2}, "split-window"),
        ({"state": "needs-input"}, "needs-input"),
        ({"state": "done"}, "done"),
        ({"state": "working"}, "working"),
    ],
)
def test_verdict_names_why_a_pane_stays(over, code):
    assert verdict(**over) == code


def test_verdict_keeps_a_pinned_pane():
    assert verdict(pinned={"aaaa1111"}) == "pinned"


def test_verdict_says_weekend_for_a_pane_over_the_limit_only_in_wall_clock_hours():
    # Friday 17:00 to Monday 18:00 is 73 hours, but only 25 weekday hours.
    assert verdict(at=datetime(2026, 11, 23, 18), idle_since=ts(datetime(2026, 11, 20, 17))) == "weekend"


def due(now, settings=None, log=None):
    return idle_close.due(now, {"idle_close_weekdays": 3} if settings is None else settings, log or {})


def test_due_from_04_00_for_90_minutes_once_a_night():
    assert not due(datetime(2026, 11, 20, 3, 59))
    assert due(datetime(2026, 11, 20, 4, 0))
    assert due(datetime(2026, 11, 20, 5, 29))
    assert not due(datetime(2026, 11, 20, 5, 30))
    assert not due(FRI, log={"date": "2026-11-20", "at": 1})
    assert due(FRI, log={"date": "2026-11-19", "at": 1})


def test_due_never_with_the_setting_off():
    assert not due(FRI, settings={})


def test_next_sweep_is_the_next_04_00():
    assert idle_close.next_sweep(datetime(2026, 11, 20, 3, 59)) == FRI
    assert idle_close.next_sweep(FRI) == datetime(2026, 11, 21, 4)
    assert idle_close.next_sweep(datetime(2026, 11, 20, 22)) == datetime(2026, 11, 21, 4)


# --- pane_from_view -----------------------------------------------------------


def view(**over):
    return {
        "agent": "claude",
        "pid": "aaaa1111",
        "pane_id": "%1",
        "name": "notify-buttons",
        "state": "idle",
        "panes": 1,
        "session": "main",
        "index": 3,
        "completed_at": 0,
        "acted_at": 0,
        "focused_at": 0,
        "res": {"mem_mb": 812, "cpu_pct": 0.0},
        "profile": "default",
        **over,
    }


@pytest.fixture
def transcript(monkeypatch, tmp_path):
    """The transcript of session s1, which runs in pane %1: last written on
    Monday at 17:00."""
    path = tmp_path / "s1.jsonl"
    path.write_text("{}\n")
    os.utime(path, (ts(MON), ts(MON)))
    monkeypatch.setattr(idle_close, "session_id_for_pane", {"%1": "s1"}.get)
    monkeypatch.setattr(idle_close, "jsonl_for_session", {"s1": path}.get)
    return path


def test_pane_from_view_reads_the_view_and_the_transcript(transcript):
    assert idle_close.pane_from_view(view()) == pane()


def test_pane_from_view_does_not_count_terminal_focus(transcript):
    assert idle_close.pane_from_view(view(focused_at=ts(FRI))) == pane()


@pytest.mark.parametrize("agent", ["codex", None])
def test_pane_from_view_skips_a_pane_that_is_not_claude(transcript, agent):
    assert idle_close.pane_from_view(view(agent=agent)) is None


def test_pane_from_view_with_no_process_snapshot_can_still_close(transcript):
    got = idle_close.pane_from_view(view(res=None))
    assert got is not None and got["mem_mb"] is None
    assert idle_close.verdict(got, FRI, 3, set()) == "closes"


def test_pane_from_view_reads_the_wrapper_profile(transcript):
    assert idle_close.pane_from_view(view(profile="full"))["profile"] == "full"
    assert idle_close.pane_from_view(view(profile="default"))["profile"] is None


def test_pane_from_view_finds_no_transcript_outside_the_default_projects_folder(monkeypatch):
    # jsonl_for_session looks only in ~/.claude/projects, so an account-B pane stays.
    monkeypatch.setattr(idle_close, "session_id_for_pane", lambda pane_id: "s9")
    monkeypatch.setattr(idle_close, "jsonl_for_session", lambda sid: None)
    got = idle_close.pane_from_view(view(completed_at=ts(MON)))
    assert got["transcript_mtime"] is None
    assert idle_close.verdict(got, FRI, 3, set()) == "no-transcript"
