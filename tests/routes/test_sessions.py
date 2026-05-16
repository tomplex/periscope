"""Tests for /api/session/* and /api/window/*."""

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    from periscope.app import app
    return TestClient(app)


def _patch(mocker, name, **kwargs):
    """Patch in either the route module (post-extract) or server (pre)."""
    for prefix in (f"periscope.routes.sessions.{name}", f"server.{name}"):
        try:
            return mocker.patch(prefix, **kwargs)
        except (AttributeError, ModuleNotFoundError):
            continue


def test_session_new_creates_session(client, mocker):
    _patch(mocker, "_tmux_mutate", return_value=(True, ""))
    r = client.post("/api/session/new", json={"name": "foo", "cwd": "/tmp"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["session"] == "foo"


def test_session_new_rejects_empty_name(client, mocker):
    _patch(mocker, "_tmux_mutate", return_value=(True, ""))
    r = client.post("/api/session/new", json={"name": "   "})
    body = r.json()
    assert body["ok"] is False
    assert "empty" in body["error"]


def test_session_delete(client, mocker):
    _patch(mocker, "_tmux_mutate", return_value=(True, ""))
    r = client.delete("/api/session?session=foo")
    body = r.json()
    assert body["ok"] is True
    assert body["session"] == "foo"


def test_window_new_simple_shell(client, mocker):
    # display-message → cwd
    _patch(mocker, "tmux", return_value="/tmp")
    _patch(mocker, "_tmux_mutate", return_value=(True, "3"))
    r = client.post("/api/window/new?session=main&mode=shell")
    body = r.json()
    assert body["ok"] is True
    assert body["index"] == 3
    assert body["target"] == "main:3"


def test_window_new_resume_unknown_session_id(client, mocker):
    # Patch history.search.get_session to return None.
    mocker.patch("history.search.get_session", return_value=None)
    r = client.post("/api/window/new?session=resumes&mode=resume&resume_id=nope")
    body = r.json()
    assert body["ok"] is False
    assert "unknown session_id" in body["error"]


def test_window_move(client, mocker):
    # display-message → window_id "@42"; list-windows → "@42 5"
    def fake_tmux(*args):
        if args and args[0] == "display-message":
            return "@42"
        if args and args[0] == "list-windows":
            return "@42 5\n"
        return ""

    _patch(mocker, "tmux", side_effect=fake_tmux)
    _patch(mocker, "_tmux_mutate", return_value=(True, ""))
    _patch(mocker, "_run", return_value=(0, ""))
    r = client.post("/api/window/move?session=src&index=0&dest=dst")
    body = r.json()
    assert body["ok"] is True
    assert body["index"] == 5
    assert body["target"] == "dst:5"


def test_window_move_rejects_same_session(client, mocker):
    r = client.post("/api/window/move?session=main&index=0&dest=main")
    body = r.json()
    assert body["ok"] is False
    assert "same as source" in body["error"]


def test_window_delete(client, mocker):
    _patch(mocker, "_tmux_mutate", return_value=(True, ""))
    r = client.delete("/api/window?session=main&index=0")
    body = r.json()
    assert body["ok"] is True
    assert body["target"] == "main:0"
