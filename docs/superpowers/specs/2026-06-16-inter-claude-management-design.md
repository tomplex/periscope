# Inter-Claude management tools — design

## Motivation

A Claude running in a periscope pane can already spawn another Claude
(`spawn_claude`). But once spawned, the only way for one Claude to *manage*
another — message it, check on it, tear it down — is to shell out to `tmux`
directly (`send-keys`, `capture-pane`, `kill-window`). That's brittle, leaks
TUI-timing concerns into the model's job, and bypasses everything periscope
already knows about the pane.

**North star: a Claude should never invoke `tmux` directly to manage another
Claude. Every management action gets a periscope MCP tool.** This spec defines
that tool set.

## Foundational finding (verified empirically, 2026-06-16)

The design hinges on one runtime fact about Claude Code (v2.1.179, launched
with `--dangerously-load-development-channels`):

> **A server-initiated channel notification wakes an idle Claude and is obeyed
> as a directive.**

This was tested directly against periscope's existing `POST /api/channel/push`
→ `emit_channel_event` path:

1. Spawned a worker with prompt `Print exactly the word READY and then stop.`;
   it printed READY and went idle at `❯`.
2. With **no keystrokes sent**, pushed a channel message instructing it to run
   a bash command.
3. The idle worker **spontaneously started a turn**, ran the command, and
   reported done. (`⎿ Allowed by auto mode classifier` → file created.)

A prior run whose worker prompt said "Do not run any tools" produced a refusal
(`Denied by auto mode classifier`) — confirming the *only* thing that
suppressed obedience was the worker's own standing instructions, not inherent
distrust of channel content.

**Consequences for the design (all confirmed dead, not deferred):**

- No inbox / store-and-forward. Delivery wakes the recipient directly.
- No serialized "waker" task or coalescing. There is no `tmux paste-buffer`
  Enter-race to dodge — messaging rides `emit_channel_event` (MCP
  notifications), which the client transport serializes.
- No `inject-vs-channel` delivery mode. Channel push both wakes *and* is
  obeyed, so a separate keystroke-injection "trusted" mode is unnecessary.
- No worker-trust framing mechanism. An unframed worker obeys channel
  directives by default; the spawning Claude controls reliability via the
  initial prompt it already writes.

The feature is therefore **thin routing over `emit_channel_event` +
`_MCP_SESSIONS`**, both of which already exist.

## Scope

This is **flat peer messaging plus a provenance breadcrumb**, not a hard-coded
lead/worker hierarchy. Spawning records *who spawned whom* as metadata, but
carries no ownership semantics — a spawned Claude can be "severed" (handed a
task and left to run independently, e.g. a context handoff at wrap-up). The
lead/worker pattern is one usage of these primitives, not a structure baked
into them.

### Coverage map (the scope, against the `tmux` verbs it replaces)

| Management need | `tmux` today | periscope tool | Status |
|---|---|---|---|
| Create another Claude | `new-window`/`new-session` + `send-keys` | `spawn_claude` | exists; extended |
| Give it a task / message it | `send-keys` / `paste-buffer` + Enter | `send_to(handle, message)` | new |
| Report up to spawner | `send-keys` to parent pane | `report(message)` | new |
| Find other Claudes | `list-windows` + `capture-pane` | `list_claudes()` | new |
| See what one is doing | `capture-pane` / scrollback | `peek(handle)` | new |
| Stop / tear one down | `kill-window` | `terminate(handle)` | new |

### Non-goals (v1)

- **Raw keystroke control** (`send_keys(handle, keys)` — interrupt with
  `Esc`/`Ctrl-C`, answer a permission/consent dialog with `1`/`Enter`).
  Channel `send_to` cannot dismiss a modal dialog (a worker blocked in a
  permission prompt isn't taking turns, so a notification won't reach it). The
  clean recovery is `terminate` + respawn, not puppeteering keys. Excluded
  deliberately: it is literally "`tmux`, but through periscope" — high
  fiddliness, low abstraction value, and it reopens the prompt-injection/trust
  boundary the channel design sidesteps. Revisit only with a concrete need.
- **Cross-machine / cross-tmux-server addressing.** All handles resolve within
  the local tmux server periscope manages.

## Design

### Addressing: handles are `@periscope_id` (pid)

