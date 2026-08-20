"""Claude usage tracking: JSONL parsing + OAuth plan-usage endpoint."""

from periscope.usage import compute_claude_usage, parse_plan_usage


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


def test_parse_plan_usage_includes_scoped_model_meter_when_active():
    """An active per-model weekly sub-limit (Fable) in the `limits` array
    surfaces as week_<slug>; scope.model.display_name names it."""
    sample = {
        "five_hour": {"utilization": 6.0, "resets_at": "2026-07-01T17:10:00+00:00"},
        "limits": [{
            "kind": "weekly_scoped", "group": "weekly", "percent": 31, "is_active": True,
            "resets_at": "2026-07-05T14:00:00+00:00",
            "scope": {"model": {"id": "fable-5", "display_name": "Fable"}},
        }],
    }
    out = parse_plan_usage(sample)
    assert out["meters"]["week_fable"]["percent"] == 31
    assert out["meters"]["week_fable"]["label"] == "Current week (Fable only)"


def test_parse_plan_usage_surfaces_scoped_meter_with_usage_but_not_active():
    """is_active means 'currently-binding limit', not 'has usage' — it stays
    False while Fable accumulates below its cap. Any percent > 0 surfaces it."""
    sample = {
        "five_hour": {"utilization": 6.0, "resets_at": "2026-07-01T17:10:00+00:00"},
        "limits": [{
            "kind": "weekly_scoped", "group": "weekly", "percent": 1, "is_active": False,
            "resets_at": "2026-07-05T14:00:00+00:00",
            "scope": {"model": {"id": None, "display_name": "Fable"}},
        }],
    }
    out = parse_plan_usage(sample)
    assert out["meters"]["week_fable"]["percent"] == 1


def test_parse_plan_usage_skips_dormant_scoped_model_meter():
    """A scoped limit with is_active=False AND percent 0 (the pre-launch
    dormant state) is NOT surfaced — matches null seven_day_opus/sonnet."""
    sample = {
        "five_hour": {"utilization": 6.0, "resets_at": "2026-07-01T17:10:00+00:00"},
        "limits": [{
            "kind": "weekly_scoped", "group": "weekly", "percent": 0, "is_active": False,
            "resets_at": None,
            "scope": {"model": {"id": None, "display_name": "Fable"}},
        }],
    }
    out = parse_plan_usage(sample)
    assert "week_fable" not in out["meters"]


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
from datetime import UTC

from periscope.usage import _weighted_burn_from_jsonl


def _jsonl_line(ts_iso, usage):
    return _json.dumps({"timestamp": ts_iso, "message": {"usage": usage}})


def test_weighted_burn_from_jsonl_sums_recent_weighted(tmp_path):
    """Recent records weighted (out x5, cache_w x1.25, cache_r x0.1);
    records older than the cutoff and junk lines are skipped."""
    from datetime import datetime
    recent = datetime.fromtimestamp(NOW, tz=UTC).isoformat()
    old = datetime.fromtimestamp(NOW - 7200, tz=UTC).isoformat()
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
                        lambda: {"default": {"meters": {"session": {"hot": True}}}})
    monkeypatch.setattr(usage, "pane_burn_rates",
                        lambda ids: {"%1": 900.0, "%2": 100.0})
    views = [
        {"pane_id": "%1", "agent": "claude"},
        {"pane_id": "%2", "agent": "claude"},
        {"pane_id": "%3", "agent": None},
    ]
    usage.annotate_hot_panes(views)
    assert views[0].get("burn_hot") is True
    assert views[0].get("burn_wtpm") == 900
    assert "burn_hot" not in views[1]
    assert "burn_hot" not in views[2]


def test_annotate_hot_panes_noop_when_meter_not_hot(monkeypatch):
    import periscope.usage as usage
    monkeypatch.setattr(usage, "cached_plan_usage",
                        lambda: {"default": {"meters": {"session": {"hot": False}}}})
    called = []
    monkeypatch.setattr(usage, "pane_burn_rates",
                        lambda ids: called.append(ids) or {})
    views = [{"pane_id": "%1", "agent": "claude"}]
    usage.annotate_hot_panes(views)
    assert not called  # burn never even computed
    assert "burn_hot" not in views[0]


# --- weekly meters: duty-cycle-adjusted recent burn (24h slope) -----------

def _week(utilization, resets_at):
    return {"week_all": {"label": "x", "percent": round(utilization),
                         "utilization": float(utilization),
                         "resets_at": resets_at}}


