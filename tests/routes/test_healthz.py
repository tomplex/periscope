"""GET /api/healthz returns liveness + version metadata."""

import json
import sqlite3
import time

from fastapi.testclient import TestClient

from periscope import config, session_binding_db
from periscope.app import app
from periscope.routes import healthz


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
    assert data["codex_hook"]["hook_version"] == 1
    assert "trusted" not in data["codex_hook"]


def test_codex_hook_health_reports_definition_observation_and_staleness(
    tmp_path, monkeypatch
):
    home = tmp_path / "codex"
    monkeypatch.setenv("CODEX_HOME", str(home))
    path = home / "hooks.json"
    path.parent.mkdir()
    command = healthz.command_for(
        healthz.Path(healthz.__file__).resolve().parent.parent.parent
    )
    path.write_text(json.dumps({"hooks": {
        "SessionStart": [{
            "matcher": "",
            "hooks": [{"type": "command", "command": command, "timeout": 5}],
        }]
    }}))
    db = tmp_path / "periscope.db"
    monkeypatch.setattr(config, "ACTIVITY_DB", db)
    with sqlite3.connect(db) as conn:
        session_binding_db.ensure_schema(conn)
        session_binding_db.append_hook_event(
            conn,
            session_binding_db.AgentHookEvent(
                "%1", "codex", "secret", None, "SessionStart", 1, "0.146.0",
                int(time.time()) - healthz.CODEX_HOOK_STALE_S - 1,
            ),
        )
    data = healthz._codex_hook_health()
    assert data["definition"]["SessionStart"] == {
        "present": True, "target_exists": True,
    }
    assert data["definition"]["Stop"]["present"] is False
    assert data["observed"]["SessionStart"]["stale"] is True
    assert data["observed"]["SessionStart"]["cli_version"] == "0.146.0"
    assert "secret" not in json.dumps(data)
