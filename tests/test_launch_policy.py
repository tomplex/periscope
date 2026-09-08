"""The launch policy's decision table. Pure: no fixtures, no I/O.

Every case here is a row of docs/account-routing.md. `acct()` builds one
account's plan-usage payload in the exact shape `usage.parse_plan_usage`
produces and `/api/state` serializes."""

from dataclasses import replace

from periscope.launch_policy import (
    LaunchInputs,
    choose,
    model_family,
    order_accounts,
    pressured,
    sublimit,
    walled,
)

WED = 1_800_000_000          # B's weekly reset
SUN = WED + 4 * 86400        # A's weekly reset, 4 days later
NOW = WED - 10 * 3600        # the launch moment, well inside both weeks
SOON = NOW + 600             # a projected session wall inside the pressure horizon
LATER = NOW + 2 * 3600       # a projected wall beyond it — a forecast, not pressure


def acct(session=0, week=0, *, resets=None, session_resets=None,
         limit_at=None, **subs):
    """One account's payload. `subs` are sub-limit meters keyed by name
    (week_fable=100 → {"week_fable": {"percent": 100, ...}})."""
    meters = {
        "session": {"percent": session, "resets_at": session_resets,
                    "limit_at": limit_at},
        "week_all": {"percent": week, "resets_at": resets},
    }
    for key, pct in subs.items():
        meters[key] = {"percent": pct, "resets_at": resets}
    return {"available": True, "meters": meters, "fetched_at": 1}


NO_DATA = {"available": False}

BASE = LaunchInputs(
    accounts=("default", "b"),
    usage={"default": acct(resets=SUN), "b": acct(resets=WED)},
    account_arg=None,
    model_arg=None,
    account_pin=None,
    model_pin=None,
    now=NOW,
)


def pick(**over):
    """(account, model) for BASE with one or more fields overridden."""
    launch = choose(replace(BASE, **over))
    return launch.account, launch.model


# --- model_family / sublimit -------------------------------------------------

def test_model_family_strips_the_context_suffix_and_matches_full_ids():
    assert model_family("fable") == "fable"
    assert model_family("opus[1m]") == "opus"
    assert model_family("claude-opus-5") == "opus"
    assert model_family("claude-haiku-4-5") == "haiku"
    assert model_family("default") is None
    assert model_family("gpt-5") is None


def test_sublimit_matches_the_family_key_and_any_slugified_suffix():
    # Keys are slugified display names (usage.parse_plan_usage): "Fable" →
    # week_fable today, "Fable 5.1" → week_fable_5_1 tomorrow. Both must wall.
    meters = {"week_fable_5_1": {"percent": 100}, "week_all": {"percent": 3}}
    assert sublimit(meters, "fable") == {"percent": 100}
    assert sublimit({"week_fable": {"percent": 7}}, "fable[1m]") == {"percent": 7}
    assert sublimit(meters, "opus[1m]") is None
    assert sublimit(meters, "default") is None


# --- walled / pressured -------------------------------------------------------

def test_walled_is_session_or_week_all_at_100_plus_the_models_sublimit():
    assert not walled(acct()["meters"])
    assert walled(acct(session=100)["meters"])
    assert walled(acct(week=100)["meters"])
    assert not walled(acct(week_fable=100)["meters"])                  # no model asked
    assert walled(acct(week_fable=100)["meters"], model="fable")
    assert not walled(acct(week_fable=100)["meters"], model="opus[1m]")
    assert not walled(acct(week_fable=99)["meters"], model="fable")
    assert not walled(acct()["meters"], model="fable")                 # missing meter ≠ wall


def test_pressured_is_a_projected_session_wall_within_the_horizon():
    assert pressured(acct(limit_at=SOON)["meters"], now=NOW)
    assert not pressured(acct(limit_at=LATER)["meters"], now=NOW)   # a forecast, not pressure
    assert not pressured(acct()["meters"], now=NOW)


# --- order_accounts -----------------------------------------------------------

def test_order_is_soonest_weekly_reset_first_null_last_registry_order_on_ties():
    assert order_accounts(BASE) == ("b", "default")
    fresh = replace(BASE, usage={"default": acct(resets=SUN), "b": acct(resets=None)})
    assert order_accounts(fresh) == ("default", "b")      # null = 0% used = reset most recently
    tied = replace(BASE, usage={"default": acct(resets=WED), "b": acct(resets=WED)})
    assert order_accounts(tied) == ("default", "b")
    partial = replace(BASE, usage={"default": NO_DATA, "b": acct(resets=WED)})
    assert order_accounts(partial) == ("b",)


# --- choose: the routing table ------------------------------------------------

