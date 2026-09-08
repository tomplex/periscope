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


def _meters(inputs: LaunchInputs, aid: str) -> dict[str, Meter] | None:
    """An account's meters, or None when it has no usable data. No data must
    never read as infinite room, so such an account is not a candidate."""
    payload = inputs.usage.get(aid) or {}
    if not payload.get("available") or not payload.get("meters"):
        return None
    return payload["meters"]


def order_accounts(inputs: LaunchInputs) -> tuple[str, ...]:
    """Candidate accounts, soonest weekly reset first.

    A null `week_all.resets_at` sorts LAST: the endpoint reports null whenever
    utilization is 0, i.e. the account reset most recently and is the later
    deadline. Registry order breaks ties — deterministic, so the value
    published on /api/state and the value a spawn resolves agree.
    """
    rows = []
    for idx, aid in enumerate(inputs.accounts):
        meters = _meters(inputs, aid)
        if meters is None:
            continue
        resets = (meters.get("week_all") or {}).get("resets_at")
        rows.append((resets is None, resets or 0, idx, aid))
    return tuple(aid for *_, aid in sorted(rows))


def _out(model: str | None) -> str | None:
    return None if model in (None, "default") else model


def _clock(ts: int | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%a %H:%M") if ts else "—"


def _reason(aid: str, model: str | None, meters: Mapping[str, Meter],
            skipped: list[str]) -> str:
    resets = (meters.get("week_all") or {}).get("resets_at")
    head = f"{aid} · week resets {_clock(resets)} · {model or 'default'}"
    return head if not skipped else f"{head} (skipped {', '.join(skipped)})"


def choose(inputs: LaunchInputs) -> Launch:
    """Resolve (account, model) for one launch. See the module docstring for
    the policy; docs/account-routing.md for the table this implements."""
    pin = inputs.account_pin if inputs.account_pin in inputs.accounts else None
    explicit_account = inputs.account_arg or pin
    model_pin = inputs.model_pin if inputs.model_pin not in (None, "", "auto") else None
    explicit_model = inputs.model_arg or model_pin
    models = (explicit_model,) if explicit_model else FALLBACK_MODELS

    candidates = (explicit_account,) if explicit_account else order_accounts(inputs)
    with_data = tuple(a for a in candidates if _meters(inputs, a) is not None)
    if not with_data:
        return Launch(explicit_account or "default", _out(explicit_model), "no usage data")

    # An explicitly chosen account is never rerouted by session pressure; an
    # automatic pick honors pressure on the first pass and drops it on the second.
    passes = (True,) if explicit_account else (False, True)
    skipped: list[str] = []
    for ignore_pressure in passes:
        for aid in with_data:
            meters = _meters(inputs, aid) or {}
            if not ignore_pressure and pressured(meters):
                skipped.append(f"{aid}: session on pace to wall")
                continue
            for model in models:
                if walled(meters, model=model):
                    skipped.append(f"{aid}: {model} walled")
                    continue
                return Launch(aid, _out(model), _reason(aid, model, meters, skipped))

    # Everything is walled: the user is blocked either way, so minimize the wait.
    def session_reset(aid: str) -> float:
        m = (_meters(inputs, aid) or {}).get("session") or {}
        return m.get("resets_at") or float("inf")

    aid = min(with_data, key=session_reset)
    soonest = session_reset(aid)
    when = _clock(None if soonest == float("inf") else int(soonest))
    return Launch(aid, _out(models[0]), f"{aid} · every meter walled; session resets {when}")
