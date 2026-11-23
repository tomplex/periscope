"""Idle close: the rules (pure), the sweep (I/O mocked) and retention.

Nothing here spawns a thread: conftest neuters idle_close._bg, and `sweep` is
called directly."""

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
