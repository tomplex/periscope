"""Claude usage tracking: JSONL parsing + /usage screen scraping."""

import textwrap

from periscope.usage import (
    parse_usage_screen, compute_claude_usage,
    _USAGE_LABELS,
)


def test_parse_usage_screen_empty_input_returns_unavailable():
    """parse_usage_screen on '' returns {'available': False, 'meters': {}}."""
    out = parse_usage_screen("")
    assert isinstance(out, dict)
    assert out.get("available") is False
    assert out.get("meters") == {}


def test_usage_labels_dict_exists():
    """The label-to-key mapping is exposed at module level."""
    assert isinstance(_USAGE_LABELS, dict)
    # At minimum the three labels the TUI shows.
    assert "Current session" in _USAGE_LABELS
    assert "Current week (all models)" in _USAGE_LABELS
    assert "Current week (Sonnet only)" in _USAGE_LABELS


def test_parse_usage_screen_extracts_session_meter():
    """A three-line block (label, bar+pct, Resets line) yields one meter."""
    sample = textwrap.dedent("""\
        Some preamble line.

        Current session
        ████████████░░░░ 42% used
        Resets in 2h 15m

        trailing text
    """)
    out = parse_usage_screen(sample)
    assert out["available"] is True
    assert "session" in out["meters"]
    assert out["meters"]["session"]["percent"] == 42
    assert "2h 15m" in out["meters"]["session"]["resets"]


def test_parse_usage_screen_extracts_all_three_meters():
    """All three known labels parse together when all three blocks appear."""
    sample = textwrap.dedent("""\
        Current session
        ████░░░░░░ 18% used
        Resets in 1h

        Current week (all models)
        ██████░░░░ 55% used
        Resets in 3d

        Current week (Sonnet only)
        ███░░░░░░░ 27% used
        Resets in 3d
    """)
    out = parse_usage_screen(sample)
    assert out["available"] is True
    assert set(out["meters"].keys()) == {"session", "week_all", "week_sonnet"}
    assert out["meters"]["week_all"]["percent"] == 55
    assert out["meters"]["week_sonnet"]["percent"] == 27


def test_compute_claude_usage_returns_unavailable_when_dir_missing(monkeypatch, tmp_path):
    """When ~/.claude/projects/ doesn't exist, available=False."""
    monkeypatch.setattr("periscope.usage._CLAUDE_PROJECTS", tmp_path / "no-such")
    out = compute_claude_usage()
    assert out == {"available": False}


def test_compute_claude_usage_returns_zero_for_empty_dir(monkeypatch, tmp_path):
    """Empty projects dir: available=True, all counters zero, reset_at=None."""
    monkeypatch.setattr("periscope.usage._CLAUDE_PROJECTS", tmp_path)
    out = compute_claude_usage()
    assert isinstance(out, dict)
    assert out["available"] is True
    assert out["messages"] == 0
    assert out["total_tokens"] == 0
    assert out["reset_at"] is None
