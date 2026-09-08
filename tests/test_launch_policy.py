"""The launch policy's decision table. Pure: no fixtures, no I/O.

Every case here is a row of docs/account-routing.md. `acct()` builds one
account's plan-usage payload in the exact shape `usage.parse_plan_usage`
produces and `/api/state` serializes."""

from dataclasses import replace

from periscope.launch_policy import (
    Launch,
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


def test_pressured_is_a_projected_session_wall():
    assert pressured(acct(limit_at=WED)["meters"])
    assert not pressured(acct()["meters"])