def test_weekly_recent_burn_uses_24h_slope_window():
    seen = []
    def samples(k, since):
        seen.append(since)
        return []
    meters = _week(50.0, NOW + 3 * 86400)
    attach_projections(meters, NOW, samples_for=samples)
    assert seen == [NOW - 86400]


def test_weekly_recent_burn_needs_12h_of_samples():
    """A hot 6 active hours must NOT extrapolate across the whole week —
    suppressed until the slope spans a sleep/work cycle."""
    meters = _week(50.0, NOW + 3 * 86400)
    samples = lambda k, since: [(NOW - 6 * 3600, 40.0), (NOW, 50.0)]
    attach_projections(meters, NOW, samples_for=samples)
    assert meters["week_all"]["projected_recent"] is None
    assert meters["week_all"]["hot"] is False
    assert meters["week_all"]["limit_at"] is None


def test_weekly_hot_means_a_full_hot_day():
    """Even-burn for the week is ~14.3%/day. 30%/day over the last 24h
    (sleep included) is hot; 20%/day is not."""
    hot = _week(40.0, NOW + 3 * 86400)
    samples = lambda k, since: [(NOW - 86400, 10.0), (NOW, 40.0)]
    attach_projections(hot, NOW, samples_for=samples)
    assert hot["week_all"]["hot"] is True
    # 40% + 30%/day * 3 days = 130
    assert hot["week_all"]["projected_recent"] == 130

    warm = _week(30.0, NOW + 3 * 86400)
    samples = lambda k, since: [(NOW - 86400, 10.0), (NOW, 30.0)]
    attach_projections(warm, NOW, samples_for=samples)
    assert warm["week_all"]["hot"] is False


def test_parse_plan_usage_warns_once_on_live_unknown_meter(caplog):
    """A codename field going non-null logs a warning (once) instead of
    being silently dropped; null codename fields stay quiet."""
    import logging

    import periscope.usage as usage
    usage._warned_unknown_fields.clear()
    sample = {
        "five_hour": {"utilization": 10.0, "resets_at": "2026-06-11T18:00:00+00:00"},
        "seven_day_omelette": {"utilization": 12.0, "resets_at": "2026-06-14T14:00:00+00:00"},
        "cinder_cove": None,
    }
    with caplog.at_level(logging.WARNING):
        parse_plan_usage(sample)
        parse_plan_usage(sample)  # second call must not re-warn
    hits = [r for r in caplog.records if "unmapped meter" in r.message]
    assert len(hits) == 1
    assert "seven_day_omelette" in hits[0].getMessage()


# --- fetch failure visibility + cache backoff ---


def test_fetch_plan_usage_warns_when_token_missing(caplog, monkeypatch):
    """A missing/expired keychain token logs a warning instead of silently
    serving stale meters forever."""
    import logging

    import periscope.usage as usage
    monkeypatch.setattr(usage, "_read_oauth_token", lambda cfg: None)
    # The warning dedupes per keychain item across the process, so an earlier
    # test that hit the same item would otherwise silence this one.
    monkeypatch.setattr(usage, "_warned_missing_token", set())
    with caplog.at_level(logging.WARNING):
        assert usage.fetch_plan_usage() is None
    assert any("no valid OAuth token" in r.getMessage() for r in caplog.records)


def test_fetch_plan_usage_warns_on_http_failure(caplog, monkeypatch):
    """Network/HTTP failures warn — httpx only logs requests that get a
    response, so this is the only evidence a fetch was even attempted."""
    import logging

    import periscope.usage as usage

    def boom(*a, **kw):
        raise RuntimeError("connect timeout")

    monkeypatch.setattr(usage, "_read_oauth_token", lambda cfg: "tok")
    monkeypatch.setattr(usage.httpx, "get", boom)
    with caplog.at_level(logging.WARNING):
        assert usage.fetch_plan_usage() is None
    assert any("plan usage fetch failed" in r.getMessage() for r in caplog.records)


def test_refresh_success_stamps_fetched_at_and_full_backoff(monkeypatch):
    """A successful refresh attaches fetched_at and schedules the next
    attempt PLAN_USAGE_REFRESH_S out."""
    import time

    import periscope.usage as usage
    monkeypatch.setattr(usage, "fetch_plan_usage",
                        lambda cfg: {"available": True, "meters": {}})
    monkeypatch.setattr(usage.activity, "record_usage_samples", lambda rows: None)
    monkeypatch.setattr(usage, "attach_projections", lambda *a, **kw: None)
    monkeypatch.setattr(usage, "_plan_cache", {})
    monkeypatch.setattr(usage, "_plan_in_flight", {"default"})
    before = time.time()
    usage._refresh_plan_usage_into_cache("default", "")
    next_at, data = usage._plan_cache["default"]
    assert data["fetched_at"] >= int(before)
    assert next_at - before >= usage.PLAN_USAGE_REFRESH_S - 1
    assert usage._plan_in_flight == set()


