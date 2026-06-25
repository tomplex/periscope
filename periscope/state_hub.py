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
# Set when an event source (Slice 2) wants the loop to recompute now instead of
# waiting out the rest of its sleep.
_wake = asyncio.Event()


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
    """Request an immediate recompute (event-driven, Slice 2)."""
    _wake.set()


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

    loop = asyncio.get_running_loop()
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
