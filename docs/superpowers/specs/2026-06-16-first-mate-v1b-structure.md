# First Mate — v1b (Live integration) — code-structure proposal

**Spec:** `docs/superpowers/specs/2026-06-16-first-mate-design.md` (v1 → v1b)
**Continuity:** `docs/superpowers/specs/2026-06-16-first-mate-v1a-structure.md`
**Scope:** v1b only — wire the inert v1a substrate into a live, prod-gated,
budget-spending first-mate pane. Supervisor + system prompt + heartbeat +
adapter + interrupt hooks. No conn, no spawn/terminate-by-first-mate (v2).
**Date:** 2026-06-16

v1a already landed on this branch: `first_mate.py` (`PaneDigest`/`FleetDigest`,
`build_fleet_digest`, `fleet_diverged`), `activity.py` storage
(`get/set/clear_first_mate`, `append/recent_captain_log`, the `captain_log` +
`first_mate` tables), and the two captain's-log MCP tools in `channels.py`. This
proposal builds the IO half that calls all of it.

---

## 1. Spec pushback

Three structural assumptions in the spec/inputs I disagree with or need to
correct against the real code:

- **"The `bridge` tmux session (already created as the interim home)" — it does
  not exist.** `grep -rn bridge periscope/` finds nothing that creates a
  `bridge` session; the only matches are unrelated ("anyio-bridged", "LGTM
  bridge"). The supervisor must **create the `bridge` session on first spawn**
  (the `new-session -d -s bridge` path), not assume it. This is a one-line
  correction but it changes the supervisor's spawn to "ensure-session-then-
  window," exactly the shape `_do_spawn_claude_tool` already branches on
  (`has-session` → `new-session` vs `new-window`, channels.py:478-490). Treated
  as fact in §3/§4; flagged here because the spec asserts otherwise.

- **The spawn must use `config.claude_exec()`, NOT the `CLAUDE_EXEC` constant.**
  `_do_spawn_claude_tool` reaches for the bare constant (`from periscope.config
  import CLAUDE_EXEC`, channels.py:505) — which means its spawn path is **not
  stubable** via the `PERISCOPE_CLAUDE_EXEC` test seam. `_layout_two_window`
  does it right: `from periscope.config import claude_exec; exec_cmd =
  claude_exec()` (worktree_spawn.py). The supervisor's whole automated test
  story (`tmux_test_server` sets `PERISCOPE_CLAUDE_EXEC=cat`) **depends on going
  through `claude_exec()`**. So I do not "reuse `_do_spawn_claude_tool`'s shape"
  literally — I reuse `_layout_two_window`'s exec resolution and consent
  handling. Load-bearing; see §4 supervisor.

- **The supervisor cannot call `_layout_two_window` directly — it raises
  `HTTPException`.** `worktree_spawn._layout_two_window` is "deliberately coupled
  to FastAPI" (its docstring) and `raise HTTPException(500, ...)` on tmux
  failure. A lifespan task is not in a request; an `HTTPException` there is just
  a confusing 500-shaped exception with no handler. The supervisor needs its own
  small single-window spawn primitive (one Claude window in `bridge`, not the
  two-window claude+shell layout), so it is **new code that borrows the
  send-keys + `claude_exec()` + `dismiss_dev_channels_consent_bg` +
  `stamp_new_window` sequence**, not a call into the HTTP-coupled layout
  helper. This is also right on the merits: the first mate is one pane, it has
  no "shell" sibling.

Nothing else in the v1b spec violates the taste rules — it explicitly mirrors
the narrator's pure-core-plus-worker-tick shape, which is the right call.

---

## 2. Assumptions

- **The first-mate window is single-window, named `first-mate`, in session
  `bridge`.** Spec says "the `bridge` tmux session"; I name the window
  `first-mate` (matching the spec's `bridge:first-mate` demo reference). The
  supervisor's liveness check keys on the marker's stored `pane_id` (`%N`), not
  the window name — so a user-rename of the window doesn't trip a respawn.

- **`--append-system-prompt` is appended to the `claude_exec()` string, with the
  role text passed as a single shell-quoted argument.** The spec names this as
  "the one new flag on the launch string." The supervisor builds
  `f"{claude_exec()} --append-system-prompt {shlex.quote(ROLE_PROMPT)}"` and
  sends it via `send-keys ... Enter` — same delivery channel as every other
  spawn. (The role text is multi-paragraph; `shlex.quote` keeps it one arg
  through the shell that `send-keys` types into.)

- **Liveness = "the marker's `pane_id` still appears in `list_windows()`."**
  `panes.list_windows()` is already imported by `activity.py` and enumerates
  live tmux windows with their `pane_id`. A marked pane whose `%N` is absent
  from the live set is dead → respawn. This reuses the read the worker already
  does each tick; no new tmux call shape.

- **The supervisor runs on the worker's cadence, not a private clock.** Rather
  than a second lifespan task with its own `asyncio.sleep`, the cheapest correct
  thing is to fold the supervisor pass into the **start** of the existing
  prod-gated worker tick (it already wakes every 30s; the first mate doesn't
  need sub-30s respawn latency). This also means one prod gate (the worker's),
  not two. See §4 + Decision 1 — this is a genuine close call and I commit to
  the in-worker pass, with the standalone-task alternative flagged.

- **Last-sent digest lives in a module global in the heartbeat module**, exactly
  like narrator's `_enabled_checked` module global and `last_ctx` threaded
  through `run_worker`. Divergence is recomputed against it each tick; on process
  restart it resets to `None` → first tick re-pushes the full picture (correct:
  a restarted first mate needs the current picture anyway). No DB row for it —
  it is ephemeral by design.

- **`build_window_view` is assembled by the worker and passed in.** v1a's
  `build_fleet_digest` is pure-over-assembled-dicts. The worker tick already has
  `list_windows()`; the adapter (below) is what turns those + `pane_status_lines()`
  into the curated contract. `build_window_view` lives in `window_view.py`,
  which `activity.py` must **not** top-import (cycle discipline, activity.py:8-10)
  — so the adapter/heartbeat call site uses a **function-level import**, the same
  escape hatch `_worker_tick` already uses for `narrator` (activity.py:739-740).

- **`status_line` comes from `activity.pane_status_lines()`, not the view.**
  Confirmed: `build_window_view` has no `status_line` key; the narrator's status
  is in the `pane_status` table, read in bulk via `pane_status_lines() ->
  {pane_id: (status, generated_at, rail)}`. The adapter joins the two on
  `pane_id`.

- **`blocked` = the pane's most-recent `channel_alerts` entry has
  `kind == "need_human"`.** `build_window_view` carries `channel_alerts` (a list
  of `{id,message,kind,severity,ts}` dicts, channels.py:174-180). `blocked` is
  derived: the newest-`ts` alert's `kind` is `need_human`. (v1a already assumed
  this; the adapter is where it's computed from the real list.)

- **`idle_s` = `now - max(focused_at, acted_at)`.** Both keys are present in the
  view (`focused_at` explicit, `acted_at` explicit). Idle is time since the pane
  was last focused or acted on, floored at 0.

---

## 3. File layout

```
periscope/
  first_mate.py        EDIT  add the IO half below the existing pure core.
                             NEW: window_views_to_digest() adapter (pure-ish:
                             dicts in, FleetDigest out — joins pane_status_lines);
                             ROLE_PROMPT constant (the system prompt);
                             supervisor_pass() (liveness + respawn);
                             heartbeat_pass() (assemble→diverge→push);
                             _spawn_first_mate() (single-window bridge spawn);
                             module global _LAST_SENT: FleetDigest | None.
                             Pure core (build_fleet_digest/fleet_diverged) stays
                             untouched and import-light; IO half lazy-imports the
                             heavy modules inside the functions.

  activity.py          EDIT  _worker_tick: after the narrator pass, call
                             first_mate.supervisor_pass() then
                             first_mate.heartbeat_pass(panes-or-views).
                             Function-level import (cycle discipline), same as
                             the narrator import already there.

  channels.py          EDIT  _do_notify_tool: when kind == "need_human", fire an
                             immediate emit_channel_event to the first-mate pane
                             (the interrupt hook). Add the deferred
                             _do_fleet_digest_tool (the on-demand pull) — now it
                             has a cached digest to return (first_mate._LAST_SENT
                             or a fresh assemble).

  app.py               NO CHANGE  the supervisor rides the already-prod-gated
                             run_worker; no new lifespan task (see Decision 1).
                             If Decision 1 flips to a standalone task, this gets
                             one is_prod()-gated _task("first-mate-supervisor", …).

tests/
  test_first_mate.py   EDIT  add adapter mapping tests (literal view dicts +
                             pane_status_lines → assert FleetDigest fields);
                             heartbeat divergence/no-push/not-attached-fallback
                             with a fake emit; supervisor decision (marker
                             alive→noop, dead→respawn) with list_windows faked.
  test_first_mate_spawn.py  NEW  @needs_tmux real-tmux integration: supervisor
                             spawns a first-mate window into a real isolated
                             bridge session (PERISCOPE_CLAUDE_EXEC=cat), marks it,
                             kill the pane, second pass respawns + re-marks.
  test_channels.py     EDIT  need_human hook fires emit_channel_event to the
                             marked first-mate pane; non-need_human kinds don't;
                             fleet-digest pull tool returns the cached digest /
                             refuses non-first-mate callers.
  test_app.py          NO CHANGE  run_worker stays mocked (the heartbeat +
                             supervisor live inside it, so mocking run_worker
                             already prevents a live first-mate tick — verify the
                             existing patch covers it; see Test strategy).
```

No new route file. v1b adds no HTTP surface — the heartbeat is a worker-internal
push, the pull is an MCP tool, the supervisor is a lifespan-adjacent task.

---

## 4. Per-module structure

### `periscope/first_mate.py` — IO half added below the pure core (rung 1 functions + one module global)

Mirrors `narrator.py`: pure decision functions at the top (already there from
v1a), IO shell below, driven by the worker, cross-tick state in a module global.
**No class.** There is no coupled mutable state that a class would encapsulate —
the one piece of cross-tick state (`_LAST_SENT`) is exactly the narrator's
`module-global cache` shape (`narrator._enabled_checked`), and everything else is
a function over passed-in arguments. A `FirstMate` class would be grouping-by-noun;
rung-3 trigger (coupled mutable state + lifecycle) is not met — the lifecycle
lives in tmux + the `first_mate` marker row, not in a Python object.

**Module global (cross-tick state):**

```python
_LAST_SENT: FleetDigest | None = None   # last digest pushed to the first mate
```

**The adapter — the flagged integration risk (rung 1 pure function):**

```python
def window_views_to_digest(
    *, window_views: list[dict], status_lines: dict[str, tuple[str, int, str | None]],
    usage: dict | None, now: int,
) -> FleetDigest:
    """Curate the read model into the v1a contract, then build_fleet_digest.
    PURE: dicts in, FleetDigest out. window_views: build_window_view output
    (filtered to is_claude inside). status_lines: activity.pane_status_lines().
    Joins status_line onto each pane by pane_id, derives blocked from the
    newest channel_alerts entry, idle_s from focused_at/acted_at."""
```

This function is the **single seam** the plan-review flagged. Keeping it pure
(literal dicts in, `FleetDigest` out, zero imports of store/usage/view) is what
makes it unit-testable with hand-written inputs and is the deliberate move away
from the four-mocks-per-test smell. It does the join and the field derivations
(`handle←pid`, `status_line←status_lines[pid]`, `blocked←newest need_human alert`,
`pr`/`ci`←view, `idle_s←now-max(focused_at,acted_at)`) and then delegates to the
v1a `build_fleet_digest`. Per-pane curation is the spec's "Periscope does the
aggregation pass." Keyword-only per the multi-arg rule.

**The system prompt (a module constant):**

```python
ROLE_PROMPT = """..."""   # role + standing-tier + absolute prohibitions
```

A constant, not a file. It is ~40-60 lines of prose, drafted from the spec's
"Autonomy" + "Never" sections (role: chief-of-staff watcher; standing authority:
observe/summarize/peek/clearly-idle-nudge; prohibitions: no fdy-merge, no
force-push, no prod). A constant keeps it versioned in the diff and greppable;
a separate file buys nothing at this size and adds a read-at-spawn IO path. If
it grows past ~150 lines or wants non-engineer editing, promote to a file then —
not now (YAGNI).

**The supervisor (rung 1 functions, IO):**

```python
def supervisor_pass(*, now: int) -> None:
    """One liveness check. If the first_mate marker is missing or its pane_id
    is no longer a live window, spawn a fresh first-mate window and re-mark.
    Idempotent: a live marked pane is a no-op."""

def _spawn_first_mate(*, now: int) -> None:
    """Ensure the `bridge` session, open a single `first-mate` window running
    claude_exec() + --append-system-prompt ROLE_PROMPT, stamp it, set the
    first_mate marker. Borrows the send-keys + consent-dismiss + stamp sequence
    from worktree_spawn._layout_two_window but single-window and no HTTPException."""
```

`supervisor_pass` lazy-imports `activity` (marker read/write) and `panes`
(`list_windows`). `_spawn_first_mate` lazy-imports `tmux`/`_tmux_mutate`,
`config.claude_exec`, `channels.dismiss_dev_channels_consent_bg`, and
`pids.stamp_new_window`. Liveness:

```
marker = activity.get_first_mate()
live_panes = {w["pane_id"] for w in list_windows()}
if marker and marker.pane_id in live_panes:
    return                      # alive — no-op (prevents double-spawn)
_spawn_first_mate(now=now)      # missing or dead — (re)spawn + re-mark
```

The marker is what prevents double-spawn (a live marked pane short-circuits) and
tells the heartbeat which `%N` to push to. `_spawn_first_mate` ends with
`activity.set_first_mate(pane_id=<%N from stamp>, session_id=None, at=now)` —
`session_id` stays `None` in v1b (the `pane_sessions` hook fills the JSONL id on
the first prompt; the marker doesn't need it for push, which keys on `%N`).

**Spawn sequence (single window, no HTTPException):**

```
sess = "bridge"
if not has-session bridge:  _tmux_mutate("new-session","-d","-s","bridge","-c",home,"-n","first-mate")
else:                       _tmux_mutate("new-window","-t","bridge:","-n","first-mate","-c",home)
target = "bridge:first-mate"
exec_cmd = f"{config.claude_exec()} --append-system-prompt {shlex.quote(ROLE_PROMPT)}"
time.sleep(0.1)             # let rc finish — same reason as _layout_two_window
_tmux_mutate("send-keys","-t",target,exec_cmd,"Enter")
if "--dangerously-load-development-channels" in exec_cmd:
    channels.dismiss_dev_channels_consent_bg(target)
pid = stamp_new_window(target)
pane_id = tmux("display-message","-t",target,"-p","#{pane_id}").strip()
activity.set_first_mate(pane_id=pane_id, session_id=None, at=now)
```

**The heartbeat (rung 1 function, IO):**

```python
async def heartbeat_pass(*, window_views: list[dict], now: int) -> None:
    """Assemble the digest, compare to the last sent, push the delta on
    divergence to the marked first-mate pane via emit_channel_event. On a
    not-attached False, drop and let the next tick re-push (divergence-based,
    nothing lost). Also scans for watched-PR CI-red transitions and pushes
    those ahead of digest divergence."""
    global _LAST_SENT
    ...
```

`heartbeat_pass` is `async` because `emit_channel_event` is async
(channels.py:677). It:
1. Lazy-imports `activity.pane_status_lines`, `usage.cached_plan_usage`,
   `channels.emit_channel_event`.
2. Reads the marker; if none → return (no first mate to push to; supervisor will
   bring one up).
3. `cur = window_views_to_digest(window_views=…, status_lines=…, usage=…, now=now)`.
4. `diverged, reason = fleet_diverged(_LAST_SENT, cur)`.
5. CI-red scan: compares each pane's `ci` against `_LAST_SENT`'s for a
   `→ ✗` transition on a watched PR; a red flip forces a push even if the
   overall digest didn't otherwise diverge (interrupt tier 2).
6. If diverged or CI-red: `ok = await emit_channel_event(marker.pane_id,
   _render_delta(_LAST_SENT, cur, reason), meta={...})`. On `ok` → `_LAST_SENT =
   cur`. On `not ok` (pane not attached) → **leave `_LAST_SENT` unchanged** so
   the next tick re-pushes the still-diverged picture (the spec's retry-next-tick
   fallback; correctness hinges on NOT advancing `_LAST_SENT` on a failed push).

`_render_delta(prev, cur, reason)` is a small **pure** function (rung 1) turning
two digests + reason into the human-readable delta string ("since last tick: auth
pane went blocked; budget 62%→71%") — unit-testable, no IO. This is the v1b piece
the v1a structure explicitly deferred ("full delta prose isn't needed until
there's a pane to push to").

### `periscope/activity.py` — `_worker_tick` extension (one block, function-level import)

The worker already assembles the Claude `panes` list and runs the narrator. v1b
appends two calls after the narrator pass, mirroring its function-level-import
discipline exactly:

```python
# after narrator.tick(panes), inside _worker_tick:
try:
    from periscope import first_mate          # function-level: first_mate's IO
    first_mate.supervisor_pass(now=now)        # half lazy-imports back into the
                                               # worker's deps — cycle-safe
    # build the curated views for the heartbeat. The worker captured `panes`
    # (window dict + parsed); the heartbeat needs build_window_view output.
    from periscope.window_view import build_window_view
    views = [build_window_view(w, now)[0] for w, _ in panes]
    asyncio.run(first_mate.heartbeat_pass(window_views=views, now=now))
except Exception:
    log.exception("first-mate pass failed")
```

**Open structural question (Decision 2):** `_worker_tick` runs in a worker
*thread* (`asyncio.to_thread(_worker_tick, …)`), so it has no running event
loop; calling the async `heartbeat_pass` needs `asyncio.run(...)` (a fresh loop
per tick) or `heartbeat_pass` must be made sync with a sync `emit`. I commit to
making **`heartbeat_pass` accept being called via `asyncio.run`** inside the
thread (one short-lived loop per 30s tick is cheap and isolated), and flag the
alternative (push the heartbeat call back up into `run_worker`'s real loop where
`await` is natural) in Decision 2. The narrator is fully sync, which is why this
question is new to v1b.

Rationale: `activity.py` stays the worker host; it gains ~8 lines and one more
function-level import, consistent with how it already calls the narrator. No new
module-level imports → no new cycle. `build_window_view` is imported
function-level (it lives in `window_view.py`, off-limits to top-import).

### `periscope/channels.py` — interrupt hook + the deferred pull tool (rung 1)

**`need_human` interrupt hook** at the `_do_notify_tool` write point
(channels.py:181-193). After the alert is appended and the durable `activity.record`
mirror, add:

```python
if kind == "need_human":
    # immediate first-mate wake, out of band from the 30s heartbeat
    from periscope import activity
    marker = activity.get_first_mate()
    if marker is not None:
        meta = {"kind": "interrupt", "source_pane": pane}
        _schedule_emit(marker.pane_id, f"need_human from {pane}: {message}", meta)
```

`_do_notify_tool` is **sync** but `emit_channel_event` is **async** — the hook
needs to schedule the coroutine without blocking the tool handler. `_do_notify_tool`
runs inside the MCP server's anyio task (there *is* a loop here, unlike the
worker thread), so `_schedule_emit` is `asyncio.create_task(emit_channel_event(
…))` wrapped via the project's `_task` crash-wrapper (CLAUDE.md invariant 8:
naked tasks that raise vanish). This is the one place the hook differs from the
worker's `asyncio.run` — flagged in Decision 2.

**The deferred fleet-digest pull tool** (`_do_fleet_digest_tool`, the third
first-mate tool v1a deferred). Now there's a cached digest to return:

```python
def _do_fleet_digest_tool(pane: str, arguments: dict):
    if not _require_first_mate(pane):
        return _tool_result({"ok": False, "error": "first-mate-only tool"})
    from periscope import first_mate
    d = first_mate._LAST_SENT
    return _tool_result({"ok": True, "digest": _serialize_digest(d)} if d
                        else {"ok": True, "digest": None})
```

Registered in `_CHANNEL_TOOLS` alongside the two captain's-log tools, same
self-guard pattern. Reading `first_mate._LAST_SENT` (the last *pushed* digest) is
correct for an on-demand pull — the first mate asks "what's the current fleet
picture?" and gets the same digest the heartbeat would have pushed.

---

## 5. Patterns

**Used:**
- **Pure adapter + pure delta-render, IO heartbeat/supervisor shell** — mirrors
  narrator's pure-core / `tick`-`_generate` split. The spec names this mirror.
- **Module-global cross-tick state** (`_LAST_SENT`) — the narrator's
  `_enabled_checked` / `run_worker`'s `last_ctx` shape. Ephemeral, resets on
  restart by design.
- **Function-level imports for cycle-prone heavy modules** (`window_view`,
  `first_mate`, `activity`) — the established escape hatch (activity.py:739-740,
  channels.py:207).
- **Functional tool registry record + self-guard** (`_do_fleet_digest_tool` +
  `_require_first_mate`) — the v1a-established tool-add pattern.
- **`_task` crash-wrapper for the fire-and-forget emit** in the need_human hook —
  CLAUDE.md invariant 8.

**Considered and rejected:**
- **A `FirstMateSupervisor` class** holding marker + last-sent + spawn config —
  rejected. The cross-tick state is one `FleetDigest | None`; the marker lives in
  SQLite; the spawn is a function. No coupled mutable state for a class to own
  (rung-3 not met). Functions + one module global is the narrator's proven shape.
- **A standalone lifespan supervisor task** (its own `asyncio.sleep` loop in
  `app.py`) — rejected for v1b in favor of folding into the worker tick; a second
  prod gate and a second 30s clock for a respawn that tolerates 30s latency is
  more surface for no benefit. Flagged as the close call in Decision 1 (it's the
  spec's literal phrasing, "a lifespan-managed task").
- **Reusing `_do_spawn_claude_tool` / `_layout_two_window` for the spawn** —
  rejected. The former skips the `claude_exec()` test seam (unstubable); the
  latter raises `HTTPException` (request-coupled) and builds a two-window layout
  the first mate doesn't want. Borrow the *sequence*, not the function.
- **A custom exception for "no first mate marker"** — rejected per the rule.
  `heartbeat_pass` simply returns when the marker is absent; the pull tool
  returns `digest: None`. No caller needs to catch a typed error.
- **Persisting `_LAST_SENT` to a DB row** — rejected (YAGNI). Divergence is
  self-healing across restart: a fresh process re-pushes the current picture on
  tick one. A row would add a write per tick and a migration for zero behavioral
  gain.

---

## 6. Test strategy (per module)

The split's whole point: everything that *reasons* (adapter, divergence, delta,
supervisor decision, push fallback) is unit-tested with literal inputs; the one
thing that *spawns* is a real-tmux integration test with a stub exec; the live
Claude + real Haiku heartbeat reasoning is **not** auto-tested and is verified by
the spec's demo.

**`tests/test_first_mate.py` (additions) — unit, zero live deps.**
- **Adapter mapping** (the flagged risk): hand-built `window_views` dicts (the
  curated subset of real `build_window_view` keys: `pid`, `is_claude`,
  `focused_at`, `acted_at`, `pr`, `ci`, `channel_alerts`) + a literal
  `status_lines` dict + a fake `usage` dict → assert every `PaneDigest` field,
  the `status_line` join by `pid`, `blocked` from the newest `need_human` alert,
  `idle_s` from `now-max(focused,acted)`, `is_claude` filter, `usage=None →
  budget None`. No mocks — the pure adapter takes dicts. *This is the structure
  that prevents the mock-heavy smell.*
- **Heartbeat divergence + fallback**: drive `heartbeat_pass` with a fake
  `emit_channel_event` (a recording stub) and a faked marker. Assert: diverged
  picture → one emit, `_LAST_SENT` advances; identical next tick → no emit;
  emit returns `False` (not attached) → `_LAST_SENT` **unchanged** → next tick
  re-pushes (the retry-next-tick contract); CI `✓→✗` on a watched PR forces an
  emit even when otherwise non-divergent.
- **Supervisor decision**: fake `list_windows()` + the marker. marker present &
  pane in live set → `_spawn_first_mate` not called (assert via a spy); marker
  missing → spawn called; marker present but pane absent (dead) → spawn called.
  `_spawn_first_mate` itself stubbed here (its tmux reality is the integration
  test below).
- **`_render_delta`**: pure — two literal digests → assert the delta string
  mentions the changed panes/budget.

**`tests/test_first_mate_spawn.py` (NEW) — `@needs_tmux` integration, real tmux,
stub claude.** Follows `test_worktree_spawn.py` exactly: `tmux_test_server`
fixture (`PERISCOPE_TMUX_SOCKET` isolated `-L`, `PERISCOPE_CLAUDE_EXEC=cat`) +
`fresh_activity_db`. Because `_spawn_first_mate` goes through `config.claude_exec()`
(the pushback in §1), `cat` is what actually launches → the window stays alive.
- supervisor first pass (no marker) → a `bridge:first-mate` window exists,
  marker set to its `%N`, stamped with a `@periscope_id`.
- second pass (marker alive) → no new window (idempotent, no double-spawn).
- kill the marked pane → third pass respawns, marker updated to the new `%N`.
- *Real dependency on purpose* — a mocked tmux would pass while the real
  new-session/new-window/send-keys/stamp sequence broke (the Q1-2026
  mocked-migration lesson; the supervisor's spawn is exactly the kind of
  real-subprocess sequence that must run for real).

**`tests/test_channels.py` (additions) — unit, real `activity` DB via fixture,
in-memory channel dicts via `reset_channel_state`.**
- need_human hook: with the marker set to `%9` and a fake/recording
  `emit_channel_event`, `_do_notify_tool("%5", {kind:"need_human", message:…})`
  schedules an emit to `%9`; `kind:"info"`/`"done"` schedule none; no marker →
  no emit (and no crash).
- fleet-digest pull tool: registered in `_CHANNEL_TOOLS`; refuses a non-first-mate
  caller; with a marked pane and `first_mate._LAST_SENT` set, returns the
  serialized digest; with `_LAST_SENT=None` returns `digest: None`.

**`tests/test_app.py` — unchanged, but verify the invariant holds.** The
supervisor + heartbeat live *inside* `_worker_tick`, which runs inside
`run_worker`, which the lifespan tests already mock (`mocker.patch(
"periscope.activity.run_worker", side_effect=_noop)`, test_app.py:60/116). So the
existing mock already prevents a live first-mate spawn + Haiku push during
pytest. **This must stay true** — if the heartbeat call ever moves out of
`run_worker` into its own lifespan task, that task needs its own mock in
test_app.py. Called out explicitly because it is the CLAUDE.md
"lifespan-tests-mock-run_worker" landmine, now load-bearing for budget safety.

**Explicitly NOT auto-tested (honesty):**
- A real first-mate Claude actually booting from `claude_exec() +
  --append-system-prompt` and obeying the role/prohibitions — that's a live
  Claude; verified by the spec's demo, not a test.
- Real Haiku/Claude heartbeat *reasoning* over a pushed delta — same; the test
  covers that a push *happens* and *what bytes* are pushed, never what the model
  does with them.
- `dismiss_dev_channels_consent_bg` interacting with a real consent prompt — the
  stub exec (`cat`) shows no prompt, so the consent-dismiss branch is exercised
  for "is it called" but not "does it dismiss a real prompt."

No testability smells: every reasoning unit is reachable with literal inputs
because the adapter and divergence stay pure and the IO (emit, tmux, marker) is
injected/faked or run for real on an isolated socket.

---

## 7. Decisions to sanity-check

1. **Supervisor in the worker tick vs. a standalone lifespan task.** I fold
   `supervisor_pass` into the start of `_worker_tick` (one prod gate, one 30s
   clock, no `app.py` change). *Alternative:* a separate `is_prod()`-gated
   `_task("first-mate-supervisor", …)` with its own loop, which is the spec's
   literal phrasing ("a lifespan-managed task"). *Close because:* the standalone
   task is what the spec wrote and gives independent respawn latency; but the
   first mate tolerates 30s respawn latency, and a second clock + second prod
   gate is pure surface. If you want sub-30s respawn or want the supervisor to
   survive a wedged worker tick, flip to the standalone task — it's a clean swap
   (move `supervisor_pass` into its own `async def` loop in `app.py`, gated like
   `activity.run_worker`).

2. **Calling the async heartbeat/emit from sync worker-thread code. RESOLVED
   (correctness, not preference): the heartbeat emit MUST hoist to `run_worker`'s
   main loop — `asyncio.run` in the worker thread is a cross-loop bug.**
   `_worker_tick` runs via `await asyncio.to_thread(_worker_tick, …)`
   (activity.py:756) — a worker thread with no event loop. `emit_channel_event`
   does `await session._write_stream.send(…)` (channels.py) on an anyio stream
   **bound to the main loop** (the MCP listener runs there via
   `asyncio.start_unix_server`). `asyncio.run(...)` in the thread spins a *fresh*
   loop and would send on a main-loop-bound session from the wrong loop — anyio
   streams are loop-affine; this fails/corrupts. So the structure is:
   - **`_worker_tick` (thread):** runs `supervisor_pass` (sync tmux — correct in
     a thread) and assembles the heartbeat *decision* — build the digest, diverge
     vs `_LAST_SENT`, render the delta — and **stashes a pending push**
     `(pane_id, content, cur_digest)` into `last_ctx` (e.g. `last_ctx["_fm_push"]`).
     No `emit`, no `await`, no `asyncio.run`.
   - **`run_worker` (main loop):** after `await asyncio.to_thread(...)`, if a
     pending push exists, `ok = await emit_channel_event(pane_id, content)`; set
     `first_mate._LAST_SENT = cur_digest` **only on `ok`** (the retry-next-tick
     fallback hinges on not advancing `_LAST_SENT` on a failed send). This is the
     only `run_worker` change.
   The **need_human hook** stays `_task(create_task(emit_channel_event(...)))` —
   `_do_notify_tool` already runs in the MCP anyio task on the main loop with the
   sessions, so `create_task` is loop-correct there. So `heartbeat_pass` is NOT a
   single async function; it splits into a sync `heartbeat_decide(...) -> push|None`
   (thread, pure-ish, unit-testable) and the `await emit` in `run_worker`.
   Ticks are sequential (`run_worker` awaits the tick, then the emit, then
   sleeps), so `_LAST_SENT` is never accessed concurrently.

3. **`build_window_view` rebuilt in the tick vs. reusing `/api/state`
   assembly.** The heartbeat needs `build_window_view` output, which the worker
   doesn't currently build (it only `parse_pane`s). I rebuild it in the tick from
   the captured `panes`. *Alternative:* read the last `/api/state` snapshot that
   the poll route already assembled. *Close because:* reusing the route's
   assembly avoids duplicate `build_window_view` calls, but the route builds on
   *its* cadence (3s poll) not the worker's, and there's no stored last-snapshot
   to read — the route assembles per request. Rebuilding in the tick keeps the
   heartbeat self-contained and on its own clock; the per-tick cost is ~one
   `build_window_view` per Claude pane, already the order of what the route does.

4. **System prompt as a module constant vs. a file.** Constant in `first_mate.py`.
   *Alternative:* `periscope/first_mate_prompt.txt` read at spawn. *Close
   because:* a file is nicer for a non-engineer to edit and keeps the module
   lean, but at ~50 lines a constant is versioned-in-diff, greppable, and needs
   no read-IO at spawn. Promote to a file when it grows or wants out-of-band
   editing — not now.
