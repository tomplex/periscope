"""Tests for periscope.turns (pane -> session -> transcript) and the
pane_session_hook producer that records the pane -> session mapping."""
import json

import periscope.activity as activity
import periscope.turns as turns


def _write_jsonl(path, cwd, text="hello transcript"):
    path.write_text(json.dumps({
        "type": "user", "sessionId": path.stem, "cwd": cwd,
        "gitBranch": "main", "timestamp": "2026-06-01T10:00:00.000Z",
        "uuid": "u1", "parentUuid": None,
        "message": {"role": "user", "content": text},
    }) + "\n")


def _seed_projects(tmp_path, monkeypatch, cwd):
    enc = tmp_path / activity._encode_cwd(cwd)
    enc.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(activity, "_PROJECTS_DIR", tmp_path)
    return enc


# ── session_id_for_pane reads pane_sessions ──────────────────────────────

def test_session_id_for_pane_reads_db(fresh_activity_db):
    sid = "c0f5cc37-c50d-47e3-9b10-60bb363e4d10"
    activity._conn().execute(
        "INSERT INTO pane_sessions (pane_id, session_id, updated_at) VALUES (?,?,?)",
        ("%56", sid, 1),
    )
    activity._conn().commit()
    assert turns.session_id_for_pane("%56") == sid
    assert turns.session_id_for_pane("%999") is None   # no row for that pane
    assert turns.session_id_for_pane("") is None


# ── get_turns_for_pane ───────────────────────────────────────────────────

def test_picks_pane_specific_session_when_cwd_shared(tmp_path, monkeypatch):
    """Two panes, one cwd, two sessions — the recorded session id disambiguates."""
    cwd = "/Users/tom/dev/shared"
    enc = _seed_projects(tmp_path, monkeypatch, cwd)
    _write_jsonl(enc / "sid-A.jsonl", cwd, "conversation A")
    _write_jsonl(enc / "sid-B.jsonl", cwd, "conversation B")
    monkeypatch.setattr(turns, "tmux", lambda *a, **k: f"%7\t{cwd}")
    monkeypatch.setattr(turns, "session_id_for_pane", lambda pid: "sid-B")

    out = turns.get_turns_for_pane("main", 4)
    assert out["session_id"] == "sid-B"
    assert out["messages"][0]["text"] == "conversation B"


def test_resolves_session_under_different_cwd_dir(tmp_path, monkeypatch):
    """The session started in /a but the pane has since cd'd to /b, so its JSONL
    is under /a's encoded dir. Glob-by-session-id finds it regardless of the
    pane's current cwd (the worktree-switch case)."""
    start_cwd = "/Users/tom/dev/started-here"
    cur_cwd = "/Users/tom/dev/worktrees/now-here"
    enc = tmp_path / activity._encode_cwd(start_cwd)
    enc.mkdir(parents=True)
    _write_jsonl(enc / "sid-X.jsonl", start_cwd, "moved convo")
    monkeypatch.setattr(activity, "_PROJECTS_DIR", tmp_path)
    monkeypatch.setattr(turns, "tmux", lambda *a, **k: f"%2\t{cur_cwd}")
    monkeypatch.setattr(turns, "session_id_for_pane", lambda pid: "sid-X")

    out = turns.get_turns_for_pane("main", 1)
    assert out["session_id"] == "sid-X"
    assert out["messages"][0]["text"] == "moved convo"


def test_falls_back_to_newest_when_no_session(tmp_path, monkeypatch):
    cwd = "/Users/tom/dev/solo"
    enc = _seed_projects(tmp_path, monkeypatch, cwd)
    _write_jsonl(enc / "only.jsonl", cwd, "solo convo")
    monkeypatch.setattr(turns, "tmux", lambda *a, **k: f"%1\t{cwd}")
    monkeypatch.setattr(turns, "session_id_for_pane", lambda pid: None)

    out = turns.get_turns_for_pane("main", 0)
    assert out["session_id"] == "only"
    assert out["messages"][0]["text"] == "solo convo"


