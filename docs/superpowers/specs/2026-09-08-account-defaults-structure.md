# Account & model defaults — code structure

**Status:** proposal, for review before the implementation plan is written
**Spec:** `docs/superpowers/specs/2026-09-08-account-defaults-design.md`
**Date:** 2026-09-08

Structural blueprint only: what shape the code takes, which unit owns which
decision. No sequencing — that is the plan's job. Laid out per D9's three
shippable units.

Tier labels, as in the cost-pressure structure doc:

- **mechanical** — delete / rename / move / transcribe a table.
- **bounded** — implement against a stated signature with a fully enumerable
  case list.
- **judgment** — where the ambiguity actually lives.

---

## Spec pushback

**P1. The chooser splits into a pure `periscope/launch_policy.py` plus a
six-line shell in `usage.py`.** The spec puts `choose_launch` wholesale in
`periscope/usage.py`. That file is 668 lines and already three concerns (JSONL
aggregation, the OAuth plan-usage path, the cost-pressure I/O shell); the
chooser is a fourth, and it is the one with a fully enumerable case table — the
spec's own §Tests lists thirteen cases, none of which need a file, a socket, or
a keychain. This is the exact split C1 of the cost-pressure structure doc got
approved for, and `usage.py` already imports its pure core from
`cost_pressure.py` (`usage.py:29-36`), so the dependency direction is
established. The spec's call-site inventory does not change: every caller still
writes `usage.choose_launch(...)`, because `usage.choose_launch` is the shell
that gathers `cached_plan_usage()` / `get_settings()` / `get_accounts()` and
calls `launch_policy.choose()`.

**P2. `config.model_env` must swallow `"auto"` the way it swallows
`"default"`.** D5 stores `"auto"` literally in `settings.spawn_model`, and
`_MODEL_OK = re.compile(r"^[A-Za-z0-9._:\[\]-]+$")` (`config.py:69`) accepts it.
So the moment any path forwards a raw pin to `model_env` without passing through
the chooser, the pane launches with `ANTHROPIC_MODEL=auto` — which Claude
rejects, silently, per-pane. The spec's answer is "the chooser resolves them
first", which is a discipline. One line in `model_env` makes it structural, and
it costs nothing: the chooser never emits `"auto"` anyway.

