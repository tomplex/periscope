# Account & model defaults — Unit 2: the session poke — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** At 08:00 local every day, periscope sends one Haiku message on each account whose 5h session window is closed, so the window resets ~13:00 instead of wherever the first real message happened to land; it verifies the reset moved and shows the outcome in the usage pill.

**Architecture:** New `periscope/poke.py` — a pure `due()` decision (now, settings, log, usage, in-flight, registry → account ids) plus a worker-thread `poke_account()` (subprocess → forced usage refetch → verify → persist) driven by a 60s asyncio tick registered prod-only in `app.lifespan`. The two spend-leak env guards move from `bg_commander` to `config.claude_subprocess_env`; `usage.refresh_plan_usage_now` is an in-flight-safe synchronous refresh; the log lives in `state.json["poke_log"]` and rides `/api/state` as `poke`.

**Tech Stack:** Python 3.14 / FastAPI / asyncio / pytest, Preact / vitest, Vite build to the committed `static/dist/app.js`.

**Spec:** `docs/superpowers/specs/2026-09-08-account-defaults-design.md` (D6, §Poke). **Structure:** `docs/superpowers/specs/2026-09-08-account-defaults-structure.md` (Unit 2, P3–P6). **Depends on:** Unit 1 merged (`launch_policy.AccountUsage`, `docs/account-routing.md` exist).

**Read before starting:** `CLAUDE.md`, `docs/testing.md` (the no-leaked-thread invariant — this unit adds a thread that writes the activity DB), `docs/account-routing.md`.

**Verification commands:** `uv run pytest tests/test_poke.py -q`, `uv run pytest -q`, `bin/check`, `npm test`, `npm run build`.

---

## File map

| File | Change |
|---|---|
| `periscope/config.py` | NEW `claude_subprocess_env(*, config_dir)` |
| `periscope/bg_commander.py` | `_dispatch_env`, `_read_agents`, `_stop_session` use it; `_account_env` deleted |
| `periscope/usage.py` | NEW `refresh_plan_usage_now(account, config_dir)` |
| `periscope/store.py` | `Settings.poke_at` / `poke_grace_min`; `_STATE_DEFAULTS["poke_log"]`; `get_poke_log`, `record_poke`; `PokeEntry` |
| `periscope/poke.py` | NEW |
| `periscope/app.py` | `poke_task` registered in the prod block, cancelled in `finally` |
| `periscope/routes/settings.py` | `poke_at`, `poke_grace_min` validation |
| `periscope/routes/state.py` | `"poke"` on the poll |
| `static/src/poll.js`, `static/src/chrome/UsagePill.jsx` | `usage.value.poke`; tooltip line |
| `tests/test_poke.py` (NEW), `tests/test_config.py`, `tests/test_bg_commander.py`, `tests/test_usage.py`, `tests/test_store.py`, `tests/routes/test_settings.py`, `tests/routes/test_state.py`, `tests/test_app.py`, `tests/conftest.py`, `static/src/chrome/__tests__/usagePillRender.test.jsx` | tests |
| `docs/account-routing.md`, `CLAUDE.md` | docs |

---

### Task 1: `config.claude_subprocess_env`; `bg_commander` uses it

**Files:**
- Modify: `periscope/config.py` (after `model_env`), `periscope/bg_commander.py:156-197,289-309`
- Test: `tests/test_config.py`, `tests/test_bg_commander.py:51-62,146-157,178-200`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_config.py`:

```python
def test_claude_subprocess_env_strips_api_credit_auth_and_binds_the_account(monkeypatch):
    # Both are spend-leak guards: an inherited API key outranks the
    # subscription login (bills API credits), and an inherited
    # CLAUDE_CONFIG_DIR is an account nobody chose.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-leak")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "tok-leak")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/leaked")
    env = config.claude_subprocess_env(config_dir="")
    assert "ANTHROPIC_API_KEY" not in env
    assert "ANTHROPIC_AUTH_TOKEN" not in env
    assert "CLAUDE_CONFIG_DIR" not in env
    env = config.claude_subprocess_env(config_dir="/Users/x/.claude-b")
    assert env["CLAUDE_CONFIG_DIR"] == "/Users/x/.claude-b"
    assert env["PATH"] == os.environ["PATH"]   # the rest of the environment is inherited
```

(`tests/test_config.py` already imports `config`; add `import os` at its top if absent.)

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_config.py -q -k subprocess_env`
Expected: `AttributeError: module 'periscope.config' has no attribute 'claude_subprocess_env'`

- [ ] **Step 3: Implement in `config.py`** (below `model_env`; `os` is already imported there)

