"""Central state compute + broadcast — the push side of the dashboard.

One background loop computes the full /api/state blob on the server's own
clock and fans it out to every /ws/state subscriber, replacing N browser tabs
each polling independently. The loop is demand-gated: it computes nothing while
no client is subscribed, so an idle dashboard costs less than the old poll.

Latency note: the steady tick is deliberately modest. Slice 2 kicks an
immediate tick on tmux structural events (window add/close/rename) so changes
surface without raising this rate.
"""

import asyncio
import contextlib
import json
from collections.abc import Callable

from periscope.log import log

# Self-throttled: each cycle computes, then sleeps this long, then repeats — so
# a slow (git/PR in flight) compute never lets ticks pile up.
_TICK_INTERVAL_S = 1.0
# Idle wait between subscriber checks when nobody is watching.
_IDLE_POLL_S = 0.25

_subscribers: set[asyncio.Queue[str]] = set()
# Last computed blob (JSON text), handed to new subscribers immediately so a
# fresh connection paints without waiting up to a full tick.
_last_blob: str | None = None
# Set when an event source wants the loop to recompute now instead of waiting
# out the rest of its sleep.
_wake = asyncio.Event()
# Captured when the loop starts, so kick() can schedule _wake.set() onto the
# loop from any thread (mutation routes run in FastAPI's threadpool).
_loop: asyncio.AbstractEventLoop | None = None


def subscribe() -> asyncio.Queue[str]:
    """Register a client. The returned queue receives each broadcast blob.

    maxsize=1 with drop-oldest on overflow (see _broadcast): a slow client
    never backs up the loop, it just skips stale frames and gets the newest.
    """
    q: asyncio.Queue[str] = asyncio.Queue(maxsize=1)
    _subscribers.add(q)
    if _last_blob is not None:
        q.put_nowait(_last_blob)
    _wake.set()  # wake an idle loop so the first real tick lands promptly
    return q


def unsubscribe(q: asyncio.Queue[str]) -> None:
    _subscribers.discard(q)


def kick() -> None:
    """Request an immediate recompute. Thread-safe: callable from a route
    running in FastAPI's threadpool (e.g. via _tmux_mutate) as well as from
    the loop. A no-op before the loop starts — the next steady tick covers it.
    """
    loop = _loop
    if loop is None or loop.is_closed():
        return
    # Suppress the race where the loop closes between the check and the call —
    # tests tear loops down while _tmux_mutate may still fire from a thread.
    with contextlib.suppress(RuntimeError):
        loop.call_soon_threadsafe(_wake.set)


def _broadcast(text: str) -> None:
    global _last_blob
    _last_blob = text
    for q in list(_subscribers):
        if q.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                q.get_nowait()
        q.put_nowait(text)


async def run() -> None:
    """The broadcast loop. Started once from the lifespan."""
    from periscope.routes.state import build_state

    global _loop
    loop = asyncio.get_running_loop()
    _loop = loop
    try:
        await _run_loop(loop, build_state)
    finally:
        # Don't leave a dangling reference to a loop that's shutting down —
        # a late kick() would otherwise target a closed loop.
        _loop = None


async def _run_loop(
    loop: asyncio.AbstractEventLoop, build_state: Callable[[], dict]
) -> None:
    while True:
        if not _subscribers:
            # Nobody watching: compute nothing. Wait for a subscriber (or kick).
            _wake.clear()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(_wake.wait(), timeout=_IDLE_POLL_S)
            continue
        try:
            blob = await loop.run_in_executor(None, build_state)
            _broadcast(json.dumps(blob))
        except Exception:
            log.warning("state broadcast tick failed", exc_info=True)
        # Sleep the interval, but cut it short if an event kicks us.
        _wake.clear()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(_wake.wait(), timeout=_TICK_INTERVAL_S)
