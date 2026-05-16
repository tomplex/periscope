"""Tests for /api/channel/clear-unread."""

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    from server import app
    return TestClient(app)


def test_clear_unread_resets_count(client):
    from periscope.channels import _CHANNEL_UNREAD, _CHANNELS_LOCK
    with _CHANNELS_LOCK:
        _CHANNEL_UNREAD["%42"] = 5
    r = client.post("/api/channel/clear-unread?pane=%2542")
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    with _CHANNELS_LOCK:
        assert _CHANNEL_UNREAD.get("%42") == 0


def test_clear_unread_rejects_non_pane_id(client):
    r = client.post("/api/channel/clear-unread?pane=not-a-pane")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert "pane" in body["error"]
