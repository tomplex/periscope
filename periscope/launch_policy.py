"""Launch policy: which account and model an unnamed Claude launch lands on.

Pure — no I/O, no threads, no clock. `usage.choose_launch` gathers the inputs
(cached plan usage, the two header pins, the account registry) and calls
`choose`; every spawn path goes through that shell so the policy has one home
(docs/account-routing.md).

The objective: at each account's weekly reset, its Fable sub-limit and its
weekly-all meter are both at 100%. Any headroom at reset is waste. So the
account whose week expires soonest is drained first (earliest-deadline-first),
Fable before Opus within it, and an account is skipped only when it literally
cannot work — or, on the 5h session axis only, when it is on pace to wall
before the window resets (the other account's session window costs nothing
to use, so two windows burning in parallel is twice the daily rate).
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import TypedDict


class Meter(TypedDict, total=False):
    """One meter as `usage.parse_plan_usage` + `attach_projections` emit it."""
    label: str
    percent: int
    utilization: float
    resets_at: int | None
    projected_percent: int | None
    projected_recent: int | None
    limit_at: int | None
    hot: bool


class AccountUsage(TypedDict, total=False):
    """One account's entry in `usage.cached_plan_usage()`."""
    available: bool
    meters: dict[str, Meter]
    fetched_at: int


# The model order within an account when nothing pins one: Fable's sub-limit
# is ~half of weekly-all, so the other half of every week is Opus's to burn.
FALLBACK_MODELS: tuple[str, ...] = ("fable", "opus[1m]")

_FAMILIES = ("fable", "opus", "sonnet", "haiku")
_SUFFIX = re.compile(r"\[[^\]]*\]$")
_TOKEN = re.compile(r"[a-z]+")


@dataclass(frozen=True)
class Launch:
    account: str            # account id
    model: str | None       # ANTHROPIC_MODEL value; None = no override
    reason: str             # one line for the header chip's hover


@dataclass(frozen=True)
class LaunchInputs:
    accounts: tuple[str, ...]              # registry order — the tie-break
    usage: Mapping[str, AccountUsage]
    account_arg: str | None                # a call site's explicit account
    model_arg: str | None                  # a call site's explicit model
    account_pin: str | None                # settings.spawn_account
    model_pin: str | None                  # settings.spawn_model, incl. "auto"


def model_family(model: str) -> str | None:
    """'fable' / 'opus' / 'sonnet' / 'haiku' for an alias, a full id, or
    either with a `[1m]` suffix; None when the model names no known family
    (and therefore has no sub-limit meter to check)."""
    base = _SUFFIX.sub("", model).lower()
    for tok in _TOKEN.findall(base):
        if tok in _FAMILIES:
            return tok
    return None


def sublimit(meters: Mapping[str, Meter], model: str) -> Meter | None:
    """The per-model weekly sub-limit meter for `model`, or None.

    Matched by prefix, not exact key: sub-limit keys are slugified display
    names (`usage.parse_plan_usage`), so "Fable" is `week_fable` today and
    "Fable 5.1" would be `week_fable_5_1` tomorrow — an exact lookup would
    silently stop walling that day while the pill kept showing 100%.
    """
    fam = model_family(model)
    if fam is None:
        return None
    exact, prefix = f"week_{fam}", f"week_{fam}_"
    for key, m in meters.items():
        if key == exact or key.startswith(prefix):
            return m
    return None


def _pct(m: Meter | None) -> int:
    return (m or {}).get("percent") or 0


def walled(meters: Mapping[str, Meter], *, model: str | None = None) -> bool:
    """True when a launch on this account cannot work: the 5h session or the
    weekly-all meter is at 100%, or — when a model is named — that model's
    sub-limit is. A missing meter is never a wall."""
    if _pct(meters.get("session")) >= 100 or _pct(meters.get("week_all")) >= 100:
        return True
    if model is None or model == "default":
        return False
    return _pct(sublimit(meters, model)) >= 100


def pressured(meters: Mapping[str, Meter]) -> bool:
    """On pace to wall the 5h session before it resets (`attach_projections`
    sets `limit_at` only when the projected crossing precedes `resets_at`)."""
    return (meters.get("session") or {}).get("limit_at") is not None


def order_accounts(inputs: LaunchInputs) -> tuple[str, ...]:
    raise NotImplementedError  # Task 2


def choose(inputs: LaunchInputs) -> Launch:
    raise NotImplementedError  # Task 2
