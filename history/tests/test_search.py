from unittest.mock import MagicMock, patch

import pytest

from history.db import connect
from history.search import search, _build_fts_query


def _seed_session(conn, session_id, summary, tags, project, branch="main",
                   first_user="hello", final_asst="done", started_at=1000,
                   summary_model="claude-haiku-4-5"):
    """Insert a row representing a Haiku-summarized (non-trivial) session.
    Pass summary_model=None for a row that should be filtered by the default
    `include_trivial=False`."""
    conn.execute(
        """
        INSERT INTO sessions(
          session_id, jsonl_path, project_path, branch,
          started_at, ended_at, duration_s,
          user_msg_count, asst_msg_count, tool_use_count,
          summary, tags, summary_model, first_user_msg, last_user_msg, final_assistant_msg,
          files_touched, notable_cmds, tool_use_counts,
          indexed_at, mechanical_version, source_mtime, source_size
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '[]', '[]', '{}', 0, 1, 0, 0)
        """,
        (session_id, f"/p/{session_id}.jsonl", project, branch,
         started_at, started_at + 60, 60, 3, 3, 2,
         summary, tags, summary_model, first_user, first_user, final_asst),
    )
    conn.execute(
        """
        INSERT INTO sessions_fts(
          session_id, summary, tags, first_user_msg, last_user_msg,
          final_assistant_msg, user_messages, assistant_text,
          files_touched, notable_cmds
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, '', '')
        """,
        (session_id, summary, tags, first_user, first_user, final_asst,
         first_user, final_asst),
    )
    conn.commit()


@pytest.fixture
def seeded_db(tmp_path):
    db_path = tmp_path / "h.db"
    conn = connect(db_path)
    _seed_session(conn, "s1", "Fixed slow cohort query by adding events.user_id index",
                  "performance,postgres,cohorts", "/Users/tom/dev/fdy", started_at=2000)
    _seed_session(conn, "s2", "Added OAuth login flow with Google provider",
                  "auth,oauth,frontend", "/Users/tom/dev/web", started_at=1000)
    _seed_session(conn, "s3", "Fixed timezone bug in date_parser.py",
                  "bug,timezones,dates", "/Users/tom/dev/fdy", started_at=3000)
    yield conn, db_path
    conn.close()


def test_search_returns_fts_matches(seeded_db):
    _, db_path = seeded_db
    results = search("cohort", db_path=db_path, rerank=False)
    assert len(results) == 1
    assert results[0]["session_id"] == "s1"


def test_search_filters_by_project(seeded_db):
    _, db_path = seeded_db
    results = search("fixed", db_path=db_path, project="/Users/tom/dev/fdy", rerank=False)
    sids = {r["session_id"] for r in results}
    assert sids == {"s1", "s3"}


def test_search_orders_by_relevance(seeded_db):
    _, db_path = seeded_db
    results = search("timezone", db_path=db_path, rerank=False)
    assert results[0]["session_id"] == "s3"


def test_search_filters_by_since(seeded_db):
    _, db_path = seeded_db
    results = search("fixed", db_path=db_path, since=2500, rerank=False)
    sids = {r["session_id"] for r in results}
    assert sids == {"s3"}


def test_search_excludes_trivial_by_default(tmp_path):
    db_path = tmp_path / "h.db"
    conn = connect(db_path)
    # Trivial sessions have summary_model IS NULL (heuristic-only summary).
    _seed_session(conn, "trivial-1",
                  "Short session (1 messages, 5s) — first user message: hi",
                  None, "/Users/tom/dev/foo", summary_model=None)
    conn.execute("UPDATE sessions SET user_msg_count = 1, duration_s = 5 WHERE session_id = 'trivial-1'")
    conn.commit()
    results = search("session", db_path=db_path, rerank=False)
    assert results == []
    results = search("session", db_path=db_path, rerank=False, include_trivial=True)
    assert len(results) == 1
    conn.close()


def test_build_fts_query_escapes_quotes():
    # Defensive: user-typed quotes shouldn't break FTS5
    assert _build_fts_query('"foo bar"') is not None
    assert _build_fts_query("foo's") is not None


def test_rerank_reorders_and_annotates(seeded_db):
    """Rerank path: mocked Haiku reorders results and adds rerank_reason."""
    _, db_path = seeded_db
    # Mock the Anthropic client so search.rerank picks it up via indexer.get_anthropic_client
    mock_client = MagicMock()
    blk = MagicMock(); blk.type = "tool_use"; blk.name = "rank_search_results"
    blk.input = {
        "results": [
            # Reverse the FTS order: s3 first, then s1
            {"session_id": "s3", "reason": "exact match for timezone", "score": 0.95},
            {"session_id": "s1", "reason": "weakly related", "score": 0.3},
        ],
    }
    msg = MagicMock(); msg.content = [blk]
    mock_client.messages.create.return_value = msg
    with patch("history.indexer.get_anthropic_client", return_value=mock_client):
        # Query that matches both "fixed" sessions
        results = search("fixed", db_path=db_path, rerank=True, limit=10)
    # Rerank should have reordered s3 ahead of s1
    sids = [r["session_id"] for r in results]
    assert sids[:2] == ["s3", "s1"]
    assert results[0]["rerank_reason"] == "exact match for timezone"
    # Verify call shape
    call_kwargs = mock_client.messages.create.call_args.kwargs
    assert call_kwargs["tool_choice"] == {"type": "tool", "name": "rank_search_results"}
    assert call_kwargs["system"][0]["cache_control"] == {"type": "ephemeral"}


def test_rerank_falls_back_to_fts_order_on_client_failure(seeded_db):
    _, db_path = seeded_db
    mock_client = MagicMock()
    mock_client.messages.create.side_effect = RuntimeError("API down")
    with patch("history.indexer.get_anthropic_client", return_value=mock_client):
        results = search("fixed", db_path=db_path, rerank=True, limit=10)
    # FTS order preserved; no rerank_reason annotations
    assert len(results) >= 1
    for r in results:
        assert r["rerank_reason"] is None
