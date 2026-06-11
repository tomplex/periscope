"""Tests for /api/events (UI instrumentation ingest)."""
import json

import pytest

from periscope import activity, config


@pytest.fixture(autouse=True)
def isolated_db(fresh_activity_db, monkeypatch):
    """DB isolation via the shared fixture; force prod port (dev=0) by default."""
    monkeypatch.setattr(config, "PORT", 8765)


def _count(name=None):
    c = activity._conn()
    if name:
        return c.execute("SELECT COUNT(*) FROM ui_events WHERE name=?", (name,)).fetchone()[0]
    return c.execute("SELECT COUNT(*) FROM ui_events").fetchone()[0]


def test_post_events_inserts_batch(client):
    r = client.post("/api/events", json={"events": [
        {"name": "modal.open", "detail": {"tab": "terminal"}, "t": 100},
        {"name": "app.open", "t": 101},
    ]})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "n": 2}
    assert _count() == 2


def test_post_events_empty_body_is_noop(client):
    r = client.post("/api/events", content=b"")
    assert r.status_code == 200
    assert r.json()["n"] == 0


def test_post_events_malformed_json_is_noop(client):
    r = client.post("/api/events", content=b"{not json")
    assert r.status_code == 200
    assert r.json()["n"] == 0


def test_post_events_missing_events_key_is_noop(client):
    r = client.post("/api/events", json={"nope": 1})
    assert r.status_code == 200
    assert r.json()["n"] == 0


def test_post_events_caps_batch_at_1000(client):
    big = [{"name": "x", "t": 1} for _ in range(1500)]
    r = client.post("/api/events", json={"events": big})
    assert r.json()["n"] == 1000


def test_post_events_dev_flag_from_port(client, monkeypatch):
    monkeypatch.setattr(config, "PORT", 8766)
    client.post("/api/events", json={"events": [{"name": "x", "t": 1}]})
    c = activity._conn()
    assert c.execute("SELECT dev FROM ui_events").fetchone()[0] == 1
