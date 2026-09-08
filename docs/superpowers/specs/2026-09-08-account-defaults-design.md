# Account & model defaults: earliest-reset routing, session poke, move override

Periscope chooses which subscription and which model every unnamed Claude
launch lands on. Today the rule is "most headroom" on the account axis and a
static pin on the model axis. This replaces both with one policy whose
objective is: **at each account's weekly reset, its Fable sub-limit and its
weekly-all meter are both at 100%.** Any headroom at reset is waste.

Two facts about the setup drive the design. Fable's weekly sub-limit is capped
at roughly half of weekly-all (live meters on 2026-09-08: B at 14% Fable /
9% weekly-all), so draining an account means Fable *and then* a non-Fable
model. And `~/.claude-b/projects` symlinks to `~/.claude/projects`, so a
session is not bound to the account it started on — `claude --resume` works
under either `CLAUDE_CONFIG_DIR`.

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

### D5 — Pins override one axis each; `auto` is the model pin's default

An account pin forces the account; the model is still chosen by D2 within it.
A model pin forces the model; the account is still chosen by D1/D3 against
that model's sub-limit. The header model pin gains an explicit `auto` value,
and an unset pin means `auto`. `default` keeps its current meaning (no
`ANTHROPIC_MODEL`, the account's `settings.json` decides). The launcher's
per-launch pickers always send explicit, already-resolved values.

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
- Prod only (`config.is_prod()`): the dev instance never spends.

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

### Not doing

- Automatic hop of a walled pane to the other account (kill + resume mid-turn
  is a different risk class; D7's manual path first).
- Staggered pokes (only helps if the later account is untouched until its poke
  time, which D3 defeats).
- Codex panes: no subscription to choose between; unchanged.
- Rebalancing running panes when the preference flips.

## Chooser

`periscope/usage.py`:

```python
@dataclass(frozen=True)
class Launch:
    account: str          # account id
    model: str | None     # ANTHROPIC_MODEL value, None = no override
    reason: str           # "B · resets Wed 22:59 · fable" / "A: B session on pace to wall 15:19"

def choose_launch(account: str | None = None, model: str | None = None,
                  *, rand=random.random) -> Launch
```

Inputs: `cached_plan_usage()` (never blocks; stale-while-revalidate as today),
`store.get_settings()` for the two pins, `store.get_accounts()`.

Resolution:

1. `account` explicit (arg, else `settings.spawn_account` naming a registered
   account) → candidate accounts = `[account]`, pressure ignored. Else
   candidates = available accounts ordered by `week_all.resets_at` ascending;
   an account with no `resets_at` sorts last; unavailable accounts (no meters)
   are excluded.
2. `model` explicit (arg, else pin unless `auto`/unset) → candidate models =
   `[model]`; `"default"` passes through as `None`. Else `["fable", "opus[1m]"]`.
3. Walls. `walled(acct)` = `session ≥ 100 or week_all ≥ 100`.
   `walled(acct, model)` additionally checks the model's sub-limit meter:
   `fable → week_fable`, `opus`/`opus[1m]` → `week_opus`, `sonnet →
   week_sonnet`; a missing meter is not a wall. `pressured(acct)` =
   `session.limit_at is not None`.
4. First pass: for acct in candidates, for model in models: not walled and not
   pressured → return. Second pass: same without the pressure check. Third
   (everything walled): the candidate account with the soonest
   `session.resets_at`, first candidate model.
5. No candidate accounts (no usage data) → `Launch("default", <explicit model
   or None>, "no usage data")`.

`rand` breaks ties on identical `resets_at`, as `best_account` does today.
There is no hysteresis: every transition on the weekly axis is monotone until
a reset, and flicker on the session axis is just balancing.

Call sites, each passing its explicit args through and using the result for
both `account_config_dir` and `config.model_env`: `open_ops.open_path`,
`channels._do_spawn_claude_tool`, `channels` resume tool, `routes/sessions`
window-new, `worktree_spawn._layout_two_window`. `bg_commander._account_env`
uses `choose_launch().account` when `bg_account` is unset.

## State and UI

- `/api/state` gains `launch_default: {account, model, reason}` (computed per
  poll from the cache; cheap) and `poke: {account_id: {at, resets_at,
  verified}}`.
- Launcher (`LauncherModal.openLauncher`): account and model preselect from
  `launch_default`; no client-side derivation. `usageSummary.bestAccount` and
  its tests go.
- Header pickers: the model picker offers `auto` (first, default); when the
  pin is `auto`/unset the chip reads `auto → fable`; likewise the account chip
  `auto → B` when unpinned. Hover shows `reason`.
- `static/src/models.js`: `auto` is a pin-only entry; the launcher list is
  unchanged.
- Usage pill: D8's tooltip lines and 💤; the poke outcome line per account.

## Poke

`periscope/poke.py`, loop registered in `app.lifespan` as
`_task("poke", poke.run())`, 60s tick, `config.is_prod()` gate at task start.

- Settings: `poke_at: "08:00"` (`null` disables), `poke_grace_min: 90`. Both
  through `PATCH /api/settings`.
- Persisted log in `state.json`: `poke_log: {account_id: {date, at,
  resets_at, verified}}` — `date` is what "already poked today" reads.
- `due(now, settings, log, usage) -> list[account_id]` is pure: local date/time
  from `now`; returns each registered account that is available, not logged
  for today, has no open session window, and for which `poke_at ≤ local now <
  poke_at + grace`.
- `poke(account)`: `subprocess.run([claude, "-p", "ok", "--model", "haiku",
  "--strict-mcp-config"], env=...)` with `CLAUDE_CONFIG_DIR` from
  `store.account_config_dir` and `ANTHROPIC_API_KEY`/`ANTHROPIC_AUTH_TOKEN`
  stripped (the same spend-leak guard as `bg_commander._dispatch_env`; shared,
  not duplicated), 120s timeout, in `_bg`. The binary is resolved the way
  `bg_commander._dispatch_argv` does, never via the zsh wrapper.
- Then `usage.refresh_plan_usage_now(account)` (new: bypasses the cache TTL
  for one account) and verify `session.resets_at ∈ [now+5h−5m, now+5h+5m]`.
  Write the log entry either way with `verified` set accordingly.

## Move-account override

- `_window_new_resume(..., force: bool = False)`: `force` skips the mtime
  guard only. The 409 detail includes the age: `"session looks live (written
  12s ago); wait a minute or pick another"`.
- `POST /api/pane/move-account?pid&account&force=1` passes it through.
- `Rail.movePaneAccount`: on a 409 whose detail starts with `session looks
  live`, `confirmDialog("…written to 12s ago — move anyway? The original pane
  stays open.")` → retry with `force=1`.

## Tests

- `tests/test_usage.py` — `choose_launch` table: reset ordering; each wall
  (session, weekly-all, Fable sub-limit, Opus sub-limit) on the soonest
  account; both walled → soonest session reset; session pressure reroutes;
  all pressured → ignores pressure; explicit account ignores pressure; model
  pin routes by that model's sub-limit; `default` passes through as no
  override; no data → `default`; tie → `rand`. `refresh_plan_usage_now`
  bypasses the TTL.
- `tests/test_poke.py` — `due` over (now, settings, log, usage): fires at
  08:00; skips an open window then fires when it closes inside grace; skips
  past grace; skips when logged today; disabled when `poke_at` is null;
  verification success/miss writes the log. Subprocess and refetch mocked;
  nothing spawns a thread that touches the activity DB.
- `tests/routes/test_sessions.py` — `force=1` bypasses the mtime guard and
  not the already-resumed guard; 409 detail carries the age.
- `static/src/__tests__` — launcher seeds from `launch_default`;
  `bestAccount` tests removed; header chip renders `auto → fable`.

## Docs

- New `docs/account-routing.md`: the rule table above, the poke, and the
  incident each guard cites; indexed from `CLAUDE.md`'s touching-X-read-Y
  table for `usage.py`, `poke.py`, `routes/sessions.py` (move-account).
- `docs/wrapper-profiles.md`: the model-override paragraph names
  `choose_launch` as the choke point instead of `spawn_model_env`.
- `CLAUDE.md` module table: `poke.py`.
