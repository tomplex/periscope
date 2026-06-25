"""state_hub — the central compute + broadcast unit. Covers the kick contract
(must never raise from a thread when the loop is absent/closed — the bug that
broke every _tmux_mutate test) and the subscriber fan-out / drop-oldest."""

import asyncio

import pytest

from periscope import state_hub


@pytest.fixture(autouse=True)
def _reset_hub():
    state_hub._subscribers.clear()
    state_hub._last_blob = None
    state_hub._loop = None
    yield
    state_hub._subscribers.clear()
    state_hub._last_blob = None
    state_hub._loop = None


def test_kick_is_noop_without_loop():
    # _tmux_mutate fires this before the hub ever starts — must not raise.
    state_hub._loop = None
    state_hub.kick()


def test_kick_is_noop_on_closed_loop():
    # A prior test ran the lifespan, set _loop, then the loop closed. A late
    # kick() must swallow it rather than RuntimeError("Event loop is closed").
    loop = asyncio.new_event_loop()
    loop.close()
    state_hub._loop = loop
    state_hub.kick()


def test_subscribe_replays_last_blob():
    state_hub._last_blob = '{"hi":1}'
    q = state_hub.subscribe()
    assert q.get_nowait() == '{"hi":1}'


def test_broadcast_fans_out_and_caches():
    q1 = state_hub.subscribe()
    q2 = state_hub.subscribe()
    state_hub._broadcast("frame-a")
    assert q1.get_nowait() == "frame-a"
    assert q2.get_nowait() == "frame-a"
    assert state_hub._last_blob == "frame-a"


def test_broadcast_drops_oldest_for_slow_subscriber():
    # maxsize=1 + drop-oldest: a subscriber that never drains gets the newest
    # frame, never a stale backlog, and never blocks the loop.
    q = state_hub.subscribe()
    state_hub._broadcast("old")
    state_hub._broadcast("new")
    assert q.get_nowait() == "new"
    assert q.empty()