**P3. The poke's worker thread writes the activity DB, which is the leaked-thread
class `docs/testing.md` documents.** `poke_account()` calls
`usage.refresh_plan_usage_now(account)`, which reaches
`activity.record_usage_samples` — a live httpx fetch plus a sqlite write, in a
daemon thread, landing in whatever per-test `ACTIVITY_DB` is live when it
finishes. That is exactly the CPython 3.14 use-after-free segfault the autouse
`_no_plan_usage_refresh` fixture exists to prevent, and that fixture neuters
`usage._bg` by name, not `poke._bg`. Structural consequence, not a test note:
`poke.py` must import `_bg` into its own module namespace (`from periscope.log
import _bg`, called as a module global, never `log._bg(...)`), and
`tests/conftest.py`'s autouse guard must gain `monkeypatch.setattr(poke, "_bg",
lambda *a, **kw: None)`.

**P4. `poke_at: null` cannot mean "disabled" — `update_settings` deletes null
keys.** `store.update_settings` pops any key whose patch value is `None`
(`store.py:530-541`), so `PATCH {"poke_at": null}` leaves the key absent, and
absent has to read as the `"08:00"` default or the feature never runs out of the
box. Null and unset are the same state; the spec assigns them opposite meanings.
Proposal: `poke_at: str` where unset reads `"08:00"`, and the **empty string**
disables. The settings validator accepts `""`, `null` (pops, back to the
default), or `HH:MM`. Document it on the `Settings` TypedDict field, which is
where the other unset-semantics comments already live (`store.py:89-91`).

**P5. The API-key strip plus `CLAUDE_CONFIG_DIR` belongs in `config.py`, not
copied out of `bg_commander`.** The spec says "shared, not duplicated" without
saying where. `bg_commander._dispatch_env` (`bg_commander.py:156-174`) and
`_account_env` (`:176-197`) together are two things: strip
`ANTHROPIC_API_KEY`/`ANTHROPIC_AUTH_TOKEN` so a subprocess bills the
subscription rather than API credits, and set-or-pop `CLAUDE_CONFIG_DIR` so it
bills the intended one. Both are spend-leak guards, both now have a second
consumer, and `config.py` already owns the sibling env plumbing (`model_env`,
`profile_env`, `claude_bin`, `MODEL_ENV_VAR`). Extract
`config.claude_subprocess_env(*, config_dir: str) -> dict[str, str]`;
`_dispatch_env` becomes that plus `PERISCOPE_CALLER_ID`, and `_account_env`
disappears into it. The two incident comments move with the code, not away from
it.

**P6. `refresh_plan_usage_now` must respect `_plan_in_flight`, or it clears
another thread's flag.** `_refresh_plan_usage_into_cache` ends with
`_plan_in_flight.discard(account)` unconditionally (`usage.py:428`). Calling it
directly while a background refresh for the same account is in flight makes the
poke's return discard that thread's marker, and the next
`cached_plan_usage()` poll fires a duplicate refresh into the endpoint that
429s readily. The new function adds the account to `_plan_in_flight` under
`_plan_lock` first, exactly as `cached_plan_usage` does, and skips when it is
already there.

**P7. D8's tooltip line already ships.** `UsagePill.paceLines`
(`UsagePill.jsx:59-74`) already renders `window average → ${m.projected_percent}%
at reset` for every meter that has a `projected_percent`, and
`attach_projections` already computes it for dynamically-keyed `week_*`
sub-limit meters via its weekly fallback (`usage.py:346-347`). So D8 is not a
new projection field and not a server change at all: it is the 💤 marker plus,
if wanted, suppressing the existing line at ≥100%. The plan should budget unit 3
accordingly — it is smaller than the spec reads.

**P8. `Rail.movePaneAccount` should use `modalRequest`, not a bare `fetch`.**
The spec proposes a raw `fetch` because `apiCall` toasts and returns null
(`util.js:151-156`), which is correct about `apiCall`. But
`static/src/overlays/modalRequest.js` already exists for precisely this — it
returns `{data}` or `{error: data.detail || …}` instead of toasting. It drops
only one thing the 409 branch needs: the status code. Add `status: res.status`
to its failure shape (additive; every existing caller reads `.error` / `.data`
only) and Rail imports it. One raw `fetch` in a 619-line component is how the
next one gets added.

---

## Assumptions

- **A1.** `model_family` handles full model ids, not only the three aliases the
  spec names. `"opus[1m]"` → `opus`, but so must `"claude-opus-5"` — the picker
  offers aliases while `config.model_env` accepts any model-id-shaped string a
  user types into prefs by hand (`models.js:10-12`). Rule: strip a trailing
  `[...]` suffix, then return the first of `fable`/`opus`/`sonnet`/`haiku` that
  appears as a token; `None` when nothing matches, and `None` means no sub-limit
  meter and therefore no wall.
- **A2.** `"default"` as an explicit model arg reaches the chooser and comes back
  as `Launch(model=None)`. It never reaches `config.model_env`, but `model_env`
  keeps its own `"default"` → `""` branch as the existing fail-open.
- **A3.** `launch_default` on `/api/state` is computed from the already-cached
  plan usage on every poll, including the ~1s `state_hub` loop. The chooser is a
  dict read plus at most four candidate iterations, so no memoization is
  proposed. It must stay allocation-cheap; no logging inside it.
- **A4.** The account picker's chip already offers `auto`
  (`SpawnAccountPicker.jsx:15`). D5's "likewise the account chip `auto → B`" is
  a label change on an existing control, not a new value.
- **A5.** `ensure_session` and `_layout_two_window` gain a `model` parameter
  whose meaning is **already-resolved** — the `ANTHROPIC_MODEL` value or `None`
  for no override — replacing today's "picker choice, `None` falls to the pin".
  `store.spawn_model_env` is deleted, so every caller of `_layout_two_window`
  and `_window_new_plain` must resolve before calling. That is the point of D4,
  but it means the parameter's contract silently changes; the docstrings must
  say so.
- **A6.** The poke's `due()` reads local wall-clock date and time from the `now`
  it is handed. Tests pass a naive local `datetime`; no timezone parameter.
- **A7.** The verification refetch (`session.resets_at` within ±5 min of
  now+5h) tolerates the endpoint's own staleness. If `refresh_plan_usage_now`
  returns `None` (fetch failed), the log entry is written with
  `verified: false`, not skipped — a skipped entry would re-poke on the next
  tick and spend twice.

---

## File layout

### Unit 1 — the chooser (D1–D5)

```
periscope/launch_policy.py            NEW — pure decision core: Launch,
                                      LaunchInputs, Meter/AccountUsage
                                      TypedDicts, model_family, walled,
                                      pressured, order_accounts, choose.
                                      stdlib imports only.
periscope/usage.py                    CHANGED — choose_launch() shell (~8 lines);
                                      DELETES best_account + the `random` and
                                      `Callable` imports it owns;
                                      parse_plan_usage annotated -> AccountUsage.
periscope/store.py                    CHANGED — DELETES spawn_model_env; the
                                      Settings.spawn_model / spawn_account
                                      comments restate the new semantics.
periscope/config.py                   CHANGED — model_env swallows "auto" (P2).
periscope/routes/state.py             CHANGED — launch_default in build_state.
periscope/routes/settings.py          CHANGED — spawn_model stores "auto" /
                                      "default" literally.
periscope/routes/sessions.py          CHANGED — window_new plain + resume,
                                      pane_move_account route through the chooser.
periscope/open_ops.py                 CHANGED — ensure_session gains model.
periscope/channels.py                 CHANGED — _do_spawn_claude_tool,
                                      _do_resume_session_tool.
periscope/worktree_spawn.py           CHANGED — _layout_two_window model contract.
static/src/models.js                  CHANGED — PIN_MODELS (auto first) beside
                                      the unchanged launcher MODELS.
static/src/chrome/SpawnModelPicker.jsx    CHANGED — literal "auto"/"default";
                                      chip reads the resolved default.
static/src/chrome/SpawnAccountPicker.jsx  CHANGED — "auto → B" chip label.
static/src/chrome/usageSummary.js     CHANGED — DELETES bestAccount.
static/src/overlays/LauncherModal.jsx CHANGED — seed from launch_default;
                                      account param sent unconditionally.