def test_refresh_failure_keeps_data_and_retries_sooner(monkeypatch):
    """A failed refresh keeps the previously-cached data (with its old
    fetched_at) and retries at PLAN_USAGE_RETRY_S, not the full interval —
    the post-wake first attempt often fails before the network is up."""
    import time

    import periscope.usage as usage
    old = {"available": True, "meters": {}, "fetched_at": 123}
    monkeypatch.setattr(usage, "fetch_plan_usage", lambda cfg: None)
    monkeypatch.setattr(usage, "_plan_cache", {"default": (0.0, old)})
    monkeypatch.setattr(usage, "_plan_in_flight", {"default"})
    before = time.time()
    usage._refresh_plan_usage_into_cache("default", "")
    next_at, data = usage._plan_cache["default"]
    assert data is old
    assert usage.PLAN_USAGE_RETRY_S - 1 <= next_at - before < usage.PLAN_USAGE_REFRESH_S
    assert usage._plan_in_flight == set()


def _two_accounts(monkeypatch):
    import periscope.usage as usage
    monkeypatch.setattr(usage.store, "get_accounts", lambda: [
        {"id": "default", "label": "A", "config_dir": ""},
        {"id": "b", "label": "B", "config_dir": "/Users/tom/.claude-b"},
    ])
    return usage


def test_cached_plan_usage_no_spawn_before_next_attempt(monkeypatch):
    """Inside the backoff window every account is served from cache with no
    refresh spawn, and each payload keeps its single-account shape."""
    usage = _two_accounts(monkeypatch)
    a = {"available": True, "meters": {}, "fetched_at": 1}
    b = {"available": True, "meters": {}, "fetched_at": 2}
    monkeypatch.setattr(usage, "_plan_cache",
                        {"default": (float("inf"), a), "b": (float("inf"), b)})
    monkeypatch.setattr(usage, "_bg", lambda *a: (_ for _ in ()).throw(
        AssertionError("refresh spawned inside backoff window")))
    assert usage.cached_plan_usage() == {"default": a, "b": b}


def test_cached_plan_usage_unfetched_account_reports_unavailable(monkeypatch):
    """An account with nothing cached yet degrades to available:False rather
    than None — the meters of the OTHER account must still render."""
    usage = _two_accounts(monkeypatch)
    a = {"available": True, "meters": {}, "fetched_at": 1}
    monkeypatch.setattr(usage, "_plan_cache", {"default": (float("inf"), a)})
    monkeypatch.setattr(usage, "_plan_in_flight", {"b"})
    monkeypatch.setattr(usage, "_bg", lambda *a: (_ for _ in ()).throw(
        AssertionError("refresh spawned while in flight")))
    assert usage.cached_plan_usage() == {"default": a, "b": {"available": False}}


def test_cached_plan_usage_spawns_one_refresh_per_due_account(monkeypatch):
    """Past the next-attempt time: one refresh per account, each carrying its
    own config dir, and the in-flight set dedupes concurrent polls."""
    usage = _two_accounts(monkeypatch)
    spawned = []
    monkeypatch.setattr(usage, "_plan_cache", {})
    monkeypatch.setattr(usage, "_plan_in_flight", set())
    monkeypatch.setattr(usage, "_bg",
                        lambda name, fn, *args: spawned.append((name, args)))
    usage.cached_plan_usage()
    usage.cached_plan_usage()
    assert spawned == [("plan-usage:default", ("default", "")),
                       ("plan-usage:b", ("b", "/Users/tom/.claude-b"))]


def test_refresh_records_samples_under_its_own_account(monkeypatch):
    """Samples land tagged with the account that produced them."""
    import periscope.usage as usage
    rows = []
    monkeypatch.setattr(usage, "fetch_plan_usage", lambda cfg: {
        "available": True,
        "meters": {"session": {"utilization": 12.0, "resets_at": 999}},
    })
    monkeypatch.setattr(usage.activity, "record_usage_samples", rows.extend)
    monkeypatch.setattr(usage, "attach_projections", lambda *a, **kw: None)
    monkeypatch.setattr(usage, "_plan_cache", {})
    monkeypatch.setattr(usage, "_plan_in_flight", {"b"})
    usage._refresh_plan_usage_into_cache("b", "/Users/tom/.claude-b")
    assert [(r[1], r[2], r[3], r[4]) for r in rows] == [("b", "session", 12.0, 999)]


