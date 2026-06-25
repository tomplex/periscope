"""Periscope window-ids (@periscope_id): mint, stamp, rebind, resolve."""

import time

from periscope.pids import (
    _PID_TTL_S,
    _gc_windows,
    _mint_pid,
    _rebind_pid,
    _stamp_pid,
    resolve_pids,
)


def test_gc_windows_keeps_spawned_by_only_entry():
    """A freshly-written provenance entry (spawned_by set, NO last_seen yet)
    must survive a GC pass that doesn't see its window — otherwise a poll
    straddling spawn_claude reaps the breadcrumb before the window's first
    resolve stamps last_seen. Regression: report() found no spawner because
    spawned_by was GC'd at write time."""
    now = int(time.time())
    wblock = {"child99": {"spawned_by": "parent11"}}  # no last_seen
    # child99 not in `taken` (its window absent from this pass) → only
    # immunity can save it.
    deleted = _gc_windows(wblock, taken=set(), now_ts=now)
    assert "child99" in wblock
    assert wblock["child99"]["spawned_by"] == "parent11"
    assert deleted is False


def test_gc_windows_reaps_stale_unannotated_entry():
    """Control: an entry with no immunity field and an expired last_seen IS
    reaped — confirms the test above isn't passing for a trivial reason."""
    now = int(time.time())
    wblock = {"stale01": {"last_seen": {"ts": now - _PID_TTL_S - 100}}}
    deleted = _gc_windows(wblock, taken=set(), now_ts=now)
    assert "stale01" not in wblock
    assert deleted is True


def test_mint_pid_format():
    """_mint_pid returns an 8-char lowercase hex string."""
    pid = _mint_pid()
    assert isinstance(pid, str)
    assert len(pid) == 8
    assert all(c in "0123456789abcdef" for c in pid)


def test_mint_pid_uniqueness():
    """100 mints are highly unlikely to collide — guards against accidental
    counter-based regressions."""
    seen = {_mint_pid() for _ in range(100)}
    assert len(seen) == 100


def test_stamp_pid_calls_tmux_set_option(mocker):
    """_stamp_pid should call `tmux set-option -w -t <target> @periscope_id <pid>`."""
    mock_tmux = mocker.patch("periscope.pids.tmux", return_value="")
    _stamp_pid("foo:0", "abc12345")
    mock_tmux.assert_called_once()
    args = mock_tmux.call_args.args
    assert "set-option" in args
    assert "-w" in args
    assert "-t" in args
    assert "foo:0" in args
    assert "@periscope_id" in args
    assert "abc12345" in args


def test_rebind_pid_strong_match_on_session_and_name():
    """Pass 1 matches when (session, name) line up and the entry is fresh."""
    now = int(time.time())
    wblock = {
        "deadbeef": {
            "last_seen": {
                "session": "main",
                "name": "shell",
                "branch": "feature",
                "cwd": "/tmp",
                "ts": now,
            }
        }
    }
    pid = _rebind_pid(
        wblock, session="main", name="shell", branch=None, cwd=None,
        taken_pids=set(),
    )
    assert pid == "deadbeef"


def test_rebind_pid_returns_none_when_no_match():
    pid = _rebind_pid(
        {}, session="main", name="shell", branch=None, cwd=None,
        taken_pids=set(),
    )
    assert pid is None


def test_rebind_pid_skips_taken_pids():
    now = int(time.time())
    wblock = {
        "deadbeef": {
            "last_seen": {
                "session": "main", "name": "shell",
                "branch": None, "cwd": None, "ts": now,
            }
        }
    }
    pid = _rebind_pid(
        wblock, session="main", name="shell", branch=None, cwd=None,
        taken_pids={"deadbeef"},
    )
    assert pid is None


def test_rebind_pid_skips_expired_entries():
    """Entries with last_seen.ts older than _PID_TTL_S must not be rebound."""
    old = int(time.time()) - _PID_TTL_S - 100
    wblock = {
        "deadbeef": {
            "last_seen": {
                "session": "main", "name": "shell",
                "branch": None, "cwd": None, "ts": old,
            }
        }
    }
    pid = _rebind_pid(
        wblock, session="main", name="shell", branch=None, cwd=None,
        taken_pids=set(),
    )
    assert pid is None


