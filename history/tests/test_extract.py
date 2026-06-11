import json
from history.extract import (
    extract_record,
    compute_summary_input_hash,
    is_trivial,
    heuristic_summary,
)
from history.jsonl import parse_jsonl


def _extract_fixture(fixture_dir, name):
    path = fixture_dir / name
    events = list(parse_jsonl(str(path)))
    return extract_record(str(path), events, source_mtime=1000000, source_size=path.stat().st_size)


def test_counts_and_timestamps(fixture_dir):
    rec = _extract_fixture(fixture_dir, "normal_session.jsonl")
    assert rec.session_id == "normal-001"
    assert rec.project_path == "/Users/tom/dev/foo"
    assert rec.branch == "feat/bar"
    assert rec.user_msg_count == 3   # excludes tool_result wrappers
    assert rec.asst_msg_count == 4
    assert rec.tool_use_count == 3
    assert rec.duration_s == 3 * 60 + 30  # 10:00:00 → 10:03:30
    assert rec.was_interrupted == 0
    assert rec.ended_cleanly == 1  # last event is assistant text


def test_first_last_final_messages(fixture_dir):
    rec = _extract_fixture(fixture_dir, "normal_session.jsonl")
    assert rec.first_user_msg.startswith("investigate the slow query")
    assert rec.last_user_msg.startswith("now verify with EXPLAIN")
    assert rec.final_assistant_msg.startswith("index works")


def test_files_touched_dedup_and_order(fixture_dir):
    rec = _extract_fixture(fixture_dir, "normal_session.jsonl")
    files = json.loads(rec.files_touched)
    # First-touched ordering: resolve_cohort.py, then migrations/0042.sql
    assert files == [
        "/Users/tom/dev/foo/resolve_cohort.py",
        "/Users/tom/dev/foo/migrations/0042.sql",
    ]


def test_glob_grep_paths_excluded_from_files_touched(tmp_path):
    """Glob and Grep tools use `path` to mean a search directory, not a file
    touch. Including them in _FILE_KEYS pollutes the index with directory
    entries. Regression test for that fix."""
    p = tmp_path / "glob.jsonl"
    p.write_text(
        '{"type":"user","sessionId":"s","cwd":"/p","timestamp":"2026-01-01T00:00:00Z","uuid":"u1","parentUuid":null,"message":{"role":"user","content":[{"type":"text","text":"go"}]}}\n'
        '{"type":"assistant","sessionId":"s","cwd":"/p","timestamp":"2026-01-01T00:00:01Z","uuid":"a1","parentUuid":"u1","message":{"role":"assistant","content":['
        '{"type":"tool_use","id":"t1","name":"Glob","input":{"pattern":"*.py","path":"/Users/tom/dev/foo/src"}},'
        '{"type":"tool_use","id":"t2","name":"Grep","input":{"pattern":"needle","path":"/Users/tom/dev/foo"}},'
        '{"type":"tool_use","id":"t3","name":"Read","input":{"file_path":"/Users/tom/dev/foo/real_file.py"}}'
        ']}}\n'
    )
    events = list(parse_jsonl(str(p)))
    rec = extract_record(str(p), events, source_mtime=0, source_size=p.stat().st_size)
    files = json.loads(rec.files_touched)
    assert files == ["/Users/tom/dev/foo/real_file.py"]


def test_notable_cmds_filters_trivial(tmp_path):
    # Build a session with both notable and trivial Bash commands
    p = tmp_path / "cmds.jsonl"
    p.write_text(
        '{"type":"user","sessionId":"x","cwd":"/p","timestamp":"2026-01-01T00:00:00Z","uuid":"u1","parentUuid":null,"message":{"role":"user","content":[{"type":"text","text":"go"}]}}\n'
        '{"type":"assistant","sessionId":"x","cwd":"/p","timestamp":"2026-01-01T00:00:01Z","uuid":"a1","parentUuid":"u1","message":{"role":"assistant","content":['
        '{"type":"tool_use","id":"t1","name":"Bash","input":{"command":"ls"}},'
        '{"type":"tool_use","id":"t2","name":"Bash","input":{"command":"pwd"}},'
        '{"type":"tool_use","id":"t3","name":"Bash","input":{"command":"pytest tests/foo_test.py -k slow"}},'
        '{"type":"tool_use","id":"t4","name":"Bash","input":{"command":"grep -r needle ./src"}}'
        ']}}\n'
    )
    events = list(parse_jsonl(str(p)))
    rec = extract_record(str(p), events, source_mtime=0, source_size=p.stat().st_size)
    cmds = json.loads(rec.notable_cmds)
    assert "ls" not in cmds
    assert "pwd" not in cmds
    assert "pytest tests/foo_test.py -k slow" in cmds
    assert "grep -r needle ./src" in cmds


def test_tool_use_counts(fixture_dir):
    rec = _extract_fixture(fixture_dir, "normal_session.jsonl")
    counts = json.loads(rec.tool_use_counts)
    assert counts == {"Read": 1, "Bash": 1, "Write": 1}


def test_interrupted_detection(fixture_dir):
    rec = _extract_fixture(fixture_dir, "interrupted_session.jsonl")
    assert rec.was_interrupted == 1
    assert rec.ended_cleanly == 0  # last event is a user "Request interrupted" message


