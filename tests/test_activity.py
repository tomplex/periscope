"""Tests for periscope/activity.py."""
import pytest

from periscope import config, activity


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    """Every test gets an isolated periscope.db and empty caches."""
    monkeypatch.setattr(config, "ACTIVITY_DB", tmp_path / "t.db")
    activity._CONN = None
    activity._git_cache.clear()
    activity._git_fetching.clear()
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


import time as _time


def test_cached_pane_activity_merges_and_sorts(monkeypatch):
    # Pre-seed the git SWR cache so the merge runs with no bg fetch.
    activity._git_cache[("/repo", "main")] = (_time.time(), [
        {"kind": "commit", "at": 50, "text": "older commit"},
        {"kind": "commit", "at": 150, "text": "newer commit"},
    ])
    monkeypatch.setattr(activity, "_acted_at", {"sess:1": 100})
    activity.record("pane", "%9", "alert", "an alert", detail="info", at=120)

    out = activity.cached_pane_activity("sess:1", "%9", "/repo", "main")
    # Newest-first: 150 commit, 120 alert, 100 open, 50 commit.
    assert [e["at"] for e in out] == [150, 120, 100, 50]
    assert out[1]["src"] == "alert"
    assert out[2]["kind"] == "open"


def test_cached_pane_activity_tags_git_events_with_src(monkeypatch):
    activity._git_cache[("/repo", "main")] = (_time.time(), [
        {"kind": "commit", "at": 10, "text": "c", "url": "http://x"},
    ])
    monkeypatch.setattr(activity, "_acted_at", {})
    out = activity.cached_pane_activity("s:1", "%1", "/repo", "main")
    assert out[0]["src"] == "git"
    assert out[0]["url"] == "http://x"


import json as _json


def _write_transcript(path, cwd, *, mtime=None):
    # Faithful to the real shape: the first line is a file-history-snapshot
    # with no cwd; user text lives at message.content, not a top-level key.
    lines = [
        {"type": "file-history-snapshot"},
        {"type": "user", "cwd": cwd,
         "message": {"role": "user", "content": "hi"}},
    ]
    path.write_text("\n".join(_json.dumps(d) for d in lines) + "\n")
    if mtime is not None:
        import os
        os.utime(path, (mtime, mtime))


def test_live_transcript_for_matches_on_cwd(tmp_path, monkeypatch):
    projects = tmp_path / "projects"
    d = projects / "-Users-tom-dev-periscope"
    d.mkdir(parents=True)
    tf = d / "abc.jsonl"
    _write_transcript(tf, "/Users/tom/dev/periscope")
    monkeypatch.setattr(activity, "_PROJECTS_DIR", projects)
    assert activity.live_transcript_for("/Users/tom/dev/periscope") == tf


def test_live_transcript_for_picks_newest_mtime(tmp_path, monkeypatch):
    projects = tmp_path / "projects"
    d = projects / "-repo"
    d.mkdir(parents=True)
    old, new = d / "old.jsonl", d / "new.jsonl"
    _write_transcript(old, "/repo", mtime=1000)
    _write_transcript(new, "/repo", mtime=2000)
    monkeypatch.setattr(activity, "_PROJECTS_DIR", projects)
    assert activity.live_transcript_for("/repo") == new


def test_live_transcript_for_rejects_cwd_mismatch(tmp_path, monkeypatch):
    projects = tmp_path / "projects"
    d = projects / "-repo"
    d.mkdir(parents=True)
    tf = d / "abc.jsonl"
    _write_transcript(tf, "/some/other/repo")
    monkeypatch.setattr(activity, "_PROJECTS_DIR", projects)
    assert activity.live_transcript_for("/repo") is None


def test_check_reset_fires_on_context_drop(monkeypatch):
    # Keep _compact_or_clear hermetic — no real ~/.claude lookup.
    monkeypatch.setattr(activity, "live_transcript_for", lambda cwd: None)
    last = {}
    assert activity._check_reset("%1", "/repo", 60, last) is False   # baseline
    assert activity._check_reset("%1", "/repo", 62, last) is False   # climbing
    assert activity._check_reset("%1", "/repo", 8, last) is True     # dropped
    out = activity.events_for("%1", "/repo", "main")
    assert len(out) == 1 and out[0]["kind"] == "reset"


def test_check_reset_ignores_none_readings(monkeypatch):
    monkeypatch.setattr(activity, "live_transcript_for", lambda cwd: None)
    last = {}
    activity._check_reset("%1", "/repo", 60, last)
    assert activity._check_reset("%1", "/repo", None, last) is False  # obscured
    assert activity._check_reset("%1", "/repo", 61, last) is False    # climbed
    assert activity.events_for("%1", "/repo", "main") == []


def test_compact_or_clear_labels_cleared_without_marker(tmp_path, monkeypatch):
    monkeypatch.setattr(activity, "live_transcript_for", lambda cwd: None)
    detail, text = activity._compact_or_clear("/repo")
    assert detail == "cleared"
    assert "clear" in text.lower()


def test_compact_or_clear_labels_compacted_with_marker(tmp_path, monkeypatch):
    tf = tmp_path / "t.jsonl"
    entry = {
        "type": "system", "subtype": "compact_boundary",
        "timestamp": "2026-05-21T12:00:00.000Z",
        "compactMetadata": {"trigger": "auto",
                            "preTokens": 303000, "postTokens": 14000},
    }
    tf.write_text(_json.dumps(entry) + "\n")
    monkeypatch.setattr(activity, "live_transcript_for", lambda cwd: tf)
    monkeypatch.setattr(activity, "_compact_is_recent", lambda ts: True)
    detail, text = activity._compact_or_clear("/repo")
    assert detail == "compacted"
    assert "303k" in text and "14k" in text
