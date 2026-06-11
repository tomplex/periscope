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


def test_projected_recent_extrapolates_current_burn():
    """40% -> 70% over the last hour (30%/h), 2.5h to reset -> 70 + 75 = 145."""
    meters = _meter(70.0, NOW + int(2.5 * 3600))
    samples = lambda k, since: [(NOW - 3600, 40.0), (NOW, 70.0)]
    attach_projections(meters, NOW, samples_for=samples)
    assert meters["session"]["projected_recent"] == 145


def test_hot_when_recent_rate_at_least_twice_even_burn():
    """Even burn for the 5h session window is 20%/h; 30%/h is warm (not
    hot), 40%/h is hot."""
    warm = _meter(50.0, NOW + 3600)
    samples = lambda k, since: [(NOW - 3600, 20.0), (NOW, 50.0)]  # 30%/h
    attach_projections(warm, NOW, samples_for=samples)
    assert warm["session"]["hot"] is False

    hot = _meter(60.0, NOW + 3600)
    samples = lambda k, since: [(NOW - 3600, 20.0), (NOW, 60.0)]  # 40%/h
    attach_projections(hot, NOW, samples_for=samples)
    assert hot["session"]["hot"] is True


def test_projected_recent_and_hot_default_none_false():
    meters = _meter(50.0, NOW + 3600)
    attach_projections(meters, NOW, samples_for=_no_samples)
    assert meters["session"]["projected_recent"] is None
    assert meters["session"]["hot"] is False


# --- per-pane burn attribution -------------------------------------------

import json as _json
from periscope.usage import _weighted_burn_from_jsonl, annotate_hot_panes


def _jsonl_line(ts_iso, usage):
    return _json.dumps({"timestamp": ts_iso, "message": {"usage": usage}})


def test_weighted_burn_from_jsonl_sums_recent_weighted(tmp_path):
    """Recent records weighted (out x5, cache_w x1.25, cache_r x0.1);
    records older than the cutoff and junk lines are skipped."""
    from datetime import datetime, timezone
    recent = datetime.fromtimestamp(NOW, tz=timezone.utc).isoformat()
    old = datetime.fromtimestamp(NOW - 7200, tz=timezone.utc).isoformat()
    f = tmp_path / "s.jsonl"
    f.write_text("\n".join([
        _jsonl_line(recent, {"input_tokens": 100, "output_tokens": 10,
                             "cache_creation_input_tokens": 80,
                             "cache_read_input_tokens": 1000}),
        _jsonl_line(old, {"output_tokens": 99999}),
        "not json",
        _json.dumps({"timestamp": recent}),  # no usage block
    ]) + "\n")
    # 100*1 + 10*5 + 80*1.25 + 1000*0.1 = 350
    assert _weighted_burn_from_jsonl(f, NOW - 1800) == 350.0


def test_annotate_hot_panes_flames_majority_burner(monkeypatch):
    """Session meter hot -> the pane carrying >=40% of burn gets flamed."""
    import periscope.usage as usage
    monkeypatch.setattr(usage, "cached_plan_usage",
                        lambda: {"meters": {"session": {"hot": True}}})
    monkeypatch.setattr(usage, "pane_burn_rates",
                        lambda ids: {"%1": 900.0, "%2": 100.0})
    views = [
        {"pane_id": "%1", "is_claude": True},
        {"pane_id": "%2", "is_claude": True},
        {"pane_id": "%3", "is_claude": False},
    ]
    usage.annotate_hot_panes(views)
    assert views[0].get("burn_hot") is True
    assert views[0].get("burn_wtpm") == 900
    assert "burn_hot" not in views[1]
    assert "burn_hot" not in views[2]


def test_annotate_hot_panes_noop_when_meter_not_hot(monkeypatch):
    import periscope.usage as usage
    monkeypatch.setattr(usage, "cached_plan_usage",
                        lambda: {"meters": {"session": {"hot": False}}})
    called = []
    monkeypatch.setattr(usage, "pane_burn_rates",
                        lambda ids: called.append(ids) or {})
    views = [{"pane_id": "%1", "is_claude": True}]
    usage.annotate_hot_panes(views)
    assert not called  # burn never even computed
    assert "burn_hot" not in views[0]
