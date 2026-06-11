"""Claude usage tracking: JSONL parsing + OAuth plan-usage endpoint."""

from periscope.usage import parse_plan_usage, compute_claude_usage


def test_parse_plan_usage_empty_input_returns_unavailable():
    """parse_plan_usage on {} returns {'available': False, 'meters': {}}."""
    out = parse_plan_usage({})
    assert out == {"available": False, "meters": {}}


def test_parse_plan_usage_maps_meters_and_skips_nulls():
    """Real response shape: present meters map to dashboard keys with epoch
    reset timestamps; null meters (no Opus meter on this plan) are skipped."""
    sample = {
        "five_hour": {"utilization": 52.0, "resets_at": "2026-06-11T18:39:59.916459+00:00"},
        "seven_day": {"utilization": 43.0, "resets_at": "2026-06-14T13:59:59.916478+00:00"},
        "seven_day_opus": None,
        "seven_day_sonnet": {"utilization": 0.0, "resets_at": "2026-06-14T14:00:00.916486+00:00"},
        "extra_usage": {"is_enabled": False},
    }
    out = parse_plan_usage(sample)
    assert out["available"] is True
    assert set(out["meters"].keys()) == {"session", "week_all", "week_sonnet"}
    sess = out["meters"]["session"]
    assert sess["label"] == "Current session"
    assert sess["percent"] == 52
    assert sess["resets_at"] == 1781203199  # 2026-06-11T18:39:59Z
    # 0% utilization is a real meter, not a missing one.
    assert out["meters"]["week_sonnet"]["percent"] == 0


def test_parse_plan_usage_includes_opus_meter_when_present():
    sample = {
        "five_hour": {"utilization": 10.0, "resets_at": "2026-06-11T18:00:00+00:00"},
        "seven_day_opus": {"utilization": 88.4, "resets_at": "2026-06-14T14:00:00+00:00"},
    }
    out = parse_plan_usage(sample)
    assert out["meters"]["week_opus"]["percent"] == 88
    assert out["meters"]["week_opus"]["label"] == "Current week (Opus only)"


def test_parse_plan_usage_tolerates_bad_resets_at():
    """A malformed resets_at yields resets_at=None, not a parse failure."""
    sample = {"five_hour": {"utilization": 5.0, "resets_at": "soon"}}
    out = parse_plan_usage(sample)
    assert out["available"] is True
    assert out["meters"]["session"]["percent"] == 5
    assert out["meters"]["session"]["resets_at"] is None


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