def test_short_session(fixture_dir):
    rec = _extract_fixture(fixture_dir, "short_session.jsonl")
    assert rec.user_msg_count == 1   # only one real user message; the tool_result is excluded
    assert rec.asst_msg_count == 2
    assert rec.tool_use_count == 1


def test_summary_input_hash_stable_for_same_inputs(fixture_dir):
    rec = _extract_fixture(fixture_dir, "normal_session.jsonl")
    h1 = compute_summary_input_hash(rec)
    h2 = compute_summary_input_hash(rec)
    assert h1 == h2
    assert isinstance(h1, str) and len(h1) == 64  # sha256 hex


def test_summary_input_hash_changes_with_user_msgs(fixture_dir):
    rec = _extract_fixture(fixture_dir, "normal_session.jsonl")
    h1 = compute_summary_input_hash(rec)
    rec.user_messages_blob = rec.user_messages_blob + "\nextra user message"
    h2 = compute_summary_input_hash(rec)
    assert h1 != h2


def test_summary_input_hash_ignores_counts(fixture_dir):
    rec = _extract_fixture(fixture_dir, "normal_session.jsonl")
    h1 = compute_summary_input_hash(rec)
    rec.tool_use_count = rec.tool_use_count + 99
    rec.duration_s = rec.duration_s + 9999
    h2 = compute_summary_input_hash(rec)
    assert h1 == h2  # only summary-input-relevant fields contribute


def test_trivial_session_short_duration(fixture_dir):
    rec = _extract_fixture(fixture_dir, "short_session.jsonl")
    # short_session.jsonl: 1 user msg, ~9s duration → trivial
    assert is_trivial(rec) is True


def test_trivial_session_low_msg_count(fixture_dir):
    rec = _extract_fixture(fixture_dir, "normal_session.jsonl")
    # normal_session: 3 user msgs, 3.5 min duration → not trivial
    assert is_trivial(rec) is False


def test_heuristic_summary_mentions_msg_count(fixture_dir):
    rec = _extract_fixture(fixture_dir, "short_session.jsonl")
    text = heuristic_summary(rec)
    assert "1 messages" in text or "1 message" in text
    # Should include first user message snippet
    assert "hi, run ls" in text


def test_single_prompt_agentic_session_is_not_trivial(fixture_dir):
    """One user message + substantial tool use = a real working session
    (single-prompt agentic), not a false-start."""
    from dataclasses import replace
    rec = _extract_fixture(fixture_dir, "short_session.jsonl")
    agentic = replace(rec, duration_s=7042, user_msg_count=1, tool_use_count=80)
    assert is_trivial(agentic) is False


def test_single_prompt_without_tool_work_stays_trivial(fixture_dir):
    from dataclasses import replace
    rec = _extract_fixture(fixture_dir, "short_session.jsonl")
    walked_away = replace(rec, duration_s=600, user_msg_count=1, tool_use_count=0)
    assert is_trivial(walked_away) is True


def test_quick_abort_is_trivial_regardless_of_tool_use(fixture_dir):
    from dataclasses import replace
    rec = _extract_fixture(fixture_dir, "short_session.jsonl")
    abort = replace(rec, duration_s=30, user_msg_count=1, tool_use_count=20)
    assert is_trivial(abort) is True


def _user_line(uuid, text, ts="2026-01-01T00:00:00Z"):
    return json.dumps({
        "type": "user", "sessionId": "s", "cwd": "/p", "timestamp": ts,
        "uuid": uuid, "parentUuid": None,
        "message": {"role": "user", "content": text},
    }) + "\n"


def test_harness_boilerplate_excluded_from_user_fields(tmp_path):
    """Slash-command echoes, task notifications, and hook reminders are
    harness-generated user events, not human input. Live data showed most
    sessions' first_user_msg was a <local-command-caveat> block."""
    p = tmp_path / "boiler.jsonl"
    p.write_text(
        _user_line("u1", "<local-command-caveat>Caveat: blah</local-command-caveat>\n<command-name>/clear</command-name>")
        + _user_line("u2", "  <task-notification>\n<task-id>x</task-id>\n</task-notification>")
        + _user_line("u3", "fix the actual bug", ts="2026-01-01T00:01:00Z")
        + _user_line("u4", "<system-reminder>hook output</system-reminder>", ts="2026-01-01T00:02:00Z")
        + _user_line("u5", "thanks, ship it", ts="2026-01-01T00:03:00Z")
    )
    events = list(parse_jsonl(str(p)))
    rec = extract_record(str(p), events, source_mtime=1, source_size=1)

    assert rec.user_msg_count == 2
    assert rec.first_user_msg == "fix the actual bug"
    assert rec.last_user_msg == "thanks, ship it"
    assert "Caveat" not in rec.user_messages_blob
    assert "task-notification" not in rec.user_messages_blob
    assert rec.user_messages_blob == "fix the actual bug\nthanks, ship it"


def test_interrupt_detection_survives_boilerplate_filter(tmp_path):
    p = tmp_path / "interrupt.jsonl"
    p.write_text(
        _user_line("u1", "do the thing")
        + _user_line("u2", "[Request interrupted by user]", ts="2026-01-01T00:01:00Z")
    )
    events = list(parse_jsonl(str(p)))
    rec = extract_record(str(p), events, source_mtime=1, source_size=1)
    assert rec.was_interrupted == 1
