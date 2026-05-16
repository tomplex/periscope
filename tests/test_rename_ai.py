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
