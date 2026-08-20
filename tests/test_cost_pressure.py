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


def test_summarize_tail_counts_only_records_at_or_after_cutoff():
    records = [
        cp.UsageRecord(ts=100.0, ctx_tokens=1000),
        cp.UsageRecord(ts=200.0, ctx_tokens=2000),
        cp.UsageRecord(ts=300.0, ctx_tokens=3000),
    ]
    got = cp.summarize_tail(records, cutoff=200.0)
    assert got.calls == 2
    assert got.cur_ctx == 3000
    assert got.last_ts == 300.0


def test_summarize_tail_keeps_cur_ctx_when_everything_predates_the_cutoff():
    """The parked case. A pane idle longer than the window has no records in it,
    but its debt is real and must still be reported."""
    records = [cp.UsageRecord(ts=100.0, ctx_tokens=600_000)]
    got = cp.summarize_tail(records, cutoff=99_999.0)
    assert got.calls == 0
    assert got.cur_ctx == 600_000
    assert got.last_ts == 100.0


def test_summarize_tail_on_no_records():
    got = cp.summarize_tail([], cutoff=0.0)
    assert got == cp.TailSummary(cur_ctx=None, last_ts=None, calls=0)


def test_summarize_tail_takes_the_last_record_not_the_largest():
    records = [
        cp.UsageRecord(ts=100.0, ctx_tokens=900_000),
        cp.UsageRecord(ts=200.0, ctx_tokens=50_000),
    ]
    got = cp.summarize_tail(records, cutoff=0.0)
    assert got.cur_ctx == 50_000
