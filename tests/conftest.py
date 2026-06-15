"""Shared pytest fixtures for periscope's test suite.

These fixtures sandbox side-effecting helpers so tests don't write to
~/.config/periscope or bind real unix sockets.
"""

import os
import subprocess
import uuid
from pathlib import Path

import pytest


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
