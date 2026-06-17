# Inter-Claude management tools — code structure

Structure proposal for `docs/superpowers/specs/2026-06-16-inter-claude-management-design.md`.
This is plumbing over existing primitives (`emit_channel_event`, `_MCP_SESSIONS`,
the pid model, `_tmux_mutate`, `get_turns_for_pane`), not a new subsystem. The
structure stays at the lowest rung throughout: plain functions in the file that
already owns every primitive they call.

## Spec pushback

- **"extend the existing spawn test" (Testing Strategy) — there is no existing
  spawn test.** `grep -rn spawn_claude tests/` finds nothing; `_do_spawn_claude_tool`
  is currently untested. The provenance assertion therefore lands in a **new**
  `tests/test_channels.py::test_spawn_claude_writes_spawned_by` (plus a sibling
  asserting the no-parent case is tolerated). Don't plan an edit to a file that
  isn't there. This is the only factual error in the spec; everything else verified.

- **No new module — keep all five handlers in `channels.py`.** (Central question 1.)
  The spec doesn't mandate a split, but it's worth recording the decision explicitly
  since the file is ~1071 lines. Recommendation: **handlers stay in `channels.py`.**
  Rationale below under Per-module structure. Not pushback against the spec — pushback
  against the instinct to extract on line-count alone.

## Assumptions

- **pid-resolution helper picks the `pid_raw`-match option, not the re-resolve-pass
  option.** The spec offers both (design §Resolution helper) and defers to the plan.
  I commit to `lambda w: w.get("pid_raw") == handle` filtered over `list_windows()`,
  then one `_attach_git_then_resolve_pids([w])` on the single match to attach
  `pid`/strip `pid_raw` — mirroring the existing `_resolve_window` pattern exactly
  (it already resolves *after* matching). This avoids a full-list resolve pass
  (which writes state.json + stamps every window) on every `peek`/`send_to`. The
  never-stamped edge the second option defends against cannot occur: a handle is
  only ever returned by `spawn_claude` *after* `_resolve_window` stamped it. Listed
  in Decisions to sanity-check.

- **`peek` reads the transcript via `get_turns_for_pane(session, index)` after the
  `session_id_for_pane(pane_id)` gate**, not by re-deriving the session id. The gate
  makes the cwd fallback inside `get_turns_for_pane` unreachable (a recorded session
  always resolves its JSONL by glob first), so reusing it is safe and avoids
  duplicating the JSONL-read logic. Tail slice: last 20 turns, same trim shape as
  `_do_get_history_session_tool`.

- **`list_claudes` reuses `build_window_view` directly** with a small serial loop
  (not the `/api/state` ThreadPoolExecutor fan-out). Window count is dozens; an
  occasional tool call doesn't need the pool, and the pool path also runs
  focus-tracking/stamp-persistence side effects `list_claudes` shouldn't trigger.
  Status-line merge replicates the `pane_status_lines()` read from `routes/state.py`.

- **Accessor is `get_window_fields(pid) -> WindowAnnotation`**, not a single-key
  `get_window_field`. `get_window(pid)` already exists in store.py and returns
  exactly this. See pushback-adjacent note under store.py.

## File layout

No new files. All changes land in existing modules beside the code they extend.

```
periscope/channels.py        +5 handlers, +1 resolver helper, +5 _CHANNEL_TOOLS records,
                              +1 line in _do_spawn_claude_tool (provenance write)
periscope/store.py           +get_window_fields  (next to set_window_fields) — see note
periscope/turns.py           UNCHANGED — peek consumes session_id_for_pane +
                              get_turns_for_pane as-is
periscope/pids.py            UNCHANGED — _attach_git_then_resolve_pids reused as-is
periscope/tmux.py            UNCHANGED — _tmux_mutate reused as-is
periscope/panes.py           UNCHANGED — list_windows reused as-is
periscope/window_view.py     UNCHANGED — build_window_view reused as-is

tests/test_channels.py       +cases for all 5 tools + resolver + provenance (see Test strategy)
tests/test_store.py          +case for get_window_fields
```

## store.py — `get_window_fields`

