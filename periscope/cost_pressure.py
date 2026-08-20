"""Pure decision core for per-pane cost pressure.

No I/O, no threads, no state. Every ambiguous choice in this feature lives in
exactly one function here, which is what makes `usage.py`'s readers thin and
this module testable with zero fixtures. Modelled on `narrator.py`, whose
decision core is pure for the same reason.

The model: a Claude pane re-sends its whole conversation on every API call,
billed at the cache-read rate. A long-running pane therefore overpays on every
future call by an amount set by how far its context has grown past a fresh
start. Clearing costs one cache WRITE of the base context and then makes every
subsequent call cheap, so the question "should this be cleared" reduces to "how
many calls until that write pays for itself".
"""

from dataclasses import dataclass
from typing import Literal

Band = Literal["none", "warn", "hot"]

# Above the measured fresh-session ceiling (p50 50.7k, p95 67.9k, max 71.0k on
# this machine's corpus), so a genuinely fresh session is never clamped, while a
# transcript that inherited history stays an order of magnitude above it.
_BASE_CAP = 80_000

# Measured p50 of payback_calls across sessions active in the last 7 days.
# Warn is deliberately common: most working panes ARE past break-even, and warn
# is a recolour of a number already on screen rather than a new mark.
_PAYBACK_CALLS_WARN = 4

# Hot must stay rare — its remedy (/clear) is disruptive. An earlier value of 5
# put 12 of 15 recently-active panes in hot.
_PAYBACK_MINS_HOT = 2

# A pane with no API call in five minutes is not working, so it gets the parked
# remedy ("write a handoff") rather than the active one ("clear now").
_ACTIVE_WITHIN_S = 300

# Ratios to base input price, identical across every current model — which is
# why this feature is model-independent and never shows dollars.
_CACHE_READ_MULT = 0.1   # re-reading the whole context, once per API call
_CACHE_WRITE_MULT = 2.0  # re-writing base_ctx on the 1h TTL a subscription gets


@dataclass(frozen=True)
class UsageRecord:
    """One real assistant response's usage, already validated."""

    ts: float
    ctx_tokens: int  # input + cache_creation + cache_read


@dataclass(frozen=True)
class TailSummary:
    """What one pass over a transcript tail yields."""

    cur_ctx: int | None  # last record's context, IGNORING the cutoff
    last_ts: float | None
    calls: int  # records at/after the cutoff — API calls, not prompts


@dataclass(frozen=True)
class CostSample:
    """Clock-free measurement for one pane. This is what the cache holds."""

    cur_ctx: int
    base_ctx: int
    pace: float  # calls per minute over the burn window
    last_ts: float


@dataclass(frozen=True)
class Pressure:
    """A scored sample, ready to render."""

    band: Band
    active: bool
    cur_ctx: int
    payback_calls: float
    payback_mins: float | None  # None when pace is zero