def test_missing_token_degrades_only_that_account(monkeypatch):
    """A dead/absent keychain item for one account leaves the other's cached
    meters untouched."""
    usage = _two_accounts(monkeypatch)
    a = {"available": True, "meters": {}, "fetched_at": 1}
    monkeypatch.setattr(usage, "_read_oauth_token", lambda cfg: None)
    monkeypatch.setattr(usage, "_plan_cache", {"default": (float("inf"), a)})
    monkeypatch.setattr(usage, "_plan_in_flight", {"b"})
    usage._refresh_plan_usage_into_cache("b", "/Users/tom/.claude-b")
    monkeypatch.setattr(usage, "_bg", lambda *args: None)
    assert usage.cached_plan_usage() == {"default": a, "b": {"available": False}}


# --- per-account credentials ---------------------------------------------

def test_keychain_item_default_account_is_bare_name():
    """The default config dir ("") uses the unsuffixed item name."""
    from periscope.usage import _keychain_item
    assert _keychain_item("") == "Claude Code-credentials"


def test_keychain_item_named_config_dir_uses_sha256_prefix():
    """Verified live against the real keychain: ~/.claude-b's credential is
    stored under the sha256[:8] of the config dir path."""
    from periscope.usage import _keychain_item
    assert (_keychain_item("/Users/tom/.claude-b")
            == "Claude Code-credentials-f79ff3dd")


def test_missing_token_warns_once_per_account(caplog, monkeypatch):
    """A second subscription that was never logged in has no keychain item at
    all, and the retry cadence is 60s — warning on every attempt would put
    ~1400 identical lines a day in the log. Once per item is enough evidence."""
    import logging

    import periscope.usage as usage
    monkeypatch.setattr(usage, "_read_oauth_token", lambda cfg: None)
    monkeypatch.setattr(usage, "_warned_missing_token", set())
    with caplog.at_level(logging.WARNING):
        assert usage.fetch_plan_usage("/Users/tom/.claude-b") is None
        assert usage.fetch_plan_usage("/Users/tom/.claude-b") is None
        assert usage.fetch_plan_usage("") is None
    msgs = [r.getMessage() for r in caplog.records if "no valid OAuth token" in r.getMessage()]
    assert len(msgs) == 2  # one per distinct keychain item, not per attempt


# --- best_account: which subscription a new pane should land on -------------

from periscope import usage


def _plan(**pcts):
    """account id -> plan payload; None means 'no usable data'."""
    return {
        aid: ({"available": False} if p is None else
              {"available": True, "meters": {"session": {"percent": p}}, "fetched_at": 1})
        for aid, p in pcts.items()
    }


def test_best_account_picks_the_most_headroom(monkeypatch, clean_state):
    monkeypatch.setattr(usage, "cached_plan_usage", lambda: _plan(default=100, b=8))
    assert usage.best_account() == "b"
    monkeypatch.setattr(usage, "cached_plan_usage", lambda: _plan(default=4, b=70))
    assert usage.best_account() == "default"


def test_best_account_uses_the_binding_meter_not_the_emptiest_one(monkeypatch, clean_state):
    # b's week is nearly free but its 5h window is nearly spent: the limit that
    # binds first is what decides whether work can actually run there.
    monkeypatch.setattr(usage, "cached_plan_usage", lambda: {
        "default": {"available": True, "fetched_at": 1,
                    "meters": {"session": {"percent": 30}, "week_all": {"percent": 30}}},
        "b": {"available": True, "fetched_at": 1,
              "meters": {"session": {"percent": 96}, "week_all": {"percent": 2}}},
    })
    assert usage.best_account() == "default"


def test_best_account_skips_accounts_with_no_data(monkeypatch, clean_state):
    # No data must never read as infinite room.
    monkeypatch.setattr(usage, "cached_plan_usage", lambda: _plan(default=90, b=None))
    assert usage.best_account() == "default"


def test_best_account_falls_back_to_default_when_nothing_is_known(monkeypatch, clean_state):
    monkeypatch.setattr(usage, "cached_plan_usage", lambda: _plan(default=None, b=None))
    assert usage.best_account() == "default"
    monkeypatch.setattr(usage, "cached_plan_usage", dict)
    assert usage.best_account() == "default"


