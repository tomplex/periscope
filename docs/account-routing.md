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
| 5 | An account whose session is **on pace to wall within the next hour** (`limit_at` set and ≤ 60 min out) is skipped on a first pass; a second pass ignores pressure when the first finds nothing (every account pressured, or the unpressured ones walled) | Session headroom is a rate limit, not a budget: the other window costs nothing to use and two windows in parallel is 2× the daily rate. The horizon: the 1h burn slope is reliable about an hour out and no further — a wall projected two hours ahead at 17:30 mostly never arrives as evening usage tails off, and rerouting on it moved spawns off an account that was about to be fine (2026-09-08) |
| 6 | Everything walled → the account whose session resets soonest | Blocked either way; minimize the wait |
| 7 | An explicit account (call arg or header pin) is never rerouted; the model is still chosen within it. An explicit model (arg or pin) fixes the model; the account is chosen against that model's sub-limit. An account pin naming no registered account is ignored | Pins override one axis each. `account_config_dir` fails open to the default on an unknown id, so honoring a stale pin would silently reroute every spawn |
| 8 | `spawn_model`: `"auto"` (or unset) = rules 2–3; `"default"` = no `ANTHROPIC_MODEL`; an alias = pinned | Stored literally — coercing `default` to unset would silently turn "no override" into "chooser decides". A pin clicked to `default` before this landed was stored as unset and now reads as `auto`; click `default` again to restore it |
| 9 | No usage data → the explicit account, else `default`; the explicit or pinned model, else no override | A launch never waits on usage |

`choose_launch`'s answer is published as `launch_default` on every
`/api/state` poll; the header chips render `auto → fable` / `auto → B` from
it and the launcher preselects from it, sending both values explicitly.

## Not routed

Resurrect (`resurrect.py`) re-emits a restored pane's OWN account and model
env — it restores intent, it does not choose.

Background-commander jobs (`bg_account`): `claude agents` / `claude stop` are
per-config-dir, and a job whose account is re-resolved between dispatch and
sync is the unkillable-job incident `bg_commander._account_env` documents.

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
| verify | after the poke, `usage.refresh_plan_usage_now` until the reading's `fetched_at` postdates the poke (one retry after 15s — a refresh already in flight yields the pre-poke cache); `verified` iff `session.resets_at` is within ±5 min of poke + 5h; logged at WARNING when not — "NOT anchored — reset did not move" for a real post-poke reading, "no post-poke reading" when none could be had | "reset did not move" is the signal that the anchoring assumption is wrong, so it must never be logged from a stale reading; the second text keeps the two apart |

The outcome rides `/api/state.poke` and shows in the usage pill's account
tooltip (`poked 08:01 → resets 13:01`). The poke's env comes from
`config.claude_subprocess_env`, which also serves background-commander jobs:
API-key vars stripped (bill the subscription, not API credits) and
`CLAUDE_CONFIG_DIR` set-or-popped (never an account nobody chose).

## Moving a running pane (`/api/pane/move-account`)

A preference flip changes only new spawns. Moving a running pane is manual:
the rail's move action resumes the same session on the other account in a
second pane (the original stays open — `~/.claude-b/projects` is a symlink to
`~/.claude/projects`, so either account can resume it). The server refuses
with 409 when the transcript was written to in the last 60s, because two
concurrent appenders would interleave into one JSONL. That guard misreads the
case it matters most for: Claude writes the *limit-reached* message into the
transcript, so a walled pane always looks live. `force=1` skips only that
guard — never the already-resumed-elsewhere one — and the client offers it
as "Move anyway?" on exactly that 409 (matched on the detail's
`session looks live` prefix; the server test and the client test pin the
text together).

## 💤 — Fable budget on pace to go unused

The pill marks an account 💤 when its `week_fable*` meter projects under 100%
at reset and the reset is within 48h: past that point the remaining budget is
unlikely to be burned. It is the inverse of 🔥 (on pace to blow), computed on
the client from the same `projected_percent` field (`usageSummary.wasteMark`).
