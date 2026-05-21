"""Tests for periscope/activity.py."""
import pytest

from periscope import config, activity


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    """Every test gets an isolated periscope.db."""
    monkeypatch.setattr(config, "ACTIVITY_DB", tmp_path / "t.db")
    activity._CONN = None
    yield
    if activity._CONN is not None:
        activity._CONN.close()
        activity._CONN = None


def test_record_then_events_for_roundtrips():
    activity.record("pane", "%1", "alert", "tests pass", detail="done", at=100)
    out = activity.events_for("%1", "/repo", "main")
    assert len(out) == 1
    assert out[0]["src"] == "alert"
    assert out[0]["kind"] == "done"
    assert out[0]["text"] == "tests pass"
    assert out[0]["at"] == 100


def test_events_for_returns_pane_and_branch_scopes():
    activity.record("pane", "%1", "alert", "a", detail="info", at=10)
    activity.record("branch", "/repo\x1fmain", "milestone", "m", at=20)
    activity.record("pane", "%2", "alert", "other pane", detail="info", at=30)
    out = activity.events_for("%1", "/repo", "main")
    texts = {e["text"] for e in out}
    assert texts == {"a", "m"}  # %2's alert excluded


def test_events_for_newest_first():
    activity.record("pane", "%1", "alert", "old", detail="info", at=10)
    activity.record("pane", "%1", "alert", "new", detail="info", at=20)
    out = activity.events_for("%1", "/repo", "main")
    assert [e["text"] for e in out] == ["new", "old"]


def test_dedup_key_makes_record_idempotent():
    activity.record("branch", "/r\x1fmain", "milestone", "x", at=1, dedup_key="m:abc")
    activity.record("branch", "/r\x1fmain", "milestone", "x again", at=2, dedup_key="m:abc")
    out = activity.events_for(None, "/r", "main")
    assert len(out) == 1
    assert out[0]["text"] == "x"  # first write wins


def test_prune_drops_old_rows():
    import time
    now = int(time.time())
    activity.record("pane", "%1", "alert", "recent", detail="info", at=now)
    activity.record("pane", "%1", "alert", "ancient", detail="info", at=now - 99 * 86400)
    activity.prune(max_age_days=30)
    out = activity.events_for("%1", "/repo", "main")
    assert [e["text"] for e in out] == ["recent"]
