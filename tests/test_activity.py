"""Tests for periscope/activity.py."""
import pytest

from periscope import activity


@pytest.fixture(autouse=True)
def fresh_db(fresh_activity_db):
    """Every test here hits the DB — make the shared isolation autouse."""


def test_record_then_events_for_roundtrips():
    activity.record("pane", "%1", "alert", "tests pass", detail="done", at=100)
    out = activity.events_for("%1", "/repo", "main")
    assert len(out) == 1
    assert out[0]["src"] == "alert"
    assert out[0]["kind"] == "done"
    assert out[0]["text"] == "tests pass"
    assert out[0]["at"] == 100


def test_channel_event_roundtrips_with_full_text():
    body = "long message body that periscope pushed into the pane " * 3
    activity.record("pane", "%1", "channel", body, detail="message", at=200)
    out = activity.events_for("%1", "/repo", "main")
    assert len(out) == 1
    assert out[0]["src"] == "channel"
    assert out[0]["kind"] == "message"
    assert out[0]["text"] == body   # untruncated
    assert out[0]["at"] == 200


def test_channel_event_kind_defaults_to_message():
    activity.record("pane", "%1", "channel", "hi", at=5)   # no detail
    out = activity.events_for("%1", "/repo", "main")
    assert out[0]["kind"] == "message"


def test_events_for_returns_pane_and_branch_scopes():
    activity.record("pane", "%1", "alert", "a", detail="info", at=10)
    activity.record("branch", "/repo\x1fmain", "note", "m", at=20)
    activity.record("pane", "%2", "alert", "other pane", detail="info", at=30)
    out = activity.events_for("%1", "/repo", "main")
    texts = {e["text"] for e in out}
    assert texts == {"a", "m"}  # %2's alert excluded


def test_events_for_newest_first():
    activity.record("pane", "%1", "alert", "old", detail="info", at=10)
    activity.record("pane", "%1", "alert", "new", detail="info", at=20)
    out = activity.events_for("%1", "/repo", "main")
    assert [e["text"] for e in out] == ["new", "old"]


def test_events_for_excludes_status_kind():
    # 'status' is the narrator's high-frequency thread log — it must never
    # surface in the live activity timeline.
    activity.record("pane", "%1", "status", "wiring the filter", detail="g", at=10)
    activity.record("pane", "%1", "rename", "renamed: a → b", at=20)
    out = activity.events_for("%1", "/repo", "main")
    kinds = {e["kind"] for e in out}
    assert "status" not in kinds
    assert "rename" in kinds


def test_status_log_for_returns_status_events_newest_first():
    activity.record("pane", "%1", "status", "first", detail="g1", at=100)
    activity.record("pane", "%1", "status", "second", detail="g2", at=200)
    activity.record("pane", "%1", "alert", "noise", detail="info", at=150)
    assert activity.status_log_for("%1") == [
        {"at": 200, "status": "second", "goal": "g2"},
        {"at": 100, "status": "first", "goal": "g1"}]


def test_dedup_key_makes_record_idempotent():
    activity.record("branch", "/r\x1fmain", "note", "x", at=1, dedup_key="m:abc")
    activity.record("branch", "/r\x1fmain", "note", "x again", at=2, dedup_key="m:abc")
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


# --- UI instrumentation ------------------------------------------------

def test_record_ui_events_inserts_and_stamps_dev():
    n = activity.record_ui_events(
        [{"name": "modal.open", "detail": {"tab": "terminal"}, "t": 100}],
        dev=True,
    )
    assert n == 1
    c = activity._conn()
    row = c.execute("SELECT at, name, dev, detail FROM ui_events").fetchone()
    assert row[0] == 100
    assert row[1] == "modal.open"
    assert row[2] == 1
    assert row[3] == '{"tab": "terminal"}'


def test_record_ui_events_dev_false_stamps_zero():
    activity.record_ui_events([{"name": "app.open", "t": 5}], dev=False)
    c = activity._conn()
    assert c.execute("SELECT dev FROM ui_events").fetchone()[0] == 0


def test_record_ui_events_skips_rows_missing_name():
    n = activity.record_ui_events(
        [{"detail": {"x": 1}, "t": 1}, {"name": "", "t": 1}, {"name": "ok", "t": 1}],
        dev=False,
    )
    assert n == 1
    c = activity._conn()
    assert c.execute("SELECT name FROM ui_events").fetchone()[0] == "ok"


def test_record_ui_events_skips_non_dict_elements():
    n = activity.record_ui_events(["nope", 42, {"name": "ok", "t": 1}], dev=False)
    assert n == 1


