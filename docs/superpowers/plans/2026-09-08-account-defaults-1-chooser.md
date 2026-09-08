# Account & model defaults — Unit 1: the chooser — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every unnamed Claude launch lands on the account whose weekly reset is soonest (earliest-deadline-first), on Fable before Opus 1M within that account, failing over only when a meter is literally at 100% or the 5h session is on pace to wall; the answer is published on `/api/state` so the header and launcher show and preselect it.

**Architecture:** A new pure module `periscope/launch_policy.py` (frozen inputs → `Launch`) holds the whole decision table; `usage.choose_launch(account, model)` is the six-line impure shell every spawn path calls, replacing `usage.best_account` and `store.spawn_model_env`. `settings.spawn_model` stores `"auto"` / `"default"` / a model id literally (unset = auto). `/api/state.launch_default` carries `{account, model, reason}`; the client's own copy of the rule is deleted.

**Tech Stack:** Python 3.14 / FastAPI / pytest (`uv run pytest`), Preact + `@preact/signals` / vitest (`npm test`), Vite build to the committed `static/dist/app.js`.

**Spec:** `docs/superpowers/specs/2026-09-08-account-defaults-design.md` (D1–D5, §Chooser, §State and UI). **Structure:** `docs/superpowers/specs/2026-09-08-account-defaults-structure.md` (Unit 1).

**Read before starting:** `CLAUDE.md` (commit-as-you-go, tests must not spawn DB-touching threads), `docs/testing.md`, `docs/wrapper-profiles.md` (the model-override paragraph you will rewrite in Task 9).

**Verification commands used throughout:**
- `uv run pytest tests/test_launch_policy.py -q` — the decision table
- `uv run pytest -q` — full suite (~1083 tests; must stay green)
- `bin/check` — ruff + ty + biome, gate is ZERO violations
- `npm test` — vitest (~252 tests)
- `npm run build` — rebuilds `static/dist/app.js` (commit it whenever `static/src/` changes)

---

## File map

| File | Change |
|---|---|
| `periscope/launch_policy.py` | NEW — `Meter`, `AccountUsage`, `Launch`, `LaunchInputs`, `model_family`, `sublimit`, `walled`, `pressured`, `order_accounts`, `choose` |
| `periscope/usage.py` | `choose_launch` shell; delete `best_account`, `random`, `Callable` |
| `periscope/config.py` | `model_env` swallows `"auto"` |
| `periscope/store.py` | delete `spawn_model_env`; `Settings` comments |
| `periscope/routes/settings.py` | `spawn_model` stores `auto`/`default` literally |
| `periscope/routes/state.py` | `launch_default` |
| `periscope/routes/sessions.py` | `window_new` resolves via the chooser (plain + resume); `_window_new_plain` takes resolved values; `pane_move_account` |
| `periscope/open_ops.py` | `ensure_session` |
| `periscope/channels.py` | `_do_spawn_claude_tool`, `_do_resume_session_tool` |
| `periscope/worktree_spawn.py` | `_layout_two_window` model contract |
| `static/src/models.js` | `PIN_MODELS` |
| `static/src/store.js`, `static/src/poll.js` | `launchDefault` signal |
| `static/src/chrome/SpawnModelPicker.jsx`, `SpawnAccountPicker.jsx` | literal values; `auto → X` chip |
| `static/src/chrome/usageSummary.js` | delete `bestAccount` |
| `static/src/overlays/LauncherModal.jsx` | seed from `launchDefault`; send `account` unconditionally; delete `accountQuery` |
| `tests/test_launch_policy.py` | NEW |
| `tests/test_usage.py`, `tests/routes/test_settings.py`, `tests/routes/test_state.py`, `tests/test_open_ops.py`, `tests/test_channels.py`, `tests/conftest.py` | updated |
| `static/src/chrome/__tests__/{usageSummary,spawnModelPickerRender,spawnAccountPickerRender}.test.*`, `static/src/overlays/__tests__/LauncherModal.test.js` | updated |
| `docs/account-routing.md` (NEW), `docs/wrapper-profiles.md`, `CLAUDE.md` | docs |

---

### Task 1: `launch_policy` — types, `model_family`, `sublimit`, `walled`, `pressured`

**Files:**
- Create: `periscope/launch_policy.py`
- Test: `tests/test_launch_policy.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_launch_policy.py
"""The launch policy's decision table. Pure: no fixtures, no I/O.

Every case here is a row of docs/account-routing.md. `acct()` builds one
account's plan-usage payload in the exact shape `usage.parse_plan_usage`
produces and `/api/state` serializes."""

from dataclasses import replace

from periscope.launch_policy import (
    Launch,
    LaunchInputs,
    choose,
    model_family,
    order_accounts,
    pressured,
    sublimit,
    walled,
)

WED = 1_800_000_000          # B's weekly reset
SUN = WED + 4 * 86400        # A's weekly reset, 4 days later


def acct(session=0, week=0, *, resets=None, session_resets=None,
         limit_at=None, **subs):
    """One account's payload. `subs` are sub-limit meters keyed by name
    (week_fable=100 → {"week_fable": {"percent": 100, ...}})."""
    meters = {
        "session": {"percent": session, "resets_at": session_resets,
                    "limit_at": limit_at},
        "week_all": {"percent": week, "resets_at": resets},
    }
    for key, pct in subs.items():
        meters[key] = {"percent": pct, "resets_at": resets}
    return {"available": True, "meters": meters, "fetched_at": 1}


NO_DATA = {"available": False}

BASE = LaunchInputs(
    accounts=("default", "b"),
    usage={"default": acct(resets=SUN), "b": acct(resets=WED)},
    account_arg=None,
    model_arg=None,
    account_pin=None,
    model_pin=None,
)


def pick(**over):
    """(account, model) for BASE with one or more fields overridden."""
    launch = choose(replace(BASE, **over))
    return launch.account, launch.model


# --- model_family / sublimit -------------------------------------------------

def test_model_family_strips_the_context_suffix_and_matches_full_ids():
    assert model_family("fable") == "fable"
    assert model_family("opus[1m]") == "opus"
    assert model_family("claude-opus-5") == "opus"
    assert model_family("claude-haiku-4-5") == "haiku"
    assert model_family("default") is None
    assert model_family("gpt-5") is None


def test_sublimit_matches_the_family_key_and_any_slugified_suffix():
    # Keys are slugified display names (usage.parse_plan_usage): "Fable" →
    # week_fable today, "Fable 5.1" → week_fable_5_1 tomorrow. Both must wall.
    meters = {"week_fable_5_1": {"percent": 100}, "week_all": {"percent": 3}}
    assert sublimit(meters, "fable") == {"percent": 100}
    assert sublimit({"week_fable": {"percent": 7}}, "fable[1m]") == {"percent": 7}
    assert sublimit(meters, "opus[1m]") is None
    assert sublimit(meters, "default") is None


# --- walled / pressured -------------------------------------------------------

def test_walled_is_session_or_week_all_at_100_plus_the_models_sublimit():
    assert not walled(acct()["meters"])
    assert walled(acct(session=100)["meters"])
    assert walled(acct(week=100)["meters"])
    assert not walled(acct(week_fable=100)["meters"])                  # no model asked
    assert walled(acct(week_fable=100)["meters"], model="fable")
    assert not walled(acct(week_fable=100)["meters"], model="opus[1m]")
    assert not walled(acct(week_fable=99)["meters"], model="fable")
    assert not walled(acct()["meters"], model="fable")                 # missing meter ≠ wall


def test_pressured_is_a_projected_session_wall():
    assert pressured(acct(limit_at=WED)["meters"])
    assert not pressured(acct()["meters"])
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_launch_policy.py -q`
Expected: `ImportError` / `ModuleNotFoundError: No module named 'periscope.launch_policy'`