A handle is the stable `@periscope_id` string (`pid`) that `spawn_claude`
already returns. Rationale: `pane_id` (`%N`) churns across tmux server
restarts; `pid` survives renames/moves and is rebound across restarts
(`periscope/pids.py`). Tools resolve `pid → pane_id` internally via the
existing `_resolve_window(lambda w: w.get("pid") == handle)` helper, which
returns `(pid, pane_id)` or `("", "")` if the window has vanished.

`emit_channel_event` and `_MCP_SESSIONS` are keyed by `pane_id`, so every tool
does `handle (pid) → pane_id → action`.

### Provenance: `spawned_by` on the child's window entry

`spawn_claude` already resolves the spawned window's `pid` (`_resolve_window`)
and has the caller's `pane` in hand. It will additionally:

1. Resolve the caller's pid: `parent_pid = _resolve_pid_for_pane(pane)`.
2. Persist the link on the **child's** state.json window entry:
   `set_window_fields(child_pid, spawned_by=parent_pid)`.

`spawned_by` is pure metadata. It powers `report()` routing and a future
lineage view; it is *not* an immunity field and is *not* an ownership lock. A
window with no `spawned_by` (hand-created, or whose parent record was GC'd) is
a valid root — `report()` simply errors for it.

`spawn_claude`'s response shape is unchanged: the caller *is* the parent and
already knows its own pid, so it needs nothing new echoed back.

### Tools

All handlers follow the existing `channels.py` convention: `(pane, arguments)
→ _tool_result(body)`, registered in the tool list with name / description /
`inputSchema` / handler. Async only where they sleep; these don't, so all are
sync except where noted.

#### `send_to(handle, message)`

Lead → any live Claude.

- Resolve `handle (pid) → pane_id`. If no live window: error
  `404`-equivalent (`{"ok": False, "error": "no live window for handle ..."}`).
- `sent = await emit_channel_event(pane_id, message)`.
- If `sent` is `False` (pane exists in tmux but has no attached MCP session —
  worker exited, or is mid-boot before connecting): error
  (`{"ok": False, "error": "target not attached to periscope channel"}`).
  **Delivery failure is surfaced, never silently dropped** (decision: explicit
  error to the caller).
- Async (wraps `await emit_channel_event`).
- Returns `{"ok": True, "handle": handle, "pane_id": pane_id}`.
- Guard: refuse `send_to` targeting the caller's own pane (self-send loop).

#### `report(message)`

Worker → its spawner. Sugar over `send_to(my_spawner, message)`.

- Resolve caller: `caller_pid = _resolve_pid_for_pane(pane)`.
- Read `spawned_by` from the caller's window entry. If absent: error
  (`{"ok": False, "error": "this pane has no spawner to report to"}`).
- Resolve `spawned_by (pid) → pane_id` and `await emit_channel_event(...)`,
  with the same not-live / not-attached error handling as `send_to`. A spawner
  that has since exited yields the not-attached error — correct for the sever
  case.
- The worker never has to carry the parent handle; the server knows it.
- Returns `{"ok": True, "to": spawned_by}`.

#### `list_claudes()`

Discovery: **all** live Claude panes (flat — supports peer discovery and
handoff, not just a subtree).

- Iterate `list_windows()`, keep entries with `is_claude` true.
- For each, emit a trimmed row: `pid` (the handle), `name`, `session`,
  `cwd`, `status_line` (already merged into window views from the narrator),
  `attached` (from `channel_state_for(pane_id)["attached"]`), and `spawned_by`
  (lineage). Omit `pane_id` from the payload — `pid` is the public handle.
- No arguments.
- Returns `{"ok": True, "claudes": [...]}`.

#### `peek(handle)`

Read another Claude's recent transcript instead of waiting for a report.

- Resolve `handle (pid) → window` (need `session` + `index`).
- `turns = get_turns_for_pane(session, index)` (`periscope/turns.py`). Returns
  `None` if the pane has no resolvable session (no `pane_sessions` row yet) —
  surface as `{"ok": False, "error": "no transcript for handle ..."}`.
- Return a **compact tail slice**, mirroring `_do_get_history_session_tool`'s
  token economy: default to the last N turns (N≈20), strip UI-only fields.
  Exact field trimming finalized against `get_turns_for_pane`'s return shape
  during implementation.
- Returns `{"ok": True, "handle": handle, "turns": [...]}`.

#### `terminate(handle)`

Tear down a Claude (cleanup after delegation, or kill a stuck worker).

- Resolve `handle (pid) → window` (need the `session:index` target).
- `_tmux_mutate("kill-window", "-t", target)`. Use `_tmux_mutate` (not the
  read-style `tmux()`) so a failure surfaces.
- Returns `{"ok": True, "terminated": handle}` on success; the mutate error
  string otherwise.
- Guard: refuse `terminate` targeting the caller's own pane (a Claude killing
  its own window mid-tool-call is a footgun, not a feature).