```python
def claude_subprocess_env(*, config_dir: str) -> dict[str, str]:
    """The environment for a `claude` subprocess periscope runs itself
    (background-commander jobs, the session poke).

    The Anthropic API-key auth vars are STRIPPED: server.py load_dotenv()s
    ANTHROPIC_API_KEY into os.environ (for the narrator/rename SDK calls), and
    an inherited key takes precedence over the claude.ai subscription login —
    the subprocess must bill on the subscription, not API credits (a spend
    leak).

    CLAUDE_CONFIG_DIR picks WHICH subscription — same class of decision, so
    it lives here too. An empty `config_dir` POPS rather than leaving whatever
    leaked in: a subprocess must never silently run on an account nobody
    chose (the launchd env never carries the var, but a dev shell might).
    """
    env = dict(os.environ)
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        env.pop(k, None)
    if config_dir:
        env["CLAUDE_CONFIG_DIR"] = config_dir
    else:
        env.pop("CLAUDE_CONFIG_DIR", None)
    return env
```

- [ ] **Step 4: Rewire `bg_commander.py`**

Replace `_dispatch_env` and `_account_env` (lines 156–197) with:

```python
def _bg_config_dir() -> str:
    """The account background jobs bill. EVERY claude subprocess here must
    agree on it, not just dispatch: `claude agents` and `claude stop` are
    per-config-dir, so a job dispatched under account B is invisible to a
    listing taken under the default one. sync_jobs would then see it absent
    past the grace window, mark it `done` while it kept running, and never
    call stop() — an unkillable job the dashboard reports as finished. Read
    from the `bg_account` setting on every call, never from a launch-time
    chooser (docs/account-routing.md "Not routed")."""
    return store.account_config_dir(store.get_settings().get("bg_account"))


def _dispatch_env(*, handle: str) -> dict[str, str]:
    """Subscription-billing env (config.claude_subprocess_env) + a per-command
    caller handle. channel_shim reads PERISCOPE_CALLER_ID (falling back to
    TMUX_PANE). The handle is a unique cmdr:<token> — its only jobs are to
    (a) trip is_commander's prefix check and (b) key _MCP_SESSIONS uniquely
    across concurrent commanders. It is NOT the claude session id (which
    isn't known until claude prints it post-spawn)."""
    env = config.claude_subprocess_env(config_dir=_bg_config_dir())
    env["PERISCOPE_CALLER_ID"] = f"cmdr:{handle}"
    return env
```

In `_read_agents` and `_stop_session`, replace `env=_account_env(dict(os.environ)),` with `env=config.claude_subprocess_env(config_dir=_bg_config_dir()),`. Then run `grep -n "os\." periscope/bg_commander.py` — `os` is still used by `dispatch` (`os.path.expanduser`), so its import stays.

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/test_config.py tests/test_bg_commander.py -q`
Expected: all pass — the existing `_dispatch_env` / `_read_agents` / `_stop_session` account tests exercise the new path unchanged (they patch `periscope.store.get_settings`, which `_bg_config_dir` reads).

- [ ] **Step 6: Commit**

```bash
git add periscope/config.py periscope/bg_commander.py tests/test_config.py
git commit -m "config.claude_subprocess_env: the API-credit strip and CLAUDE_CONFIG_DIR set-or-pop move out of bg_commander (second consumer incoming)"
```

---

### Task 2: `usage.refresh_plan_usage_now`

**Files:**
- Modify: `periscope/usage.py` (after `cached_plan_usage`)
- Test: `tests/test_usage.py` (after `test_refresh_failure_keeps_data_and_retries_sooner`)

- [ ] **Step 1: Write the failing tests**

```python
def test_refresh_plan_usage_now_bypasses_the_ttl_and_returns_the_fresh_payload(monkeypatch):
    import periscope.usage as usage
    # `utilization` is required: _refresh_plan_usage_into_cache reads m["utilization"]
    # for the sample row, and a KeyError there is swallowed into "keep the stale entry".
    fresh = {"available": True,
             "meters": {"session": {"percent": 1, "utilization": 0.01, "resets_at": 99}}}
    monkeypatch.setattr(usage, "fetch_plan_usage", lambda cfg: dict(fresh))
    monkeypatch.setattr(usage.activity, "record_usage_samples", lambda rows: None)
    monkeypatch.setattr(usage, "attach_projections", lambda *a, **kw: None)
    stale = {"available": True, "meters": {}, "fetched_at": 1}
    monkeypatch.setattr(usage, "_plan_cache", {"b": (9e12, stale)})   # TTL far in the future
    monkeypatch.setattr(usage, "_plan_in_flight", set())
    out = usage.refresh_plan_usage_now("b", "/cfg")
    assert out["meters"]["session"]["resets_at"] == 99
    assert usage._plan_cache["b"][1] is out
    assert usage._plan_in_flight == set()


