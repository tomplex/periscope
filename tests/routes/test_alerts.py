"""Tests for GET /api/alerts/recent."""

from periscope.channels import _CHANNEL_ALERTS, _CHANNELS_LOCK, _do_notify_tool


def test_alerts_recent_surfaces_id(client, mocker):
    with _CHANNELS_LOCK:
        _CHANNEL_ALERTS.clear()
    mocker.patch(
        "periscope.routes.alerts.list_windows",
        return_value=[{
            "pane_id": "%5", "session": "tc/x", "index": "0",
            "name": "win", "cwd": "/tmp",
        }],
    )
    _do_notify_tool("%5", {"message": "hello", "kind": "need_human"})
    res = client.get("/api/alerts/recent?limit=10")
    assert res.status_code == 200
    items = res.json()["items"]
    assert items and all("id" in it for it in items)
