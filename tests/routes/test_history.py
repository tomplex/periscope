"""Tests for /api/history/* and /history page."""

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    from server import app
    return TestClient(app)


def test_history_search_with_query(client, mocker):
    mocker.patch(
        "history.search",
        return_value=[{"session_id": "abc", "title": "x"}],
    )
    r = client.get("/api/history/search?q=hello")
    assert r.status_code == 200
    body = r.json()
    assert body["query"] == "hello"
    assert body["results"][0]["session_id"] == "abc"
    # is_resuming was annotated from the in-process _resuming dict.
    assert body["results"][0]["is_resuming"] is False


def test_history_search_empty_query_uses_recent(client, mocker):
    recent = mocker.patch("history.recent", return_value=[])
    r = client.get("/api/history/search?q=")
    assert r.status_code == 200
    assert r.json()["results"] == []
    assert recent.called


def test_history_session_found(client, mocker):
    mocker.patch(
        "history.search.get_session",
        return_value={"session_id": "abc", "messages": []},
    )
    r = client.get("/api/history/session/abc")
    assert r.status_code == 200
    body = r.json()
    assert body["session_id"] == "abc"
    assert body["is_resuming"] is False


def test_history_session_missing_returns_404(client, mocker):
    mocker.patch("history.search.get_session", return_value=None)
    r = client.get("/api/history/session/nope")
    assert r.status_code == 404
    assert r.json()["ok"] is False


def test_history_stats(client, mocker):
    mocker.patch("history.stats", return_value={"sessions": 42})
    r = client.get("/api/history/stats")
    assert r.status_code == 200
    assert r.json() == {"sessions": 42}


def test_history_page_serves_html(client):
    r = client.get("/history")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