def test_falls_back_when_recorded_session_jsonl_missing(tmp_path, monkeypatch):
    """A recorded session whose JSONL isn't in this cwd (stale after /clear)
    falls back to the pane's current newest transcript, not nothing."""
    cwd = "/Users/tom/dev/rot"
    enc = _seed_projects(tmp_path, monkeypatch, cwd)
    _write_jsonl(enc / "current.jsonl", cwd, "current convo")
    monkeypatch.setattr(turns, "tmux", lambda *a, **k: f"%3\t{cwd}")
    monkeypatch.setattr(turns, "session_id_for_pane", lambda pid: "gone-sid")

    out = turns.get_turns_for_pane("main", 2)
    assert out["session_id"] == "current"


def test_none_when_no_transcript(tmp_path, monkeypatch):
    monkeypatch.setattr(activity, "_PROJECTS_DIR", tmp_path)
    monkeypatch.setattr(turns, "tmux", lambda *a, **k: "%1\t/no/such/cwd")
    monkeypatch.setattr(turns, "session_id_for_pane", lambda pid: None)
    assert turns.get_turns_for_pane("main", 0) is None


# ── pane_session_hook producer (SessionStart / UserPromptSubmit) ─────────

def _hook_row(db_path, pane_id):
    import sqlite3
    with sqlite3.connect(db_path) as c:
        row = c.execute(
            "SELECT session_id FROM pane_sessions WHERE pane_id=?", (pane_id,),
        ).fetchone()
    return row[0] if row else None


def test_hook_records_pane_session(tmp_path, monkeypatch):
    import io
    import pane_session_hook as hook
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("TMUX_PANE", "%56")
    # session id comes from the hook payload (authoritative/current), not env.
    monkeypatch.setattr("sys.stdin", io.StringIO('{"session_id":"sess-abc","cwd":"/x"}'))
    hook.record()
    db = tmp_path / "periscope" / "periscope.db"
    assert _hook_row(db, "%56") == "sess-abc"


def test_hook_upserts_on_repeat(tmp_path, monkeypatch):
    """Second call for the same pane overwrites the recorded session id —
    important on /clear, which mints a fresh session id for an existing pane."""
    import io
    import pane_session_hook as hook
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("TMUX_PANE", "%56")
    monkeypatch.setattr("sys.stdin", io.StringIO('{"session_id":"old"}'))
    hook.record()
    monkeypatch.setattr("sys.stdin", io.StringIO('{"session_id":"new"}'))
    hook.record()
    db = tmp_path / "periscope" / "periscope.db"
    assert _hook_row(db, "%56") == "new"


def test_hook_noop_without_tmux_pane(tmp_path, monkeypatch):
    import io
    import pane_session_hook as hook
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("TMUX_PANE", raising=False)
    monkeypatch.setattr("sys.stdin", io.StringIO('{"session_id":"x"}'))
    hook.record()
    # No DB created — the hook bails before touching disk.
    assert not (tmp_path / "periscope" / "periscope.db").exists()


# ── pane_sessions helpers in activity.py ─────────────────────────────────

def test_prune_pane_sessions_drops_dead(fresh_activity_db):
    c = activity._conn()
    c.executemany(
        "INSERT INTO pane_sessions (pane_id, session_id, updated_at) VALUES (?,?,?)",
        [("%1", "a", 1), ("%2", "b", 1), ("%3", "c", 1)],
    )
    c.commit()
    dropped = activity.prune_pane_sessions({"%1", "%3"})
    assert dropped == 1
    rows = {r[0] for r in c.execute("SELECT pane_id FROM pane_sessions")}
    assert rows == {"%1", "%3"}


def test_migrate_legacy_pane_sessions(fresh_activity_db, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    legacy = tmp_path / "periscope" / "pane_sessions"
    legacy.mkdir(parents=True)
    (legacy / "%10").write_text("sid-10")
    (legacy / "%11").write_text("sid-11\n")
    (legacy / "skipme").write_text("not-a-pane-id")  # filtered out
    imported = activity.migrate_legacy_pane_sessions()
    assert imported == 2
    assert not legacy.exists()  # directory wiped on success
    rows = dict(activity._conn().execute(
        "SELECT pane_id, session_id FROM pane_sessions"
    ).fetchall())
    assert rows == {"%10": "sid-10", "%11": "sid-11"}
    # Idempotent: second call sees no legacy dir, does nothing.
    assert activity.migrate_legacy_pane_sessions() == 0