def test_record_ui_events_invalid_t_falls_back_to_now():
    activity.record_ui_events([{"name": "x"}, {"name": "y", "t": "bad"}], dev=False)
    c = activity._conn()
    ats = [r[0] for r in c.execute("SELECT at FROM ui_events")]
    assert all(a > 1_000_000_000 for a in ats)  # real unix timestamps


def test_record_ui_events_detail_none_and_empty_become_null():
    activity.record_ui_events(
        [{"name": "a", "t": 1}, {"name": "b", "detail": {}, "t": 1}],
        dev=False,
    )
    c = activity._conn()
    details = [r[0] for r in c.execute("SELECT detail FROM ui_events ORDER BY name")]
    assert details == [None, None]


def test_record_ui_events_empty_batch_returns_zero():
    assert activity.record_ui_events([], dev=False) == 0


def test_prune_ui_events_drops_old_keeps_recent():
    import time
    now = int(time.time())
    activity.record_ui_events([{"name": "old", "t": now - 200 * 86400}], dev=False)
    activity.record_ui_events([{"name": "new", "t": now}], dev=False)
    activity.prune_ui_events(max_age_days=90)
    c = activity._conn()
    names = [r[0] for r in c.execute("SELECT name FROM ui_events")]
    assert names == ["new"]


# --- usage_samples ------------------------------------------------------

def test_usage_samples_roundtrip_oldest_first():
    activity.record_usage_samples([
        (200, "default", "session", 42.5, 1000),
        (100, "default", "session", 40.0, 1000),
        (150, "default", "week_all", 7.0, 2000),
    ])
    out = activity.usage_samples_since("default", "session", 0)
    assert out == [(100, 40.0), (200, 42.5)]


def test_usage_samples_since_filters_by_time():
    activity.record_usage_samples([(100, "default", "session", 1.0, None),
                                   (200, "default", "session", 2.0, None)])
    assert activity.usage_samples_since("default", "session", 150) == [(200, 2.0)]


def test_usage_samples_duplicate_at_is_ignored():
    activity.record_usage_samples([(100, "default", "session", 1.0, None)])
    activity.record_usage_samples([(100, "default", "session", 9.0, None)])
    assert activity.usage_samples_since("default", "session", 0) == [(100, 1.0)]


def test_usage_samples_are_separated_by_account():
    """Two accounts sampling the same meter at the same second are distinct
    series — the whole point of widening the PK to (account, meter, at)."""
    activity.record_usage_samples([(100, "default", "session", 1.0, None),
                                   (100, "b", "session", 90.0, None)])
    assert activity.usage_samples_since("default", "session", 0) == [(100, 1.0)]
    assert activity.usage_samples_since("b", "session", 0) == [(100, 90.0)]


def test_prune_usage_samples_drops_old_rows():
    import time
    now = int(time.time())
    activity.record_usage_samples([
        (now - 20 * 86400, "default", "session", 1.0, None),
        (now, "default", "session", 2.0, None),
    ])
    activity.prune_usage_samples(max_age_days=14)
    assert activity.usage_samples_since("default", "session", 0) == [(now, 2.0)]


def test_usage_samples_migration_preserves_preaccount_rows(tmp_path, monkeypatch):
    """A live DB predating the account column is rebuilt in place, and its
    history is attributed to the default account rather than dropped."""
    import sqlite3

    from periscope import config
    db = tmp_path / "legacy.db"
    legacy = sqlite3.connect(str(db))
    legacy.executescript("""
      CREATE TABLE usage_samples (
        at        INTEGER NOT NULL,
        meter     TEXT NOT NULL,
        percent   REAL NOT NULL,
        resets_at INTEGER,
        PRIMARY KEY (meter, at)
      );
      INSERT INTO usage_samples VALUES (100, 'session', 40.0, 1000);
      INSERT INTO usage_samples VALUES (200, 'week_all', 7.5, 2000);
    """)
    legacy.commit()
    legacy.close()

    monkeypatch.setattr(config, "ACTIVITY_DB", db)
    activity._CONN = None
    assert activity.usage_samples_since("default", "session", 0) == [(100, 40.0)]
    assert activity.usage_samples_since("default", "week_all", 0) == [(200, 7.5)]


# --- pane_status: narrator storage ---------------------------------------

def _status_row(pane_id="%1", **over):
    base = {"pane_id": pane_id, "session_id": "sid-a",
                "status": "fixing flaky reconcile test", "generated_at": 1000,
                "jsonl_size": 2048, "seen_name": "claude", "renamed_at": None}
    base.update(over)
    return activity.PaneStatusRow(**base)


def test_pane_status_upsert_then_get_roundtrips():
    row = _status_row()
    activity.upsert_pane_status(row)
    assert activity.get_pane_status("%1") == row
    assert activity.get_pane_status("%nope") is None


def test_pane_status_rail_roundtrips():
    activity.upsert_pane_status(_status_row(rail="comparing lookup hit rates"))
    assert activity.get_pane_status("%1").rail == "comparing lookup hit rates"


