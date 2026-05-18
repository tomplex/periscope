"""Shared pytest fixtures for periscope's test suite.

These fixtures sandbox side-effecting helpers so tests don't write to
~/.config/periscope or bind real unix sockets.
"""

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
