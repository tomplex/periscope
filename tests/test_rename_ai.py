"""Auto-rename via the Anthropic SDK."""

from unittest.mock import MagicMock

from periscope.rename_ai import build_rename_prompt, claude_complete


def test_build_rename_prompt_includes_window_indices_and_names():
    windows = [
        {"index": 0, "current_name": "claude", "branch": "main"},
        {"index": 1, "current_name": "shell"},
    ]
    prompt = build_rename_prompt(windows)
    # The prompt must surface every window's index + current_name so the LLM
    # has a useful signal to either keep or rewrite.
    assert "index 0" in prompt
    assert "index 1" in prompt
    assert "claude" in prompt
    assert "shell" in prompt
    # And the branch context for the first window.
    assert "main" in prompt
    # The output-format instruction (JSON mapping) must always be present.
    assert "JSON" in prompt


def test_build_rename_prompt_omits_pr_when_missing():
    """Windows without a PR number shouldn't emit a `PR #` line."""
    prompt = build_rename_prompt(
        [{"index": 0, "current_name": "claude", "branch": "main"}]
    )
    assert "PR #" not in prompt


def test_claude_complete_returns_concatenated_text_blocks(mocker):
    """claude_complete invokes get_anthropic().messages.create and returns the
    text concatenated across content blocks."""
    block_a = MagicMock(type="text", text="hello ")
    block_b = MagicMock(type="text", text="world")
    fake_msg = MagicMock(content=[block_a, block_b])
    fake_client = MagicMock()
    fake_client.messages.create.return_value = fake_msg
    mocker.patch("periscope.rename_ai.get_anthropic", return_value=fake_client)
    out = claude_complete("a prompt")
    assert out == "hello world"
    fake_client.messages.create.assert_called_once()
    call_kwargs = fake_client.messages.create.call_args.kwargs
    assert call_kwargs["messages"] == [{"role": "user", "content": "a prompt"}]


def test_claude_complete_skips_non_text_blocks(mocker):
    """Tool-use / image blocks must not appear in the joined string."""
    text_block = MagicMock(type="text", text="answer")
    other_block = MagicMock(type="tool_use")
    # MagicMock auto-creates .text, which would land in the join unless we
    # filter on `type`. Confirm the filter actually trips.
    other_block.text = "should-not-appear"
    fake_msg = MagicMock(content=[other_block, text_block])
    fake_client = MagicMock()
    fake_client.messages.create.return_value = fake_msg
    mocker.patch("periscope.rename_ai.get_anthropic", return_value=fake_client)
    out = claude_complete("p")
    assert out == "answer"


# ---- transcript_summary ----

from periscope.rename_ai import transcript_summary


def test_transcript_summary_extracts_recent_signals(monkeypatch):
    fake = {
        "messages": [
            {"role": "user", "text": "old prompt"},
            {"role": "user", "text": "implement liveness check"},
            {"role": "assistant", "text": "ok", "tool_uses": [
                {"name": "Read", "input": {"file_path": "/repo/anthology/liveness.py"}},
                {"name": "Edit", "input": {"file_path": "/repo/anthology/liveness.py"}},
            ]},
            {"role": "user", "text": "now wire it into the pipeline"},
            {"role": "assistant", "text": "", "tool_uses": [
                {"name": "Bash", "input": {"command": "uv run pytest -k liveness"}},
            ]},
        ],
    }
    monkeypatch.setattr("periscope.turns.get_turns_for_pane", lambda s, i: fake)
    out = transcript_summary("sess", 1)
    # Last 3 user prompts in chronological order.
    assert out["recent_user_prompts"][-1] == "now wire it into the pipeline"
    assert out["recent_user_prompts"][0] == "old prompt"
    # Tool calls flattened in chronological order.
    assert "Read /repo/anthology/liveness.py" in out["recent_tool_calls"]
    assert "Edit /repo/anthology/liveness.py" in out["recent_tool_calls"]
    assert any("Bash uv run pytest" in tc for tc in out["recent_tool_calls"])
    # Files deduped, only file-path tools.
    assert out["files_touched"] == ["/repo/anthology/liveness.py"]


def test_transcript_summary_no_jsonl_returns_empty(monkeypatch):
    monkeypatch.setattr("periscope.turns.get_turns_for_pane", lambda s, i: None)
    assert transcript_summary("sess", 1) == {}


def test_transcript_summary_swallows_errors(monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("malformed jsonl")
    monkeypatch.setattr("periscope.turns.get_turns_for_pane", boom)
    # Rename request must NOT fail because of a bad transcript.
    assert transcript_summary("sess", 1) == {}


def test_build_rename_prompt_uses_transcript_signals():
    windows = [{
        "index": 0,
        "current_name": "claude",
        "branch": "tc/foo",
        "recent_user_prompts": ["implement liveness check"],
        "recent_tool_calls": ["Edit anthology/liveness.py", "Bash pytest -k liveness"],
        "files_touched": ["anthology/liveness.py"],
    }]
    prompt = build_rename_prompt(windows)
    assert "implement liveness check" in prompt
    assert "Edit anthology/liveness.py" in prompt
    assert "anthology/liveness.py" in prompt
    # Signal priority hint that steers the model.
    assert "recent_user_prompts" in prompt