def test_pane_status_rail_defaults_to_none():
    # Existing keyword constructions never pass rail — the default must hold
    # through a full write/read cycle.
    activity.upsert_pane_status(_status_row())
    assert activity.get_pane_status("%1").rail is None


def test_pane_status_goal_and_history_roundtrip():
    activity.upsert_pane_status(_status_row(
        goal="redesign the rail into track-based organization",
        history='[{"t": 100, "s": "sketching tracks"}]'))
    got = activity.get_pane_status("%1")
    assert got.goal == "redesign the rail into track-based organization"
    assert got.history == '[{"t": 100, "s": "sketching tracks"}]'


def test_pane_status_goal_and_history_default_to_none():
    activity.upsert_pane_status(_status_row())
    got = activity.get_pane_status("%1")
    assert got.goal is None and got.history is None


def test_pane_status_migration_adds_rail_to_old_db():
    # The prod DB has pane_status rows that predate the rail column;
    # CREATE TABLE IF NOT EXISTS won't add it. Fabricate the old shape at
    # the (fixture-redirected) DB path BEFORE activity opens it, then let
    # _conn()'s probe-then-ALTER run on first use.
    import sqlite3

    from periscope import config
    db = sqlite3.connect(str(config.ACTIVITY_DB))
    db.execute(
        "CREATE TABLE pane_status ("
        "  pane_id TEXT PRIMARY KEY, session_id TEXT, status TEXT NOT NULL,"
        "  generated_at INTEGER NOT NULL, jsonl_size INTEGER NOT NULL,"
        "  seen_name TEXT, renamed_at INTEGER)"
    )
    db.execute("INSERT INTO pane_status VALUES "
               "('%1', 'sid-a', 'old status', 1000, 10, 'claude', NULL)")
    db.commit()
    db.close()
    got = activity.get_pane_status("%1")   # first _conn() → migration runs
    assert got.status == "old status"      # rows survive
    assert got.rail is None                # column added, backfilled NULL
    assert got.goal is None and got.history is None   # same for goal/history
    # Idempotent: a reconnect on the now-current shape must not raise.
    activity._CONN.close()
    activity._CONN = None
    assert activity.get_pane_status("%1").rail is None


def test_pane_status_lines_carries_rail():
    activity.upsert_pane_status(_status_row(
        "%1", status="doing a thing", generated_at=42, rail="short rail"))
    assert activity.pane_status_lines() == {"%1": ("doing a thing", 42, "short rail")}


def test_pane_status_upsert_overwrites_existing():
    activity.upsert_pane_status(_status_row(status="old", generated_at=1))
    activity.upsert_pane_status(_status_row(status="new", generated_at=2))
    got = activity.get_pane_status("%1")
    assert got.status == "new"
    assert got.generated_at == 2


def test_all_pane_statuses_returns_every_row():
    activity.upsert_pane_status(_status_row("%1"))
    activity.upsert_pane_status(_status_row("%2"))
    assert {r.pane_id for r in activity.all_pane_statuses()} == {"%1", "%2"}


def test_stamp_pane_rename_inserts_placeholder_row():
    # A manual rename can land before the narrator's first generation — the
    # cooldown must still stick (spec: never clobber a human-chosen name).
    activity.stamp_pane_rename("%7", name="my-name", at=5000)
    got = activity.get_pane_status("%7")
    assert got.status == ""            # placeholder
    assert got.jsonl_size == 0
    assert got.generated_at == 0
    assert got.seen_name == "my-name"
    assert got.renamed_at == 5000
    assert got.session_id is None
    assert got.rail is None                # 8th VALUES slot must be NULL


def test_stamp_pane_rename_updates_existing_row_only_in_place():
    activity.upsert_pane_status(_status_row(status="working on x", generated_at=900,
                                            rail="short rail"))
    activity.stamp_pane_rename("%1", name="human-name", at=6000)
    got = activity.get_pane_status("%1")
    assert got.status == "working on x"   # status untouched
    assert got.generated_at == 900        # generation clock untouched
    assert got.rail == "short rail"       # rail untouched
    assert got.seen_name == "human-name"
    assert got.renamed_at == 6000


def test_pane_status_lines_bulk_read_skips_placeholders():
    activity.upsert_pane_status(_status_row("%1", status="doing a thing", generated_at=42))
    activity.stamp_pane_rename("%2", name="n", at=1)   # placeholder, status=''
    assert activity.pane_status_lines() == {"%1": ("doing a thing", 42, None)}


