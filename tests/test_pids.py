"""Periscope window-ids (@periscope_id): mint, stamp, rebind, resolve."""

import time

from periscope.pids import (
    _PID_TTL_S,
    _REBIND_TTL_S,
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


def test_rebind_pid_skips_entries_older_than_rebind_ttl():
    """A fresh pane must NOT inherit a long-dead entry's identity.

    Regression: rebind shared the 30-day GC TTL, so a brand-new Claude at a
    repo root on master matched any entry from the past month via the
    (branch, cwd) fallback and inherited its immunity fields — surfacing as a
    fresh pane wearing a stale PR and Linear ticket.
    """
    stale = int(time.time()) - _REBIND_TTL_S - 60
    wblock = {
        "deadbeef": {
            "last_seen": {
                "session": "periscope", "name": "old-goal",
                "branch": "master", "cwd": "/repo", "ts": stale,
            }
        }
    }
    pid = _rebind_pid(
        wblock, session="periscope", name="fresh-claude",
        branch="master", cwd="/repo", taken_pids=set(),
    )
    assert pid is None, "stale entry must not capture a fresh pane"


def test_rebind_pid_still_matches_within_rebind_ttl():
    """The legitimate case survives: a tmux server restart drops @periscope_id
    from every window, and they are re-sighted unstamped moments later."""
    recent = int(time.time()) - 30
    wblock = {
        "deadbeef": {
            "last_seen": {
                "session": "periscope", "name": "old-goal",
                "branch": "master", "cwd": "/repo", "ts": recent,
            }
        }
    }
    pid = _rebind_pid(
        wblock, session="periscope", name="fresh-claude",
        branch="master", cwd="/repo", taken_pids=set(),
    )
    assert pid == "deadbeef"


def test_rebind_pid_skips_expired_entries():
    """Entries with last_seen.ts older than the rebind TTL must not be rebound."""
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
         "cwd": "/x", "pid_raw": "deadbeef", "window_id": "@11"},
        {"session": "lgtm", "index": 2, "name": "walkthrough-pr-review",
         "cwd": "/x", "pid_raw": "deadbeef", "window_id": "@12"},
        {"session": "lgtm", "index": 3, "name": "walkthrough-design",
         "cwd": "/x", "pid_raw": "deadbeef", "window_id": "@13"},
    ]
    resolve_pids(windows)
    pids = [w["pid"] for w in windows]
    assert len(set(pids)) == 3, f"expected 3 distinct pids, got {pids}"
    assert pids[0] == "deadbeef"  # first window keeps the id
    assert "deadbeef" not in pids[1:]  # duplicates re-minted
    # Re-stamped the corrected pid on the duplicate windows so the next poll
    # sees the right @periscope_id directly — targeted by WINDOW ID, never
    # session:index. An index-targeted stamp lands on the wrong window after a
    # move-window renumber, leaving the duplicate uncleared so the next poll
    # re-mints again (the observed 683-re-mint loop).
    stamped_targets = {call.args[0] for call in mock_stamp.call_args_list}
    assert stamped_targets == {"@12", "@13"}
    assert "lgtm:2" not in stamped_targets


def test_rebind_never_steals_a_pid_a_live_window_still_carries(clean_state, mocker):
    """An unstamped window must NOT rebind onto a pid that another window in
    the SAME pass is still carrying in tmux.

    `taken` is built incrementally, so a window resolved LATER in the pass is
    an eligible rebind candidate: its state entry was refreshed 3s ago and is
    well inside `_REBIND_TTL_S`. A freshly-created window sitting earlier in
    the list therefore matches the live window's entry — on (session, name),
    or on the (branch, cwd) fallback that a spawn into the caller's own
    worktree hits by construction — and gets that live pid stamped onto it.

    That is the duplicate FACTORY. The dedup gate in `_resolve_one` only
    cleans up afterwards: on the next poll one of the two windows is re-minted
    and silently changes identity, detaching pid-keyed UI state and orphaning
    the `spawned_by` breadcrumb that `report()` routes on.
    """
    mocker.patch("periscope.pids._stamp_pid")
    mocker.patch("periscope.pids._mint_pid", side_effect=[f"fresh{i:03x}" for i in range(10)])
    from periscope import store as _store

    now = int(time.time())
    _store._STATE["windows"] = {
        "beef0001": {
            "last_seen": {"session": "periscope", "name": "claude",
                          "branch": "main", "cwd": "/repo", "ts": now},
            "linked_pr": 1234,
        },
    }
    windows = [
        # Brand-new spawn, not yet stamped, sharing the caller's worktree.
        {"session": "periscope", "index": 1, "name": "claude",
         "branch": "main", "cwd": "/repo", "pid_raw": "", "window_id": "@20"},
        # The live window that actually owns livepid0.
        {"session": "periscope", "index": 2, "name": "claude",
         "branch": "main", "cwd": "/repo", "pid_raw": "beef0001", "window_id": "@21"},
    ]
    resolve_pids(windows)
    assert windows[1]["pid"] == "beef0001", (
        f"live window lost its identity to a rebind steal: {windows[1]['pid']}"
    )
    assert windows[0]["pid"] != "beef0001"
