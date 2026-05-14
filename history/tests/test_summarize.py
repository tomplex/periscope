from unittest.mock import MagicMock

from history.summarize import (
    SUMMARIZE_TOOL, SUMMARIZE_SYSTEM_PROMPT,
    build_summary_prompt, call_summarizer, SummaryResult,
)
from history.extract import SessionRecord


def _sample_record() -> SessionRecord:
    return SessionRecord(
        session_id="s1", jsonl_path="/p.jsonl", project_path="/Users/tom/dev/foo",
        branch="feat/cohort", started_at=0, ended_at=120, duration_s=120,
        user_msg_count=2, asst_msg_count=3, tool_use_count=2,
        was_interrupted=0, ended_cleanly=1,
        first_user_msg="investigate slow query",
        last_user_msg="verify with explain",
        final_assistant_msg="P99 down to 200ms after adding index",
        files_touched='["resolve_cohort.py", "migrations/0042.sql"]',
        notable_cmds='["psql -c EXPLAIN", "pytest -k slow"]',
        tool_use_counts='{"Read": 1, "Bash": 1, "Write": 1}',
        user_messages_blob="investigate slow query\nverify with explain",
        assistant_text_blob="reading the file\nP99 down to 200ms after adding index",
    )


def test_build_prompt_includes_key_fields():
    rec = _sample_record()
    prompt = build_summary_prompt(rec)
    assert "/Users/tom/dev/foo" in prompt
    assert "feat/cohort" in prompt
    assert "resolve_cohort.py" in prompt
    assert "investigate slow query" in prompt
    assert "P99 down to 200ms" in prompt  # final_assistant_msg
    # Assistant prose mid-conversation NOT included
    assert "reading the file" not in prompt


def test_call_summarizer_uses_forced_tool_use():
    mock_client = MagicMock()
    mock_block = MagicMock()
    mock_block.type = "tool_use"
    mock_block.name = SUMMARIZE_TOOL["name"]
    mock_block.input = {
        "summary": "Fixed slow cohort query by adding events.user_id index.",
        "tags": ["performance", "postgres", "cohorts"],
    }
    mock_msg = MagicMock()
    mock_msg.content = [mock_block]
    mock_client.messages.create.return_value = mock_msg

    result = call_summarizer(mock_client, _sample_record(), model="claude-haiku-4-5")
    assert isinstance(result, SummaryResult)
    assert result.summary.startswith("Fixed slow cohort query")
    assert result.tags == ["performance", "postgres", "cohorts"]
    assert result.model == "claude-haiku-4-5"

    # Verify the call shape
    call_kwargs = mock_client.messages.create.call_args.kwargs
    assert call_kwargs["tool_choice"] == {"type": "tool", "name": "save_session_summary"}
    assert call_kwargs["tools"] == [SUMMARIZE_TOOL]
    # Prompt caching set on the system block
    assert call_kwargs["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert call_kwargs["system"][0]["text"] == SUMMARIZE_SYSTEM_PROMPT
    assert call_kwargs["model"] == "claude-haiku-4-5"


def test_call_summarizer_retries_on_missing_tool_use():
    mock_client = MagicMock()
    # First call: no tool_use block (model misbehaved)
    bad_block = MagicMock(); bad_block.type = "text"; bad_block.text = "I refuse"
    bad_msg = MagicMock(); bad_msg.content = [bad_block]
    # Second call: succeeds
    ok_block = MagicMock(); ok_block.type = "tool_use"; ok_block.name = SUMMARIZE_TOOL["name"]
    ok_block.input = {"summary": "ok", "tags": ["a", "b", "c"]}
    ok_msg = MagicMock(); ok_msg.content = [ok_block]
    mock_client.messages.create.side_effect = [bad_msg, ok_msg]

    result = call_summarizer(mock_client, _sample_record())
    assert result.summary == "ok"
    assert mock_client.messages.create.call_count == 2


def test_call_summarizer_returns_none_after_exhausting_retries():
    mock_client = MagicMock()
    bad_block = MagicMock(); bad_block.type = "text"; bad_block.text = "no"
    bad_msg = MagicMock(); bad_msg.content = [bad_block]
    mock_client.messages.create.return_value = bad_msg

    result = call_summarizer(mock_client, _sample_record())
    assert result is None
    # Default max_retries=3 → 4 total attempts (initial + 3 retries)
    assert mock_client.messages.create.call_count == 4