def test_prune_pane_status_drops_dead_panes():
    activity.upsert_pane_status(_status_row("%1"))
    activity.upsert_pane_status(_status_row("%2"))
    assert activity.prune_pane_status({"%1"}) == 1
    assert activity.get_pane_status("%2") is None
    assert activity.get_pane_status("%1") is not None
    assert activity.prune_pane_status({"%1"}) == 0


def test_rename_event_kind_maps_to_session_src():
    # 'rename' is a free-form event kind: _row_to_event must pass it through
    # as src=session/kind=rename with no code change in the mapper.
    activity.record("pane", "%1", "rename", "renamed: claude → fs-liveness", at=10)
    out = activity.events_for("%1", "/repo", "main")
    assert out[0] == {"src": "session", "kind": "rename", "at": 10,
                      "text": "renamed: claude → fs-liveness",
                      "state": None, "url": None}


def test_worker_tick_invokes_narrator_with_claude_panes(monkeypatch):
    import periscope.narrator as narrator
    calls = []
    monkeypatch.setattr(narrator, "tick", lambda panes: calls.append(panes))
    monkeypatch.setattr(activity, "list_windows", lambda: [
        {"session": "s", "index": 0, "pane_id": "%1", "cwd": ""}])
    monkeypatch.setattr(activity, "tmux", lambda *a: "pane content")
    monkeypatch.setattr(activity, "parse_pane",
                        lambda c: {"agent": "claude", "context_pct": None,
                                   "state": "idle"})
    activity._worker_tick({})
    assert len(calls) == 1
    assert calls[0][0][0]["pane_id"] == "%1"


def test_worker_tick_syncs_bg_jobs(monkeypatch, fresh_activity_db):
    from periscope import activity, bg_commander
    monkeypatch.setattr(activity, "list_windows", list)     # no panes => skip narrator path
    called = {"sync": False}
    monkeypatch.setattr(bg_commander, "sync_jobs", lambda **kw: called.__setitem__("sync", True))
    activity._worker_tick({})
    assert called["sync"] is True


def test_pane_workspace_set_get(fresh_activity_db):
    activity = fresh_activity_db
    assert activity.get_pane_workspace("%1") is None
    activity.set_pane_workspace("%1", "ws_auth")
    assert activity.get_pane_workspace("%1") == "ws_auth"


def test_pane_workspace_retag_overwrites(fresh_activity_db):
    activity = fresh_activity_db
    activity.set_pane_workspace("%1", "ws_a")
    activity.set_pane_workspace("%1", "ws_b")
    assert activity.get_pane_workspace("%1") == "ws_b"


def test_pane_workspace_untag_clears(fresh_activity_db):
    activity = fresh_activity_db
    activity.set_pane_workspace("%1", "ws_a")
    activity.set_pane_workspace("%1", None)
    assert activity.get_pane_workspace("%1") is None


def test_pane_workspace_map(fresh_activity_db):
    activity = fresh_activity_db
    activity.set_pane_workspace("%1", "ws_a")
    activity.set_pane_workspace("%2", "ws_a")
    activity.set_pane_workspace("%3", "ws_b")
    assert activity.pane_workspace_map() == {"%1": "ws_a", "%2": "ws_a", "%3": "ws_b"}


def test_prune_pane_workspaces(fresh_activity_db):
    activity = fresh_activity_db
    activity.set_pane_workspace("%1", "ws_a")
    activity.set_pane_workspace("%2", "ws_a")
    dropped = activity.prune_pane_workspaces({"%1"})
    assert dropped == 1
    assert activity.get_pane_workspace("%2") is None
    assert activity.get_pane_workspace("%1") == "ws_a"


def test_pane_tracks_set_get_map_prune():
    activity.set_pane_track("%1", "tk_foo")
    activity.set_pane_track("%2", "loose")
    assert activity.get_pane_track("%1") == "tk_foo"
    assert activity.pane_track_map() == {"%1": "tk_foo", "%2": "loose"}
    activity.set_pane_track("%1", None)            # clear
    assert activity.get_pane_track("%1") is None
    removed = activity.prune_pane_tracks({"%2"})    # %1 already gone
    assert removed == 0
    activity.set_pane_track("%3", "tk_foo")
    assert activity.prune_pane_tracks({"%2"}) == 1  # %3 dead


def test_tracks_row_crud():
    activity.insert_track({"id": "tk_a", "name": "Alpha", "repo": "/r/a",
                           "created_at": 100, "archived_at": None})
    assert activity.get_track("tk_a")["name"] == "Alpha"
    assert [t["id"] for t in activity.all_tracks()] == ["tk_a"]
    activity.update_track("tk_a", name="Alpha2")
    assert activity.get_track("tk_a")["name"] == "Alpha2"
    activity.archive_track("tk_a", ts=200)
    assert activity.get_track("tk_a")["archived_at"] == 200
    activity.delete_track("tk_a")
    assert activity.get_track("tk_a") is None