- [ ] **Step 3: Write the module (types + the four bounded functions; `order_accounts` and `choose` are Task 2 — leave them as the stubs shown so the import in the test file resolves)**

```python
# periscope/launch_policy.py
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_launch_policy.py -q`
Expected: `4 passed`

- [ ] **Step 5: Commit**

```bash
git add periscope/launch_policy.py tests/test_launch_policy.py
git commit -m "launch_policy: pure module skeleton — Meter/AccountUsage/Launch/LaunchInputs, model_family, prefix-matched sublimit, walled, pressured"
```

---

### Task 2: `order_accounts` and `choose` — the decision table

**Files:**
- Modify: `periscope/launch_policy.py` (replace the two stubs)
- Test: `tests/test_launch_policy.py`

- [ ] **Step 1: Append the failing tests**

```python
# append to tests/test_launch_policy.py

# --- order_accounts -----------------------------------------------------------

def test_order_is_soonest_weekly_reset_first_null_last_registry_order_on_ties():
    assert order_accounts(BASE) == ("b", "default")
    fresh = replace(BASE, usage={"default": acct(resets=SUN), "b": acct(resets=None)})
    assert order_accounts(fresh) == ("default", "b")      # null = 0% used = reset most recently
    tied = replace(BASE, usage={"default": acct(resets=WED), "b": acct(resets=WED)})
    assert order_accounts(tied) == ("default", "b")
    partial = replace(BASE, usage={"default": NO_DATA, "b": acct(resets=WED)})
    assert order_accounts(partial) == ("b",)


# --- choose: the routing table ------------------------------------------------

def test_soonest_reset_gets_fable():
    assert pick() == ("b", "fable")


def test_session_wall_on_the_soonest_account_fails_over():
    assert pick(usage={"default": acct(resets=SUN), "b": acct(session=100, resets=WED)}) == ("default", "fable")


def test_weekly_wall_on_the_soonest_account_fails_over():
    assert pick(usage={"default": acct(resets=SUN), "b": acct(week=100, resets=WED)}) == ("default", "fable")


def test_fable_wall_means_opus_on_the_SAME_account_not_fable_on_the_other():
    # EDF-strict (D2): the soonest account's weekly-all is the budget expiring
    # first, so its non-Fable half is burned before the other account is touched.
    assert pick(usage={"default": acct(resets=SUN),
                       "b": acct(resets=WED, week_fable=100)}) == ("b", "opus[1m]")


def test_sublimit_matches_a_slugified_suffix():
    assert pick(usage={"default": acct(resets=SUN),
                       "b": acct(resets=WED, week_fable_5_1=100)}) == ("b", "opus[1m]")


def test_both_models_walled_on_the_soonest_account_moves_to_the_other():
    # week_opus is synthetic: no plan on this host has ever reported one
    # (0 of 6,679 samples). The rule is generic; this proves the branch, not the host.
    assert pick(usage={"default": acct(resets=SUN),
                       "b": acct(resets=WED, week_fable=100, week_opus=100)}) == ("default", "fable")


def test_everything_walled_picks_the_soonest_session_reset():
    later, sooner = WED + 7200, WED + 600
    assert pick(usage={"default": acct(week=100, resets=SUN, session_resets=sooner),
                       "b": acct(week=100, resets=WED, session_resets=later)}) == ("default", "fable")


def test_session_pressure_reroutes_new_spawns():
    assert pick(usage={"default": acct(resets=SUN),
                       "b": acct(resets=WED, limit_at=WED)}) == ("default", "fable")


def test_all_pressured_ignores_pressure():
    assert pick(usage={"default": acct(resets=SUN, limit_at=SUN),
                       "b": acct(resets=WED, limit_at=WED)}) == ("b", "fable")


def test_explicit_account_is_never_rerouted_by_pressure():
    assert pick(account_arg="b",
                usage={"default": acct(resets=SUN),
                       "b": acct(resets=WED, limit_at=WED)}) == ("b", "fable")


def test_account_pin_behaves_like_an_explicit_account_and_still_picks_the_model():
    assert pick(account_pin="default",
                usage={"default": acct(resets=SUN, week_fable=100),
                       "b": acct(resets=WED)}) == ("default", "opus[1m]")


def test_a_pin_naming_no_registered_account_is_ignored():
    assert pick(account_pin="gone") == ("b", "fable")


def test_model_pin_routes_by_that_models_sublimit():
    assert pick(model_pin="sonnet",
                usage={"default": acct(resets=SUN),
                       "b": acct(resets=WED, week_sonnet=100)}) == ("default", "sonnet")


def test_model_pin_auto_is_the_same_as_unset():
    assert pick(model_pin="auto") == pick()


def test_explicit_default_model_means_no_override():
    assert pick(model_arg="default") == ("b", None)
    assert pick(model_pin="default") == ("b", None)


def test_explicit_model_arg_beats_the_pin():
    assert pick(model_arg="sonnet", model_pin="fable") == ("b", "sonnet")


def test_no_usage_data_falls_back_to_default_without_waiting():
    assert pick(usage={"default": NO_DATA, "b": NO_DATA}) == ("default", None)
    assert pick(usage={}) == ("default", None)
    assert pick(usage={}, model_pin="fable") == ("default", "fable")
    # An explicit account is honored even blind — the caller asked for it.
    assert pick(usage={}, account_arg="b") == ("b", None)


def test_reason_names_the_account_and_the_model():
    launch = choose(BASE)
    assert launch == Launch("b", "fable", launch.reason)
    assert launch.reason.startswith("b · week resets ")
    assert launch.reason.endswith(" · fable")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_launch_policy.py -q`