The spec says "add a `get_window_fields(pid) -> dict` accessor". Note: store.py
**already exposes `get_window(pid) -> WindowAnnotation`** (line 329), which returns
a copy of `windows[pid]` — functionally identical to what the spec describes.

Recommendation: **add a thin `get_window_fields = get_window` alias-or-wrapper named
per the spec for call-site readability** at the `report()` site, OR just call
`get_window(caller_pid).get("spawned_by")` directly and skip the new symbol. Either
is fine; the spec's named accessor doesn't structurally exist yet only because it's
already there under a different name. I commit to **reusing `get_window` directly in
`report()`** — no new store symbol — and flag it. Adding a second name for an
existing function is the kind of duplication the taste rules discourage.

If the plan-writer prefers the spec's literal name (for grep-ability against the
spec), a one-line `def get_window_fields(pid): return get_window(pid)` is acceptable;
don't reimplement the lock/copy logic.

## Per-module structure

### `periscope/channels.py` — the five handlers + resolver (rung 1: plain functions)

Every new handler is a module-level `_do_*_tool(pane, arguments)` function returning
`_tool_result(body)`, registered as one `_CHANNEL_TOOLS` record. This is the
established convention for all eight existing tools; there is no mutable state to
encapsulate and no polymorphism — the dispatch is already data-driven via
`_CHANNEL_TOOLS`. Rung 1 is correct and the precedent is unanimous.

**Why not a new `inter_claude.py` module** (central question 1): the strong "tools
live in channels.py" convention wins decisively here.
- Every primitive the handlers call is private to channels.py: `emit_channel_event`,
  `_MCP_SESSIONS`, `_CHANNELS_LOCK`, `channel_state_for`, `_resolve_window`,
  `_resolve_pid_for_pane`, `_tool_result`, `_CHANNEL_TOOLS`. A separate module would
  either import a fistful of underscored internals (breaking the module boundary) or
  force those internals public.
- The registry (`_CHANNEL_TOOLS`) and dispatch (`_call_tool`) are in channels.py;
  splitting handlers out means the registry references symbols across a module seam
  for no gain.
- The 1071-line count is one tightly-coupled concern (the MCP channel server),
  exactly the case the taste rules say a large file is acceptable. It is not several
  concerns wearing a trenchcoat.
- The grouping rule is "group by domain area" — these tools ARE the channel domain.

If channels.py ever needs to split, the natural seam is the *history* tools
(`search_history`/`get_history_session`/`resume_session`, which already lazy-import
the `history` package and are a distinct concern) — not the inter-Claude tools, which
are pure channel-domain. Note that for a future cleanup; don't do it now.

**New resolver helper — `_resolve_window_by_pid`:**

```python
def _resolve_window_by_pid(handle: str) -> tuple[str, str, dict]:
    """Resolve an @periscope_id handle to (pid, pane_id, window).

    Matches on pid_raw (the stamped @periscope_id on the raw list_windows row)
    BEFORE resolution — _resolve_window's predicate runs on raw rows that carry
    pid_raw, not pid. Returns ("", "", {}) if the handle matches no live window.
    peek/terminate need session/index off the returned window dict."""
```

- Returns `(pid, pane_id, window)` per the spec — peek/terminate read
  `window["session"]`/`window["index"]` to build the `session:index` target.
- Internally: iterate `list_windows()`, match `w.get("pid_raw") == handle`, on hit
  call `_attach_git_then_resolve_pids([w])` (which attaches `pid`, strips `pid_raw`),
  return `(w["pid"], w["pane_id"], w)`.
- This is a sibling to the existing `_resolve_window`/`_resolve_pid_for_pane` pair —
  same rung, same file, same shape. Generic enough that `peek`, `terminate`, and
  `send_to` all share it; `report` uses `_resolve_pid_for_pane` for the caller then
  `_resolve_window_by_pid` for the resolved parent handle.

**Per-handler shape (all rung 1, all `(pane, arguments) -> _tool_result`):**

