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


# --- poke_account: subprocess → refetch → verify → record --------------------

def _worker(monkeypatch, *, resets_at, returncode=0, raise_subprocess=False):
    """Patch the two I/O seams; return the captured argv/env and the record."""
    seen = {}

    def fake_run(argv, **kw):
        if raise_subprocess:
            raise OSError("no claude binary")
        seen["argv"], seen["env"], seen["cwd"] = argv, kw.get("env"), kw.get("cwd")
        return type("R", (), {"returncode": returncode, "stdout": "ok", "stderr": ""})()

    monkeypatch.setattr(poke.subprocess, "run", fake_run)
    monkeypatch.setattr(poke.usage, "refresh_plan_usage_now",
                        lambda aid, cfg: {"available": True, "fetched_at": int(poke.time.time()) + 1,
                                          "meters": {"session": {"percent": 1, "resets_at": resets_at}}})
    monkeypatch.setattr(poke.time, "sleep", lambda s: None)
    monkeypatch.setattr(poke.store, "record_poke", lambda aid, e: seen.update(record=(aid, e)))
    monkeypatch.setattr(poke, "_in_flight", {"b"})
    return seen


def test_poke_account_runs_haiku_headless_on_the_account_and_records_a_verified_poke(monkeypatch):
    at = int(poke.time.time())
    seen = _worker(monkeypatch, resets_at=at + 5 * 3600 + 10)
    poke.poke_account("b", "/Users/x/.claude-b")
    argv = seen["argv"]
    assert argv[1:] == ["-p", "ok", "--model", "claude-haiku-4-5", "--strict-mcp-config"]
    assert seen["env"]["CLAUDE_CONFIG_DIR"] == "/Users/x/.claude-b"
    assert "ANTHROPIC_API_KEY" not in seen["env"]
    assert seen["cwd"] == poke.os.path.expanduser("~")
    aid, entry = seen["record"]
    assert aid == "b"
    assert entry["verified"] is True
    assert entry["resets_at"] == at + 5 * 3600 + 10
    assert entry["date"] == poke.datetime.fromtimestamp(entry["at"]).strftime("%Y-%m-%d")
    assert poke._in_flight == set()


def test_poke_account_records_an_unverified_poke_when_the_reset_did_not_move(monkeypatch, caplog):
    seen = _worker(monkeypatch, resets_at=None)
    poke.poke_account("b", "/Users/x/.claude-b")
    assert seen["record"][1]["verified"] is False
    assert any("poke b" in r.message and r.levelname == "WARNING" for r in caplog.records)


def test_poke_account_retries_once_for_a_reading_that_postdates_the_poke(monkeypatch):
    # A background refresh in flight at verify time makes refresh_plan_usage_now
    # yield the PRE-poke cache; verifying against that would log a false
    # "not anchored". The first stale reading is retried once after a wait.
    at = int(poke.time.time())
    seen = _worker(monkeypatch, resets_at=at + 5 * 3600)
    stale = {"available": True, "fetched_at": at - 100,
             "meters": {"session": {"percent": 0, "resets_at": None}}}
    fresh = {"available": True, "fetched_at": at + 5,
             "meters": {"session": {"percent": 1, "resets_at": at + 5 * 3600}}}
    readings = iter([stale, fresh])
    monkeypatch.setattr(poke.usage, "refresh_plan_usage_now", lambda aid, cfg: next(readings))
    slept = []
    monkeypatch.setattr(poke.time, "sleep", slept.append)
    poke.poke_account("b", "/Users/x/.claude-b")
    assert slept == [poke._RETRY_WAIT_S]
    assert seen["record"][1]["verified"] is True


def test_poke_account_gives_up_after_two_stale_readings_without_a_false_alarm(monkeypatch, caplog):
    at = int(poke.time.time())
    seen = _worker(monkeypatch, resets_at=None)
    stale = {"available": True, "fetched_at": at - 100,
             "meters": {"session": {"percent": 0, "resets_at": None}}}
    monkeypatch.setattr(poke.usage, "refresh_plan_usage_now", lambda aid, cfg: dict(stale))
    monkeypatch.setattr(poke.time, "sleep", lambda s: None)
    poke.poke_account("b", "/Users/x/.claude-b")
    entry = seen["record"][1]
    assert entry["verified"] is False and entry["resets_at"] is None
    assert any("no post-poke reading" in r.message for r in caplog.records)
    assert not any("did not move" in r.message for r in caplog.records)


def test_poke_account_records_even_when_the_subprocess_fails_so_it_does_not_repoke(monkeypatch):
    # A failed spawn re-tried every tick for 90 minutes is a warning storm and,
    # if the failure was transient, a double spend.
    seen = _worker(monkeypatch, resets_at=None, raise_subprocess=True)
    poke.poke_account("b", "/Users/x/.claude-b")
    assert seen["record"][1]["verified"] is False
    assert poke._in_flight == set()


def test_poke_account_warns_on_a_nonzero_exit_but_still_records(monkeypatch, caplog):
    seen = _worker(monkeypatch, resets_at=None, returncode=1)
    poke.poke_account("b", "/Users/x/.claude-b")
    assert any("exited 1" in r.message and r.levelname == "WARNING" for r in caplog.records)
    assert seen["record"][0] == "b"


def test_tick_spawns_one_worker_per_due_account_and_marks_it_in_flight(monkeypatch, clean_state):
    spawned = []
    monkeypatch.setattr(poke, "_bg", lambda name, fn, *a: spawned.append((name, a)))
    monkeypatch.setattr(poke, "_in_flight", set())
    monkeypatch.setattr(poke.store, "get_accounts", lambda: ACCOUNTS)
    monkeypatch.setattr(poke.usage, "cached_plan_usage", lambda: usage())
    monkeypatch.setattr(poke, "_now", lambda: T0)
    poke._tick()
    assert spawned == [("poke:default", ("default", "")), ("poke:b", ("b", "/Users/x/.claude-b"))]
    assert poke._in_flight == {"default", "b"}
    poke._tick()                       # in flight → nothing new
    assert len(spawned) == 2