### Worker framing is the caller's job, not the tool's

No `role` flag, no auto-injected preamble. Delegation vs. handoff is purely a
function of what the spawning Claude writes into the `spawn_claude` prompt and
whether it follows up:

- **Delegate:** spawn with a prompt that says "report your result via the
  `report` tool when done"; stay alive; expect a wake from `report`.
- **Hand off / sever:** spawn with a self-contained prompt; don't follow up.
  The `spawned_by` breadcrumb is still recorded (for lineage) but never used.

The empirical finding shows even an unframed worker obeys channel directives,
so framing is reliability reinforcement the lead controls — not a mechanism
the tool layer needs to provide.

## Data-flow examples

**Delegation with report-back**

```
Lead:   spawn_claude(prompt="Investigate X. When done, call report() with
        your findings.")               → handle=ab12cd34
Lead:   (continues other work, goes idle)
Worker: ...works... calls report("X is caused by Y, fix in commit Z")
        → emit_channel_event(lead_pane, ...) → LEAD WAKES, reads report
Lead:   terminate("ab12cd34")          → worker window killed
```

**Mid-flight check without interrupting**

```
Lead:   peek("ab12cd34")               → last 20 turns of the worker's
        transcript, no message sent, worker undisturbed
```

**Context handoff (sever)**

```
Claude A (low on context):
        spawn_claude(prompt="<full state dump + next steps>. Continue this
        work independently.")          → handle returned, ignored
Claude A: exits. Worker runs on; report() to A would just error (not attached).
```

## Error handling & edge cases

- **Handle resolves to no live window** → `{"ok": False, "error": ...}`
  (window vanished from `list-windows`).
- **Target not attached** (`emit_channel_event` returns `False`) → explicit
  error. Covers worker-exited and worker-mid-boot.
- **`report()` with no `spawned_by`** → explicit error (root / hand-created
  pane).
- **Self-send / self-terminate** → refused with an error.
- **Message storms / ping-pong.** Lead wakes worker, worker reports, wakes
  lead, lead sends again… is the *intended* autonomous loop, but a runaway
  could burn tokens. v1 ships no rate-limit; noted as a watch item. The
  human-in-periscope can `terminate` any participant.

## Testing strategy

- **Tool handlers are unit-testable** with `emit_channel_event`,
  `_resolve_window`/`_resolve_pid_for_pane`, `set_window_fields`, and
  `get_turns_for_pane` mocked — no tmux, no live Claude in the test path.
  One `tests/test_channels_*` case per tool covering: happy path, handle
  resolves to nothing, target not attached, and the self-target guard.
- **`report()` routing**: assert it reads `spawned_by` and routes to the
  resolved parent pane; assert the no-spawner error.
- **Provenance**: assert `spawn_claude` writes `spawned_by` on the child's
  window entry (extend the existing spawn test).
- **Verify during implementation (not yet tested):** concurrent reports —
  spawn 3 workers, have all three `report()` to one idle lead near
  simultaneously, and confirm Claude Code coalesces/queues the wakes into
  sane turns without dropping messages. This does not change the tool shape;
  it validates the wake behavior under contention. If messages *are* dropped,
  the fallback is a minimal per-recipient debounce in `emit_channel_event` —
  out of scope unless the test shows it's needed.

## Open / deferred

- `peek` transcript slice size and field trimming — finalized against
  `get_turns_for_pane`'s real return shape during implementation.
- Lineage view on the dashboard (render `spawned_by` as a tree) — separate
  frontend follow-up; this spec only persists the data.
- Raw keystroke control — non-goal (see Scope); revisit with a concrete need.