| Handler | async? | Key calls | Guards |
|---|---|---|---|
| `_do_send_to_tool` | yes (`await emit_channel_event`) | `_resolve_window_by_pid` → pane_id → `emit_channel_event` | self-send refusal (resolved pane_id == caller `pane`) |
| `_do_report_tool` | yes | `_resolve_pid_for_pane(pane)` → `get_window(pid)["spawned_by"]` → `_resolve_window_by_pid(spawned_by)` → `emit_channel_event` | no-spawner error |
| `_do_list_claudes_tool` | yes (builder may await) | `list_windows` → `build_window_view` loop → `pane_status_lines` merge → `channel_state_for` | none |
| `_do_peek_tool` | no | `_resolve_window_by_pid` → `session_id_for_pane(pane_id)` gate → `get_turns_for_pane` | refuse on `session_id_for_pane is None` |
| `_do_terminate_tool` | no | `_resolve_window_by_pid` → `_tmux_mutate("kill-window", "-t", target)` | self-terminate refusal |

`send_to`/`report` share a private `_deliver(pane_id, message, caller_pane)` helper
that does the self-send guard + `emit_channel_event` + the not-live/not-attached
error mapping, since both have identical delivery semantics (spec: "same not-live /
not-attached error handling as send_to"). One helper, two callers — earns its keep.

**Error handling** follows the existing channels.py convention exactly: handlers
return `{"ok": False, "error": ...}` in `_tool_result`, NOT `HTTPException`. This is
the MCP-tool path, not a route — the route convention (raise HTTPException) does not
apply here, and every existing `_do_*_tool` already does `{"ok": False, ...}`. No
custom exceptions; built-in error dicts with contextful messages.

### `_do_spawn_claude_tool` provenance write (central question 3)

Cleanest insertion point: **immediately after the existing
`pid, pane_id = _resolve_window(...)` block (channels.py:485–487)**, before building
the response body. At that point `pid` (the child) is resolved and `pane` (the
caller) is in hand:

```python
    parent_pid = _resolve_pid_for_pane(pane)
    if parent_pid and pid:
        set_window_fields(pid, spawned_by=parent_pid)
```

Two lines, inside the existing function, using two functions already imported in the
module. No structural change to spawn_claude — the response shape is unchanged
(spec-confirmed: the caller already knows its own pid). Guard on both pids being
truthy so a vanished caller or unresolved child doesn't write a junk link. This is a
local addition to one existing concrete function, not a new abstraction.

## Patterns

**Used:**
- *Functional registry* (existing `_CHANNEL_TOOLS` data + `_call_tool` dispatch) —
  five new records appended, no dispatch branches. The pattern the spec assumes.
- *Shared private helper* — `_resolve_window_by_pid` (3 callers), `_deliver`
  (2 callers). Extracted only because the second/third caller exists today, not
  speculatively.

**Considered and rejected:**
- *New `inter_claude.py` module* — rejected; convention + private-primitive coupling
  win (see Per-module structure). The line count is one concern.
- *A `Handle` value-object wrapping pid/pane_id/window* — rejected; the tuple
  `(pid, pane_id, window)` matches the existing `_resolve_window -> (pid, pane_id)`
  shape and there's no behavior to attach. A dataclass here is ceremony.
- *Custom exception (`HandleNotFound`)* — rejected; no caller catches a specific
  type, all errors become `{"ok": False, "error": ...}` dicts inline per convention.
- *New `get_window_fields` accessor in store* — rejected in favor of reusing the
  existing `get_window`. (Flagged; plan may override for spec-name parity.)
- *Re-resolve-the-full-list resolver option* — rejected; redundant state.json
  writes per tool call, defends an edge that can't occur (see Assumptions).

## Test strategy

All handler tests go in the **existing** `tests/test_channels.py` (new cases, not a
new file) — it already has the `_do_*_tool` test pattern with `clean_state`/`mocker`
fixtures (333 lines, ~20 cases). The store accessor case goes in `tests/test_store.py`.
No route tests: these are MCP tools, dispatched through `_call_tool`, not HTTP routes —
there is no `routes/` file to mirror.

Per the spec's Testing Strategy, handlers are unit-testable with the seams mocked
(no tmux, no live Claude). This is the correct call here and does **not** violate the
"prefer real dependencies" rule: the real dependencies are a live tmux server and a
live Claude with an attached MCP session — unconstructable in a test, and the Q1-2026
mock-masking-prod failure mode doesn't apply because there's no SQL/DB query whose
real behavior diverges. The logic under test is pure routing/branching over mocked
boundaries; that's exactly the "unit test for pure logic" case.

| Test | File | Mocks | Asserts |
|---|---|---|---|
| `test_resolve_window_by_pid_matches_stamped_handle` | test_channels | `list_windows` → row with `pid_raw`, `_attach_git_then_resolve_pids` | matches on `pid_raw` (resolve-before-match), returns `(pid, pane_id, window)`; `("","",{})` on miss |
| `test_send_to_*` (happy / no-window / not-attached / self-send) | test_channels | `_resolve_window_by_pid`, `emit_channel_event` (async) | each branch returns the spec'd body |
| `test_report_routes_to_spawner` + `test_report_no_spawner_errors` | test_channels | `_resolve_pid_for_pane`, `get_window` (→ spawned_by), `_resolve_window_by_pid`, `emit_channel_event` | reads `spawned_by`, routes to resolved parent pane; no-spawner error |
| `test_list_claudes_filters_is_claude` | test_channels | `list_windows`, `build_window_view`, `pane_status_lines`, `channel_state_for` | only `is_claude` rows; trimmed fields incl. `attached`/`spawned_by`; omits `pane_id` |
| `test_peek_*` (happy / no-session-refusal) | test_channels | `_resolve_window_by_pid`, `session_id_for_pane`, `get_turns_for_pane` | **on `session_id_for_pane → None`: errors AND `get_turns_for_pane` not called** (the cwd-collision footgun) — assert via `mocker` call-count |
| `test_terminate_*` (happy / self-terminate refusal / mutate-failure) | test_channels | `_resolve_window_by_pid`, `_tmux_mutate` | uses `_tmux_mutate` (surfaces failure), self-target refused |
| `test_spawn_claude_writes_spawned_by` (+ no-parent tolerated) | test_channels | the spawn path's tmux/`_resolve_window`/`_resolve_pid_for_pane`/`set_window_fields` | `set_window_fields(child, spawned_by=parent)` called; absent-parent doesn't crash. **NEW test — no existing spawn test to extend.** |
| `test_get_window_fields` (if the alias is added) | test_store | `clean_state` | returns the window block / empty dict |

**Testability flag:** none. Every seam is already a module-level function that
mocks cleanly. The structure does not push any logic behind a hard-to-construct
object — the handlers are flat functions over injectable boundaries.

**Deferred per spec (not in this plan):** the concurrent-reports contention test
(3 workers → 1 lead) validates Claude Code's wake coalescing, not the tool shape;
it needs live Claude and is a manual/integration verification, correctly left out
of the unit suite.

## Decisions to sanity-check

- **pid resolver: `pid_raw`-match over full re-resolve-pass.** Alternative: run
  `_attach_git_then_resolve_pids` over the whole window list first, match on attached
  `pid`. Close because the spec explicitly offers both and the second is marginally
  more robust to a never-stamped window. I chose `pid_raw`-match: it mirrors the
  existing `_resolve_window` precedent and avoids per-call state.json writes; the edge
  it forgoes can't occur (handles only exist post-stamp). Override if you want belt-
  and-suspenders robustness over the redundant-write cost.

- **Reuse `get_window` instead of adding `get_window_fields`.** Alternative: add the
  spec's literally-named accessor. Close because the spec names `get_window_fields`
  by name and a plan-writer grepping the spec won't find it. I chose reuse to avoid a
  duplicate name for an identical existing function; a one-line aliasing wrapper is
  the cheap compromise if name-parity matters.

- **`list_claudes` serial loop over `build_window_view`, not the `/api/state`
  ThreadPoolExecutor.** Alternative: reuse the pooled fan-out. Close because the pool
  is the established path for the same per-pane work. I chose serial: it sidesteps the
  pool's focus/stamp side effects (which `list_claudes` must not trigger) and dozens
  of windows on an occasional call don't need parallelism. Revisit only if the
  fan-out cost ever shows up.
