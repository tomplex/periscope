"""Constants and paths: STATIC + MCP_SOCKET_PATH.

These tests are written against `server` for Peel 1's first half;
Task 1.5 re-points them at `periscope.config` after the move.
"""

from periscope.config import STATIC, MCP_SOCKET_PATH


def test_STATIC_points_to_repo_static_dir():
    assert STATIC.name == "static"
    assert STATIC.is_absolute()
    assert STATIC.is_dir(), f"{STATIC} should exist"
    assert (STATIC / "index.html").is_file()


def test_MCP_SOCKET_PATH_is_unix_socket_path():
    assert MCP_SOCKET_PATH == "/tmp/periscope-mcp.sock"


def test_PORT_defaults_to_8765(monkeypatch):
    monkeypatch.delenv("PERISCOPE_PORT", raising=False)
    import importlib
    import periscope.config
    importlib.reload(periscope.config)
    assert periscope.config.PORT == 8765


def test_PORT_reads_PERISCOPE_PORT_env(monkeypatch):
    monkeypatch.setenv("PERISCOPE_PORT", "8766")
    import importlib
    import periscope.config
    importlib.reload(periscope.config)
    assert periscope.config.PORT == 8766


def test_is_prod_only_true_on_prod_port_and_not_dev(monkeypatch):
    """is_prod() gates all Claude-spending / singleton-owning work. It must be
    true ONLY for the real launchd prod instance: port 8765 AND not a dev
    process. The dev-on-8765 case (dev.sh's old default) must read False so a
    developer's instance never spends Haiku."""
    import periscope.config as cfg
    cases = {
        (8765, False): True,   # prod
        (8765, True): False,   # dev that landed on the prod port — the bug
        (8766, False): False,  # stray non-dev on a dev port
        (8766, True): False,   # normal dev worktree / dev.sh
    }
    for (port, dev), expected in cases.items():
        monkeypatch.setattr(cfg, "PORT", port)
        monkeypatch.setattr(cfg, "DEV", dev)
        assert cfg.is_prod() is expected, f"PORT={port} DEV={dev}"
