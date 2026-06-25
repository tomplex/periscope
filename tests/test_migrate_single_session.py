"""Real-tmux tests for the one-shot single-session consolidation migration.

These spawn a real tmux on an isolated -L socket (tmux_test_server) and move
real windows — NOT mocked. A mocked migration that passes while the real
`move-window` fails is the exact incident this exists to prevent.
"""

from tests.conftest import needs_tmux


def _sessions_of_all_windows(tmux):
    out = tmux("list-windows", "-a", "-F", "#{session_name}")
    return [s for s in out.strip().split("\n") if s]


@needs_tmux
def test_moves_windows_and_idempotent(tmux_test_server):
    from periscope import config
    from periscope.migrate_single_session import _move_managed_windows
    from periscope.tmux import _tmux_mutate, tmux

    # Two non-managed sessions, one with an extra window.
    assert _tmux_mutate("new-session", "-d", "-s", "a", "-c", "/tmp")[0]
    assert _tmux_mutate("new-window", "-t", "a:", "-c", "/tmp")[0]
    assert _tmux_mutate("new-session", "-d", "-s", "b", "-c", "/tmp")[0]

    before = _sessions_of_all_windows(tmux)
    non_managed = [s for s in before if s != config.MANAGED_SESSION]
    assert non_managed, "fixture should have created non-managed windows"

    moved = _move_managed_windows()
    assert moved >= len(non_managed)

    after = _sessions_of_all_windows(tmux)
    assert after, "windows should still exist after the move"
    assert all(s == config.MANAGED_SESSION for s in after), after

    # Second run: nothing left outside MANAGED_SESSION.
    assert _move_managed_windows() == 0


@needs_tmux
def test_pane_id_survives_move(tmux_test_server):
    """The stable %pane_id still resolves post-move — this is what makes the
    T9 pane_id-keyed bridge re-key correct across the migration."""
    from periscope import config
    from periscope.migrate_single_session import _move_managed_windows
    from periscope.tmux import _tmux_mutate, tmux

    assert _tmux_mutate("new-session", "-d", "-s", "a", "-c", "/tmp")[0]
    pane_id = tmux("display-message", "-t", "a:", "-p", "#{pane_id}").strip()
    assert pane_id.startswith("%")

    _move_managed_windows()

    # capture-pane by stable pane_id still succeeds.
    import os
    import subprocess
    sock = os.environ["PERISCOPE_TMUX_SOCKET"]
    r = subprocess.run(
        ["tmux", "-L", sock, "capture-pane", "-t", pane_id, "-p"],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr
    session = tmux("display-message", "-t", pane_id, "-p", "#{session_name}").strip()
    assert session == config.MANAGED_SESSION


@needs_tmux
def test_skips_usage_session_prefix(tmux_test_server):
    from periscope import config
    from periscope.migrate_single_session import _move_managed_windows
    from periscope.tmux import _tmux_mutate, tmux

    usage = f"{config.USAGE_SESSION_PREFIX}xyz"
    assert _tmux_mutate("new-session", "-d", "-s", usage, "-c", "/tmp")[0]
    assert _tmux_mutate("new-session", "-d", "-s", "real", "-c", "/tmp")[0]

    _move_managed_windows()

    after = _sessions_of_all_windows(tmux)
    assert usage in after, "usage scraper session must never be moved"
    assert config.MANAGED_SESSION in after, "the real session should have moved in"


@needs_tmux
def test_gating_not_prod_is_noop(tmux_test_server, fresh_activity_db, clean_state, monkeypatch):
    from periscope import config, store
    from periscope import migrate_single_session as mss
    from periscope.tmux import _tmux_mutate

    assert _tmux_mutate("new-session", "-d", "-s", "a", "-c", "/tmp")[0]
    monkeypatch.setattr(config, "is_prod", lambda: False)

    mss.run_if_needed()

    # No MANAGED_SESSION created, flag stays unset.
    assert not _tmux_mutate("has-session", "-t", config.MANAGED_SESSION)[0]
    assert not store.is_single_session_migration_done()


@needs_tmux
def test_gating_flag_set_is_noop(tmux_test_server, fresh_activity_db, clean_state, monkeypatch):
    from periscope import config, store
    from periscope import migrate_single_session as mss
    from periscope.tmux import _tmux_mutate

    assert _tmux_mutate("new-session", "-d", "-s", "a", "-c", "/tmp")[0]
    monkeypatch.setattr(config, "is_prod", lambda: True)
    store.mark_single_session_migration_done()
    assert store.is_single_session_migration_done()

    mss.run_if_needed()

    # Flag already set → no MANAGED_SESSION created.
    assert not _tmux_mutate("has-session", "-t", config.MANAGED_SESSION)[0]