def test_rebind_pid_secondary_match_on_branch_and_cwd():
    """Pass 2 (branch + cwd) fires when pass 1 (session + name) misses."""
    now = int(time.time())
    wblock = {
        "deadbeef": {
            "last_seen": {
                "session": "old-session",
                "name": "old-name",
                "branch": "feature",
                "cwd": "/tmp",
                "ts": now,
            }
        }
    }
    pid = _rebind_pid(
        wblock, session="new-session", name="new-name",
        branch="feature", cwd="/tmp", taken_pids=set(),
    )
    assert pid == "deadbeef"


def test_resolve_pids_uses_existing_periscope_id_when_well_formed(clean_state, mocker):
    """When @periscope_id is already an 8-char hex string, resolve_pids
    must NOT mint a new one or call _stamp_pid."""
    mock_stamp = mocker.patch("periscope.pids._stamp_pid")
    windows = [
        {
            "session": "main", "index": 0, "name": "shell",
            "cwd": "/tmp", "pid_raw": "deadbeef",
        },
    ]
    resolve_pids(windows)
    assert windows[0]["pid"] == "deadbeef"
    assert "pid_raw" not in windows[0]
    mock_stamp.assert_not_called()


def test_resolve_pids_mints_when_periscope_id_missing(clean_state, mocker):
    """When @periscope_id is empty, resolve_pids mints + stamps a fresh id."""
    mock_stamp = mocker.patch("periscope.pids._stamp_pid")
    mocker.patch("periscope.pids._mint_pid", return_value="aaaabbbb")
    windows = [
        {
            "session": "main", "index": 0, "name": "shell",
            "cwd": "/tmp", "pid_raw": "",
        },
    ]
    resolve_pids(windows)
    assert windows[0]["pid"] == "aaaabbbb"
    mock_stamp.assert_called_once_with("main:0", "aaaabbbb")


def test_resolve_pids_assigns_unique_pids_to_each_window(clean_state, mocker):
    """resolve_pids walks every window; each ends up with a non-empty pid."""
    mocker.patch("periscope.pids._stamp_pid")
    mocker.patch(
        "periscope.pids._mint_pid",
        side_effect=[f"pid{i:05x}" for i in range(10)],
    )
    windows = [
        {"session": "main", "index": 0, "name": "shell",
         "cwd": "/x", "pid_raw": ""},
        {"session": "main", "index": 1, "name": "claude",
         "cwd": "/y", "pid_raw": ""},
    ]
    resolve_pids(windows)
    for w in windows:
        assert w.get("pid")
    # Distinct pids.
    assert windows[0]["pid"] != windows[1]["pid"]


def test_resolve_pids_handles_empty_windows_list(clean_state):
    """No-op on an empty input list; doesn't touch state."""
    resolve_pids([])
    # Did not raise; nothing else to assert.


def test_resolve_pids_re_mints_when_two_windows_share_periscope_id(
    clean_state, mocker
):
    """Real-world failure: tmux can end up with the same @periscope_id
    stamped on multiple windows (e.g. after session-copy or set-option
    races). resolve_pids must keep that id for the first window only and
    re-mint a fresh id for the duplicate, then re-stamp tmux so future
    polls take the fast path with the corrected id.

    Without this gate, all duplicates resolve to the same state.windows
    entry — collapsing acted_at / linked_pr / notes onto a single record
    and breaking grid sort (the bug Tom hit with the `lgtm` session, where
    `shell` kept appearing leftmost because every window shared one pid).
    """
    mock_stamp = mocker.patch("periscope.pids._stamp_pid")
    mocker.patch(
        "periscope.pids._mint_pid",
        side_effect=[f"minted{i:02x}" for i in range(10)],
    )
    windows = [
        {"session": "lgtm", "index": 1, "name": "shell",
         "cwd": "/x", "pid_raw": "deadbeef"},
        {"session": "lgtm", "index": 2, "name": "walkthrough-pr-review",
         "cwd": "/x", "pid_raw": "deadbeef"},
        {"session": "lgtm", "index": 3, "name": "walkthrough-design",
         "cwd": "/x", "pid_raw": "deadbeef"},
    ]
    resolve_pids(windows)
    pids = [w["pid"] for w in windows]
    assert len(set(pids)) == 3, f"expected 3 distinct pids, got {pids}"
    assert pids[0] == "deadbeef"  # first window keeps the id
    assert "deadbeef" not in pids[1:]  # duplicates re-minted
    # Re-stamped the corrected pid on the duplicate windows so the next
    # poll sees the right @periscope_id directly.
    stamped_targets = {call.args[0] for call in mock_stamp.call_args_list}
    assert "lgtm:2" in stamped_targets
    assert "lgtm:3" in stamped_targets
