"""Tests for /api/channel/{clear-unread,push}."""

from unittest.mock import AsyncMock, patch


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


def test_push_forwards_to_emit_channel_event(client):
    with patch(
        "periscope.routes.channel.emit_channel_event",
        new=AsyncMock(return_value=True),
    ) as emit:
        r = client.post(
            "/api/channel/push?pane=%2542",
            json={"content": "hello claude"},
        )
        assert r.status_code == 200
        assert r.json() == {"ok": True}
        emit.assert_awaited_once_with("%42", "hello claude")


def test_push_returns_ok_false_when_no_session(client):
    with patch(
        "periscope.routes.channel.emit_channel_event",
        new=AsyncMock(return_value=False),
    ):
        r = client.post(
            "/api/channel/push?pane=%2542",
            json={"content": "hi"},
        )
        assert r.status_code == 200
        assert r.json() == {"ok": False}


def test_push_rejects_non_pane_id(client):
    r = client.post(
        "/api/channel/push?pane=not-a-pane",
        json={"content": "hi"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert "pane" in body["error"]


def test_push_rejects_empty_content(client):
    r = client.post(
        "/api/channel/push?pane=%2542",
        json={"content": "   "},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert "content" in body["error"]
