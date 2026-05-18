"""GET /api/healthz returns liveness + version metadata."""

from fastapi.testclient import TestClient

from periscope.app import app


def test_healthz_returns_ok_with_metadata():
    client = TestClient(app)
    res = client.get("/api/healthz")
    assert res.status_code == 200
    data = res.json()
    assert data["ok"] is True
    assert isinstance(data["pid"], int) and data["pid"] > 0
    assert isinstance(data["port"], int)
    assert isinstance(data["uptime_s"], (int, float)) and data["uptime_s"] >= 0
    assert isinstance(data["version"], str) and data["version"]