def test_refresh_plan_usage_now_yields_to_a_refresh_already_in_flight(monkeypatch):
    # _refresh_plan_usage_into_cache discards the in-flight marker
    # unconditionally; a second caller must not drop the first's marker and
    # let cached_plan_usage fire a duplicate into an endpoint that 429s readily.
    import periscope.usage as usage
    calls = []
    monkeypatch.setattr(usage, "_refresh_plan_usage_into_cache",
                        lambda a, c: calls.append(a))
    stale = {"available": True, "meters": {}, "fetched_at": 1}
    monkeypatch.setattr(usage, "_plan_cache", {"b": (0.0, stale)})
    monkeypatch.setattr(usage, "_plan_in_flight", {"b"})
    assert usage.refresh_plan_usage_now("b", "/cfg") is stale
    assert calls == []
    assert usage._plan_in_flight == {"b"}
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_usage.py -q -k refresh_plan_usage_now`
Expected: `AttributeError: ... no attribute 'refresh_plan_usage_now'`

- [ ] **Step 3: Implement** (directly below `cached_plan_usage`)

```python
def refresh_plan_usage_now(account: str, config_dir: str) -> AccountUsage | None:
    """Synchronous, TTL-bypassing refresh of one account — the poke's
    verification step. BLOCKING (a live httpx call + an activity-DB write):
    call it from a worker thread, never from the event loop or a request
    handler.

    Claims the account's in-flight slot first and yields (returning whatever
    is cached) when a background refresh already holds it:
    `_refresh_plan_usage_into_cache` discards the marker unconditionally, so
    an unguarded second caller would drop the first's marker and the next
    `cached_plan_usage` poll would fire a duplicate into an endpoint that
    429s readily.
    """
    with _plan_lock:
        if account in _plan_in_flight:
            return _plan_cache.get(account, (0.0, None))[1]
        _plan_in_flight.add(account)
    _refresh_plan_usage_into_cache(account, config_dir)
    with _plan_lock:
        return _plan_cache.get(account, (0.0, None))[1]
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_usage.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add periscope/usage.py tests/test_usage.py
git commit -m "usage.refresh_plan_usage_now: synchronous TTL bypass for one account, in-flight-safe"
```

---

### Task 3: `store` — `poke_log`, `PokeEntry`, settings fields

**Files:**
- Modify: `periscope/store.py:89-97` (`Settings`), `:111-119` (`_STATE_DEFAULTS`), after `update_settings` (accessors), `tests/conftest.py:210-218` (`clean_state`'s fresh dict)
- Test: `tests/test_store.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_store.py`:

```python
def test_poke_log_round_trips_per_account(clean_state):
    from periscope import store
    assert store.get_poke_log() == {}
    entry = {"date": "2026-09-08", "at": 1_800_000_000, "resets_at": 1_800_018_000, "verified": True}
    store.record_poke("b", entry)
    assert store.get_poke_log() == {"b": entry}
    # A copy out, not a reference in.
    store.get_poke_log()["b"]["verified"] = False
    assert store.get_poke_log()["b"]["verified"] is True


def test_poke_log_tolerates_a_state_file_that_predates_it(clean_state):
    from periscope import store
    del store._STATE["poke_log"]
    assert store.get_poke_log() == {}
    store.record_poke("default", {"date": "2026-09-08", "at": 1, "resets_at": None, "verified": False})
    assert store.get_poke_log()["default"]["verified"] is False
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_store.py -q -k poke_log`
Expected: `AttributeError: ... no attribute 'get_poke_log'`

- [ ] **Step 3: Implement**

`periscope/store.py` — in `Settings`, after the `spawn_model` line:
```python
    poke_at: str  # local HH:MM the session poke fires; unset => "08:00", "" => disabled
    poke_grace_min: int  # minutes past poke_at a missed poke may still fire; unset => 90
```

Below the `Settings` class add:
```python
class PokeEntry(TypedDict):
    """One account's most recent session poke (periscope.poke)."""
    date: str          # local YYYY-MM-DD — what "already poked today" reads
    at: int            # epoch of the poke
    resets_at: int | None   # the 5h session reset observed right after
    verified: bool     # resets_at landed within tolerance of at + 5h
```

In `_STATE_DEFAULTS` add `"poke_log": {},` after `"settings": {},`.

After `update_settings`:
```python
def get_poke_log() -> dict[str, PokeEntry]:
    """Snapshot of state['poke_log'] (copies of each entry). `.get` because a
    state file written before the key existed loads without it."""
    with _STATE_LOCK:
        return {k: cast(PokeEntry, dict(v)) for k, v in (_STATE.get("poke_log") or {}).items()}


def record_poke(account_id: str, entry: PokeEntry) -> None:
    with _STATE_LOCK:
        _STATE.setdefault("poke_log", {})[account_id] = dict(entry)
        _write_state(_STATE)
```

`tests/conftest.py` — in `clean_state`'s `fresh` dict add `"poke_log": {},` after `"settings": {},`.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_store.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add periscope/store.py tests/test_store.py tests/conftest.py
git commit -m "store: poke_log (PokeEntry per account) + poke_at/poke_grace_min settings"
```

---

### Task 4: `poke.py` — `due` and `verified` (pure)

