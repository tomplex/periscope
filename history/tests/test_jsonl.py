from history.jsonl import parse_jsonl, Event


def test_parses_short_session(fixture_dir):
    path = fixture_dir / "short_session.jsonl"
    events = list(parse_jsonl(str(path)))
    types = [e.type for e in events]
    assert types == ["permission-mode", "user", "assistant", "user", "assistant"]


def test_event_carries_metadata(fixture_dir):
    events = list(parse_jsonl(str(fixture_dir / "short_session.jsonl")))
    first_user = next(e for e in events if e.type == "user")
    assert first_user.session_id == "abc-001"
    assert first_user.cwd == "/Users/tom/dev/foo"
    assert first_user.git_branch == "main"
    assert first_user.ts_ms is not None and first_user.ts_ms > 0


def test_user_text_extraction(fixture_dir):
    events = list(parse_jsonl(str(fixture_dir / "short_session.jsonl")))
    user_events = [e for e in events if e.type == "user"]
    # Second user event is a tool_result wrapper, not real user text
    assert user_events[0].user_text == "hi, run ls"
    assert user_events[1].user_text is None
    assert user_events[1].tool_results == [{"tool_use_id": "tu-1", "content": "total 0\ndrwx 3 tom staff 96 Apr 13 10:00 ."}]


def test_assistant_blocks_classified(fixture_dir):
    events = list(parse_jsonl(str(fixture_dir / "short_session.jsonl")))
    assistants = [e for e in events if e.type == "assistant"]
    assert assistants[0].assistant_text == "on it"
    assert assistants[0].tool_uses == [{"id": "tu-1", "name": "Bash", "input": {"command": "ls -la /tmp/foo", "description": "List foo"}}]
    assert assistants[1].assistant_text == "directory is empty"
    assert assistants[1].tool_uses == []


def test_unknown_types_pass_through(fixture_dir, tmp_path):
    p = tmp_path / "novel.jsonl"
    p.write_text('{"type":"future-feature","x":1}\n{"type":"user","sessionId":"s","message":{"role":"user","content":[{"type":"text","text":"hi"}]}}\n')
    events = list(parse_jsonl(str(p)))
    assert [e.type for e in events] == ["future-feature", "user"]


def test_corrupted_file_skips_bad_lines(fixture_dir):
    events = list(parse_jsonl(str(fixture_dir / "corrupted_session.jsonl")))
    # Good lines: permission-mode, user, last assistant ("recovered")
    types = [e.type for e in events]
    assert types == ["permission-mode", "user", "assistant"]
    assert events[-1].assistant_text == "recovered"


def test_session_id_inferred_from_filename(tmp_path):
    p = tmp_path / "abc-999.jsonl"
    p.write_text('{"type":"permission-mode","permissionMode":"default","sessionId":"abc-999"}\n')
    events = list(parse_jsonl(str(p)))
    assert events[0].session_id == "abc-999"


def test_user_text_from_string_content(tmp_path):
    """Many real Claude JSONLs use message.content as a plain string, not a
    list of blocks. Dropping these silently corrupted ~85% of user prompts in
    an early version of the parser — kept as a regression test."""
    p = tmp_path / "str.jsonl"
    p.write_text(
        '{"type":"user","sessionId":"s","cwd":"/p","timestamp":"2026-01-01T00:00:00Z","uuid":"u1","parentUuid":null,"message":{"role":"user","content":"plain string prompt"}}\n'
        '{"type":"assistant","sessionId":"s","cwd":"/p","timestamp":"2026-01-01T00:00:01Z","uuid":"a1","parentUuid":"u1","message":{"role":"assistant","content":"plain string reply"}}\n'
    )
    events = list(parse_jsonl(str(p)))
    user_ev = next(e for e in events if e.type == "user")
    asst_ev = next(e for e in events if e.type == "assistant")
    assert user_ev.user_text == "plain string prompt"
    assert asst_ev.assistant_text == "plain string reply"
