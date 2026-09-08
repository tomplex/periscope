# tests/test_poke.py
"""The session poke's decision (pure) and its worker (I/O mocked).

Nothing here spawns a thread: conftest neuters poke._bg, and poke_account is
called directly with its subprocess and refetch patched."""

from datetime import datetime

import pytest

from periscope import poke

ACCOUNTS = [
    {"id": "default", "label": "account A", "config_dir": ""},
    {"id": "b", "label": "account B", "config_dir": "/Users/x/.claude-b"},
]
T0 = datetime(2026, 9, 8, 8, 0)          # Tue 08:00 local


def usage(*, default_open=False, b_open=False, now=T0):
    """Both accounts available; a session window is 'open' when resets_at is
    in the future — the endpoint reports null once the window has closed."""
    later = int(now.timestamp()) + 3600

    def acct(is_open):
        return {"available": True, "fetched_at": 1,
                "meters": {"session": {"percent": 3 if is_open else 0,
                                       "resets_at": later if is_open else None}}}
    return {"default": acct(default_open), "b": acct(b_open)}


def due(now=T0, settings=None, log=None, u=None, in_flight=frozenset()):
    return poke.due(now=now, settings=settings or {}, log=log or {},
                    usage=u if u is not None else usage(), in_flight=in_flight,
                    accounts=ACCOUNTS)


def test_fires_for_every_account_at_the_default_time():
    assert due() == ["default", "b"]


def test_waits_until_poke_at():
    assert due(now=datetime(2026, 9, 8, 7, 59)) == []
    assert due(now=datetime(2026, 9, 8, 8, 0)) == ["default", "b"]


def test_honors_a_configured_time_and_grace():
    s = {"poke_at": "06:30", "poke_grace_min": 30}
    assert due(now=datetime(2026, 9, 8, 6, 29), settings=s) == []
    assert due(now=datetime(2026, 9, 8, 6, 59), settings=s) == ["default", "b"]
    assert due(now=datetime(2026, 9, 8, 7, 0), settings=s) == []


def test_skips_the_day_past_the_grace_window():
    # A 10:00 catch-up would shorten the first work block, not help it.
    assert due(now=datetime(2026, 9, 8, 9, 29)) == ["default", "b"]
    assert due(now=datetime(2026, 9, 8, 9, 30)) == []


def test_empty_poke_at_disables():
    assert due(settings={"poke_at": ""}) == []


def test_skips_an_account_with_an_open_window_until_it_closes():
    assert due(u=usage(b_open=True)) == ["default"]
    assert due(u=usage()) == ["default", "b"]        # closed → fires (inside grace)


def test_skips_an_account_already_poked_today_but_not_yesterday():
    log = {"b": {"date": "2026-09-08", "at": 1, "resets_at": None, "verified": True}}
    assert due(log=log) == ["default"]
    stale = {"b": {"date": "2026-09-07", "at": 1, "resets_at": None, "verified": True}}
    assert due(log=stale) == ["default", "b"]


def test_skips_an_account_in_flight():
    assert due(in_flight={"default"}) == ["b"]


def test_skips_an_account_with_no_usable_credential():
    u = usage()
    u["b"] = {"available": False}
    assert due(u=u) == ["default"]


@pytest.mark.parametrize("resets_at,ok", [
    (1_800_000_000 + 5 * 3600, True),
    (1_800_000_000 + 5 * 3600 + 299, True),
    (1_800_000_000 + 5 * 3600 - 299, True),
    (1_800_000_000 + 5 * 3600 + 301, False),
    (1_800_000_000 + 3600, False),          # a pre-existing window, not ours
    (None, False),
])
def test_verified_is_within_five_minutes_of_now_plus_5h(resets_at, ok):
    assert poke.verified(resets_at, at=1_800_000_000) is ok
