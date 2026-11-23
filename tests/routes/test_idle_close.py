"""GET /api/idle-close/preview: the panes that close at the next 04:00."""

import os
from datetime import datetime

import pytest

from periscope import idle_close

# Monday 19:00. The next sweep is Tuesday 04:00, and 3 weekdays is 72 hours.
NOW = datetime(2026, 11, 23, 19)
OLD = datetime(2026, 11, 18, 1)  # Wednesday 01:00: 99 weekday hours at the sweep


def ts(at):
    return int(at.timestamp())


@pytest.fixture
def board(monkeypatch, tmp_path, clean_state):
    """Adds a Claude pane: `add("closes")`. Each one ran last at `idle`."""
    clean_state["settings"]["idle_close_weekdays"] = 3
    clean_state["ui"]["pinned_pids"] = ["pinned00"]
    views, sids, paths = [], {}, {}

    async def current_windows():
        return views

    monkeypatch.setattr("periscope.state_hub.current_windows", current_windows)
    monkeypatch.setattr(idle_close, "_now", lambda: NOW)
    monkeypatch.setattr(idle_close, "session_id_for_pane", sids.get)
    monkeypatch.setattr(idle_close, "jsonl_for_session", paths.get)

    def add(name, idle=OLD, transcript=True, pid="aaaa1111", **over):
        pane_id = f"%{len(views)}"
        sids[pane_id] = f"s-{name}"
        if transcript:
            path = tmp_path / f"{name}.jsonl"
            path.write_text("{}\n")
            os.utime(path, (ts(idle), ts(idle)))
            paths[f"s-{name}"] = path
        base = {"agent": "claude", "pid": pid, "pane_id": pane_id, "name": name, "state": "idle", "panes": 1}
        stamps = {"completed_at": 0 if transcript else ts(idle), "acted_at": 0}
        views.append({**base, **stamps, "res": {"mem_mb": 812}, "profile": "default", **over})

    return add


def preview(client):
    r = client.get("/api/idle-close/preview")
    assert r.status_code == 200
    return {row["name"]: row["verdict"] for row in r.json()}


def test_preview_has_one_row_for_each_verdict(client, board):
    board("closes")
    board("weekend", idle=datetime(2026, 11, 20, 23))  # Friday 23:00: 77 hours, 29 of them on weekdays
    board("pinned", pid="pinned00")
    board("split", panes=2)
    board("waits", state="needs-input")
    board("unread", state="done")
    board("runs", state="working")
    board("archived", transcript=False)
    assert preview(client) == {
        "closes": "closes",
        "weekend": "weekend",
        "pinned": "pinned",
        "split": "split-window",
        "waits": "needs-input",
        "unread": "done",
        "runs": "working",
        "archived": "no-transcript",
    }


def test_preview_leaves_out_a_pane_with_no_stamp_or_not_near_the_limit(client, board):
    board("no-stamp", transcript=False, completed_at=0)
    board("fresh", idle=datetime(2026, 11, 23, 12))
    board("shell", agent=None)
    assert preview(client) == {}


def test_preview_counts_to_the_next_sweep_not_to_now(client, board):
    # Thursday 02:00: 65 weekday hours now, 74 at 04:00.
    board("crosses", idle=datetime(2026, 11, 19, 2))
    assert preview(client) == {"crosses": "closes"}


def test_preview_row_fields(client, board):
    board("closes")
    (row,) = client.get("/api/idle-close/preview").json()
    assert row == {"name": "closes", "idle_since": ts(OLD), "weekday_hours": 99.0, "mem_mb": 812, "verdict": "closes"}


def test_preview_is_empty_with_the_setting_off(client, board, clean_state):
    board("closes")
    del clean_state["settings"]["idle_close_weekdays"]
    assert preview(client) == {}
