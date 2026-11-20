"""Tests for /api/history/* and /history page."""

import pytest


@pytest.fixture
def no_transcripts(mocker):
    """Claude keeps no transcript for any session, and no pane ran one."""
    mocker.patch("periscope.routes.history.jsonl_for_session", return_value=None)
    mocker.patch("periscope.store.all_windows", return_value={})


def test_history_search_with_query(client, mocker, no_transcripts):
    mocker.patch(
        "history.search",
        return_value=[{"session_id": "abc", "title": "x"}],
    )
    r = client.get("/api/history/search?q=hello")
    assert r.status_code == 200
    body = r.json()
    assert body["query"] == "hello"
    assert body["results"][0]["session_id"] == "abc"
    # The same answer the resume route gives, so the page offers no dead resume.
    assert body["results"][0]["resume_refusal"] == {"status": 404, "detail": "Claude has no transcript for session abc"}


def test_history_search_rows_resume_when_claude_has_a_quiet_transcript(client, mocker, tmp_path):
    import os
    import time

    jsonl = tmp_path / "abc.jsonl"
    jsonl.write_text("{}\n")
    os.utime(jsonl, (time.time() - 600, time.time() - 600))
    mocker.patch("periscope.routes.history.jsonl_for_session", return_value=jsonl)
    mocker.patch("periscope.store.all_windows", return_value={})
    mocker.patch("history.recent", return_value=[{"session_id": "abc", "branch": "main"}])
    r = client.get("/api/history/search?q=")
    assert r.json()["results"][0]["resume_refusal"] is None


def test_history_search_rows_carry_the_pane_that_last_ran_the_session(client, mocker, no_transcripts):
    """Name, PR and ticket come from the state.json entry whose last sighting
    carried the session id; of two entries, the newer sighting wins."""
    mocker.patch(
        "periscope.store.all_windows",
        return_value={
            "p-old": {"last_seen": {"sid": "abc", "ts": 100, "name": "old-name"}, "linked_pr": 7},
            "p-new": {
                "last_seen": {"sid": "abc", "ts": 200, "name": "history-p1-fixes"},
                "linked_pr": 66,
                "linked_linear": "THI-470",
            },
            "p-other": {"last_seen": {"sid": "zzz", "ts": 300, "name": "other"}},
        },
    )
    mocker.patch(
        "history.recent",
        return_value=[{"session_id": "abc", "branch": "main"}, {"session_id": "def", "branch": "thi-402-rank"}],
    )
    mocker.patch("periscope.store.linear_team_keys", return_value=frozenset({"THI"}))
    rows = client.get("/api/history/search?q=").json()["results"]
    assert rows[0]["pane_name"] == "history-p1-fixes"
    assert rows[0]["linked_pr"] == 66
    assert rows[0]["linked_linear"] == "THI-470"
    # No pane ran it: the ticket comes from the branch, as on the dashboard.
    assert rows[1]["pane_name"] is None
    assert rows[1]["linked_pr"] is None
    assert rows[1]["linear_ticket"] == "THI-402"


@pytest.mark.parametrize("name", ["claude.exe", "claude", "2.1.220"])
def test_history_search_drops_a_placeholder_pane_name(client, mocker, no_transcripts, name):
    """tmux's automatic name says nothing about the session; the page titles the row with the summary."""
    mocker.patch(
        "periscope.store.all_windows", return_value={"p": {"last_seen": {"sid": "abc", "ts": 1, "name": name}}}
    )
    mocker.patch("history.recent", return_value=[{"session_id": "abc", "branch": "main"}])
    assert client.get("/api/history/search?q=").json()["results"][0]["pane_name"] is None


def test_history_search_says_rerank_was_used_only_when_a_row_was_scored(client, mocker, no_transcripts):
    """A failed Haiku call (no key, 401) keeps the FTS order; the page must not
    head that order "Reranked by relevance"."""
    search = mocker.patch("history.search", return_value=[{"session_id": "a"}, {"session_id": "b"}])
    assert client.get("/api/history/search?q=x&rerank=true").json()["rerank_used"] is False
    search.return_value = [{"session_id": "a", "rerank_score": 0.9}, {"session_id": "b", "rerank_score": 0.1}]
    assert client.get("/api/history/search?q=x&rerank=true").json()["rerank_used"] is True


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("q=x&limit=50", 50),
        # Haiku reranks 20 candidates, so 20 rows is a full page.
        ("q=x&limit=50&rerank=true", 20),
        # An empty query lists recent sessions, which nothing reranks.
        ("q=&limit=50&rerank=true", 50),
        ("q=x&limit=500", 200),
    ],
)
def test_history_search_answers_with_the_limit_that_applied(client, mocker, no_transcripts, query, expected):
    """The page writes "first N" only when the list is full at this limit."""
    mocker.patch("history.search", return_value=[])
    mocker.patch("history.recent", return_value=[])
    assert client.get(f"/api/history/search?{query}").json()["limit"] == expected


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
    assert r.json()["session_id"] == "abc"


def test_history_session_missing_returns_404(client, mocker):
    mocker.patch("history.search.get_session", return_value=None)
    r = client.get("/api/history/session/nope")
    assert r.status_code == 404
    assert "unknown session_id" in r.json()["detail"]


def test_history_stats(client, mocker):
    mocker.patch("history.stats", return_value={"sessions": 42})
    r = client.get("/api/history/stats")
    assert r.status_code == 200
    assert r.json() == {"sessions": 42}


def test_history_page_serves_html(client):
    r = client.get("/history")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