**Files:**
- Create: `periscope/poke.py`
- Test: `tests/test_poke.py`
- Modify: `tests/conftest.py:21-52` (`_no_plan_usage_refresh` neuters `poke._bg`)

- [ ] **Step 1: Neuter `poke._bg` in the autouse guard first** — the module does not exist yet, so guard the import the way the fixture already guards `usage`:

In `tests/conftest.py`'s `_no_plan_usage_refresh`, after the existing `monkeypatch.setattr(usage, "_base_ctx_cache", {}, raising=False)` line add:
```python
    # The session poke's worker thread does the same httpx + record_usage_samples
    # write through usage.refresh_plan_usage_now — the same leaked-thread class.
    # It imports _bg into its own namespace, so neuter it by that name too.
    try:
        from periscope import poke
    except ImportError:
        return
    monkeypatch.setattr(poke, "_bg", lambda *a, **kw: None, raising=False)
    monkeypatch.setattr(poke, "_in_flight", set(), raising=False)
```

- [ ] **Step 2: Write the failing tests**

```python
# tests/test_poke.py
"""The session poke's decision (pure) and its worker (I/O mocked).

Nothing here spawns a thread: conftest neuters poke._bg, and poke_account is
called directly with its subprocess and refetch patched."""

from datetime import datetime

import pytest

from periscope import poke

ACCOUNTS = [
    {"id": "default", "label": "account A", "config_dir": ""},
    {"id": "b", "label": "account B", "config_dir": "/Users/x/.claude-b"},
]
T0 = datetime(2026, 9, 8, 8, 0)          # Tue 08:00 local


def usage(*, default_open=False, b_open=False, now=T0):
    """Both accounts available; a session window is 'open' when resets_at is
    in the future — the endpoint reports null once the window has closed."""
    later = int(now.timestamp()) + 3600

    def acct(is_open):
        return {"available": True, "fetched_at": 1,
                "meters": {"session": {"percent": 3 if is_open else 0,
                                       "resets_at": later if is_open else None}}}
    return {"default": acct(default_open), "b": acct(b_open)}


def due(now=T0, settings=None, log=None, u=None, in_flight=frozenset()):
    return poke.due(now=now, settings=settings or {}, log=log or {},
                    usage=u if u is not None else usage(), in_flight=in_flight,
                    accounts=ACCOUNTS)


def test_fires_for_every_account_at_the_default_time():
    assert due() == ["default", "b"]


def test_waits_until_poke_at():
    assert due(now=datetime(2026, 9, 8, 7, 59)) == []
    assert due(now=datetime(2026, 9, 8, 8, 0)) == ["default", "b"]


def test_honors_a_configured_time_and_grace():
    s = {"poke_at": "06:30", "poke_grace_min": 30}
    assert due(now=datetime(2026, 9, 8, 6, 29), settings=s) == []
    assert due(now=datetime(2026, 9, 8, 6, 59), settings=s) == ["default", "b"]
    assert due(now=datetime(2026, 9, 8, 7, 0), settings=s) == []


def test_skips_the_day_past_the_grace_window():
    # A 10:00 catch-up would shorten the first work block, not help it.
    assert due(now=datetime(2026, 9, 8, 9, 29)) == ["default", "b"]
    assert due(now=datetime(2026, 9, 8, 9, 30)) == []


def test_empty_poke_at_disables():
    assert due(settings={"poke_at": ""}) == []


def test_skips_an_account_with_an_open_window_until_it_closes():
    assert due(u=usage(b_open=True)) == ["default"]
    assert due(u=usage()) == ["default", "b"]        # closed → fires (inside grace)


def test_skips_an_account_already_poked_today_but_not_yesterday():
    log = {"b": {"date": "2026-09-08", "at": 1, "resets_at": None, "verified": True}}
    assert due(log=log) == ["default"]
    stale = {"b": {"date": "2026-09-07", "at": 1, "resets_at": None, "verified": True}}
    assert due(log=stale) == ["default", "b"]


def test_skips_an_account_in_flight():
    assert due(in_flight={"default"}) == ["b"]


def test_skips_an_account_with_no_usable_credential():
    u = usage()
    u["b"] = {"available": False}
    assert due(u=u) == ["default"]


@pytest.mark.parametrize("resets_at,ok", [
    (1_800_000_000 + 5 * 3600, True),
    (1_800_000_000 + 5 * 3600 + 299, True),
    (1_800_000_000 + 5 * 3600 - 299, True),
    (1_800_000_000 + 5 * 3600 + 301, False),
    (1_800_000_000 + 3600, False),          # a pre-existing window, not ours
    (None, False),
])
def test_verified_is_within_five_minutes_of_now_plus_5h(resets_at, ok):
    assert poke.verified(resets_at, at=1_800_000_000) is ok
```

- [ ] **Step 3: Run to verify they fail**

Run: `uv run pytest tests/test_poke.py -q`
Expected: `ModuleNotFoundError: No module named 'periscope.poke'`

