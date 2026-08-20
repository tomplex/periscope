"""Shared pytest fixtures for periscope's test suite.

These fixtures sandbox side-effecting helpers so tests don't write to
~/.config/periscope or bind real unix sockets.
"""

import os
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

# Shared across the real-tmux integration tests (test_open_ops,
# test_worktree_spawn). Imported as `from tests.conftest import needs_tmux`.
needs_tmux = pytest.mark.skipif(not shutil.which("tmux"), reason="tmux not installed")


@pytest.fixture(autouse=True)
def _no_plan_usage_refresh(monkeypatch):
    """Stop cached_plan_usage() from spawning its real network+DB refresh thread
    during tests.

    On a cache miss cached_plan_usage() fires _bg("plan-usage", ...), which does
    a live httpx fetch then record_usage_samples(...). As a leaked daemon that
    thread writes into whichever per-test ACTIVITY_DB happens to be live when it
    finishes — bleeding real usage_samples rows into unrelated tests AND racing
    fresh_activity_db's connection close (use-after-free → the intermittent
    CPython 3.14 sqlite segfault). It is reachable from any path that builds
    /api/state, so patching one call site (e.g. periscope.app.cached_plan_usage)
    isn't enough — routes.state holds its own binding.

    The guard is neutering usage._bg itself, not seeding the cache: the cache is
    now per account (account id -> (next_attempt, data)), so a seeded entry only
    covers the accounts it names — any account the registry grows, or a test
    that swaps the registry, would still spawn. A no-op _bg closes the hazard by
    construction for every account and for the sibling pane-cost thread too.
    Tests that exercise spawning monkeypatch _bg themselves, which overrides
    this; the fresh cache/in-flight containers keep per-test state from leaking
    into the module globals."""
    try:
        from periscope import usage
    except ImportError:
        return
    monkeypatch.setattr(usage, "_plan_cache", {}, raising=False)
    monkeypatch.setattr(usage, "_plan_in_flight", set(), raising=False)
    monkeypatch.setattr(usage, "_bg", lambda *a, **kw: None, raising=False)
    monkeypatch.setattr(usage, "_cost_cache", (0.0, {}), raising=False)
    monkeypatch.setattr(usage, "_cost_in_flight", False, raising=False)
    monkeypatch.setattr(usage, "_base_ctx_cache", {}, raising=False)


@pytest.fixture(autouse=True)
def _no_live_session_scan(monkeypatch):
    """Keep turns.session_id_for_pane's live lookup off the real machine.

    It resolves a pane to its running claude by walking the process tree
    (`tmux list-panes` + `ps`), and it runs on every /api/state poll — so any
    test that builds a window view would otherwise fork against the developer's
    real tmux server and read their real ~/.claude/sessions. Stubbing the tmux
    seam makes pane_claude_pids() return {} by its own no-panes path (no ps
    fork either), so every test falls back to the pane_sessions row unless it
    opts in. Tests of the live path patch these same seams themselves, which
    overrides this."""
    try:
        from periscope import session_status
    except ImportError:
        return
    monkeypatch.setattr(session_status, "_tmux_pane_pids", dict, raising=False)
    monkeypatch.setattr(session_status, "_pane_pids_cache", None, raising=False)
    monkeypatch.setattr(session_status, "_proc_table_cache", None, raising=False)
    # pane_config_dirs' 15s TTL outlives a test — a populated cache would bleed
    # one test's pane->config-dir map into every later view build.
    monkeypatch.setattr(session_status, "_config_dirs_cache", None, raising=False)


@pytest.fixture(scope="session", autouse=True)
def _session_activity_db_guard(tmp_path_factory):
    """Session-wide floor that the real periscope.db is NEVER the active
    ACTIVITY_DB — not even for a `_bg` daemon thread that leaks past a test.

    The lifespan spawns daemon housekeeping threads (e.g. pane-sessions
    housekeeping → prune_pane_workspaces/projects) that can run AFTER a test's
    function-scoped `monkeypatch` has reverted ACTIVITY_DB. A per-test redirect
    therefore can't protect the real DB from a late-firing leaked thread — the
    revert target must itself be a temp path. Pinning ACTIVITY_DB to a session
    temp here makes every revert land on a throwaway DB. Function-scoped
    redirects (below / fresh_activity_db) layer per-test isolation on top; their
    revert target is this session DB, never the user's real file."""
    from periscope import config
    mp = pytest.MonkeyPatch()
    db = tmp_path_factory.mktemp("activity-session") / "session.db"
    mp.setattr(config, "ACTIVITY_DB", db)
    yield
    mp.undo()


