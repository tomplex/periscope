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

import asyncio
import os
import subprocess
import time
from collections.abc import Mapping, Sequence
from collections.abc import Set as AbstractSet
from datetime import datetime

from periscope import config, store, usage
from periscope.launch_policy import AccountUsage
from periscope.log import _bg, log
from periscope.store import Account, PokeEntry, Settings

TICK_S = 60.0
DEFAULT_POKE_AT = "08:00"
DEFAULT_GRACE_MIN = 90
# Full id, not an alias: `claude --help` documents only fable/opus/sonnet as
# aliases, and a rejected alias would fail silently every morning.
_POKE_MODEL = "claude-haiku-4-5"
_VERIFY_TOLERANCE_S = 300
_SESSION_WINDOW_S = 5 * 3600
_SUBPROCESS_TIMEOUT_S = 120
_RETRY_WAIT_S = 15

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
    """Worker-thread body: send the message, force a usage refetch, check the
    reset moved, persist the outcome. Records an entry even when the
    subprocess fails — a skipped entry would re-poke on the next tick for the
    rest of the grace window (a warning storm, or a double spend if the
    failure was transient). Always releases the in-flight slot."""
    at = int(time.time())
    try:
        argv = [config.claude_bin(), "-p", "ok", "--model", _POKE_MODEL,
                # No --mcp-config → zero MCP servers: a one-word prompt must
                # not spin up the channel shim.
                "--strict-mcp-config"]
        try:
            proc = subprocess.run(
                argv, env=config.claude_subprocess_env(config_dir=config_dir),
                # Never periscope's own cwd: from the prod checkout the poke
                # would load that project's CLAUDE.md and project hooks every
                # morning. bg_commander pins its cwd for the same reason.
                cwd=os.path.expanduser("~"),
                capture_output=True, text=True, timeout=_SUBPROCESS_TIMEOUT_S,
            )
            if proc.returncode != 0:
                log.warning("poke %s: claude exited %d: %s", account_id,
                            proc.returncode, (proc.stderr or "")[-300:])
        except (subprocess.SubprocessError, OSError) as e:
            log.warning("poke %s: subprocess failed: %s", account_id, e)
        # refresh_plan_usage_now yields the CACHED payload when a background
        # refresh already holds the slot (and after a failed fetch) — a
        # pre-poke reading would log a false "not anchored" WARNING. Insist on
        # a fetch that postdates the poke; one retry covers a refresh that was
        # mid-flight when we asked.
        payload = _post_poke_usage(account_id, config_dir, at=at)
        session = (payload.get("meters") or {}).get("session") or {}
        resets_at = session.get("resets_at")
        ok = verified(resets_at, at=at)
        dt = datetime.fromtimestamp(at)
        # {} means neither post-poke attempt got a reading that postdates the
        # poke — distinct from a reading that DID land but showed the window
        # still open: only the latter means the anchoring assumption is wrong.
        state = ("anchored" if ok else
                 "no post-poke reading — usage refresh stale/failed" if not payload else
                 "NOT anchored — reset did not move")
        (log.info if ok else log.warning)(
            "poke %s at %s: session resets %s (%s)", account_id,
            dt.strftime("%H:%M"),
            datetime.fromtimestamp(resets_at).strftime("%H:%M") if resets_at else "—",
            state,
        )
        store.record_poke(account_id, {
            "date": dt.strftime("%Y-%m-%d"),
            "at": at, "resets_at": resets_at, "verified": ok,
        })
    finally:
        _in_flight.discard(account_id)


def _post_poke_usage(account_id: str, config_dir: str, *, at: int) -> AccountUsage:
    """The account's usage as fetched AFTER `at`, or {} when two attempts
    could not get one (the verify then records `verified: False` with a
    null reset — honest, and it does not re-poke)."""
    for attempt in range(2):
        payload = usage.refresh_plan_usage_now(account_id, config_dir) or {}
        if (payload.get("fetched_at") or 0) >= at:
            return payload
        if attempt == 0:
            time.sleep(_RETRY_WAIT_S)
    return {}


def _now() -> datetime:
    return datetime.now()


def _tick() -> None:
    for aid in due(now=_now(), settings=store.get_settings(), log=store.get_poke_log(),
                   usage=usage.cached_plan_usage(), in_flight=_in_flight,
                   accounts=store.get_accounts()):
        _in_flight.add(aid)
        try:
            _bg(f"poke:{aid}", poke_account, aid, store.account_config_dir(aid))
        except Exception:
            _in_flight.discard(aid)
            raise


async def run() -> None:
    """Lifespan task (prod only — registered in app.lifespan, cancelled in
    its finally). The tick is cheap and non-blocking: it reads caches and
    hands any real work to a thread."""
    while True:
        try:
            _tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("poke tick failed")
        await asyncio.sleep(TICK_S)
