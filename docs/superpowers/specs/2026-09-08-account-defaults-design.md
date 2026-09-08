# Account & model defaults: earliest-reset routing, session poke, move override

Periscope chooses which subscription and which model every unnamed Claude
launch lands on. Today the rule is "most headroom" on the account axis and a
static pin on the model axis. This replaces both with one policy whose
objective is: **at each account's weekly reset, its Fable sub-limit and its
weekly-all meter are both at 100%.** Any headroom at reset is waste.

Three facts about the setup drive the design. Fable's weekly sub-limit is
capped at roughly half of weekly-all (live meters on 2026-09-08: B at 14%
Fable / 9% weekly-all; the `usage.py` comment on the `limits` array), so
draining an account means Fable *and then* a non-Fable model.
`~/.claude-b/projects` symlinks to `~/.claude/projects`, so a session is not
bound to the account it started on — `claude --resume` works under either
`CLAUDE_CONFIG_DIR`. And the two meters anchor differently, per five weeks of
prod `usage_samples` (2026-08-19 → 2026-09-08):

- **Weekly resets are a fixed cadence.** B resets Wed 23:00 and A Sun 10:00
  every week, including when the first use after a reset came 21h later (A:
  reset Sun 06 10:00, first non-zero sample Mon 07 07:26, next reset still Sun
  13 10:00). `resets_at` is reported `null` whenever utilization is 0 — a
  fresh account, not an unanchored one. Nothing periscope does can move a
  weekly reset.
- **The 5h session window is anchored at its first message.** A closed window
  reports `resets_at = null` (B: 03:40, 0%); the next window's reset is first
  message + 5h (B: 15:20 = 10:20 + 5h). This is what the poke exploits.

## Decisions

### D1 — Account order: earliest weekly reset first; fail over only on a hard wall

Accounts are ranked by their weekly-all `resets_at`, soonest first. The
soonest-resetting account is the one whose unused budget expires first, so it
is drained first (earliest-deadline-first over perishable inventory). An
account is skipped only when it **cannot work**: its 5h session meter or its
weekly-all meter is at ≥100%, or the sub-limit of the model being launched is
at ≥100%. No projection on the weekly axis — switching off an account before
it is actually walled preserves exactly the headroom this policy exists to
burn.

Both accounts walled → the one whose *session* meter resets soonest (the user
is blocked either way; minimize the wait). No usage data at all → `default`,
as today; a launch never waits on usage.

### D2 — Model order within an account: Fable, then Opus 1M; drain the account before moving on

Within the chosen account: Fable unless its sub-limit is walled, else
`opus[1m]` unless Opus's sub-limit (when the plan reports one) is walled. The
candidate list is therefore

```
(soonest, fable) → (soonest, opus[1m]) → (other, fable) → (other, opus[1m])
```

and the first non-walled pair wins. This is EDF-strict: Opus on the
soonest-resetting account beats Fable on the other, because the soonest
account's weekly-all is the budget expiring first. Opus is not a fallback; it
is how the non-Fable half of every account's week gets used. The model pin is
the escape hatch when Fable-today matters more than waste.

