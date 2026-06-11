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


def test_get_session_returns_full_row_and_messages(tmp_path, fixture_dir, monkeypatch):
    from history.search import get_session
    from history.indexer import index_one
    archive_dir = tmp_path / "archive"
    monkeypatch.setattr("history.indexer.ARCHIVE_DIR", archive_dir)
    db_path = tmp_path / "h.db"
    client = MagicMock()
    blk = MagicMock(); blk.type = "tool_use"; blk.name = "save_session_summary"
    blk.input = {"summary": "fake", "tags": ["a", "b", "c"]}
    m = MagicMock(); m.content = [blk]
    client.messages.create.return_value = m
    with patch("history.indexer.get_anthropic_client", return_value=client):
        index_one(str(fixture_dir / "normal_session.jsonl"), db_path=db_path)
    sess = get_session("normal-001", db_path=db_path)
    assert sess is not None
    assert sess["session_id"] == "normal-001"
    assert sess["project_path"] == "/Users/tom/dev/foo"
    assert sess["branch"] == "feat/bar"
    assert sess["summary"] == "fake"
    assert sess["tags"] == ["a", "b", "c"]
    # Full record fields that search() drops
    assert sess["final_assistant_msg"].startswith("index works")
    assert sess["ended_cleanly"] is True
    assert sess["was_interrupted"] is False
    assert "Read" in sess["tool_use_counts"]
    # Messages are parsed from the JSONL
    roles = [m["role"] for m in sess["messages"]]
    assert "user" in roles and "assistant" in roles
    assert sess["jsonl_missing"] is False


def test_get_session_returns_none_for_missing(tmp_path):
    from history.search import get_session
    db_path = tmp_path / "h.db"
    from history.db import connect
    connect(db_path).close()  # init schema
    assert get_session("does-not-exist", db_path=db_path) is None


def test_get_session_marks_jsonl_missing(tmp_path):
    from history.search import get_session
    from history.db import connect
    db_path = tmp_path / "h.db"
    conn = connect(db_path)
    # Insert a row pointing at a path that doesn't exist
    conn.execute(
        "INSERT INTO sessions(session_id, jsonl_path, project_path, started_at, ended_at, "
        "duration_s, user_msg_count, asst_msg_count, tool_use_count, indexed_at, "
        "mechanical_version, source_mtime, source_size, files_touched, notable_cmds, "
        "tool_use_counts) "
        "VALUES ('orphan', '/nope/missing.jsonl', '/p', 0, 0, 0, 0, 0, 0, 0, 1, 0, 0, '[]', '[]', '{}')"
    )
    conn.commit()
    conn.close()
    sess = get_session("orphan", db_path=db_path)
    assert sess is not None
    assert sess["jsonl_missing"] is True
    assert sess["messages"] == []


def test_messages_from_jsonl_pairs_filters_and_stamps_uuid(fixture_dir):
    from history.search import messages_from_jsonl
    msgs = messages_from_jsonl(str(fixture_dir / "turns_session.jsonl"))

    # Order preserved; isMeta(m1)/isSidechain(sc1)/tool-result-only(u2)/
    # non-compact-system(sys2) dropped.
    assert [m["uuid"] for m in msgs] == ["u1", "a1", "a2", "c1", "u3"]

    # tool_use/result pairing (tool_use.id <-> tool_result.tool_use_id)
    a1 = next(m for m in msgs if m["uuid"] == "a1")
    assert a1["role"] == "assistant"
    assert a1["text"] == "Running them now"
    assert a1["tool_uses"][0]["name"] == "Bash"
    assert a1["tool_uses"][0]["result"] == "All pass"

    # in-flight tool_use: no matching result yet -> None
    a2 = next(m for m in msgs if m["uuid"] == "a2")
    assert a2["tool_uses"][0]["result"] is None

    # compact_boundary emitted as a divider
    c1 = next(m for m in msgs if m["uuid"] == "c1")
    assert c1["role"] == "system" and c1["kind"] == "compact"

    # every emitted message carries a uuid + ts_ms (reconciliation depends on it)
    assert all(m["uuid"] and m["ts_ms"] for m in msgs)

    # deterministic: a second parse yields the same uuids in the same order
    again = [m["uuid"] for m in messages_from_jsonl(str(fixture_dir / "turns_session.jsonl"))]
    assert again == ["u1", "a1", "a2", "c1", "u3"]


