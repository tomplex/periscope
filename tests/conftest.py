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
    isn't enough — routes.state holds its own binding. Seeding the cache with a
    far-future next-attempt time makes cached_plan_usage serve the cache without
    ever spawning. Tests that exercise the refresh set their own _plan_cache via
    monkeypatch, which overrides this."""
    try:
        from periscope import usage
    except ImportError:
        return
    monkeypatch.setattr(usage, "_plan_cache", (float("inf"), None), raising=False)
    monkeypatch.setattr(usage, "_plan_in_flight", True, raising=False)


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