Rejected: a projection-based Fable→Opus downgrade ("projected >100% and actual
>75%"). It preserves Fable headroom by design, and the 50% cap means the Opus
half must be spent regardless.

### D3 — Session pressure reroutes new spawns (projection is right here)

The 5h session meter is the throughput cap, not a perishable budget: not
using the other account's 5h window costs nothing, and both windows burning in
parallel is twice the daily rate. So when the account D1/D2 would pick is **on
pace to wall its session before it resets** (the existing `limit_at`
projection, 1h slope), new spawns go to the next account in D1 order, model
chosen by D2 on that account. Only when every account is session-pressured
does the pick ignore pressure. An explicitly chosen account is never rerouted.

### D4 — One chooser, server-side, published on `/api/state`

`usage.choose_launch(account, model)` is the single choke point on both axes
and replaces `best_account` and `store.spawn_model_env`. The server publishes
its current answer as `launch_default = {account, model, reason}` on every
poll; the launcher preselects from it and the header shows it. The client's
own copy of the rule (`usageSummary.bestAccount`) is deleted. Rejected:
keeping a client mirror — two implementations of one policy that now consults
projections.

The chooser is deterministic: ties on identical weekly `resets_at` break by
account registry order. `best_account`'s random tie-break goes, because the
published value and the value a spawn resolves must agree — a random
tie-break would make the header chip alternate A/B every 3s poll.

Background-commander jobs (`bg_account`) are **not** routed through the
chooser: `claude agents` / `claude stop` are per-config-dir, and a job whose
account is re-resolved between dispatch and sync is the unkillable-job
incident `bg_commander._account_env` documents. `bg_account` keeps its
current pop-when-unset semantics.

### D5 — Pins override one axis each; `auto` is the model pin's default

An account pin forces the account; the model is still chosen by D2 within it.
A model pin forces the model; the account is still chosen by D1/D3 against
that model's sub-limit. The header model pin gains an explicit `auto` value,
and an unset pin means `auto`. `default` keeps its current meaning (no
`ANTHROPIC_MODEL`, the account's `settings.json` decides).

`settings.spawn_model` therefore stores three kinds of value literally:
`"auto"`, `"default"`, or a model id; unset reads as `auto`. Today the
settings route and `SpawnModelPicker` coerce `"default"` to unset, which
under the new reading would silently turn "no override" into "chooser
decides" — both stop coercing, and the validator accepts the two words
alongside model-id-shaped strings. `config.model_env` never sees either word:
the chooser resolves them first (`default` → no override).

The launcher's per-launch pickers send explicit, already-resolved values on
**both** axes. Today the launcher omits the `account` param for account A
(`accountQuery` maps `"default"` to null), which would let the server re-run
the chooser and land a launch the user pointed at A on B; it sends the param
unconditionally.

Resume paths (dashboard resume, the MCP `resume_session` tool, move-account)
carry the **account only** and never a model: `--resume` restores the
session's own model unless `ANTHROPIC_MODEL` is set at launch, and a moved
pane must change nothing but its subscription (D7).

### D6 — Session poke: both accounts, 08:00 local, Haiku, verified

Every day at 08:00 periscope sends one Haiku message on each account so the
5h window is anchored there and resets ~13:00 — the middle of the working day
instead of wherever the first real message happened to land. Both accounts,
not just the preferred one: with D3 both see daily use, and the second call
costs nothing.

- Skip an account while a 5h window is already open on it (`session.resets_at`
  in the future). The poke would not move the reset. Re-check each tick; the
  window may close inside the grace period.
- Catch-up grace of 90 minutes: a poke fired past ~09:30 (Mac asleep at 8)
  would shorten the first work block instead of helping it, so past the grace
  window the day is skipped.
- Verify: after the poke, refetch that account's usage and check
  `session.resets_at` landed within ±5 min of now+5h. Log info on success,
  warning on miss — the warning is the signal that the anchoring assumption is
  wrong. The pill tooltip shows the outcome (`poked 08:01 → resets 13:01`).
- Prod only (`config.is_prod()`): the dev instance never spends. The gate is at
  task registration in `app.lifespan`, and the task is cancelled in the
  lifespan `finally`, exactly like `mcp_task` and `activity_task` — a gate
  inside the coroutine would leave a live task behind on every dev `--reload`.
- In-flight guard: an account being poked is excluded from `due()` until its
  log entry is written (the `_plan_in_flight` pattern), or the 08:00 and 08:01
  ticks both spend.
- The weekly cadence is fixed (see the top of this doc), so a poke moves only
  the 5h window; it cannot erode D1's stagger.

### D7 — Running panes stay; moving one is manual, and the busy refusal can be overridden

A preference flip (B's Wed-night reset) changes only new spawns. Moving a
running pane is the existing `/api/pane/move-account`, which is refused with
409 when the transcript was written to in the last 60s. That guard misreads
exactly the case it matters for: Claude writes the limit-reached message into
the JSONL, so a walled pane looks live. The endpoint gains `force=1`, which
skips **only** the mtime guard (never the already-resumed-elsewhere guard),
and the client turns that specific 409 into a "move anyway?" confirm. The
original pane still stays open, as today.

### D8 — Waste indicator on the usage pill

The objective made visible: each weekly meter's tooltip line shows its
projected end-of-week value when it is under 100% ("on pace for 18% at
reset"). An account row gets a 💤 marker when its Fable meter projects under
100% **and** its reset is within 48h — the point past which the remaining
budget is unlikely to be burned. The inverse of the existing 🔥 signal, from
the same `projected_percent` field.

### D9 — Three independently shippable units

The chooser (D1–D5 with its state/UI), the poke (D6), and the move override
plus waste indicator (D7, D8) share no code and no state. The plan delivers
them as three phases, each mergeable and demoable on its own; the chooser
goes first because it is what the other two are measured against.

### Not doing

- Automatic hop of a walled pane to the other account (kill + resume mid-turn
  is a different risk class; D7's manual path first).
- Staggered pokes (only helps if the later account is untouched until its poke
  time, which D3 defeats).
- Codex panes: no subscription to choose between; unchanged.
- Rebalancing running panes when the preference flips.
- Routing background-commander jobs (see D4).
- The `limits`-loop-after-`_PLAN_METERS` ordering in `parse_plan_usage`, under
  which an Opus-scoped `limits` entry would overwrite the `seven_day_opus`
  meter under the same `week_opus` key. Both are Opus walls, so the chooser's
  answer is the same either way; noted, not fixed here.

## Chooser

`periscope/usage.py`:

```python
@dataclass(frozen=True)
class Launch:
    account: str          # account id
    model: str | None     # ANTHROPIC_MODEL value, None = no override
    reason: str           # "B · resets Wed 22:59 · fable" / "A: B session on pace to wall 15:19"

def choose_launch(account: str | None = None, model: str | None = None) -> Launch
```

Inputs: `cached_plan_usage()` (never blocks; stale-while-revalidate as today),
`store.get_settings()` for the two pins, `store.get_accounts()` (registry
order is the tie-break).

Resolution:

1. `account` explicit (arg, else `settings.spawn_account` naming a registered
   account) → candidate accounts = `[account]` and `ignore_pressure = True`.
   Else candidates = available accounts ordered by `week_all.resets_at`
   ascending, registry order on ties; an account whose `resets_at` is null
   sorts **last** — null means 0% used, i.e. it reset most recently and is the
   later deadline; unavailable accounts (no meters) are excluded.
2. `model` explicit (arg, else pin unless `auto`/unset) → candidate models =
   `[model]`; `"default"` passes through as `None`. Else `["fable", "opus[1m]"]`.
3. Walls. `walled(acct)` = `session ≥ 100 or week_all ≥ 100`.
   `walled(acct, model)` additionally checks the model's sub-limit meter,
   found by **prefix**: the alias's family (`fable`, `opus`, `sonnet` —
   `opus[1m]` → `opus`) matches any meter keyed `week_<family>` or
   `week_<family>_*`. Sub-limit keys are slugified display names
   (`parse_plan_usage`), so an exact `week_fable` lookup would silently stop
   walling the day the display name becomes "Fable 5.1". A missing meter is
   not a wall. `pressured(acct)` = `session.limit_at is not None` (the
   existing 1h-slope projection; `_SLOPE_WINDOW_S` defines it for `session`).
4. First pass: for acct in candidates, for model in models: not walled and
   (`ignore_pressure` or not pressured) → return. Second pass: same without
   the pressure check. Third (everything walled): the candidate account with
   the soonest `session.resets_at`, first candidate model.
5. No candidate accounts (no usage data) → `Launch("default", <explicit model
   or None>, "no usage data")`.

There is no hysteresis: every transition on the weekly axis is monotone until
a reset, and flicker on the session axis is just balancing.

Call sites. Spawn paths pass their explicit args through and use the result
for both `account_config_dir` and `config.model_env`: `open_ops.open_path`,
`channels._do_spawn_claude_tool`, `routes/sessions` window-new,
`worktree_spawn._layout_two_window`. Resume paths use
`choose_launch(account=explicit).account` and never the model: the dashboard
resume in `routes/sessions` (today it passes no account at all and bills the
default), the MCP `resume_session` tool, and `pane_move_account` (whose
account is always explicit).

## State and UI

- `/api/state` gains `launch_default: {account, model, reason}` (computed per
  poll from the cache; cheap) and `poke: {account_id: {at, resets_at,
  verified}}`.
- Launcher (`LauncherModal.openLauncher`): account and model preselect from
  `launch_default`; no client-side derivation; the submit sends `account`
  unconditionally (D5). `usageSummary.bestAccount` and its tests go.
- Header pickers: the model picker offers `auto` (first, default); when the
  pin is `auto`/unset the chip reads `auto → fable`; likewise the account chip
  `auto → B` when unpinned. Hover shows `reason`. `SpawnModelPicker` stores
  `"default"` and `"auto"` literally (D5).
- `PATCH /api/settings`: `spawn_model` accepts `"auto"`, `"default"`, a
  model-id-shaped string, or null (= auto); `poke_at` (HH:MM or null) and
  `poke_grace_min` (int) added.
- `static/src/models.js`: `auto` is a pin-only entry; the launcher list is
  unchanged.
- Usage pill: D8's tooltip lines and 💤; the poke outcome line per account.

## Poke

`periscope/poke.py`, loop registered in `app.lifespan` as `poke_task =
_task("poke", poke.run()) if config.is_prod() else None` beside `mcp_task`,
cancelled in the lifespan `finally` with the others; 60s tick.

- Settings: `poke_at: "08:00"` (`null` disables), `poke_grace_min: 90`. Both
  through `PATCH /api/settings`.
- Persisted log in `state.json`: `poke_log: {account_id: {date, at,
  resets_at, verified}}` — `date` is what "already poked today" reads.
- `due(now, settings, log, usage, in_flight) -> list[account_id]` is pure:
  local date/time from `now`; returns each registered account that is
  available, not logged for today, not in flight, has no open session window
  (`session.resets_at` null or past), and for which `poke_at ≤ local now <
  poke_at + grace`.
- `poke(account)`: `subprocess.run([claude, "-p", "ok", "--model",
  "claude-haiku-4-5", "--strict-mcp-config"], env=...)` — the full id, as
  `rename_ai.py` uses, because `claude --help` documents only `fable`/`opus`/
  `sonnet` as aliases and a rejected alias would fail silently every morning.
  `CLAUDE_CONFIG_DIR` from `store.account_config_dir`;
  `ANTHROPIC_API_KEY`/`ANTHROPIC_AUTH_TOKEN` stripped (the same spend-leak
  guard as `bg_commander._dispatch_env`; shared, not duplicated); 120s
  timeout; in `_bg`. The binary is resolved the way
  `bg_commander._dispatch_argv` does, never via the zsh wrapper.
- Then `usage.refresh_plan_usage_now(account)` (new: bypasses the cache TTL
  for one account) and verify `session.resets_at ∈ [now+5h−5m, now+5h+5m]`.
  Write the log entry either way with `verified` set accordingly.

## Move-account override

- `_window_new_resume(..., force: bool = False)`: `force` skips the mtime
  guard only. The 409 detail includes the age: `"session looks live (written
  12s ago); wait a minute or pick another"`.
- `POST /api/pane/move-account?pid&account&force=1` passes it through.
- `Rail.movePaneAccount` uses a raw `fetch` rather than `apiCall`: `apiCall`
  toasts every non-OK response and returns null, so the 409 detail never
  reaches the caller. On a 409 whose detail starts with `session looks live`,
  `confirmDialog("…written to 12s ago — move anyway? The original pane stays
  open.")` → retry with `force=1`. Any other failure toasts as before.

## Tests

- `tests/test_usage.py` — `choose_launch` table: reset ordering; null
  `resets_at` sorts last; each wall (session, weekly-all, Fable sub-limit,
  Opus sub-limit) on the soonest account; sub-limit matched by prefix
  (`week_fable_5_1` walls `fable`); both walled → soonest session reset;
  session pressure reroutes; all pressured → ignores pressure; explicit
  account ignores pressure; model pin routes by that model's sub-limit;
  `default` passes through as no override; no data → `default`; tie →
  registry order. `refresh_plan_usage_now` bypasses the TTL.
- `tests/test_poke.py` — `due` over (now, settings, log, usage, in_flight):
  fires at 08:00; skips an open window then fires when it closes inside
  grace; skips past grace; skips when logged today; skips in-flight; disabled
  when `poke_at` is null; verification success/miss writes the log.
  Subprocess and refetch mocked; nothing spawns a thread that touches the
  activity DB.
- `tests/routes/test_sessions.py` — `force=1` bypasses the mtime guard and
  not the already-resumed guard; 409 detail carries the age; dashboard resume
  passes the chosen account; `spawn_model` settings accept `auto`/`default`
  literally.
- `static/src/__tests__` — launcher seeds from `launch_default` and sends
  `account` for A; `bestAccount` tests removed; header chip renders `auto →
  fable`; picker round-trips `default` and `auto` unchanged.

## Docs

- New `docs/account-routing.md`: the rule table above, the poke, and the
  incident each guard cites; indexed from `CLAUDE.md`'s touching-X-read-Y
  table for `usage.py`, `poke.py`, `routes/sessions.py` (move-account).
- `docs/wrapper-profiles.md`: the model-override paragraph names
  `choose_launch` as the choke point instead of `spawn_model_env`.
- `CLAUDE.md` module table: `poke.py`.