static/src/store.js, poll.js          CHANGED — launchDefault signal.
tests/test_launch_policy.py           NEW — the whole decision table, zero fixtures.
tests/test_usage.py                   CHANGED — best_account tests deleted; a
                                      handful of choose_launch shell tests.
tests/routes/test_state.py            CHANGED — launch_default rides the poll.
tests/routes/test_settings.py         CHANGED — auto/default round-trip.
static/src/chrome/__tests__/usageSummary.test.js  CHANGED — bestAccount tests go.
static/src/overlays/__tests__/LauncherModal.test.js CHANGED — accountQuery.
```

### Unit 2 — the poke (D6)

```
periscope/poke.py                     NEW — PokeEntry, due() pure, poke_account()
                                      I/O, run() tick loop.
periscope/config.py                   CHANGED — claude_subprocess_env (P5).
periscope/bg_commander.py             CHANGED — _dispatch_env uses it;
                                      _account_env folds in.
periscope/usage.py                    CHANGED — refresh_plan_usage_now(account).
periscope/store.py                    CHANGED — get_poke_log / record_poke;
                                      Settings gains poke_at, poke_grace_min.
periscope/app.py                      CHANGED — poke_task registration + cancel.
periscope/routes/settings.py          CHANGED — poke_at / poke_grace_min validation.
periscope/routes/state.py             CHANGED — "poke" block on the poll.
static/src/chrome/UsagePill.jsx       CHANGED — poke outcome line in acctTitle.
tests/test_poke.py                    NEW
tests/conftest.py                     CHANGED — neuter poke._bg (P3).
CLAUDE.md                             CHANGED — poke.py module row + doc index.
docs/account-routing.md               NEW.
docs/wrapper-profiles.md              CHANGED — choke point is choose_launch.
```

### Unit 3 — move override + waste indicator (D7, D8)

```
periscope/routes/sessions.py          CHANGED — _window_new_resume(force=),
                                      409 detail carries the age,
                                      pane_move_account force param.
static/src/overlays/modalRequest.js   CHANGED — failure shape gains status (P8).
static/src/split/Rail.jsx             CHANGED — movePaneAccount confirm + retry.
static/src/chrome/usageSummary.js     CHANGED — wasteMark() pure helper.
static/src/chrome/UsagePill.jsx       CHANGED — 💤 on the account row.
tests/routes/test_sessions.py         CHANGED
static/src/chrome/__tests__/usageSummary.test.js      CHANGED
static/src/chrome/__tests__/usagePillRender.test.jsx  CHANGED
```

`static/dist/app.js` is rebuilt and committed with each of the three units that
touches `static/src/`.

---

## Per-module structure

### Unit 1

#### `periscope/launch_policy.py` — rung 2 (frozen data + pure functions)

New module. No I/O, no threads, no `periscope` imports except `store`'s `Account`
TypedDict for the registry shape. This is where every case in the spec's
§Chooser resolution list lives, and it is implementable from the signatures below
without opening `usage.py`.

```python
class Meter(TypedDict, total=False):
    label: str
    percent: int
    utilization: float
    resets_at: int | None
    projected_percent: int | None
    projected_recent: int | None
    limit_at: int | None
    hot: bool

class AccountUsage(TypedDict, total=False):
    available: bool
    meters: dict[str, Meter]
    fetched_at: int

FALLBACK_MODELS: tuple[str, ...] = ("fable", "opus[1m]")

@dataclass(frozen=True)
class Launch:
    account: str
    model: str | None      # ANTHROPIC_MODEL value; None = no override
    reason: str

@dataclass(frozen=True)
class LaunchInputs:
    accounts: tuple[str, ...]                  # registry order — the tie-break
    usage: Mapping[str, AccountUsage]
    account_arg: str | None
    model_arg: str | None
    account_pin: str | None                    # settings.spawn_account
    model_pin: str | None                      # settings.spawn_model, incl "auto"