Expected: the new cases fail with `NotImplementedError`; the Task 1 cases still pass.

- [ ] **Step 3: Replace the two stubs**

```python
# periscope/launch_policy.py — replace the two NotImplementedError stubs with:

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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_launch_policy.py -q`
Expected: `23 passed`

- [ ] **Step 5: Lint and type-check**

Run: `bin/check`
Expected: zero violations. If `ty` complains about the `Meter` TypedDict access patterns, the fix is annotating `_meters`' return as `Mapping[str, Meter] | None`, not a cast.

- [ ] **Step 6: Commit**

```bash
git add periscope/launch_policy.py tests/test_launch_policy.py
git commit -m "launch_policy: order_accounts (EDF, null last, registry tie-break) + choose (two passes over (account, model), explicit account ignores pressure, all-walled → soonest session reset)"
```

---

### Task 3: `usage.choose_launch` shell; delete `best_account`

**Files:**
- Modify: `periscope/usage.py:17-23` (imports), `:148` (`_plan_cache`), `:223-238` (`parse_plan_usage`), `:372` (`fetch_plan_usage`), `:431-460` (`cached_plan_usage`), `:629-668` (delete `best_account`)
- Test: `tests/test_usage.py:481-550`

- [ ] **Step 1: Replace the `best_account` tests**

Delete everything from the line `# --- best_account: which subscription a new pane should land on ---` (line 481) through the end of `test_best_account_breaks_ties_randomly` (line 549), and put this in its place:

```python
# --- choose_launch: the impure shell over launch_policy.choose ----------------

from periscope import store, usage
from periscope.launch_policy import Launch


def _plan(**pcts):
    """account id -> plan payload; None means 'no usable data'. Session-only
    meters: enough for the shell tests, which check plumbing, not policy."""
    return {
        aid: ({"available": False} if p is None else
              {"available": True, "meters": {"session": {"percent": p}}, "fetched_at": 1})
        for aid, p in pcts.items()
    }


def test_choose_launch_feeds_the_registry_pins_and_cache_to_the_policy(monkeypatch, clean_state):
    monkeypatch.setattr(usage, "cached_plan_usage", lambda: _plan(default=100, b=8))
    launch = usage.choose_launch()
    assert isinstance(launch, Launch)
    assert launch.account == "b"          # default is session-walled


def test_choose_launch_honors_the_account_pin(monkeypatch, clean_state):
    store.update_settings({"spawn_account": "default"})
    monkeypatch.setattr(usage, "cached_plan_usage", lambda: _plan(default=100, b=8))
    assert usage.choose_launch().account == "default"


def test_choose_launch_ignores_a_pin_naming_no_registered_account(monkeypatch, clean_state):
    store.update_settings({"spawn_account": "gone"})
    monkeypatch.setattr(usage, "cached_plan_usage", lambda: _plan(default=100, b=8))
    assert usage.choose_launch().account == "b"


def test_choose_launch_passes_explicit_args_through(monkeypatch, clean_state):
    monkeypatch.setattr(usage, "cached_plan_usage", lambda: _plan(default=100, b=8))
    launch = usage.choose_launch("default", "sonnet")
    assert (launch.account, launch.model) == ("default", "sonnet")


def test_choose_launch_falls_back_to_default_when_nothing_is_known(monkeypatch, clean_state):
    monkeypatch.setattr(usage, "cached_plan_usage", lambda: _plan(default=None, b=None))
    assert usage.choose_launch().account == "default"
    monkeypatch.setattr(usage, "cached_plan_usage", dict)
    assert usage.choose_launch().account == "default"
```

