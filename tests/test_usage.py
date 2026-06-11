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


# --- attach_projections: "on track to blow the limit" heuristics ---------

from periscope.usage import attach_projections

NOW = 1_800_000_000


def _meter(utilization, resets_at, key="session"):
    return {key: {"label": "x", "percent": round(utilization),
                  "utilization": float(utilization), "resets_at": resets_at}}


def _no_samples(_meter_key, _since):
    return []


def test_projected_percent_average_pace():
    """Halfway through the 5h window at 60% -> on pace for 120%."""
    meters = _meter(60.0, NOW + int(2.5 * 3600))
    attach_projections(meters, NOW, samples_for=_no_samples)
    assert meters["session"]["projected_percent"] == 120
    assert meters["session"]["limit_at"] is None


def test_projected_percent_suppressed_early_in_window():
    """10 minutes into a 5h window (3% elapsed) the ratio explodes — None."""
    meters = _meter(2.0, NOW + 5 * 3600 - 600)
    attach_projections(meters, NOW, samples_for=_no_samples)
    assert meters["session"]["projected_percent"] is None


def test_projections_none_without_resets_at():
    meters = _meter(50.0, None)
    attach_projections(meters, NOW, samples_for=_no_samples)
    assert meters["session"]["projected_percent"] is None
    assert meters["session"]["limit_at"] is None


def test_limit_at_from_recent_slope():
    """40% -> 70% over the last hour; at that rate 100% lands in 1h,
    before the reset 2.5h out."""
    meters = _meter(70.0, NOW + int(2.5 * 3600))
    samples = lambda k, since: [(NOW - 3600, 40.0), (NOW, 70.0)]
    attach_projections(meters, NOW, samples_for=samples)
    assert meters["session"]["limit_at"] == NOW + 3600


def test_limit_at_none_when_eta_lands_after_reset():
    """Slow burn: 100% would land after resets_at -> never blows -> None."""
    meters = _meter(52.0, NOW + int(2.5 * 3600))
    samples = lambda k, since: [(NOW - 3600, 40.0), (NOW, 52.0)]  # 4h to 100
    attach_projections(meters, NOW, samples_for=samples)
    assert meters["session"]["limit_at"] is None


def test_limit_at_none_for_flat_or_falling_slope():
    meters = _meter(50.0, NOW + 4 * 3600)
    samples = lambda k, since: [(NOW - 3600, 50.0), (NOW, 50.0)]
    attach_projections(meters, NOW, samples_for=samples)
    assert meters["session"]["limit_at"] is None


def test_limit_at_none_when_samples_span_too_short():
    meters = _meter(50.0, NOW + 4 * 3600)
    samples = lambda k, since: [(NOW - 300, 40.0), (NOW, 50.0)]
    attach_projections(meters, NOW, samples_for=samples)
    assert meters["session"]["limit_at"] is None


def test_limit_at_clamped_to_now_when_already_at_100():
    meters = _meter(101.0, NOW + 3600)
    samples = lambda k, since: [(NOW - 3600, 80.0), (NOW, 101.0)]
    attach_projections(meters, NOW, samples_for=samples)
    assert meters["session"]["limit_at"] == NOW


def test_weekly_meter_uses_seven_day_window():
    """3.5 days into the week at 80% -> on pace for 160%."""
    meters = {"week_all": {"label": "x", "percent": 80, "utilization": 80.0,
                           "resets_at": NOW + int(3.5 * 86400)}}
    attach_projections(meters, NOW, samples_for=_no_samples)
    assert meters["week_all"]["projected_percent"] == 160