- [ ] **Step 4: Write the module (pure half; the worker and loop are Task 5 — include the two stubs so imports resolve)**

```python
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
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/test_poke.py -q`
Expected: `15 passed` (9 `due` cases + 6 parametrized `verified` cases).

- [ ] **Step 6: Lint**

Run: `bin/check` — Expected: zero violations (the module imports only what Task 4 uses; verified against the repo's ruff config).

- [ ] **Step 7: Commit**

```bash
git add periscope/poke.py tests/test_poke.py tests/conftest.py
git commit -m "poke: due() (poke_at + grace, skip open window / logged today / in flight / no credential) and verified(); conftest neuters poke._bg"
```

---

### Task 5: `poke_account` worker and `run` loop; lifespan registration

**Files:**
- Modify: `periscope/poke.py` (replace the two stubs), `periscope/app.py:127-137,164-176`
- Test: `tests/test_poke.py` (append), `tests/test_app.py:38-95`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_poke.py`:

```python
# --- poke_account: subprocess → refetch → verify → record --------------------

def _worker(monkeypatch, *, resets_at, returncode=0, raise_subprocess=False):
    """Patch the two I/O seams; return the captured argv/env and the record."""
    seen = {}

    def fake_run(argv, **kw):
        if raise_subprocess:
            raise OSError("no claude binary")
        seen["argv"], seen["env"], seen["cwd"] = argv, kw.get("env"), kw.get("cwd")
        return type("R", (), {"returncode": returncode, "stdout": "ok", "stderr": ""})()

    monkeypatch.setattr(poke.subprocess, "run", fake_run)
    monkeypatch.setattr(poke.usage, "refresh_plan_usage_now",
                        lambda aid, cfg: {"available": True,
                                          "meters": {"session": {"percent": 1, "resets_at": resets_at}}})
    monkeypatch.setattr(poke.store, "record_poke", lambda aid, e: seen.update(record=(aid, e)))
    monkeypatch.setattr(poke, "_in_flight", {"b"})
    return seen


def test_poke_account_runs_haiku_headless_on_the_account_and_records_a_verified_poke(monkeypatch):
    at = int(poke.time.time())
    seen = _worker(monkeypatch, resets_at=at + 5 * 3600 + 10)
    poke.poke_account("b", "/Users/x/.claude-b")
    argv = seen["argv"]
    assert argv[1:] == ["-p", "ok", "--model", "claude-haiku-4-5", "--strict-mcp-config"]
    assert seen["env"]["CLAUDE_CONFIG_DIR"] == "/Users/x/.claude-b"
    assert "ANTHROPIC_API_KEY" not in seen["env"]
    assert seen["cwd"] == poke.os.path.expanduser("~")
    aid, entry = seen["record"]
    assert aid == "b"
    assert entry["verified"] is True
    assert entry["resets_at"] == at + 5 * 3600 + 10
    assert entry["date"] == poke.datetime.fromtimestamp(entry["at"]).strftime("%Y-%m-%d")
    assert poke._in_flight == set()


def test_poke_account_records_an_unverified_poke_when_the_reset_did_not_move(monkeypatch, caplog):
    seen = _worker(monkeypatch, resets_at=None)
    poke.poke_account("b", "/Users/x/.claude-b")
    assert seen["record"][1]["verified"] is False
    assert any("poke b" in r.message and r.levelname == "WARNING" for r in caplog.records)


def test_poke_account_records_even_when_the_subprocess_fails_so_it_does_not_repoke(monkeypatch):
    # A failed spawn re-tried every tick for 90 minutes is a warning storm and,
    # if the failure was transient, a double spend.
    seen = _worker(monkeypatch, resets_at=None, raise_subprocess=True)
    poke.poke_account("b", "/Users/x/.claude-b")
    assert seen["record"][1]["verified"] is False
    assert poke._in_flight == set()


def test_tick_spawns_one_worker_per_due_account_and_marks_it_in_flight(monkeypatch, clean_state):
    spawned = []
    monkeypatch.setattr(poke, "_bg", lambda name, fn, *a: spawned.append((name, a)))
    monkeypatch.setattr(poke, "_in_flight", set())
    monkeypatch.setattr(poke.store, "get_accounts", lambda: ACCOUNTS)
    monkeypatch.setattr(poke.usage, "cached_plan_usage", lambda: usage())
    monkeypatch.setattr(poke, "_now", lambda: T0)
    poke._tick()
    assert spawned == [("poke:default", ("default", "")), ("poke:b", ("b", "/Users/x/.claude-b"))]
    assert poke._in_flight == {"default", "b"}
    poke._tick()                       # in flight → nothing new
    assert len(spawned) == 2
```

In `tests/test_app.py`, in BOTH lifespan tests that run with the prod port — `test_lifespan_starts_and_shuts_down_cleanly` (after its `mocker.patch("periscope.activity.run_worker", side_effect=_noop)` line) and `test_lifespan_binds_mcp_on_prod_port` (after its own `run_worker` patch at ~line 150) — add:
```python
    # The poke loop is prod-gated too and its first tick runs on startup; with
    # PORT at 8765 here a real tick between 08:00 and 09:30 would spend a
    # Haiku call on the developer's real subscription.
    mocker.patch("periscope.poke.run", side_effect=_noop)
```
Also fix the stale comment at `tests/test_app.py:88` — the autouse fixture no longer "seeds the cache"; it neuters `usage._bg`. Change that sentence to `The autouse _no_plan_usage_refresh fixture neuters usage._bg so no spawn happens.`

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_poke.py -q`
Expected: the four new tests fail with `NotImplementedError` / `AttributeError: ... '_tick'`.

- [ ] **Step 3: Replace the stubs in `poke.py`**

First add the I/O imports at the top of the module (and delete the parenthetical comment Task 4 left there), so the import block reads:
```python
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
```
(`due`'s `log=` / `usage=` parameters shadow the module-level `log` / `usage` inside that one function only; ruff accepts it once both are used elsewhere in the module — verified.)

Then replace the two stubs:

```python
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
        payload = usage.refresh_plan_usage_now(account_id, config_dir) or {}
        session = (payload.get("meters") or {}).get("session") or {}
        resets_at = session.get("resets_at")
        ok = verified(resets_at, at=at)
        (log.info if ok else log.warning)(
            "poke %s at %s: session resets %s (%s)", account_id,
            datetime.fromtimestamp(at).strftime("%H:%M"),
            datetime.fromtimestamp(resets_at).strftime("%H:%M") if resets_at else "—",
            "anchored" if ok else "NOT anchored — reset did not move",
        )
        store.record_poke(account_id, {
            "date": datetime.fromtimestamp(at).strftime("%Y-%m-%d"),
            "at": at, "resets_at": resets_at, "verified": ok,
        })
    finally:
        _in_flight.discard(account_id)


def _now() -> datetime:
    return datetime.now()


def _tick() -> None:
    for aid in due(now=_now(), settings=store.get_settings(), log=store.get_poke_log(),
                   usage=usage.cached_plan_usage(), in_flight=_in_flight,
                   accounts=store.get_accounts()):
        _in_flight.add(aid)
        _bg(f"poke:{aid}", poke_account, aid, store.account_config_dir(aid))


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
```

- [ ] **Step 4: Register in `app.py`**

Inside the existing `if config.is_prod():` block that creates `activity_task` (after the `bg_commander.write_mcp_config()` try/except), add:
```python
        # Session poke (periscope.poke): one Haiku message per account each
        # morning. Prod only — it spends. Gated HERE, at registration, not
        # inside the coroutine: a gate inside would still leave a live task
        # behind on every dev --reload.
        from periscope import poke
        poke_task = _task("poke", poke.run())
```
and in the `else:` branch, after `activity_task = None`, add `poke_task = None`.

In the `finally`, after the `activity_task` cancel:
```python
        if poke_task is not None:
            poke_task.cancel()
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/test_poke.py tests/test_app.py -q`
Expected: all pass.

Run: `bin/check` — Expected: zero violations.

- [ ] **Step 6: Commit**

```bash
git add periscope/poke.py periscope/app.py tests/test_poke.py tests/test_app.py
git commit -m "poke: worker (headless haiku on the account, forced refetch, verify, record) + 60s tick loop registered prod-only in lifespan"
```

---

### Task 6: Settings validation and `/api/state.poke`

**Files:**
- Modify: `periscope/routes/settings.py:25-35` (fields) and the patch body, `periscope/routes/state.py` (below `launch_default`)
- Test: `tests/routes/test_settings.py`, `tests/routes/test_state.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/routes/test_settings.py`:

```python
def test_patch_poke_at_accepts_hhmm_empty_and_null(client, mocker):
    update_spy = mocker.patch("periscope.routes.settings.update_settings")
    mocker.patch("periscope.routes.settings.get_settings", return_value={})
    assert client.patch("/api/settings", json={"poke_at": "07:30"}).status_code == 200
    assert client.patch("/api/settings", json={"poke_at": ""}).status_code == 200      # disabled
    assert client.patch("/api/settings", json={"poke_at": None}).status_code == 200    # back to the default
    assert [c.args[0] for c in update_spy.call_args_list] == [
        {"poke_at": "07:30"}, {"poke_at": ""}, {"poke_at": None},
    ]


def test_patch_poke_at_rejects_a_non_clock(client, mocker):
    for bad in ("8am", "25:00", "08:60", "8:00"):
        assert client.patch("/api/settings", json={"poke_at": bad}).status_code == 400, bad


def test_patch_poke_grace_min_bounds(client, mocker):
    update_spy = mocker.patch("periscope.routes.settings.update_settings")
    mocker.patch("periscope.routes.settings.get_settings", return_value={})
    assert client.patch("/api/settings", json={"poke_grace_min": 45}).status_code == 200
    assert client.patch("/api/settings", json={"poke_grace_min": 0}).status_code == 400
    assert client.patch("/api/settings", json={"poke_grace_min": 721}).status_code == 400
    update_spy.assert_called_once_with({"poke_grace_min": 45})
```

Append to `tests/routes/test_state.py`:

```python
def test_state_carries_the_poke_log(client, mocker, clean_state):
    from periscope import store
    _patch(mocker, "list_windows", return_value=[])
    _patch(mocker, "update_focus_from_windows")
    _patch(mocker, "_attach_git_then_resolve_pids")
    _patch(mocker, "cached_claude_usage", return_value={})
    _patch(mocker, "cached_plan_usage", return_value={})
    store.record_poke("b", {"date": "2026-09-08", "at": 5, "resets_at": 18005, "verified": True})
    body = client.get("/api/state").json()
    assert body["poke"] == {"b": {"date": "2026-09-08", "at": 5, "resets_at": 18005, "verified": True}}
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/routes/test_settings.py tests/routes/test_state.py -q -k poke`
Expected: the settings tests fail with 200s where 400 is expected / `update_settings` never called (unknown fields are ignored by the model); the state test fails with `KeyError: 'poke'`.

- [ ] **Step 3: Implement**

`periscope/routes/settings.py` — add `import re` at the top; in `SettingsPatch` add:
```python
    poke_at: str | None = None
    poke_grace_min: int | None = None
```
Add a module constant below `_VALID_LAYOUTS`:
```python
_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
```
In `settings_patch`, before the `editor` block:
```python
    if "poke_at" in sent:
        v = body.poke_at
        # "" disables; null pops the key (back to the 08:00 default) — the two
        # cannot share a meaning because update_settings deletes null keys.
        if v is None or v == "" or _HHMM.match(v):
            patch["poke_at"] = v
        else:
            raise HTTPException(400, f"poke_at must be HH:MM (24h) or empty, got {v!r}")

    if "poke_grace_min" in sent:
        v = body.poke_grace_min
        if v is None or 1 <= v <= 720:
            patch["poke_grace_min"] = v
        else:
            raise HTTPException(400, "poke_grace_min must be 1..720")
```

`periscope/routes/state.py` — below the `launch_default` entry:
```python
        # Each account's most recent session poke; the pill tooltip shows it.
        "poke": store.get_poke_log(),
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/routes/test_settings.py tests/routes/test_state.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add periscope/routes/settings.py periscope/routes/state.py tests/routes/test_settings.py tests/routes/test_state.py
git commit -m "settings: poke_at (HH:MM | '' disables | null → default) and poke_grace_min; state carries the poke log"
```

---

### Task 7: Usage pill tooltip shows the poke outcome; rebuild dist

**Files:**
- Modify: `static/src/poll.js:56`, `static/src/chrome/UsagePill.jsx:111-120,145`
- Test: `static/src/chrome/__tests__/usagePillRender.test.jsx`

- [ ] **Step 1: Write the failing test**

Append inside the `describe("<UsagePill>", ...)` block:

```jsx
  it("names the morning poke and the reset it anchored in the account tooltip", () => {
    const at = NOW() - 3600;
    usage.value = {
      plan: { b: { available: true, fetched_at: NOW(), meters: meters({ session: 9 }) } },
      fallback: null,
      poke: { b: { date: "2026-09-08", at, resets_at: at + 5 * 3600, verified: true } },
    };
    expect(render(<UsagePill />)).toMatch(/poked [^→]+ → resets /);
    usage.value = {
      ...usage.value,
      poke: { b: { date: "2026-09-08", at, resets_at: null, verified: false } },
    };
    expect(render(<UsagePill />)).toContain("→ not anchored");
  });
```

- [ ] **Step 2: Run to verify it fails**

Run: `npm test -- usagePillRender`
Expected: the new case fails (no "poked" text).

- [ ] **Step 3: Implement**

`static/src/poll.js` — change the `usage.value` line to:
```js
  usage.value = { plan: data.usage_plan, fallback: data.usage, poke: data.poke || {} };
```

`static/src/chrome/UsagePill.jsx` — the file's header comment describes the poll payload as `{ plan, fallback }`; extend it to `{ plan, fallback, poke }` with `poke` = "each account's latest session poke, for the tooltip". Change `acctTitle`'s signature to `function acctTitle(a, expanded, poke)` and, after the `lines` map and before the `if (a.stale)` line, add:
```js
  // The morning session poke (periscope.poke): when it fired and whether the
  // 5h reset actually landed ~5h later. "not anchored" means a window was
  // already open, or the anchoring assumption broke — the server log has the
  // detail.
  if (poke) {
    const outcome = poke.verified ? `resets ${fmtClock(poke.resets_at)}` : "not anchored";
    lines.push(`poked ${fmtClock(poke.at)} → ${outcome}`);
  }
```
and at the call site (line ~145) pass it: `title={acctTitle(a, expanded, u.poke?.[a.id])}`.

- [ ] **Step 4: Run the tests, lint, build**

Run: `npm test` — Expected: all pass.
Run: `bin/check` — Expected: zero violations.
Run: `npm run build` — Expected: `static/dist/app.js` rebuilt.

- [ ] **Step 5: Commit**

```bash
git add static/src/poll.js static/src/chrome/UsagePill.jsx static/src/chrome/__tests__/usagePillRender.test.jsx static/dist
git commit -m "usage pill: 'poked 08:01 → resets 13:01' per account in the tooltip; rebuild dist"
```

---

### Task 8: Docs

**Files:**
- Modify: `docs/account-routing.md` (append), `CLAUDE.md` (module table + reference-docs row)

- [ ] **Step 1: Append to `docs/account-routing.md`**

```markdown
## Session poke (`poke.py`)

Every day at `settings.poke_at` (default 08:00; `""` disables) periscope runs
`claude -p ok --model claude-haiku-4-5 --strict-mcp-config` under each
account's `CLAUDE_CONFIG_DIR` so the 5h session window is anchored at 08:00
and resets ~13:00 — four hours on each side of the working day. Both
accounts: with session-pressure rerouting both see daily use, and the second
call costs nothing.

| Guard | Rule | Why |
|---|---|---|
| open window | skip while `session.resets_at` is in the future; re-check every tick | the poke could not move the reset; the window may close inside the grace |
| grace | fire only inside `[poke_at, poke_at + poke_grace_min)` (default 90 min) | a 10:00 catch-up shortens the first work block instead of helping it |
| once a day | skip an account whose `poke_log` entry carries today's date | — |
| in flight | skip an account whose worker thread is running | the 08:00 and 08:01 ticks must not both spend |
| credential | skip an unavailable account | it could not authenticate either |
| prod only | the task is registered only under `config.is_prod()` | the dev instance never spends |
| verify | after the poke, `usage.refresh_plan_usage_now`; `verified` iff `session.resets_at` is within ±5 min of poke + 5h; logged at WARNING when not | the warning is the signal that the anchoring assumption is wrong |

The outcome rides `/api/state.poke` and shows in the usage pill's account
tooltip (`poked 08:01 → resets 13:01`). The poke's env comes from
`config.claude_subprocess_env`, which also serves background-commander jobs:
API-key vars stripped (bill the subscription, not API credits) and
`CLAUDE_CONFIG_DIR` set-or-popped (never an account nobody chose).
```

- [ ] **Step 2: `CLAUDE.md`**

Module table — add a row after the `bg_commander.py` row:

`| `poke.py` | Daily session poke: one Haiku message per account at 08:00 so the 5h window resets ~13:00; verified, logged, prod-only |`

Reference-docs table — extend the `launch_policy.py` row's first cell (added by Unit 1) with `, `poke.py``.

- [ ] **Step 3: Commit**

```bash
git add docs/account-routing.md CLAUDE.md
git commit -m "docs: session poke section in account-routing.md; CLAUDE.md rows"
```

---

### Task 9: Real-binary check and final verification

- [ ] **Step 1: Suites**

Run: `uv run pytest -q` — Expected: all pass.
Run: `npm test` — Expected: all pass.
Run: `bin/check` — Expected: zero violations.
Run: `git status --short` — Expected: empty.

- [ ] **Step 2: One manual poke against the real binary (the one place a mock could hide a production failure)**

Run in the worktree under a SCRATCH config dir. `store.record_poke` and the usage refetch write `state.json` and `periscope.db` wholesale, and prod is running on the real ones — a second process writing them is the two-instances-one-store clobber `config.instance_file` exists to prevent. `XDG_CONFIG_HOME` redirects both (`config.config_dir()`), while the account registry and the keychain lookup still resolve the real `~/.claude-b`:
```bash
XDG_CONFIG_HOME=$(mktemp -d) uv run python -c "
from periscope import poke, store
poke._in_flight.add('b')
poke.poke_account('b', store.account_config_dir('b'))
print(store.get_poke_log())
"
```
Expected: within ~30s, one log line `poke b at HH:MM: session resets HH:MM (anchored)` if B's window was closed, or `(NOT anchored — reset did not move)` if one was open, and the printed log entry has `verified` matching. Then confirm against prod's own view: `curl -s http://127.0.0.1:8765/api/state | python3 -c "import json,sys; print(json.load(sys.stdin)['usage_plan']['b']['meters']['session'])"` shows the same `resets_at` (allow up to 5 min for prod's next refresh).

Note in the completion message which outcome you observed and the exact log line — this is the evidence the spec's anchoring assumption holds on the real endpoint.

- [ ] **Step 3: Report**

Paste the last ~20 lines of `uv run pytest -q` and the manual poke output. Unit 2 is then ready for whole-branch review and merge.
