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

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
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


def parse_usage_record(rec: dict) -> UsageRecord | None:
    """One record from a session JSONL, or None if it is not a real API call.

    Rejecting the right records is the whole job. Claude Code writes an
    `assistant` record with `model: "<synthetic>"` and a fully-populated but
    all-zero usage block for interrupts, API errors and rate-limit messages.
    Unfiltered, those break the signal in both directions, and they land exactly
    when the user is already looking at the dashboard: one real transcript ends
    with a zero record reading "You've reached your Fable 5 limit" immediately
    after a 503,613-token record. Ending on one would report cur_ctx=0 and paint
    a half-million-token pane plain; beginning on one would report base_ctx=0 and
    paint it hot instantly.

    Sidechain records need no filter: on Claude Code 2.1.236 subagent transcripts
    live in a sibling `<session-uuid>/subagents/` directory, and zero of 72,999
    August usage records in main transcripts carry `isSidechain: true`. Re-verify
    if record shapes change.
    """
    if rec.get("type") != "assistant":
        return None
    message = rec.get("message") or {}
    if message.get("model") == "<synthetic>":
        return None
    usage = message.get("usage") or {}
    if not usage:
        return None

    ts_str = rec.get("timestamp")
    if not isinstance(ts_str, str):
        return None
    try:
        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None

    fresh = int(usage.get("input_tokens") or 0)
    written = int(usage.get("cache_creation_input_tokens") or 0)
    read = int(usage.get("cache_read_input_tokens") or 0)
    out = int(usage.get("output_tokens") or 0)
    # An all-zero block is a synthetic placeholder even when the model field
    # doesn't say so. An output-only record is real work and survives.
    if fresh + written + read + out == 0:
        return None

    return UsageRecord(ts=ts, ctx_tokens=fresh + written + read)


def summarize_tail(records: Iterable[UsageRecord], *, cutoff: float) -> TailSummary:
    """Reduce one pass over a transcript tail.

    `cur_ctx` and `last_ts` IGNORE the cutoff; `calls` respects it. That
    asymmetry is the parked case: a pane idle longer than the burn window has no
    records inside it, but its context debt is real and is owed the moment it
    resumes. Filtering everything by the cutoff would silence exactly the panes
    the parked remedy exists for.
    """
    cur_ctx: int | None = None
    last_ts: float | None = None
    calls = 0
    for rec in records:
        cur_ctx = rec.ctx_tokens
        last_ts = rec.ts
        if rec.ts >= cutoff:
            calls += 1
    return TailSummary(cur_ctx=cur_ctx, last_ts=last_ts, calls=calls)


def score(sample: CostSample, *, now: float) -> Pressure | None:
    """Band one pane's measurement against a fresh clock, or None for silence.

    None is the single "no annotation" contract, covering both a base we cannot
    trust (<= 0) and a pane that has not grown past its own fresh start
    (debt <= 0). The caller already handles absence for panes with no session
    and no usage record, so this collapses three silent paths into one.

    Scoring happens here rather than at cache-refresh time because `active` is a
    clock comparison and the cache is up to 60s stale — freezing `active` at
    refresh time would keep showing the active remedy for a minute after a pane
    stopped working.
    """
    base = min(sample.base_ctx, _BASE_CAP)
    if base <= 0:
        return None
    debt = sample.cur_ctx - base
    if debt <= 0:
        return None

    payback_calls = (_CACHE_WRITE_MULT * base) / (_CACHE_READ_MULT * debt)
    payback_mins = payback_calls / sample.pace if sample.pace > 0 else None
    active = (now - sample.last_ts) <= _ACTIVE_WITHIN_S

    band: Band = "none"
    if payback_calls <= _PAYBACK_CALLS_WARN:
        band = "warn"
        if active and payback_mins is not None and payback_mins <= _PAYBACK_MINS_HOT:
            band = "hot"

    return Pressure(
        band=band,
        active=active,
        cur_ctx=sample.cur_ctx,
        payback_calls=payback_calls,
        payback_mins=payback_mins,
    )