def test_soonest_reset_gets_fable():
    assert pick() == ("b", "fable")


def test_session_wall_on_the_soonest_account_fails_over():
    assert pick(usage={"default": acct(resets=SUN), "b": acct(session=100, resets=WED)}) == ("default", "fable")


def test_weekly_wall_on_the_soonest_account_fails_over():
    assert pick(usage={"default": acct(resets=SUN), "b": acct(week=100, resets=WED)}) == ("default", "fable")


def test_fable_wall_means_opus_on_the_SAME_account_not_fable_on_the_other():
    # EDF-strict (D2): the soonest account's weekly-all is the budget expiring
    # first, so its non-Fable half is burned before the other account is touched.
    assert pick(usage={"default": acct(resets=SUN),
                       "b": acct(resets=WED, week_fable=100)}) == ("b", "opus[1m]")


def test_sublimit_matches_a_slugified_suffix():
    assert pick(usage={"default": acct(resets=SUN),
                       "b": acct(resets=WED, week_fable_5_1=100)}) == ("b", "opus[1m]")


def test_both_models_walled_on_the_soonest_account_moves_to_the_other():
    # week_opus is synthetic: no plan on this host has ever reported one
    # (0 of 6,679 samples). The rule is generic; this proves the branch, not the host.
    assert pick(usage={"default": acct(resets=SUN),
                       "b": acct(resets=WED, week_fable=100, week_opus=100)}) == ("default", "fable")


def test_everything_walled_picks_the_soonest_session_reset():
    later, sooner = WED + 7200, WED + 600
    assert pick(usage={"default": acct(week=100, resets=SUN, session_resets=sooner),
                       "b": acct(week=100, resets=WED, session_resets=later)}) == ("default", "fable")


def test_session_pressure_reroutes_new_spawns():
    assert pick(usage={"default": acct(resets=SUN),
                       "b": acct(resets=WED, limit_at=SOON)}) == ("default", "fable")


def test_a_wall_projected_beyond_the_horizon_does_not_reroute():
    # 17:30, B at 61% and burning: the slope says "wall at 19:30" but evening
    # usage tails off and that wall mostly never arrives. Two hours out is a
    # forecast; only an imminent wall moves new spawns.
    assert pick(usage={"default": acct(resets=SUN),
                       "b": acct(resets=WED, limit_at=LATER)}) == ("b", "fable")


def test_all_pressured_ignores_pressure():
    usage = {"default": acct(resets=SUN, limit_at=SOON),
              "b": acct(resets=WED, limit_at=SOON)}
    assert pick(usage=usage) == ("b", "fable")
    # The second pass starts its own skip list — it must not carry over "b:
    # session on pace to wall" from the first pass, since b is what it picked.
    assert "skipped" not in choose(replace(BASE, usage=usage)).reason


def test_explicit_account_is_never_rerouted_by_pressure():
    assert pick(account_arg="b",
                usage={"default": acct(resets=SUN),
                       "b": acct(resets=WED, limit_at=SOON)}) == ("b", "fable")


def test_account_pin_behaves_like_an_explicit_account_and_still_picks_the_model():
    assert pick(account_pin="default",
                usage={"default": acct(resets=SUN, week_fable=100),
                       "b": acct(resets=WED)}) == ("default", "opus[1m]")


def test_a_pin_naming_no_registered_account_is_ignored():
    assert pick(account_pin="gone") == ("b", "fable")


def test_model_pin_routes_by_that_models_sublimit():
    assert pick(model_pin="sonnet",
                usage={"default": acct(resets=SUN),
                       "b": acct(resets=WED, week_sonnet=100)}) == ("default", "sonnet")


def test_model_pin_auto_is_the_same_as_unset():
    assert pick(model_pin="auto") == pick()


def test_explicit_default_model_means_no_override():
    assert pick(model_arg="default") == ("b", None)
    assert pick(model_pin="default") == ("b", None)


def test_explicit_model_arg_beats_the_pin():
    assert pick(model_arg="sonnet", model_pin="fable") == ("b", "sonnet")


def test_no_usage_data_falls_back_to_default_without_waiting():
    assert pick(usage={"default": NO_DATA, "b": NO_DATA}) == ("default", None)
    assert pick(usage={}) == ("default", None)
    assert pick(usage={}, model_pin="fable") == ("default", "fable")
    # An explicit account is honored even blind — the caller asked for it.
    assert pick(usage={}, account_arg="b") == ("b", None)


def test_reason_names_the_account_and_the_model():
    launch = choose(BASE)
    assert (launch.account, launch.model) == ("b", "fable")
    assert launch.reason.startswith("b · week resets ")
    assert launch.reason.endswith(" · fable")
