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