def test_best_account_honors_the_spawn_pin_over_headroom(monkeypatch, clean_state):
    # Pin wins even when the pinned account is the FULL one — that's what a
    # deliberate pin means, and second-guessing it would make the header
    # control a suggestion.
    from periscope import store
    store.update_settings({"spawn_account": "default"})
    monkeypatch.setattr(usage, "cached_plan_usage", lambda: _plan(default=100, b=8))
    assert usage.best_account() == "default"


def test_best_account_ignores_a_pin_naming_no_registered_account(monkeypatch, clean_state):
    # account_config_dir fails OPEN on an unknown id, so honoring a stale pin
    # would silently reroute every spawn to the default account.
    from periscope import store
    store.update_settings({"spawn_account": "gone"})
    monkeypatch.setattr(usage, "cached_plan_usage", lambda: _plan(default=100, b=8))
    assert usage.best_account() == "b"


def test_best_account_breaks_ties_randomly(monkeypatch, clean_state):
    monkeypatch.setattr(usage, "cached_plan_usage", lambda: _plan(default=20, b=20))
    assert usage.best_account(rand=lambda: 0.0) == "default"
    assert usage.best_account(rand=lambda: 0.99) == "b"


import time


def _iso(epoch):
    from datetime import UTC, datetime
    return datetime.fromtimestamp(epoch, UTC).isoformat().replace("+00:00", "Z")


def _assistant_line(ts_iso, usage, model="claude-opus-5"):
    """A well-formed assistant record — the shape parse_usage_record accepts.

    NOTE: this file imports `json as _json` (near line 231), not `json`.
    """
    return _json.dumps({
        "type": "assistant",
        "timestamp": ts_iso,
        "message": {"model": model, "usage": usage},
    })


def _synthetic_line(ts_iso):
    """Claude Code's interrupt/error/limit placeholder: all-zero usage."""
    return _json.dumps({
        "type": "assistant",
        "timestamp": ts_iso,
        "message": {"model": "<synthetic>", "usage": {
            "input_tokens": 0, "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0, "output_tokens": 0}},
    })


def test_tail_summary_ignores_a_synthetic_final_record(tmp_path):
    """A transcript that ends on a limit message must still report its real
    context, not zero."""
    path = tmp_path / "s.jsonl"
    now = time.time()
    path.write_text("\n".join([
        _assistant_line(_iso(now - 60), {"input_tokens": 5,
                                         "cache_read_input_tokens": 503_608}),
        _synthetic_line(_iso(now - 30)),
    ]) + "\n")
    got = usage._tail_summary_from_jsonl(path, cutoff=now - 1800)
    assert got.cur_ctx == 503_613
    assert got.calls == 1


def test_tail_summary_reports_cur_ctx_for_a_fully_parked_transcript(tmp_path):
    path = tmp_path / "s.jsonl"
    now = time.time()
    path.write_text(_assistant_line(_iso(now - 7200),
                                    {"cache_read_input_tokens": 600_000}) + "\n")
    got = usage._tail_summary_from_jsonl(path, cutoff=now - 1800)
    assert got.cur_ctx == 600_000
    assert got.calls == 0
    assert got.last_ts is not None


def test_base_ctx_skips_a_synthetic_first_record(tmp_path):
    path = tmp_path / "s.jsonl"
    now = time.time()
    path.write_text("\n".join([
        _synthetic_line(_iso(now - 300)),
        _assistant_line(_iso(now - 200), {"input_tokens": 1,
                                          "cache_creation_input_tokens": 49_999}),
        _assistant_line(_iso(now - 100), {"cache_read_input_tokens": 400_000}),
    ]) + "\n")
    assert usage._base_ctx_from_jsonl(path) == 50_000


def test_readers_return_nothing_on_a_usage_free_transcript(tmp_path):
    path = tmp_path / "s.jsonl"
    path.write_text(_json.dumps({"type": "user", "timestamp": "x"}) + "\n")
    assert usage._tail_summary_from_jsonl(path, cutoff=0).cur_ctx is None
    assert usage._base_ctx_from_jsonl(path) is None


def test_readers_survive_a_missing_file(tmp_path):
    path = tmp_path / "nope.jsonl"
    assert usage._tail_summary_from_jsonl(path, cutoff=0).cur_ctx is None
    assert usage._base_ctx_from_jsonl(path) is None
