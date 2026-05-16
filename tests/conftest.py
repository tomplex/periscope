"""Shared pytest fixtures for periscope's test suite.

These fixtures sandbox side-effecting helpers so tests don't write to
~/.config/periscope or bind real unix sockets.
"""

from pathlib import Path

import pytest


# Exclude the standalone PEP-723 smoke script from pytest collection.
# `tests/test_channel_smoke.py` is `uv run`-shaped (declares its own
# deps in the script header) and predates pytest discovery here. Keep
# it runnable via `uv run tests/test_channel_smoke.py` but don't let
# pytest try to import it (its deps — mcp/anyio/websockets — are not
# in the dev-deps group).
collect_ignore = ["test_channel_smoke.py"]


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
def clean_state(tmp_xdg_home, monkeypatch):
    """Reset periscope.store._STATE to a fresh defaults dict for the test.
    Available after Peel 4. Returns the dict so the test can prepopulate
    fields before exercising code-under-test.

    Consumers of `_STATE` typically do `from periscope.store import _STATE`
    at module top, binding to the dict object — so monkeypatching only
    `periscope.store._STATE` would leave those references pointing at the
    original dict. We patch every module that's imported `_STATE` by name
    so the test sees a consistent view.
    """
    try:
        import periscope.store as store
    except ImportError:
        pytest.skip("periscope.store not yet present (pre-Peel-4)")
    fresh = {
        "version": 1,
        "ui": {},
        "windows": {},
        "commands": [],
    }
    monkeypatch.setattr(store, "_STATE", fresh)
    # Re-bind in every other module that did `from periscope.store import _STATE`.
    # Add modules here as Stage B peels expose more consumers.
    import importlib
    for mod_name in (
        "periscope.channels",
        "periscope.pids",
        "periscope.routes.pane",
        "periscope.routes.prefs",
        "periscope.routes.state",
        "server",
    ):
        try:
            mod = importlib.import_module(mod_name)
        except ImportError:
            continue
        if hasattr(mod, "_STATE"):
            monkeypatch.setattr(mod, "_STATE", fresh)
    return fresh