@pytest.fixture(autouse=True)
def _isolate_activity_db(tmp_path, monkeypatch):
    """Backstop: NO test may ever touch the real ~/.config/periscope/periscope.db.

    `config.ACTIVITY_DB` is frozen to the real path at import, and
    `activity._conn()` lazily caches a connection to it — so ANY test that runs
    activity-DB code without redirecting writes to the user's real database.
    This actually caused data loss: `test_app.py`'s lifespan tests run the
    housekeeping pruners (`prune_pane_workspaces`/`prune_pane_tracks`) against
    the real DB, and with an `alive` set that didn't cover the live panes they
    deleted every row — real workspace/project membership gone. The opt-in
    `fresh_activity_db` fixture only protected tests that remembered to request
    it. This autouse guard redirects every test to a per-test temp DB and resets
    the cached connection unconditionally. `fresh_activity_db` still layers its
    own redirect on top for tests that want the module handle."""
    from periscope import activity, config
    monkeypatch.setattr(config, "ACTIVITY_DB", tmp_path / "autouse-activity.db")
    activity._CONN = None
    activity._git_cache.clear()
    activity._git_fetching.clear()
    yield
    # Close under _LOCK so a leaked in-flight write doesn't use-after-free the
    # connection mid-close (the documented CPython 3.14 sqlite segfault).
    with activity._LOCK:
        if activity._CONN is not None:
            activity._CONN.close()
            activity._CONN = None


@pytest.fixture(autouse=True)
def _no_track_maintenance(monkeypatch):
    """Keep resolve_pids' one-shot pane_tracks maintenance OFF by default.

    The module flag `pids._TRACKS_MAINTAINED` defaults to False (prod runs
    maintenance once per boot, from the first full-roster resolve), and a
    completed pass flips it True — module state that would leak across tests
    and make any suite exercising a full-roster resolve (routes/state,
    list_claudes) order-dependent. Pin it True here so maintenance never fires
    unless a test opts in by setting it False itself; monkeypatch restores the
    import-time default afterward."""
    from periscope import pids
    monkeypatch.setattr(pids, "_TRACKS_MAINTAINED", True, raising=False)


@pytest.fixture
def tmp_xdg_home(monkeypatch, tmp_path: Path) -> Path:
    """Redirect XDG_CONFIG_HOME so state.json, pidfile, and the log
    file all land in a per-test tempdir."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def fake_tmux(mocker):
    """Replace periscope.tmux.tmux with a Mock. Returns the mock so the
    test can configure side_effect / return_value and assert call args.

    Activates only after Peel 2 ships periscope/tmux.py. Tests written
    before then skip when they request this fixture.
    """
    try:
        import periscope.tmux as tmux_mod
    except ImportError:
        pytest.skip("periscope.tmux not yet present (pre-Peel-2)")
    mock = mocker.patch.object(tmux_mod, "tmux", autospec=True)
    mock.return_value = ""
    return mock


@pytest.fixture
def fresh_activity_db(tmp_path, monkeypatch):
    """Point periscope.activity's lazy SQLite connection at a per-test
    tmp_path DB and clear its in-module caches, so tests never touch the
    user's real ~/.config/periscope/periscope.db. Yields the activity
    module for convenience."""
    from periscope import activity, config
    monkeypatch.setattr(config, "ACTIVITY_DB", tmp_path / "periscope.db")
    activity._CONN = None
    activity._git_cache.clear()
    activity._git_fetching.clear()
    yield activity
    # Acquire _LOCK before closing: a leaked background thread (e.g. the lifespan
    # prewarms) may be mid-executemany on this connection, and closing it from
    # under the C layer is a use-after-free → segfault under CPython 3.14. The
    # lock serializes us behind any in-flight write; every DB accessor holds it.
    with activity._LOCK:
        if activity._CONN is not None:
            activity._CONN.close()
            activity._CONN = None


@pytest.fixture
def clean_state(tmp_xdg_home, monkeypatch):
    """Reset periscope.store._STATE to a fresh defaults dict for the test.
    Returns the dict so the test can prepopulate fields before exercising
    code-under-test.

    All in-tree consumers now go through the typed accessors in
    `periscope.store` (set_window_fields, get_window, update_ui, etc.) or
    via module-qualified `periscope.store._STATE` access. Monkeypatching
    `periscope.store._STATE` here is therefore sufficient — no rebind loop
    across consumer modules needed.
    """
    import periscope.store as store
    fresh = {
        "version": 2,
        "ui": {},
        "windows": {},
        "commands": [],
        "projects": {},
        "workspaces": {},
        "settings": {},
    }
    monkeypatch.setattr(store, "_STATE", fresh)
    return fresh


@pytest.fixture
def tmux_test_server(monkeypatch):
    """Isolated tmux server (-L) + a harmless CLAUDE_EXEC stub, so spawns
    don't touch the default server or launch real Claude."""
    sock = f"periscope-open-test-{uuid.uuid4().hex[:8]}"
    monkeypatch.setenv("PERISCOPE_TMUX_SOCKET", sock)
    monkeypatch.setenv("PERISCOPE_CLAUDE_EXEC", "cat")   # sits on stdin; window stays alive
    yield sock
    subprocess.run(["tmux", "-L", sock, "kill-server"], capture_output=True)


@pytest.fixture
def tmp_git_repo(tmp_path):
    """Real git repo with one commit. Returns a realpath'd Path (macOS
    /var -> /private/var, so callers compare against realpath)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "--allow-empty", "-qm", "init"],
                   cwd=repo, env=env, check=True)
    return Path(os.path.realpath(repo))