def model_family(model: str) -> str | None: ...
def sublimit(meters: Mapping[str, Meter], model: str) -> Meter | None: ...
def walled(meters: Mapping[str, Meter], *, model: str | None = None) -> bool: ...
def pressured(meters: Mapping[str, Meter]) -> bool: ...
def order_accounts(inputs: LaunchInputs) -> tuple[str, ...]: ...
def choose(inputs: LaunchInputs) -> Launch: ...
```

`LaunchInputs` is a frozen dataclass rather than six keyword args because the
test table is built by `dataclasses.replace(base, model_pin="fable")` — one
field per case, the `narrator.py` idiom (`from dataclasses import dataclass,
replace`, `narrator.py:22`). Six parameters is also over Tom's typed-config
threshold.

`Meter` / `AccountUsage` are TypedDicts, not dataclasses, because they are the
shape `parse_plan_usage` already produces and `/api/state` already serializes —
DTOs crossing a boundary, no behavior attached. Annotating
`parse_plan_usage(data: dict) -> AccountUsage` (`usage.py:223`) makes that
contract checked rather than remembered, and the import direction matches
`usage.py` already importing `CostSample` from `cost_pressure.py`.

| Unit | Tier | Notes |
|---|---|---|
| the two TypedDicts + `Launch` + `LaunchInputs` | mechanical | |
| `model_family` | bounded | strip `[...]`, then first token match against the four families; `None` on no match (A1) |
| `sublimit` | bounded | `week_<family>` or `week_<family>_*` prefix scan. The spec's own rationale is the docstring: an exact `week_fable` lookup stops walling the day the display name becomes "Fable 5.1", because the key is a slugified display name (`usage.py:267`) |
| `walled` | bounded | `session >= 100 or week_all >= 100`, plus the sub-limit when `model` is given. A missing meter is never a wall |
| `pressured` | mechanical | `session.limit_at is not None` |
| `order_accounts` | bounded | available accounts by `week_all.resets_at` ascending, `None` sorting **last**, registry index as the tie-break. `sorted(key=lambda a: (resets_at is None, resets_at or 0, index))` |
| `choose` | **judgment** | the three passes; ~30 lines |
| `_reason` | bounded | copy only, kept separate so a wording edit cannot break a routing assertion |

`choose` is the one judgment unit in unit 1. Its shape:

1. Resolve `account`: `account_arg`, else `account_pin` when it names a
   registered account, else `None`. An explicit account sets
   `ignore_pressure = True` and collapses the candidate list to that one id.
2. Resolve `model`: `model_arg`, else `model_pin` unless it is `"auto"` or
   unset, else `None`. Explicit collapses the candidate models to one;
   `"default"` becomes `None` at the point of return, not here — the wall check
   for `"default"` is the account-level check with no sub-limit.
3. First pass over `(account, model)` pairs skipping walled and pressured;
   second pass skipping only walled; third pass returns the candidate account
   with the soonest `session.resets_at`.
4. No candidate accounts → `Launch("default", <explicit model or None>, "no usage
   data")`.

**There is no `random` anywhere in this module, and that is the point.** D4's
determinism requirement is what makes the published `launch_default` and a
spawn's resolution agree; a random tie-break would flip the header chip on every
3s poll. Deleting `best_account`'s `rand` parameter deletes the only source.

#### `periscope/usage.py` — rung 1 (functions)

```python
def choose_launch(account: str | None = None, model: str | None = None) -> Launch:
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

Positional `account` / `model` (not keyword-only) because the spec's published
signature is positional and every call site names them anyway. The gather is the
only impure part; `cached_plan_usage` stays a module global so the existing
`monkeypatch.setattr(usage, "cached_plan_usage", ...)` idiom
(`tests/test_usage.py:495`) keeps working for the shell tests.

`best_account` and its docstring are deleted whole. The pin rationale in that
docstring ("lives HERE rather than at the call sites because this function is
the one choke point every unnamed spawn path shares") moves to `choose_launch`,
because it is still true and still the reason the function exists.

#### Call sites — all mechanical

| Site | Change |
|---|---|
| `open_ops.ensure_session:199-207` | `launch = usage.choose_launch(account)`; pass `account=launch.account, model=launch.model` to `_layout_two_window`. Codex branch untouched |
| `channels._do_spawn_claude_tool:597-602` | one `choose_launch(arguments.get("account"), arguments.get("model"))`, feeding both `account_config_dir` and `config.model_env` |
| `channels._do_resume_session_tool:915` | `choose_launch(account=arguments.get("account")).account`; no model |
| `routes/sessions.window_new:500` | resume branch passes `account=usage.choose_launch(account=account).account` — today it passes no account at all and bills the default |
| `routes/sessions._window_new_plain:291-293` | `store.spawn_model_env(model)` → `config.model_env(launch.model)` |
| `routes/sessions.pane_move_account:542` | account is always explicit and already validated against the registry at `:527`; it still goes through `choose_launch(account=account).account` so one function owns the mapping |
| `worktree_spawn._layout_two_window:261-265` | `store.spawn_model_env(model)` → `config.model_env(model)`; docstring records that `model` is now pre-resolved (A5) |

**Not touched, deliberately:** `bg_commander`. D4's reasoning is the
unkillable-job incident already documented at `bg_commander.py:176-197`, and
that comment stays where it is.

#### `periscope/routes/settings.py` — mechanical, one block rewritten

The `spawn_model` block (`:100-107`) stops coercing. New rule: `None` clears;
`"auto"` and `"default"` store literally; anything else must pass
`config.model_env`. The existing comment explaining why a bad value must fail
loudly rather than fail open stays and gains one sentence: under D5's reading,
coercing `"default"` to unset would silently convert "no override" into "chooser
decides", which are now different launches.

#### `periscope/routes/state.py` — mechanical

```python
"launch_default": dataclasses.asdict(usage.choose_launch()),
```
beside the existing `spawn_account` / `spawn_model` keys (`:171-174`), which stay
— the chips still need the raw pin to render which button is active.

#### Frontend

`static/src/models.js` — `MODELS` unchanged (the launcher list), plus
`export const PIN_MODELS = [{ id: "auto", label: "auto" }, ...MODELS];`. Two
named lists beat one list plus a filter at each call site, and the file's
existing header comment already explains the two-surface split.

`SpawnModelPicker.jsx` — delete the `m.id === "default" ? null : m.id`
normalization at `:40`; map `PIN_MODELS`; active state is
`(spawnModel.value || "auto") === m.id`. When the active pin is `auto`, the
label renders `auto → ${launchDefault.value?.model ?? "default"}`.
`SpawnAccountPicker.jsx` gets the same treatment against
`accountLabel(launchDefault.value?.account)`. Both chips gain
`title={launchDefault.value?.reason}`.

The `auto → X` composition is written inline in each picker rather than
extracted: two consumers, four lines each, and they differ (one goes through
`accountLabel`, one does not).

`usageSummary.js` — `bestAccount` (`:83-100`) and its `rand` parameter are
deleted along with the whole `describe` block in
`__tests__/usageSummary.test.js:82-113`. `summarizeAccounts` is untouched.

`LauncherModal.jsx` — `openLauncher`'s seed block (`:223-226`) becomes
`account.value = launchDefault.value?.account || "default"` and
`model.value = launchDefault.value?.model || "default"`; the `bestAccount`
import at `:32` goes. In `run(t)` (`:285-286`), `accountQuery` is no longer used
to decide whether to *send* the param — the value is sent unconditionally for a
Claude agent target, matching how `model` already behaves at `:292`. `accountQuery`
itself is deleted; its only job was the omit decision, and D5 removes it. That
resolves the three-way inconsistency in the current code where `account` omits,
`model` always sends, and `SpawnModelPicker` normalizes inline.

`store.js` / `poll.js` — `export const launchDefault = signal(null);` and one
line in `applyState` below the editing/drag guards, the documented pattern.

### Unit 2

#### `periscope/poke.py` — rung 1 with a pure core

Modeled on `narrator.py`: the decision is pure, `run()` is the only place I/O
happens.

```python
class PokeEntry(TypedDict):
    date: str          # local YYYY-MM-DD — what "already poked today" reads
    at: int            # epoch of the poke
    resets_at: int | None
    verified: bool

TICK_S = 60.0
DEFAULT_POKE_AT = "08:00"
DEFAULT_GRACE_MIN = 90
_POKE_MODEL = "claude-haiku-4-5"   # full id: `claude --help` documents only
                                   # fable/opus/sonnet as aliases
_VERIFY_TOLERANCE_S = 300
_SESSION_WINDOW_S = 5 * 3600
_SUBPROCESS_TIMEOUT_S = 120

def due(*, now: datetime, settings: Settings,
        log: Mapping[str, PokeEntry],
        usage: Mapping[str, AccountUsage],
        in_flight: AbstractSet[str],
        accounts: Sequence[Account]) -> list[str]: ...

def verified(resets_at: int | None, *, at: float) -> bool: ...

def poke_account(account_id: str, config_dir: str) -> None: ...   # thread body
async def run() -> None: ...                                      # tick loop
```

| Unit | Tier | Notes |
|---|---|---|
| `PokeEntry` + constants | mechanical | |
| `due` | **judgment** | six conjoined conditions; the enumerable case list |
| `verified` | mechanical | `abs(resets_at - (at + 5h)) <= 300`, `False` on `None` |
| `poke_account` | bounded | subprocess → `refresh_plan_usage_now` → `verified` → `store.record_poke`; discard from `_in_flight` in a `finally` |
| `run` | bounded | `while True`: build the argument set, `_bg` each due account, `await asyncio.sleep(TICK_S)`. Re-raises `CancelledError` like `_lgtm_periodic_refresh` (`lgtm.py:199-209`) |

`due` is pure and takes everything it needs, which is what makes the spec's
seven test cases fixture-free. It is the only place local-time arithmetic
happens: `now.strftime("%Y-%m-%d")` for the date key, `now.hour*60+now.minute`
against the parsed `poke_at` and the grace window. It returns account ids in
registry order.

The in-flight set is module state (`_in_flight: set[str]`), passed into `due` as
a parameter rather than read from inside it — same reason `attach_projections`
takes an injectable `samples_for` (`usage.py:331-337`): the seam keeps the pure
function free of module globals.

`poke_account` builds its argv the way `bg_commander._dispatch_argv` does —
`config.claude_bin()`, never the zsh wrapper, never `CLAUDE_EXEC` — and its env
from `config.claude_subprocess_env(config_dir=...)` (P5). `--strict-mcp-config`
is there so the poke does not spin up the channel shim for a one-word prompt.

No class here. There is no coupled mutable state: the log is in `state.json`, the
in-flight set is one module-level `set`, and the tick loop holds nothing between
iterations.

#### `periscope/config.py` — one new function

```python
def claude_subprocess_env(*, config_dir: str) -> dict[str, str]:
```
Returns `dict(os.environ)` with `ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN`
popped and `CLAUDE_CONFIG_DIR` set when `config_dir` is truthy, popped
otherwise. Both incident comments from `bg_commander` move here verbatim —
the API-credit spend leak and the "unset POPS rather than leaving whatever
leaked in" rule.

#### `periscope/usage.py` — one new function

```python
def refresh_plan_usage_now(account: str, config_dir: str) -> AccountUsage | None:
```
Takes the in-flight slot under `_plan_lock` (P6), calls the existing
`_refresh_plan_usage_into_cache` synchronously, returns the cached payload.
Blocking — it is called from the poke's worker thread, never from the event
loop or a request handler.

#### `periscope/store.py` — two typed accessors

```python
def get_poke_log() -> dict[str, PokeEntry]: ...
def record_poke(account_id: str, entry: PokeEntry) -> None: ...
```
Same shape as `get_settings` / `update_settings`: `_STATE_LOCK`, a copy out, a
`_write_state` on the way in. The log lives at `state["poke_log"]`.
`Settings` gains `poke_at: str` and `poke_grace_min: int` with the unset/empty
semantics from P4 as their comments.

#### `periscope/app.py` — mechanical, mirroring `mcp_task`

Inside the existing `if config.is_prod():` block that already guards
`activity_task`, add `poke_task = _task("poke", poke.run())`, `else: poke_task =
None`, and `if poke_task is not None: poke_task.cancel()` in the `finally`.
The spec's reasoning is right and worth keeping as a comment: a gate inside the
coroutine leaves a live task behind on every dev `--reload`.

#### `UsagePill.jsx` — one line in `acctTitle`

`poke` rides `/api/state` as `{account_id: {at, resets_at, verified}}`. The
account tooltip gains `poked 08:01 → resets 13:01` (or `poked 08:01 → not
anchored` when `verified` is false), formatted with the existing `fmtClock`
(`:44`). No new component.

### Unit 3

#### `periscope/routes/sessions.py` — mechanical plus one signature

`_window_new_resume(..., force: bool = False)`. The mtime block (`:173-176`)
becomes:

```python
if not force and mtime_age < 60:
    raise HTTPException(409, f"session looks live (written {int(mtime_age)}s ago); "
                             "wait a minute or pick another")
```

`force` is a boolean flag on a function that already takes five parameters, which
would normally argue for a config object — it stays a flag here because it is a
single guard bypass on an existing internal signature, and bundling five
unrelated positional params into a dataclass is a refactor this unit did not ask
for. Flagged in close calls.

The already-resumed guard at `:178-180` is **not** behind `force`, and that must
be a comment, not just an ordering: two concurrent appenders interleaving into
one JSONL is the failure the guard exists for, and it is unrelated to the walled
pane D7 is about.

`pane_move_account(pid: str, account: str, force: bool = False)` passes it
through. The registry validation at `:527` is untouched — `force` never widens
which account is legal.

#### `static/src/split/Rail.jsx` — `movePaneAccount`

```js
async function movePaneAccount(w, accountId, force = false) {
  const { data, error, status } = await modalRequest("move account", url, { method: "POST" });
  if (data?.pid) { /* select, persist */ return; }
  if (status === 409 && error?.includes("session looks live") && !force) {
    if (await confirmDialog(`…${error}… — move anyway? The original pane stays open.`,
                            { okLabel: "Move anyway" })) {
      return movePaneAccount(w, accountId, true);
    }
    return;
  }
  showToast(error, "bad", 6000);
}
```

One recursion level, guarded by the `force` parameter, so a second 409 cannot
loop. Not `danger: true` — the move is additive (the original pane stays open,
`sessions.py:511-517`), so the red-OK / focus-Cancel treatment the other two
`confirmDialog` call sites use (`Rail.jsx:256`, `:335`, both destructive) would
misrepresent it.

Matching on the detail *string* is fragile and it is the reason the server-side
detail text and this check must be a single fact: the 409 detail is asserted in
`tests/routes/test_sessions.py`, and the client match is asserted in the vitest
render test. Both cite each other.

#### `static/src/chrome/usageSummary.js` — one pure export

```js
export function wasteMark(acct, nowSec) // -> boolean
```
True when the account has a `week_fable*`-prefixed meter with
`projected_percent < 100` and `resets_at - nowSec < 48 * 3600`. The prefix scan
duplicates the server's `sublimit` in four lines of JS; that is accepted rather
than shipping a server-computed boolean, because it keeps unit 3 free of any
Python change and therefore independently shippable, which is D9's whole claim.

`UsagePill` renders it as `<span class="usage-acct-waste">💤</span>` in the
account row beside the existing `.usage-stale-mark`, and adds a tooltip line.
The 🔥 at `:102` is untouched — 💤 is the account-row inverse, not a meter-level
one.

---

## Patterns

**Used:**

- *Pure decision core + I/O shell* — `launch_policy.py` vs `usage.choose_launch`;
  `poke.due` vs `poke.run`. The `narrator.py` / `cost_pressure.py` shape this
  repo already documents.
- *Frozen dataclass value objects* — `Launch`, `LaunchInputs`. `replace()` in
  the test table, as `narrator.py` does.
- *TypedDict for DTOs crossing a boundary* — `Meter`, `AccountUsage`,
  `PokeEntry`, matching `store.Account` / `store.Settings` /
  `activity.PaneStatusRow`.
- *Injected seam instead of a module global* — `due(..., in_flight=...)`,
  mirroring `attach_projections(samples_for=...)`.
- *Keyword-only args* on `due`, `walled`, `verified`,
  `claude_subprocess_env`.
- *Module-level task registered in `app.lifespan`, cancelled in the `finally`*
  — `poke_task` beside `lgtm_task` / `activity_task`.
- *Typed accessors over raw `_STATE`* — `get_poke_log` / `record_poke`.
- *Write-boundary validation* — `poke_at` / `poke_grace_min` / `spawn_model` in
  `routes/settings.py`, the file's established convention.

**Considered and rejected:**

- *A `LaunchChooser` class holding the registry and the meters* — no coupled
  mutable state; every input is read fresh per call, by design (D4's
  determinism).
- *A strategy/policy interface so the ranking rule is swappable* — one policy,
  and the spec names no second. The rejected projection-based downgrade in D2 is
  documented as rejected, not as a future implementation.
- *A custom `NoUsageDataError`* — "no usage data" is an ordinary outcome
  returning `Launch("default", …, "no usage data")`; nothing catches a type.
  Same for the settings validators, which keep raising `HTTPException(400)`.
- *A `Poke` class owning the log and the in-flight set* — the log is in
  `state.json` and the in-flight set is one module `set`; a class would only
  group functions.
- *Putting `choose_launch` behind an `lru_cache` for the ~1s state-hub loop* —
  it is a dict read and four iterations, and a cache would need invalidating on
  every settings write.
- *Server-computed 💤* — would put a unit-3 change into `usage.py` and break
  D9's independence claim for four lines of duplicated prefix matching.
- *A raw `fetch` in `Rail.jsx`* — see P8.
- *Routing `bg_account` through the chooser* — D4; the unkillable-job incident.
- *A shared `auto → X` chip helper* — two consumers, four lines, divergent
  label functions.
- *A `ResumeOptions` dataclass to absorb `_window_new_resume`'s parameters* —
  see close call C4.

---

## Test strategy

| Module | Approach | Dependencies |
|---|---|---|
| `launch_policy.py` | `tests/test_launch_policy.py` — pure unit, **zero fixtures, no monkeypatch, no `clean_state`**. One `LaunchInputs` base literal plus `dataclasses.replace` per case. Every case in the spec's §Tests list: reset ordering; null `resets_at` last; each of the four walls on the soonest account; `week_fable_5_1` walls `fable` by prefix; both walled → soonest session reset; pressure reroutes; all pressured → ignore pressure; explicit account ignores pressure; model pin routes by that model's sub-limit; `default` → `model is None`; no data → `"default"`; tie → registry order. Plus `model_family` over `opus[1m]` / `claude-opus-5` / `fable` / a garbage string | none |
| `usage.choose_launch` | `tests/test_usage.py` — three or four shell tests only: the pin is read from settings, the registry order is the tuple order, `cached_plan_usage` is the meter source. `monkeypatch.setattr(usage, "cached_plan_usage", …)` + `clean_state`, the existing idiom at `test_usage.py:495`. The routing table does **not** live here | one monkeypatch |
| `usage.refresh_plan_usage_now` | `tests/test_usage.py` — monkeypatch `fetch_plan_usage`; assert it bypasses the TTL (a cached entry with a future `next_at` still refetches) and that it leaves `_plan_in_flight` empty on both the success and the exception path | monkeypatch |
| `poke.due` | `tests/test_poke.py` — pure unit over `(now, settings, log, usage, in_flight, accounts)`. Fires at 08:00; skips an open window then fires when it closes inside grace; skips past grace; skips when logged today; skips in-flight; disabled on empty `poke_at`; both accounts on the same tick. Naive local `datetime` literals | none |
| `poke.poke_account` | `tests/test_poke.py` — monkeypatch `poke.subprocess.run` and `poke.refresh_plan_usage_now`; assert the argv (full Haiku id, `--strict-mcp-config`, `config.claude_bin()`), the env (`CLAUDE_CONFIG_DIR` set, both API-key vars absent), and that the log entry is written with `verified` true on a landing reset and false on a miss **and** on a `None` refresh (A7). `clean_state` for the store write | two monkeypatches |
| `config.claude_subprocess_env` | `tests/test_config.py` — real `os.environ` via `monkeypatch.setenv`, both directions of the `CLAUDE_CONFIG_DIR` set/pop | monkeypatch |
| `bg_commander` | `tests/test_bg_commander.py` — the existing `_dispatch_env` assertions stay green unchanged. That is the regression test for the P5 extraction; do not rewrite them to call the new function | as today |
| `routes/settings.py` | `tests/routes/test_settings.py` — real `TestClient`, `clean_state`. `spawn_model` round-trips `"auto"` and `"default"` literally (the regression this feature introduces); a garbage value 400s; `poke_at` accepts `HH:MM` and `""` and rejects `"25:00"`; `poke_grace_min` rejects a negative | none mocked |
| `routes/state.py` | `tests/routes/test_state.py` — `launch_default` and `poke` ride the poll with the right keys | as today |
| `routes/sessions.py` | `tests/routes/test_sessions.py` — **real tmp-file transcripts**, not a mocked `os.path.getmtime`: `force=1` bypasses the mtime guard, `force=1` does **not** bypass the already-resumed guard, the 409 detail carries the integer age. Plus: the dashboard resume path passes the chosen account (monkeypatch `usage.choose_launch`, assert what reaches `_window_new_resume`) | real filesystem |
| `open_ops`, `channels`, `worktree_spawn` call sites | existing test modules — `monkeypatch.setattr(usage, "best_account", …)` at `test_open_ops.py:395,403,412` and `test_channels.py:857,1545,1579,1593` becomes `choose_launch` returning a literal `Launch`. Mechanical, but it is seven call sites and the plan should list them | as today |
| `tests/conftest.py` | `_no_plan_usage_refresh` gains `monkeypatch.setattr(poke, "_bg", lambda *a, **kw: None, raising=False)` inside a `try/ImportError` block, matching the existing shape. **This is not optional** — see P3 | — |
| `usageSummary.js` | `static/src/chrome/__tests__/usageSummary.test.js` — delete the `bestAccount` describe (`:82-113`); add `wasteMark` cases: under 100% inside 48h, under 100% outside 48h, at 100%, no fable meter | none |
| `LauncherModal.jsx` | `static/src/overlays/__tests__/LauncherModal.test.js` — `accountQuery` tests deleted with the function; add an assertion that the submit query string carries `account` for the default account | none |
| pickers + pill | `chrome/__tests__/spawnModelPickerRender.test.jsx` — the `auto` entry renders first and is active when the pin is unset; the chip reads `auto → fable` from `launchDefault`. `usagePillRender.test.jsx` — 💤 renders on a wasteful account and not otherwise; the poke line lands in the account title | none |
| `Rail.movePaneAccount` | Not unit-tested. It is a `fetch` + `confirmDialog` + recursion, and per `vitest.config.js:4-7` interaction is browser-verified. The two testable facts are asserted on either side: the 409 detail string in `tests/routes/test_sessions.py`, and `modalRequest` returning `status` in a new case in the overlays tests | none |
| gate | `bin/check` at zero violations; `uv run pytest -q`; `npm test`; `npm run build` + commit `static/dist/app.js` | — |

**Testability flags:**

- **No mocked usage endpoint anywhere in the routing tests.** The routing table
  runs against literal meter dicts in a pure function, which is stronger than
  mocking `httpx`: a mocked fetch that returns a shape the real endpoint no
  longer sends is the failure mode, and `parse_plan_usage`'s existing tests are
  what guard the shape. Keep those two layers separate.
- **`poke_account` is the one unit where a mock could hide a production
  failure** — a mocked `subprocess.run` passes while `claude -p` rejects the
  model id or the flag every morning at 08:00. The structural mitigation is the
  verification step itself (D6): a real miss logs a warning and shows in the
  pill. The plan should include one manual run against the real binary before
  merge, and say so; that is not something a test can cover.
- **Nothing in this feature requires constructing a tmux pane, a window view, or
  a DB row to reach its logic.** `due` and `choose` are both reachable with
  literals only. If either ends up needing a fixture during implementation, the
  split is wrong.

---

## Decisions to sanity-check

**C1. New module `periscope/launch_policy.py` rather than a section of
`usage.py`.** Alternative: the spec's own placement, inside `usage.py` beside
where `best_account` lives today. Close because `best_account` really is there,
every caller already writes `usage.<something>`, and the shell keeps that true
either way — so the split buys only test isolation and file size. Decided for
the split: thirteen fixture-free routing cases in front of 668 lines of OAuth,
keychain, and JSONL code is the same argument that carried C1 in the
cost-pressure doc, and "launch policy" names one concept with one home.

**C2. `LaunchInputs` as a frozen dataclass rather than six keyword-only
arguments.** Close because the repo does write multi-argument functions
(`attach_projections` takes four), and a single-call-site parameter object can
read as ceremony. Decided for the dataclass because the test table is
`replace(base, field=…)` per case — thirteen call sites with five repeated
arguments each is worse than one literal and thirteen one-field overrides.

**C3. `Meter` / `AccountUsage` as TypedDicts in `launch_policy.py`, with
`usage.parse_plan_usage` annotated to return one.** Close because it makes the
pure module the owner of a shape the impure one produces, which reads backwards.
Decided that way because the alternative is `Mapping[str, object]` and casts at
every read, and `usage.py` already imports its value types from
`cost_pressure.py` — the direction is established.

**C4. `_window_new_resume` gains a bare `force: bool` flag, its sixth
parameter.** Close because a boolean flag on an already-long signature is
exactly what the typed-config rule exists to prevent, and the honest fix is a
`ResumeRequest` frozen dataclass. Decided for the flag: bundling five unrelated
existing parameters is a refactor of a function three call sites reach through
`HTTPException`-based control flow, it would bury unit 3's actual change, and
unit 3 is supposed to be the small one. Worth doing later, on its own commit.

**C5. Rail's 409 branch matches on the detail string.** Close because
string-matching an error message is brittle, and the alternative — a machine
field, e.g. `detail` as a dict or a custom header — is more robust. Decided for
the string because every route in this repo reports errors as
`raise HTTPException(status, detail)` with a human sentence (CLAUDE.md
conventions), and introducing a structured error shape for one endpoint would be
the first of its kind. The mitigation is that both halves are asserted, in
`tests/routes/test_sessions.py` and in the overlays test.

**C6. Unit 3's 💤 rule is duplicated on the client rather than stamped by the
server.** Close because the prefix-matching rule now exists in two languages,
and P-class drift between them is exactly what D4 deletes `usageSummary.bestAccount`
to avoid. Decided for the duplication because it is a four-line display rule with
no launch consequence, and stamping it server-side would put a `usage.py` change
inside unit 3 and break D9's independence. If it grows a second rule, move it.

**C7. `poke_at: ""` disables rather than `null` (P4).** Close because `""` as a
sentinel is not obvious from the API, and the alternative — a separate
`poke_enabled: bool` — is explicit. Decided against the boolean because it is a
two-field state with an unrepresentable-illegal-states problem (`poke_enabled:
true, poke_at: null`), and `Settings` already carries unset-means-something
comments for four other fields.
