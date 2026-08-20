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


def _sample(**over):
    kw = {"cur_ctx": 300_000, "base_ctx": 50_000, "pace": 2.0, "last_ts": 1000.0}
    kw.update(over)
    return cp.CostSample(**kw)


def test_score_computes_payback_calls():
    # 2.0 * 50_000 / (0.1 * 250_000) == 4.0
    got = cp.score(_sample(), now=1000.0)
    assert got is not None
    assert got.payback_calls == 4.0
    assert got.payback_mins == 2.0  # 4 calls / 2.0 calls-per-min


def test_score_clamps_base_ctx_at_the_cap():
    """The clamp lives here, not in the reader — the reader stays honest about
    what it saw, and the clamp is testable without touching a file."""
    got = cp.score(_sample(base_ctx=500_000, cur_ctx=900_000), now=1000.0)
    assert got is not None
    # base clamped to 80_000: 2.0 * 80_000 / (0.1 * 820_000)
    assert round(got.payback_calls, 4) == round(160_000 / 82_000, 4)


def test_score_returns_none_when_there_is_nothing_to_say():
    assert cp.score(_sample(base_ctx=0), now=1000.0) is None
    assert cp.score(_sample(cur_ctx=50_000, base_ctx=50_000), now=1000.0) is None
    assert cp.score(_sample(cur_ctx=10_000, base_ctx=50_000), now=1000.0) is None


def test_score_bands_plain_above_the_warn_threshold():
    # base 50k, cur 100k -> debt 50k -> 2.0*50_000/(0.1*50_000) == 20 calls
    got = cp.score(_sample(cur_ctx=100_000), now=1000.0)
    assert got is not None
    assert got.band == "none"


def test_score_bands_warn_at_the_threshold():
    got = cp.score(_sample(), now=1000.0)  # exactly 4.0 calls
    assert got is not None
    assert got.band in ("warn", "hot")
    assert got.payback_calls == 4.0


def test_score_bands_hot_only_when_active_and_fast():
    fast = cp.score(_sample(pace=8.0), now=1000.0)  # 4 calls / 8 per min = 0.5 min
    assert fast is not None
    assert fast.band == "hot"
    assert fast.active is True


def test_score_parked_pane_is_warn_never_hot():
    """Idle past _ACTIVE_WITHIN_S: the remedy is a handoff, not /clear."""
    got = cp.score(_sample(pace=8.0), now=1000.0 + 301)
    assert got is not None
    assert got.active is False
    assert got.band == "warn"


def test_score_zero_pace_gives_no_payback_mins_and_never_hot():
    got = cp.score(_sample(pace=0.0), now=1000.0)
    assert got is not None
    assert got.payback_mins is None
    assert got.band == "warn"


def test_score_active_boundary_is_inclusive():
    assert cp.score(_sample(pace=8.0), now=1000.0 + 300).active is True
    assert cp.score(_sample(pace=8.0), now=1000.0 + 301).active is False


def test_hint_hot_names_the_payback_time_and_the_remedy():
    p = cp.Pressure(band="hot", active=True, cur_ctx=600_000,
                    payback_calls=4.0, payback_mins=2.0)
    got = cp.hint(p)
    assert "600k" in got
    assert "2 min" in got
    assert "clearing" in got.lower()


def test_hint_warn_parked_points_at_a_handoff_not_a_clear():
    p = cp.Pressure(band="warn", active=False, cur_ctx=600_000,
                    payback_calls=4.0, payback_mins=None)
    got = cp.hint(p)
    assert "handoff" in got.lower()
    assert "clearing pays" not in got.lower()


def test_hint_warn_active_names_the_payback_in_calls():
    p = cp.Pressure(band="warn", active=True, cur_ctx=300_000,
                    payback_calls=4.0, payback_mins=30.0)
    got = cp.hint(p)
    assert "300k" in got
    assert "4 calls" in got


def test_hint_plain_says_it_is_near_a_fresh_start():
    p = cp.Pressure(band="none", active=True, cur_ctx=90_000,
                    payback_calls=20.0, payback_mins=10.0)
    assert "fresh start" in cp.hint(p)


def test_fmt_tokens_rounds_to_k():
    assert cp.fmt_tokens(600_000) == "600k"
    assert cp.fmt_tokens(1_500) == "2k"
    assert cp.fmt_tokens(0) == "0k"
