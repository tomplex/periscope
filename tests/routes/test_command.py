"""Tests for POST /api/command — deliver a free-text command to the commander."""


def test_command_sends_to_commander(client, fresh_activity_db, monkeypatch):
    from periscope import commander, activity
    from periscope.routes import command as cmd_route

    async def fake_ensure():
        activity.set_commander(pane_id="%C", session_id=None, at=1)
        return activity.get_commander()
    monkeypatch.setattr(commander, "ensure_commander", fake_ensure)
    monkeypatch.setattr(cmd_route, "list_windows",
                        lambda: [{"pane_id": "%C", "session": "bridge", "index": 0}])
    sent = {}
    monkeypatch.setattr(cmd_route, "_send_to_target",
                        lambda target, paste, keys: sent.update(target=target, paste=paste, keys=keys) or {})
    r = client.post("/api/command", json={"text": "do a thing"})
    assert r.status_code == 200
    assert r.json() == {"session": "bridge", "index": 0}
    assert sent["paste"] == "do a thing" and sent["keys"] == ["Enter"]
    assert sent["target"] == "bridge:0"


def test_command_503_when_no_commander(client, monkeypatch):
    from periscope import commander

    async def fake_ensure():
        return None
    monkeypatch.setattr(commander, "ensure_commander", fake_ensure)
    r = client.post("/api/command", json={"text": "x"})
    assert r.status_code == 503