(`store` was previously imported only *inside* the deleted `best_account` tests, so the module-level import above is new.)

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_usage.py -q -k choose_launch`
Expected: `AttributeError: module 'periscope.usage' has no attribute 'choose_launch'`

- [ ] **Step 3: Write the shell and delete `best_account`**

In `periscope/usage.py`:

Remove these two import lines:
```python
import random
from collections.abc import Callable
```

Add after `from periscope import activity, store`:
```python
from periscope import launch_policy
from periscope.launch_policy import AccountUsage, Launch, Meter
```

Thread the `AccountUsage` type through the plan-usage path — `ty` rejects the bare `-> AccountUsage` annotation alone because `meters` is declared `dict[str, dict]` (verified: four `invalid-return-type` / `invalid-argument-type` errors otherwise). Five edits, all annotations, no behavior change:

- line ~148: `_plan_cache: dict[str, tuple[float, AccountUsage | None]] = {}`
- `parse_plan_usage`: signature `def parse_plan_usage(data: dict) -> AccountUsage:` and, inside it, `meters: dict[str, Meter] = {}`
- `fetch_plan_usage`: `-> AccountUsage | None`
- `cached_plan_usage`: `-> dict[str, AccountUsage]` and `out: dict[str, AccountUsage] = {}`

After these, `uv run ty check` must report "All checks passed" — run it before Step 4.

Delete the whole `best_account` function (from `def best_account(` through `return best[int(rand() * len(best))]`) and put this in its place:

```python
def choose_launch(account: str | None = None, model: str | None = None) -> Launch:
    """The account and model a new pane lands on. The one choke point every
    unnamed spawn path shares — launcher New Tab, unified open, MCP
    spawn_claude / resume_session — so the header pins are honored
    server-side and MCP spawns see them without any client pref.

    Explicit args win over the pins (an explicit "default" model means no
    override — that is how a single launch opts out of a model pin). The
    policy itself is `launch_policy.choose`; this gathers its inputs.
    `cached_plan_usage` never blocks, so a launch never waits on usage.
    """
    settings = store.get_settings()
    return launch_policy.choose(launch_policy.LaunchInputs(
        accounts=tuple(a["id"] for a in store.get_accounts() if a.get("id")),
        usage=cached_plan_usage(),
        account_arg=account,
        model_arg=model,
        account_pin=settings.get("spawn_account"),
        model_pin=settings.get("spawn_model"),
    ))
```

- [ ] **Step 4: Run the usage tests**

Run: `uv run pytest tests/test_usage.py tests/test_launch_policy.py -q`
Expected: all pass. (Other test modules that patch `usage.best_account` now fail — Tasks 6 and 7 fix them; do not run the full suite yet.)

- [ ] **Step 5: Commit**

```bash
git add periscope/usage.py tests/test_usage.py
git commit -m "usage: choose_launch shell over launch_policy replaces best_account; random tie-break gone (published value and spawn must agree)"
```

---

### Task 4: `spawn_model` stores `auto`/`default` literally; `model_env` swallows `auto`; delete `store.spawn_model_env`

**Files:**
- Modify: `periscope/config.py:84-98` (`model_env`), `periscope/store.py:89-91` (`Settings` comments), `:355-368` (delete `spawn_model_env`), `periscope/routes/settings.py:95-107`, `tests/conftest.py:198-201` (docstring)
- Test: `tests/routes/test_settings.py:111-131`, `tests/test_config.py` (append)

- [ ] **Step 1: Write the failing tests**

Replace `test_patch_spawn_model_default_and_null_clear` in `tests/routes/test_settings.py` with:

```python
def test_patch_spawn_model_stores_auto_and_default_literally_and_null_clears(client, mocker):
    # "default" (no override) and "auto" (chooser decides) are now different
    # launches; coercing either to unset would silently turn one into the other.
    update_spy = mocker.patch("periscope.routes.settings.update_settings")
    mocker.patch("periscope.routes.settings.get_settings", return_value={})
    assert client.patch("/api/settings", json={"spawn_model": "default"}).status_code == 200
    assert client.patch("/api/settings", json={"spawn_model": "auto"}).status_code == 200
    assert client.patch("/api/settings", json={"spawn_model": None}).status_code == 200
    assert [c.args[0] for c in update_spy.call_args_list] == [
        {"spawn_model": "default"}, {"spawn_model": "auto"}, {"spawn_model": None},
    ]
```

Append to `tests/test_config.py` (it exists; add the `config` import only if the file does not already import it — check its first 20 lines):

```python
from periscope import config


def test_model_env_swallows_auto_like_default():
    # "auto" passes the character-set check, so without this a raw pin that
    # reached model_env would launch a pane with ANTHROPIC_MODEL=auto.
    assert config.model_env("auto") == ""
    assert config.model_env("default") == ""
    assert config.model_env("fable") == "fable"
    assert config.model_env("opus[1m]") == "opus[1m]"
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/routes/test_settings.py tests/test_config.py -q -k "spawn_model or model_env"`
Expected: the settings test fails on the call list (`"default"` was coerced to `None`); the config test fails with `assert 'auto' == ''`.

- [ ] **Step 3: Implement**

`periscope/config.py` — in `model_env`, change the guard line to:
```python
    if not m or m in ("default", "auto") or not _MODEL_OK.match(m):
        return ""
```
and add one sentence to the docstring after "...like `profile_env`.": `"auto" is the header pin's "chooser decides" value; it is resolved by launch_policy before any launch and must never reach the env.`

`periscope/routes/settings.py` — replace the `spawn_model` block with:
```python
    if "spawn_model" in sent:
        v = body.spawn_model
        # Character-set check only (config.model_env): Claude accepts aliases
        # and full ids alike and the list moves, but a value that fails the
        # check would fail OPEN to no override at every spawn — reject it here
        # where the user can see it. "auto" (launch_policy decides) and
        # "default" (no override) are stored LITERALLY: they are different
        # launches, and unset reads as "auto".
        if v is None or v in ("auto", "default") or config.model_env(v):
            patch["spawn_model"] = v
        else:
            raise HTTPException(400, f"spawn_model {v!r} is not a model id")
```

`periscope/store.py` — delete `spawn_model_env` entirely (the `def` through its `return config.model_env(get_settings().get("spawn_model"))`). The `from periscope import config` import stays: `config.DEV` / `config.config_dir()` are still used at lines ~129–131. Change the two `Settings` comments:
```python
    spawn_account: str  # account id every unnamed spawn lands on; unset => launch_policy (earliest weekly reset)
    spawn_model: str  # "auto" | "default" | ANTHROPIC_MODEL value; unset => "auto" (launch_policy: fable, then opus[1m])
```

`tests/conftest.py:198` — the `clean_state` docstring's sentence `store.spawn_model_env(None)` consults `settings.spawn_model` becomes `usage.choose_launch()` consults `settings.spawn_model` (docstring only).

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/routes/test_settings.py tests/test_config.py tests/test_usage.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add periscope/config.py periscope/store.py periscope/routes/settings.py tests/routes/test_settings.py tests/test_config.py tests/conftest.py
git commit -m "spawn_model: auto/default stored literally (unset = auto), model_env swallows auto, store.spawn_model_env deleted"
```

---

### Task 5: Route every spawn and resume path through `choose_launch`

**Files:**
- Modify: `periscope/open_ops.py:199-207`, `periscope/channels.py:35,596-602,911-916`, `periscope/routes/sessions.py:288-293,498-505,540-544`, `periscope/worktree_spawn.py:29,261-265`
- Test: `tests/test_open_ops.py:393-414`, `tests/test_channels.py:851-861,1543-1547,1577-1583,1591-1597`, `tests/routes/test_sessions.py` (after line 84)

- [ ] **Step 1: Update the existing tests that stub `best_account`**

`tests/test_open_ops.py` — in the three tests at lines ~393, ~401, ~410 replace
```python
    monkeypatch.setattr(usage, "best_account", lambda: "b")
```
with
```python
    from periscope.launch_policy import Launch
    monkeypatch.setattr(usage, "choose_launch",
                        lambda account=None, model=None: Launch(account or "b", None, ""))
```
(The second test passes `account="default"` and asserts `seen["account"] == "default"`; the stub echoes an explicit account, so it still holds.)

`tests/test_channels.py` — replace each `mocker.patch.object(usage, "best_account", return_value="b")` (lines ~1545, ~1579, ~1593) with
```python
    from periscope.launch_policy import Launch
    mocker.patch.object(usage, "choose_launch",
                        side_effect=lambda account=None, model=None: Launch(account or "b", None, ""))
```
and the one at ~857 (`return_value="default"`) with
```python
    from periscope.launch_policy import Launch
    mocker.patch.object(usage, "choose_launch",
                        side_effect=lambda account=None, model=None: Launch(account or "default", None, ""))
```

Add one new test at the end of `tests/test_channels.py`:

```python
def test_spawn_claude_sets_the_model_the_chooser_picked(mocker):
    from periscope import channels, usage
    from periscope.launch_policy import Launch
    cap = _mock_spawn_plumbing(mocker)
    mocker.patch.object(usage, "choose_launch",
                        return_value=Launch("b", "opus[1m]", "b · fable walled"))

    asyncio.run(channels._do_spawn_claude_tool("%1", {"prompt": "go"}))

    args = _created_call(cap)
    assert "ANTHROPIC_MODEL=opus[1m]" in args
```

(`_mock_spawn_plumbing` and `_created_call` already exist in that file; `_created_call` (line ~781) returns the tmux argv tuple of the `new-window` / `new-session` call, so a `-e KEY=VALUE` binding appears as the literal element `"ANTHROPIC_MODEL=opus[1m]"` — the same form `test_spawn_claude_account_sets_config_dir_env` asserts for `CLAUDE_CONFIG_DIR`.)

Add one new test to `tests/routes/test_sessions.py` after `test_window_new_resume_unknown_session_id` — the dashboard resume path today passes no account at all, and nothing would go red if a later edit dropped the kwarg again:

```python
def test_window_new_resume_routes_the_account_through_the_chooser(client, mocker):
    from periscope.launch_policy import Launch
    resume = _patch(mocker, "_window_new_resume",
                    return_value={"ok": True, "session": "resumes", "index": 3,
                                  "target": "resumes:3", "mode": "resume",
                                  "resumed_session_id": "abc"})
    mocker.patch("periscope.usage.choose_launch",
                 side_effect=lambda account=None, model=None: Launch(account or "b", None, ""))
    r = client.post("/api/window/new?session=resumes&mode=resume&resume_id=abc")
    assert r.status_code == 200
    assert resume.call_args.kwargs.get("account") == "b"        # unnamed → the chooser's pick
    client.post("/api/window/new?session=resumes&mode=resume&resume_id=abc&account=default")
    assert resume.call_args.kwargs.get("account") == "default"  # explicit passes through
```

(`sessions.py` calls `usage.choose_launch` through the module attribute, so patching `periscope.usage.choose_launch` takes effect.)

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_open_ops.py tests/test_channels.py -q -k "account or model"`
Expected: failures — the code still calls `usage.best_account` / `store.spawn_model_env`, which no longer exist.

- [ ] **Step 3: `open_ops.ensure_session`**

Replace the `if agent == "claude":` branch body with:
```python
    if agent == "claude":
        # No account named → launch_policy decides (earliest weekly reset
        # first, Fable before Opus). This is periscope's primary launch path
        # (⌘K omnibox, POST /api/open, PR review); pinning it to the default
        # account would send every open to the subscription that fills up
        # first. Codex is excluded below: it has no Claude subscription to
        # choose between. choose_launch degrades to "default" when usage is
        # unknown, so an open never waits on it.
        launch = usage.choose_launch(account)
        agent_pid, _ = _layout_two_window(
            session, pinned_dir, account=launch.account, model=launch.model
        )
```

- [ ] **Step 4: `channels`**

Line 35: `from periscope import store, tracks, usage` → `from periscope import config, store, tracks, usage`.

In `_do_spawn_claude_tool`, replace
```python
    config_dir = store.account_config_dir(
        arguments.get("account") or usage.best_account()
    )
    model_env = store.spawn_model_env(arguments.get("model"))
```
with
```python
    launch = usage.choose_launch(arguments.get("account"), arguments.get("model"))
    config_dir = store.account_config_dir(launch.account)
    model_env = config.model_env(launch.model)
```
and edit the comment above it: `best_account degrades to "default"` → `choose_launch degrades to "default"`.

In `_do_resume_session_tool`, replace
```python
            account=arguments.get("account") or usage.best_account(),
```
with
```python
            # Account only, never a model: --resume restores the session's own
            # model unless ANTHROPIC_MODEL is set at launch.
            account=usage.choose_launch(arguments.get("account")).account,
```

- [ ] **Step 5: `routes/sessions`**

Add `usage` to the `from periscope import (...)` block (alphabetical, after `tracks`).

In `window_new`, replace
```python
    if mode == "resume":
        result = _window_new_resume(session, exec_cmd, resume_id, mode)
        return {**result, "agent": "claude"}
    return _window_new_plain(
        session, exec_cmd, mode, cwd_param=cwd, branch=branch, agent=agent,
        account=account, profile=profile, model=model,
    )
```
with
```python
    if mode == "resume":
        # The dashboard's own resume button. Account only (a resumed session
        # keeps its own model); before this it passed nothing and billed the
        # default account regardless of headroom.
        result = _window_new_resume(
            session, exec_cmd, resume_id, mode,
            account=usage.choose_launch(account).account,
        )
        return {**result, "agent": "claude"}
    if agent == "claude":
        launch = usage.choose_launch(account, model)
        account, model = launch.account, launch.model
    return _window_new_plain(
        session, exec_cmd, mode, cwd_param=cwd, branch=branch, agent=agent,
        account=account, profile=profile, model=model,
    )
```

In `_window_new_plain`, replace `model_env = store.spawn_model_env(model)` with `model_env = config.model_env(model)` and add to its docstring, after the cwd precedence list: `` `account` and `model` arrive RESOLVED (window_new ran them through `usage.choose_launch`); `model` is the ANTHROPIC_MODEL value or None for no override. ``

In `pane_move_account`, replace `"resume", account=account,` with `"resume", account=usage.choose_launch(account).account,` (the id is already validated against the registry above; routing it through the chooser keeps one function owning the mapping).

- [ ] **Step 6: `worktree_spawn._layout_two_window`**

Line 29: `from periscope import store, worktrees` → `from periscope import config, store, worktrees`.

Replace `model_env = store.spawn_model_env(model) if agent == "claude" else ""` with `model_env = config.model_env(model) if agent == "claude" else ""`, and add to the docstring after the `account` paragraph: `` `model` is the already-resolved ANTHROPIC_MODEL value (or None): callers run `usage.choose_launch` first — this layout primitive applies no pin of its own. ``

- [ ] **Step 7: Run the affected suites, then the whole suite**

Run: `uv run pytest tests/test_open_ops.py tests/test_channels.py tests/routes/test_sessions.py tests/test_worktree_spawn.py -q`
Expected: all pass.

Run: `uv run pytest -q`
Expected: all pass (~1083 + the new ones). Any remaining `best_account` / `spawn_model_env` reference shows up here — `grep -rn "best_account\|spawn_model_env" periscope tests` must print nothing except the `conftest.py` docstring mention updated in Task 4.

Run: `bin/check`
Expected: zero violations.

- [ ] **Step 8: Commit**

```bash
git add periscope/open_ops.py periscope/channels.py periscope/routes/sessions.py periscope/worktree_spawn.py tests/test_open_ops.py tests/test_channels.py tests/routes/test_sessions.py
git commit -m "spawn paths: every launch and resume resolves through usage.choose_launch — dashboard resume now picks an account instead of billing the default"
```

---

### Task 6: `launch_default` on `/api/state`

**Files:**
- Modify: `periscope/routes/state.py:9,28-32,171-174`
- Test: `tests/routes/test_state.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/routes/test_state.py`:

```python
def test_state_publishes_the_chooser_answer(client, mocker, clean_state):
    # The header chip and the launcher preselect read this; it must be the
    # SAME function a spawn resolves through, so the two never disagree.
    _patch(mocker, "list_windows", return_value=[])
    _patch(mocker, "update_focus_from_windows")
    _patch(mocker, "_attach_git_then_resolve_pids")
    _patch(mocker, "cached_claude_usage", return_value={})
    _patch(mocker, "cached_plan_usage", return_value={})
    body = client.get("/api/state").json()
    # clean_state + the autouse no-refresh guard: no usage known → default, no override.
    assert body["launch_default"] == {"account": "default", "model": None, "reason": "no usage data"}
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/routes/test_state.py -q -k chooser`
Expected: `KeyError: 'launch_default'`

- [ ] **Step 3: Implement**

`periscope/routes/state.py`: add `import dataclasses` above `import time`; add `choose_launch,` to the `from periscope.usage import (...)` list (alphabetical: `annotate_cost_pressure, cached_claude_usage, cached_plan_usage, choose_launch`); and in `build_state`'s dict, directly below the `"spawn_model"` line:

```python
        # The chooser's current answer — what an unnamed spawn would get right
        # now. Computed from the cache (never blocks) on every poll so the
        # header chip and the launcher's preselect track the meters.
        "launch_default": dataclasses.asdict(choose_launch()),
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/routes/test_state.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add periscope/routes/state.py tests/routes/test_state.py
git commit -m "state: publish launch_default {account, model, reason} on every poll"
```

---

### Task 7: Frontend — `launchDefault` signal, `PIN_MODELS`, the two pickers

**Files:**
- Modify: `static/src/models.js`, `static/src/store.js:26-27`, `static/src/poll.js:16-17,58-59`, `static/src/chrome/SpawnModelPicker.jsx`, `static/src/chrome/SpawnAccountPicker.jsx`
- Test: `static/src/chrome/__tests__/spawnModelPickerRender.test.jsx`, `static/src/chrome/__tests__/spawnAccountPickerRender.test.jsx`

- [ ] **Step 1: Update the render tests to the new contract**

`spawnModelPickerRender.test.jsx` — replace the whole file body below the imports:

```jsx
import render from "preact-render-to-string";
import { afterEach, describe, expect, it } from "vitest";
import { PIN_MODELS } from "../../models.js";
import { launchDefault, spawnModel } from "../../store.js";
import { SpawnModelPicker } from "../SpawnModelPicker.jsx";

afterEach(() => {
  spawnModel.value = null;
  launchDefault.value = null;
});

function activeLabel(html) {
  const m = html.match(/class="spawn-acct-btn is-active"[^>]*>([^<]+)</);
  return m?.[1] ?? null;
}

describe("<SpawnModelPicker>", () => {
  it("marks auto active when no pin is set and shows what auto resolves to", () => {
    spawnModel.value = null;
    launchDefault.value = { account: "b", model: "fable", reason: "b · week resets Wed 22:59 · fable" };
    expect(activeLabel(render(<SpawnModelPicker />))).toBe("auto → fable");
  });

  it("reads auto → default before the first poll", () => {
    expect(activeLabel(render(<SpawnModelPicker />))).toBe("auto → default");
  });

  it("marks a stored auto pin active exactly like unset", () => {
    spawnModel.value = "auto";
    expect(activeLabel(render(<SpawnModelPicker />))).toBe("auto → default");
  });

  it("marks the pinned alias active, and only it", () => {
    spawnModel.value = "opus[1m]";
    const html = render(<SpawnModelPicker />);
    expect(activeLabel(html)).toBe("opus 1m");
    expect(html.match(/is-active/g)).toHaveLength(1);
  });

  it("marks a literal default pin active", () => {
    spawnModel.value = "default";
    expect(activeLabel(render(<SpawnModelPicker />))).toBe("default");
  });

  it("renders one chip per pin entry", () => {
    const html = render(<SpawnModelPicker />);
    expect(html.match(/spawn-acct-btn/g).length).toBe(PIN_MODELS.length);
  });
});
```

`spawnAccountPickerRender.test.jsx` — change the imports to `import { launchDefault, spawnAccount } from "../../store.js";`, add `launchDefault.value = null;` to `afterEach`, and replace the first `it` with:

```jsx
  it("marks auto active when no pin is set and shows the account auto resolves to", () => {
    spawnAccount.value = null;
    launchDefault.value = { account: "b", model: "fable", reason: "b · week resets Wed 22:59 · fable" };
    expect(activeLabel(render(<SpawnAccountPicker />))).toBe("auto → B");
  });

  it("reads plain auto before the first poll", () => {
    expect(activeLabel(render(<SpawnAccountPicker />))).toBe("auto");
  });
```

- [ ] **Step 2: Run to verify they fail**

Run: `npm test -- spawnModelPickerRender spawnAccountPickerRender`
Expected: failures — `PIN_MODELS` / `launchDefault` are not exported.

- [ ] **Step 3: `models.js`, `store.js`, `poll.js`**

`static/src/models.js` — append:
```js
// The header pin's list: "auto" first — launch_policy picks fable, then
// opus[1m], per account — then the same aliases the launcher offers. Two
// named lists rather than one plus a filter: the launcher never offers auto
// (it preselects the resolved answer and sends it explicitly).
export const PIN_MODELS = [{ id: "auto", label: "auto" }, ...MODELS];
```

`static/src/store.js` — below the `spawnModel` line:
```js
export const launchDefault = signal(null);    // /api/state launch_default: { account, model, reason } — what an unnamed spawn gets right now
```
and change the two comments above it to `null = auto (launch_policy)` / `null = auto (launch_policy)`.

`static/src/poll.js` — add `launchDefault,` to the store import list, and below `spawnModel.value = data.spawn_model || null;`:
```js
  launchDefault.value = data.launch_default || null;
```

- [ ] **Step 4: `SpawnModelPicker.jsx`**

Replace the file with:

```jsx
// Header segmented control pinning the model new Claude panes launch on.
// "auto" (unset) leaves it to launch_policy — fable, then opus[1m], within
// whichever account the policy picked; "default" leaves it to the account's
// settings.json; an alias pins EVERY unnamed spawn path — launcher New Tab,
// unified open, MCP spawn_claude — because the pin is honored server-side in
// usage.choose_launch, the choke point they all share. The launcher's
// per-launch picker seeds from the resolved answer and still wins for one
// launch (it sends its value explicitly).
//
// A server setting (settings.spawn_model), not a client pref, for the account
// pin's reason: MCP spawns never see client prefs. Rides /api/state as
// `spawn_model` (the raw pin, "auto"/"default"/alias, null = auto) beside
// `launch_default` (what auto resolves to), so this writes optimistically
// and lets the poll confirm. Reuses the account picker's classes — same
// chrome, same row.
import { PIN_MODELS } from "../models.js";
import { launchDefault, spawnModel } from "../store.js";
import { apiCall } from "../util.js";

async function pick(id) {
  const prev = spawnModel.value;
  spawnModel.value = id; // optimistic; the poll carries the persisted value
  const res = await apiCall("spawn model", "/api/settings", {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ spawn_model: id }),
  });
  if (!res) spawnModel.value = prev;
}

export function SpawnModelPicker() {
  const cur = spawnModel.value || "auto";
  const auto = launchDefault.value;
  return (
    <div
      class="spawn-acct spawn-model"
      title={
        "which model new Claude panes launch on\n" +
        "auto — fable until its weekly sub-limit walls, then opus 1m (per account)\n" +
        "default — whatever the account's settings.json picks\n" +
        "fable / opus 1m / sonnet — pin every spawn (New Tab, + new, spawned workers); the launcher can still override one launch"
      }
    >
      <span class="spawn-acct-label">model</span>
      {PIN_MODELS.map((m) => (
        <button
          type="button"
          key={m.id}
          class={`spawn-acct-btn${cur === m.id ? " is-active" : ""}`}
          title={m.id === "auto" ? auto?.reason : undefined}
          onClick={() => pick(m.id)}
        >
          {m.id === "auto" && cur === "auto" ? `auto → ${auto?.model ?? "default"}` : m.label}
        </button>
      ))}
    </div>
  );
}
```

- [ ] **Step 5: `SpawnAccountPicker.jsx`**

Change the imports to:
```jsx
import { ACCOUNTS, accountLabel } from "../accounts.js";
import { launchDefault, spawnAccount } from "../store.js";
```
Update the header comment's second line from `"auto" (the default) keeps best-headroom routing;` to `"auto" (the default) leaves it to launch_policy — earliest weekly reset first;` and `inside usage.best_account()` to `inside usage.choose_launch()`.

In the component, add `const auto = launchDefault.value;` after `const cur = ...`, change the tooltip line `"auto — the account with the most headroom right now\n"` to `"auto — the account whose weekly reset is soonest (drain it before it resets)\n"`, and replace the button with:
```jsx
        <button
          type="button"
          key={c.id ?? "auto"}
          class={`spawn-acct-btn${cur === c.id ? " is-active" : ""}`}
          title={c.id === null ? auto?.reason : undefined}
          onClick={() => pick(c.id)}
        >
          {c.id === null && cur === null && auto ? `auto → ${accountLabel(auto.account)}` : c.label}
        </button>
```

- [ ] **Step 6: Run the tests**

Run: `npm test -- spawnModelPickerRender spawnAccountPickerRender`
Expected: all pass.

- [ ] **Step 7: Commit (source only; `dist` is rebuilt in Task 8)**

```bash
git add static/src/models.js static/src/store.js static/src/poll.js static/src/chrome/SpawnModelPicker.jsx static/src/chrome/SpawnAccountPicker.jsx static/src/chrome/__tests__/spawnModelPickerRender.test.jsx static/src/chrome/__tests__/spawnAccountPickerRender.test.jsx
git commit -m "header pins: launchDefault signal, PIN_MODELS with auto, chips read 'auto → fable' / 'auto → B' with the chooser's reason on hover"
```

---

### Task 8: Launcher seeds from `launchDefault`, sends `account` unconditionally; delete `bestAccount`; rebuild dist

**Files:**
- Modify: `static/src/overlays/LauncherModal.jsx:32,38,66-73,220-226,285-286`, `static/src/chrome/usageSummary.js:73-89`
- Test: `static/src/overlays/__tests__/LauncherModal.test.js:2,8-17`, `static/src/chrome/__tests__/usageSummary.test.js:2,82-113`

- [ ] **Step 1: Update the tests**

`static/src/overlays/__tests__/LauncherModal.test.js` — change line 2 to `import { sendsAccount } from "../LauncherModal.jsx";` (drop `accountQuery`) and delete the whole `describe("accountQuery", ...)` block (lines 8–17).

`static/src/chrome/__tests__/usageSummary.test.js` — change line 2 to `import { STALE_AFTER_S, summarizeAccounts } from "../usageSummary.js";` and delete the whole `describe("bestAccount", ...)` block (lines 82–113).

- [ ] **Step 2: Run to verify the suite still fails for the right reason**

Run: `npm test`
Expected: the two edited files pass; nothing else references `accountQuery` / `bestAccount` in tests. (The source still exports both — this step only proves the tests no longer depend on them.)

- [ ] **Step 3: `usageSummary.js`**

Delete `bestAccount` — the doc comment block starting `/** The account with the most headroom, or null...` through the function's closing `}` (lines 73–89). `summarizeAccounts` and everything above stay.

- [ ] **Step 4: `LauncherModal.jsx`**

Delete line 32 (`import { bestAccount } from "../chrome/usageSummary.js";`).

Line 38: `import { spawnAccount, spawnModel, tracks, usage, windows } from "../store.js";` → `import { launchDefault, tracks, windows } from "../store.js";`

Delete `accountQuery` and its comment (lines 66–73, from `// account id → the ` through the function's `}`).

Replace the seed block in `openLauncher` (the comment beginning `// Preselect the header's pinned spawn account` through `model.value = spawnModel.value || "default";`) with:
```js
  // Preselect what an unnamed launch would get right now — the server's
  // launch_default, re-read on every open rather than remembered: the answer
  // changes as meters burn down and weeks reset, and a stale sticky value
  // would keep routing work at an account that walled since. Pins are already
  // folded in server-side. Falls back to the default account / no override
  // before the first poll. Clicking another chip here still wins for this
  // launch: both values are sent explicitly.
  account.value = launchDefault.value?.account || "default";
  model.value = launchDefault.value?.model || "default";
```

In `run(t)`, replace
```js
    const acct = sendsAccount(t) ? accountQuery(account.value) : null;
    if (acct) qs.set("account", acct);
```
with
```js
    // Always explicit, "default" included, like `model` below: the server
    // re-runs the chooser when the param is ABSENT, so omitting it for
    // account A (as accountQuery used to) let a launch the user pointed at A
    // land on B whenever B was the chooser's answer.
    if (sendsAccount(t)) qs.set("account", account.value);
```

- [ ] **Step 5: Run the frontend tests and lint**

Run: `npm test`
Expected: all pass.

Run: `bin/check`
Expected: zero violations (biome flags any now-unused import — fix by deleting it).

- [ ] **Step 6: Rebuild the bundle**

Run: `npm run build`
Expected: vite writes `static/dist/app.js` (and its css/map siblings) with no errors.

- [ ] **Step 7: Browser check on the dev server**

Run: `PERISCOPE_PORT=8766 PERISCOPE_DEV=1 uv run server.py` (in the worktree), open `http://127.0.0.1:8766/`.
Check, and note the result in the commit message body-free style (one line in chat, not the commit):
- header model row reads `auto → fable` (or `auto → opus 1m`), account row `auto → B`/`auto → A`; hovering `auto` shows the reason.
- clicking `default` on the model row keeps `default` active after the next poll (it is stored literally now); clicking `auto` returns to `auto → …`.
- ⌘K / `+ new` launcher opens with the same account and model preselected.

- [ ] **Step 8: Commit**

```bash
git add static/src/overlays/LauncherModal.jsx static/src/chrome/usageSummary.js static/src/overlays/__tests__/LauncherModal.test.js static/src/chrome/__tests__/usageSummary.test.js static/dist
git commit -m "launcher: preselect from launch_default and send account unconditionally; client bestAccount mirror deleted; rebuild dist"
```

---

### Task 9: Docs — `docs/account-routing.md`, `wrapper-profiles.md`, `CLAUDE.md`

**Files:**
- Create: `docs/account-routing.md`
- Modify: `docs/wrapper-profiles.md` (the "Model override" paragraph), `CLAUDE.md:` the reference-docs table and the module table

- [ ] **Step 1: Write `docs/account-routing.md`**

```markdown
# Account & model routing (`launch_policy.py`, `usage.choose_launch`)

Every unnamed Claude launch — launcher New Tab, ⌘K unified open, MCP
`spawn_claude` / `resume_session`, the dashboard resume button, move-account —
resolves its account and model through `usage.choose_launch(account, model)`.
That is the one choke point; the policy it applies lives in
`periscope/launch_policy.py`, pure, tested as a table in
`tests/test_launch_policy.py`.

## Objective

At each account's weekly reset, its Fable sub-limit and its weekly-all meter
are both at 100%. Any headroom at reset is waste.

Two measured facts shape the rules (five weeks of prod `usage_samples`,
2026-08-19 → 09-08):

- **Weekly resets are a fixed cadence** (B: Wed 23:00; A: Sun 10:00), even
  when the first use after a reset comes a day later. `resets_at` is null
  whenever utilization is 0 — a fresh account, not an unanchored one. Nothing
  periscope does can move a weekly reset.
- **The 5h session window is anchored at its first message**; a closed window
  reports `resets_at = null`.
- Fable's weekly sub-limit is ~half of weekly-all, so draining an account
  means Fable *and then* a non-Fable model.

## The rules

| # | Rule | Why |
|---|---|---|
| 1 | Accounts are ordered by weekly-all `resets_at`, soonest first; null sorts last; registry order breaks ties | Earliest-deadline-first: the budget expiring soonest is drained first. Null = 0% used = reset most recently. Deterministic so `/api/state.launch_default` and a spawn agree |
| 2 | Within an account: `fable`, then `opus[1m]` | Opus is not a fallback; it is how the non-Fable half of the week gets used |
| 3 | A pair is skipped only when **walled**: session ≥100, weekly-all ≥100, or the model's sub-limit ≥100 | No projection on the weekly axis — leaving an account before it walls preserves exactly the headroom the objective burns |
| 4 | The sub-limit meter is matched by prefix (`week_fable`, `week_fable_*`) | Keys are slugified display names; an exact match stops walling the day the name becomes "Fable 5.1" |
| 5 | An account whose session is **on pace to wall** (`limit_at` set) is skipped on the first pass, unless every account is | Session headroom is a rate limit, not a budget: the other window costs nothing to use and two windows in parallel is 2× the daily rate |
| 6 | Everything walled → the account whose session resets soonest | Blocked either way; minimize the wait |
| 7 | An explicit account (call arg or header pin) is never rerouted; the model is still chosen within it. An explicit model (arg or pin) fixes the model; the account is chosen against that model's sub-limit | Pins override one axis each |
| 8 | `spawn_model`: `"auto"` (or unset) = rules 2–3; `"default"` = no `ANTHROPIC_MODEL`; an alias = pinned | Stored literally — coercing `default` to unset would silently turn "no override" into "chooser decides" |
| 9 | No usage data → `default` account, no override (or the explicit account) | A launch never waits on usage |

`choose_launch`'s answer is published as `launch_default` on every
`/api/state` poll; the header chips render `auto → fable` / `auto → B` from
it and the launcher preselects from it, sending both values explicitly.

## Not routed

Background-commander jobs (`bg_account`): `claude agents` / `claude stop` are
per-config-dir, and a job whose account is re-resolved between dispatch and
sync is the unkillable-job incident `bg_commander._account_env` documents.
```

- [ ] **Step 2: `docs/wrapper-profiles.md`**

In the paragraph beginning `**Model override (`ANTHROPIC_MODEL`) rides the same carrier.**`, replace the sentence starting `` `store.spawn_model_env(explicit)` is the one choke point `` through `` `None` falls to the pin. `` with:

`` `usage.choose_launch(account, model)` is the one choke point on both axes (docs/account-routing.md): an explicit value wins, INCLUDING an explicit `"default"` (the launcher always sends one — that is how a single launch opts out of the pin); `None` falls to the pin, and a pin of `"auto"` (or none) lets `launch_policy` pick fable, then opus[1m]. ``

- [ ] **Step 3: `CLAUDE.md`**

In the "Reference docs" table add a row after the `narrator.py` row:

`| `launch_policy.py`, `usage.choose_launch`, `spawn_model` / `spawn_account` settings, the header pin pickers | `docs/account-routing.md` — earliest-reset routing, walls vs. session pressure, what `auto`/`default` mean |`

In the module table change the `git_pr.py / lgtm.py / usage.py / cost_pressure.py` row to:

`| `git_pr.py` / `lgtm.py` / `usage.py` / `launch_policy.py` / `cost_pressure.py` | Git state + PR cache / LGTM mirror / plan usage + `choose_launch` shell / pure account-and-model routing policy / per-pane context-cost pressure |`

- [ ] **Step 4: Commit**

```bash
git add docs/account-routing.md docs/wrapper-profiles.md CLAUDE.md
git commit -m "docs: account-routing.md (the rule table + evidence), wrapper-profiles names choose_launch, CLAUDE.md index rows"
```

---

### Task 10: Final verification

- [ ] **Step 1: Full suites**

Run: `uv run pytest -q` — Expected: all pass, 0 failures.
Run: `npm test` — Expected: all pass.
Run: `bin/check` — Expected: zero violations.
Run: `grep -rn "best_account\|spawn_model_env\|accountQuery\|bestAccount" periscope static/src tests docs CLAUDE.md` — Expected: no hits outside `docs/superpowers/` and `tests/conftest.py`'s docstring.

- [ ] **Step 2: Working tree clean**

Run: `git status --short` — Expected: empty (dist committed in Task 8).

- [ ] **Step 3: Report**

Paste the last ~20 lines of each suite's output in the completion message. Unit 1 is then ready for the whole-branch review and merge; Units 2 (poke) and 3 (move override + 💤) are separate plans.
