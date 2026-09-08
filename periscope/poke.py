# periscope/poke.py
"""Session poke: one Haiku message per account each morning.

The 5h session window is anchored at its first message (prod usage_samples:
a closed window reports resets_at = null, the next window's reset is first
message + 5h). Left alone, the window opens whenever the day's first real
message lands and the reset falls wherever that plus five hours is — often
mid-afternoon, splitting the working day badly. A one-word message at 08:00
opens the window early so it resets ~13:00, four hours on each side. It
cannot touch the weekly meters: those reset on a fixed cadence (B Wed 23:00,
A Sun 10:00) regardless of use — see docs/account-routing.md.

`due` is pure and takes everything it reads; `poke_account` is the worker
thread body; `run` is the prod-only lifespan tick loop (app.lifespan).
"""

from collections.abc import Mapping, Sequence
from collections.abc import Set as AbstractSet
from datetime import datetime

from periscope.launch_policy import AccountUsage
from periscope.store import Account, PokeEntry, Settings

# (Task 5 adds: `import asyncio`, `import subprocess`, `import time`,
# `from periscope import config, store, usage`, `from periscope.log import
# _bg, log`. They are not imported here because ruff F401/F811 would fail the
# pre-commit gate on the Task 4 commit while nothing uses them yet.)

TICK_S = 60.0
DEFAULT_POKE_AT = "08:00"
DEFAULT_GRACE_MIN = 90
# Full id, not an alias: `claude --help` documents only fable/opus/sonnet as
# aliases, and a rejected alias would fail silently every morning.
_POKE_MODEL = "claude-haiku-4-5"
_VERIFY_TOLERANCE_S = 300
_SESSION_WINDOW_S = 5 * 3600
_SUBPROCESS_TIMEOUT_S = 120

# Accounts with a poke thread running. Module state passed INTO `due` (not
# read inside it) so the decision stays free of globals; conftest resets it.
_in_flight: set[str] = set()


def _poke_minute(settings: Settings) -> int | None:
    """Minute-of-day the poke fires, or None when disabled. Unset reads the
    default; the EMPTY STRING disables — `update_settings` pops null keys, so
    null and unset are one state and cannot carry opposite meanings."""
    raw = settings.get("poke_at", DEFAULT_POKE_AT)
    if raw == "":
        return None
    hh, mm = raw.split(":")
    return int(hh) * 60 + int(mm)


def due(*, now: datetime, settings: Settings, log: Mapping[str, PokeEntry],
        usage: Mapping[str, AccountUsage], in_flight: AbstractSet[str],
        accounts: Sequence[Account]) -> list[str]:
    """Account ids to poke at `now` (a naive LOCAL datetime), in registry order.

    Fires inside [poke_at, poke_at + grace) only: a catch-up past the grace
    (Mac asleep at 08:00) would shorten the first work block instead of
    helping it, so the day is skipped. An account is skipped while its 5h
    window is already open (the poke could not move the reset — re-checked
    every tick, the window may close inside the grace), once it is logged for
    today, while a poke thread is in flight, and when it has no usable
    credential (the poke could not authenticate either).
    """
    start = _poke_minute(settings)
    if start is None:
        return []
    grace = int(settings.get("poke_grace_min", DEFAULT_GRACE_MIN))
    minute = now.hour * 60 + now.minute
    if not (start <= minute < start + grace):
        return []
    today = now.strftime("%Y-%m-%d")
    out: list[str] = []
    for a in accounts:
        aid = a.get("id")
        if not aid or aid in in_flight:
            continue
        if (log.get(aid) or {}).get("date") == today:
            continue
        payload = usage.get(aid) or {}
        if not payload.get("available"):
            continue
        session = (payload.get("meters") or {}).get("session") or {}
        resets = session.get("resets_at")
        if resets and resets > now.timestamp():
            continue
        out.append(aid)
    return out


def verified(resets_at: int | None, *, at: float) -> bool:
    """The poke anchored a fresh window: the observed session reset is within
    tolerance of at + 5h. A reset elsewhere means a window was already open
    (or the anchoring assumption is wrong — the log warning is the signal)."""
    return resets_at is not None and abs(resets_at - (at + _SESSION_WINDOW_S)) <= _VERIFY_TOLERANCE_S


def poke_account(account_id: str, config_dir: str) -> None:
    raise NotImplementedError  # Task 5


async def run() -> None:
    raise NotImplementedError  # Task 5
