"""Pure unit tests for the cost-pressure decision core. No fixtures, no files."""

from periscope import cost_pressure as cp


def test_constants_match_the_calibrated_values():
    assert cp._BASE_CAP == 80_000
    assert cp._PAYBACK_CALLS_WARN == 4
    assert cp._PAYBACK_MINS_HOT == 2
    assert cp._ACTIVE_WITHIN_S == 300
    assert cp._CACHE_READ_MULT == 0.1
    assert cp._CACHE_WRITE_MULT == 2.0


def test_value_objects_are_frozen():
    import dataclasses
    import pytest

    rec = cp.UsageRecord(ts=1.0, ctx_tokens=100)
    with pytest.raises(dataclasses.FrozenInstanceError):
        rec.ctx_tokens = 200


def _rec(**over):
    """A well-formed assistant usage record, overridable per case."""
    rec = {
        "type": "assistant",
        "timestamp": "2026-08-19T12:00:00.000Z",
        "message": {
            "model": "claude-opus-5",
            "usage": {
                "input_tokens": 10,
                "cache_creation_input_tokens": 20,
                "cache_read_input_tokens": 300,
                "output_tokens": 40,
            },
        },
    }
    rec.update(over)
    return rec


def test_parse_sums_context_but_not_output():
    got = cp.parse_usage_record(_rec())
    assert got is not None
    assert got.ctx_tokens == 330  # 10 + 20 + 300; output is NOT context


def test_parse_reads_the_timestamp_as_epoch_seconds():
    got = cp.parse_usage_record(_rec())
    assert got is not None
    assert got.ts == 1787140800.0


def test_parse_rejects_synthetic_model():
    """Claude Code writes model=<synthetic> for interrupts, API errors and limit
    messages. Nine transcripts in the corpus END on one and nine BEGIN on one."""
    rec = _rec()
    rec["message"]["model"] = "<synthetic>"
    assert cp.parse_usage_record(rec) is None


def test_parse_rejects_all_zero_token_block():
    rec = _rec()
    rec["message"]["usage"] = {
        "input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "output_tokens": 0,
    }
    assert cp.parse_usage_record(rec) is None


def test_parse_keeps_an_output_only_record():
    """Output-only is real work, not a synthetic placeholder — the four fields
    do not all sum to zero, so it survives even though its context is zero."""
    rec = _rec()
    rec["message"]["usage"] = {
        "input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "output_tokens": 99,
    }
    got = cp.parse_usage_record(rec)
    assert got is not None
    assert got.ctx_tokens == 0


def test_parse_rejects_non_assistant_and_missing_pieces():
    assert cp.parse_usage_record(_rec(type="user")) is None
    assert cp.parse_usage_record(_rec(timestamp=None)) is None
    assert cp.parse_usage_record(_rec(timestamp="not-a-date")) is None
    assert cp.parse_usage_record(_rec(message={})) is None
    assert cp.parse_usage_record(_rec(message={"model": "claude-opus-5"})) is None
    assert cp.parse_usage_record({}) is None
