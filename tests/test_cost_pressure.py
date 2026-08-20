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
