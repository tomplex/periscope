"""Tests for /api/command — dispatch a free-text command as a `claude --bg`
commander job, list jobs, and fetch a job's transcript. The route is thin —
monkeypatch bg_commander."""


def test_post_command_dispatches(client, monkeypatch):
    from periscope import bg_commander
    monkeypatch.setattr(bg_commander, "dispatch", lambda text, **kw: "job-xyz")
    r = client.post("/api/command", json={"text": "do it"})
    assert r.status_code == 200
    assert r.json() == {"job_id": "job-xyz"}


def test_post_command_rejects_empty(client):
    r = client.post("/api/command", json={"text": "   "})
    assert r.status_code == 400


def test_get_jobs_syncs_then_lists(client, monkeypatch):
    from periscope import bg_commander
    calls = {"synced": False}
    monkeypatch.setattr(bg_commander, "sync_jobs", lambda **kw: calls.__setitem__("synced", True))
    monkeypatch.setattr(bg_commander, "list_jobs",
        lambda: [bg_commander.Job(id="j1", text="t", cwd="/tmp", status="done", started_at=5)])
    r = client.get("/api/command/jobs")
    assert r.status_code == 200
    assert calls["synced"] is True
    assert r.json() == [{"id": "j1", "text": "t", "status": "done", "started_at": 5}]


def test_get_job_turns_404_on_unknown(client, monkeypatch):
    from periscope import bg_commander
    monkeypatch.setattr(bg_commander, "get_job", lambda jid: None)
    r = client.get("/api/command/jobs/nope/turns")
    assert r.status_code == 404