# --- compact_messages (MCP shaping) ---

def _assistant(text="", tool_uses=None, ts=1000):
    return {"role": "assistant", "uuid": "a", "ts_ms": ts,
            "text": text, "tool_uses": tool_uses or []}


def test_compact_messages_keeps_text_and_summarizes_tools():
    from history.search import compact_messages
    msgs = compact_messages([
        {"role": "user", "uuid": "u", "ts_ms": 1, "text": "fix the bug"},
        _assistant("on it", [
            {"id": "t1", "name": "Bash", "input": {"command": "pytest -q"},
             "result": "3 passed"},
            {"id": "t2", "name": "Read", "input": {"file_path": "/a/b.py"},
             "result": "x" * 9000},
        ]),
    ])
    assert msgs[0] == {"role": "user", "ts_ms": 1, "text": "fix the bug"}
    a = msgs[1]
    assert a["text"] == "on it"
    assert a["tools"][0] == {"name": "Bash", "summary": "pytest -q",
                             "result": "3 passed"}
    # non-Bash results are dropped entirely, summary comes from file_path
    assert a["tools"][1] == {"name": "Read", "summary": "/a/b.py"}


def test_compact_messages_skips_housekeeping_tools_and_empty_messages():
    from history.search import compact_messages
    msgs = compact_messages([
        _assistant("", [
            {"id": "t1", "name": "Skill", "input": {"skill": "tdd"}, "result": "ok"},
            {"id": "t2", "name": "TaskUpdate", "input": {"taskId": "1"}, "result": ""},
            {"id": "t3", "name": "ToolSearch", "input": {"query": "x"}, "result": ""},
            {"id": "t4", "name": "TodoWrite", "input": {}, "result": ""},
        ]),
        _assistant("real text", [
            {"id": "t5", "name": "Skill", "input": {"skill": "tdd"}, "result": "ok"},
        ]),
    ])
    # first message had nothing left -> dropped wholesale
    assert len(msgs) == 1
    assert msgs[0]["text"] == "real text"
    assert "tools" not in msgs[0]


def test_compact_messages_truncates_long_fields():
    from history.search import compact_messages
    msgs = compact_messages([
        {"role": "user", "uuid": "u", "ts_ms": 1, "text": "y" * 5000},
        _assistant("", [
            {"id": "t1", "name": "Bash", "input": {"command": "z" * 900},
             "result": "r" * 900},
        ]),
    ])
    assert len(msgs[0]["text"]) < 2100 and msgs[0]["text"].endswith("…[truncated]")
    tool = msgs[1]["tools"][0]
    assert len(tool["summary"]) < 300 and tool["summary"].endswith("…[truncated]")
    assert len(tool["result"]) < 300 and tool["result"].endswith("…[truncated]")


def test_compact_messages_summary_fallback_and_compact_divider():
    from history.search import compact_messages
    msgs = compact_messages([
        {"role": "system", "kind": "compact", "uuid": "c", "ts_ms": 5},
        _assistant("", [
            # unknown tool: summary falls back to first string input value
            {"id": "t1", "name": "mcp__foo__bar",
             "input": {"n": 3, "query": "deploy logs"}, "result": "stuff"},
            # in-flight Bash (result None) must not emit a result key
            {"id": "t2", "name": "Bash", "input": {"command": "ls"}, "result": None},
        ]),
    ])
    assert msgs[0] == {"role": "system", "kind": "compact", "ts_ms": 5}
    tools = msgs[1]["tools"]
    assert tools[0] == {"name": "mcp__foo__bar", "summary": "deploy logs"}
    assert tools[1] == {"name": "Bash", "summary": "ls"}
