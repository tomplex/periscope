# Channels — design

**Date:** 2026-05-14
**Status:** draft, awaiting review
**Author:** Tom + Claude

## Summary

Periscope gains a two-way message bus with the Claude Code sessions it
spawns. The push direction uses Claude Code's
[channels](https://code.claude.com/docs/en/channels) feature: messages
emitted by an MCP server land in Claude's context on the next turn as
`<channel>` blocks, with no tool call required on Claude's side. The
reply direction is an MCP `reply` tool the channel exposes; Claude
calls it to surface free-form messages back in periscope's UI.

A small Python channel server (`channel_server.py`) runs as a stdio
child of each `claude` process started by periscope. It bridges
between Claude's MCP transport and periscope's HTTP/WS API: events
posted to `/api/channel/push` show up inside Claude; tool calls to
`reply` post to `/api/channel/reply` and appear in periscope's pane
card. Pane identity is `$TMUX_PANE` (e.g. `%32`), which tmux
guarantees stable within a tmux-server lifetime.

Channels are in research preview. Subscription requires
`--dangerously-load-development-channels server:periscope` on the
`claude` invocation — bare `--channels` only resolves entries on
Anthropic's allowlist (see "Setup" below for the product implication
of that flag name).

This is v1 — single-pane targeting, in-memory queues, free-form
messages, no auth. The follow-ups are structured event types,
cross-Claude addressing for the driver/worker pattern, and persisted
reply history.

## Goals

1. **Push without Claude buy-in.** Messages from periscope arrive in
   Claude's next-turn context regardless of what Claude is doing. No
   tool call, no polling, no scrape-and-pray.
2. **Reply when Claude wants to talk.** A single ergonomic tool —
   `reply` — lets Claude surface messages and status in periscope's UI.
   Constrained, discoverable, no sprawling MCP surface.
3. **Stay in Python.** The channel server is Python, lives in
   periscope's repo, and shares periscope's process tree as a stdio
   child. No Node, no JS toolchain, no second package manager. This
   carries a tax — the official Anthropic examples and the easy
   one-liner notification API are all TypeScript/Bun. The Python SDK's
   `send_notification` is constrained to a closed Pydantic union of
   spec-defined notification types, so we bypass it and write a
   `JSONRPCNotification` directly to the session's write stream (see
   "MCP shape: Notification emission" for the concrete code). Path-of-
   least-resistance for stdio MCP work is still TS; sticking with
   Python is a deliberate trade for single-language periscope.
4. **Stable addressing.** Pane identity is `$TMUX_PANE` (`%32`), owned
   by tmux and unique within a tmux-server lifetime. Surviving session
   and window renames eliminates a class of "where did my Claude go"
   bugs that periscope's existing `session=&index=` addressing has.
   See "Pane identity" for the recycle-on-server-restart caveat.
5. **Opt-in at spawn.** Channels only attach to Claude sessions
   periscope itself spawns (because they're the only ones launched
   with `--dangerously-load-development-channels server:periscope`).
   Hand-started sessions are unaffected.

## Non-goals

- **Pushing into hand-started Claude sessions.** Without the dev-
  channels flag on the launch command, there's no transport. Users
  who want it can spawn via periscope's UI or run a documented
  wrapper alias.
- **Cross-Claude orchestration via periscope as a bus.** The
  driver/worker pattern is real and interesting, but a v1 that
  addresses one Claude at a time is enough to validate the model.
  Routing Claude→Claude through periscope is a follow-up.
- **A full periscope MCP.** `list_panes`, `send_to_pane`, `read_pane`
  — all the Claude→periscope query surface — is deliberately out of
  scope. The `reply` tool covers the high-value Claude→periscope
  path (status declarations, "need human") without inviting Claude
  to drive periscope's UI as if it were a screen reader.
- **Persisted reply history.** Replies are in-memory per pane and
  rendered live in the pane card. A periscope restart clears them.
  Persisting is a future concern, gated by demand.
- **Structured event types from the periscope UI.** v1 push is free-
  form text plus an optional `severity`. Structured events (tests-
  passed, PR landed, build broke) are a follow-up that should land
  once we know which structures are useful.
- **Multi-pane addressing within a single window.** Periscope today
  addresses windows, not panes. The channel server is the first
  thing in periscope that's natively pane-scoped via `$TMUX_PANE`.
  UI surfaces remain window-scoped for v1 — push targets the
  window's active pane.
- **Auth.** Channel endpoints are localhost-only, same as the rest
  of periscope. Anyone on `127.0.0.1` can push to any Claude session
  periscope spawned. Acceptable for a single-user dashboard.
- **Getting periscope's channel server onto Anthropic's allowlist.**
  That's a packaging-and-publication path that gets users off the
  `--dangerously-load-development-channels` flag. Future work, not
  v1 — and contingent on channels graduating from research preview.

## The two channels

Terminology pinned for the rest of this spec:

- **Push:** periscope → Claude. Events emitted via MCP
  `notifications/claude/channel`. Surface in Claude's next turn as
  `<channel source="periscope" ...>content</channel>` blocks.
- **Reply:** Claude → periscope. Claude calls the MCP tool `reply` on
  the channel server. The server forwards to periscope's HTTP API.

These two are independent — the push side works even if Claude never
calls `reply`; the reply side works even if periscope never pushes.

## Architecture

```
periscope (server.py, FastAPI)
   │
   │  HTTP: /api/channel/push, /api/channel/reply
   │  WS:   /ws/channel
   │
   ▼
channel_server.py  ◀──── stdio (MCP) ────▶  claude
   (one subprocess per pane,
    spawned by claude via
    --dangerously-load-development-channels)
```

One channel server per Claude process, per the channels protocol
(the flag launches each named server as a stdio subprocess of
`claude`). The channel server holds:

- An MCP server that declares
  `experimental: { "claude/channel": {} }` in its
  `InitializationOptions` and exposes the `reply` tool. The server
  carries an `instructions` string that goes into Claude's system
  prompt (see "MCP shape: instructions").
- A WebSocket loop against periscope's `/ws/channel` endpoint. Each
  event received is translated into an MCP
  `notifications/claude/channel` written directly to the session's
  output stream.
- A forwarder that turns each `reply` tool call into a POST to
  periscope's `/api/channel/reply`.

This is the only viable shape: channels are stdio-bound per session
(see [channels reference](https://code.claude.com/docs/en/channels-reference)),
so you can't have a single in-process "channel server" multiplex N
Claudes. The per-pane subprocess is the bridge to periscope's central
state.

### Why WebSocket, not SSE or unix socket

Periscope already has a WebSocket (`/ws/pane`) for the xterm bridge
and no SSE anywhere. Reusing the WS pattern keeps the surface
coherent — same disconnect handling, same dependency footprint, same
mental model for someone reading the codebase. SSE would either add
a new PEP-723 dep (`sse-starlette`) for one endpoint or require
hand-rolling `StreamingResponse` with `data:` framing, keepalives,
and retry semantics. The "SSE survives proxies" argument is a non-
issue on `127.0.0.1`.

Unix socket would shave a few hundred microseconds of overhead but
costs a separate IPC mechanism, separate serialization, and a new
place to leak file descriptors. Reach for it if a real bottleneck
emerges. v1 stays on TCP+WS.

## Pane identity

The channel server learns its pane on startup from `$TMUX_PANE`, set
by tmux for every pane (`%32`, `%47`, etc.). tmux guarantees these
ids are unique within the life of the tmux server and never recycled
during that lifetime. They survive session/window renames, which is
the property that motivates using them.

They do **not** survive a tmux server restart. After `tmux kill-
server` (manual, OOM, reboot without resurrect cooperation), the
counter resets and `%32` may later refer to a different pane. This
is acceptable because (a) any Claude running pre-restart was killed
alongside the tmux server, so there's no in-flight channel to
confuse, and (b) periscope's in-memory pane state is keyed on `%N`
and self-heals via the subscribe-as-source-of-truth model — new
channel subprocesses register their new `%N` on reconnect, old `%N`
entries GC when periscope's poll notices the panes are gone.

Periscope today enumerates panes via `tmux list-windows -a` (see
`list_windows()` at `server.py:858-890`, with the format string at
`server.py:863`). The format already captures `#{@periscope_id}`
(the persistent-config-layer phase-2 stable id used in
`state.json/windows[]`); we append `#{pane_id}` so the active pane's
`%N` is captured on the window row too. Multi-pane windows are out
of v1 scope (see Non-goals), so the active-pane-only capture is
sufficient.

### Addressing tabulation

Three identifiers coexist after this spec lands. Each route uses one:

| Surface | Keys on | Notes |
|---|---|---|
| `/api/state` payload | n/a; returns `(session, index, pid, pane_id)` per window | Single read endpoint, all three keys present |
| `/api/send`, `/ws/pane`, `/api/window/new`, `/api/window/focus` | `(session, index)` | Existing surface; breaks under window-reorder and (less obviously) move-window/swap-window; not migrated by this PR |
| `state.json windows[]` rows | `pid` (from persistent-config phase 2) | Rebind-after-tmux-restart heuristic |
| `/api/channel/push`, `/api/channel/reply`, `/ws/channel` | `pane_id` (`%N`) | First surface to use this; stable within tmux-server lifetime |

The pane-card data model carries all three. UI components that need
to push translate from their existing `(session, index)` identity to
`pane_id` via the `/api/state` payload. If `pane_id` is absent (pane
is gone, or periscope hasn't yet observed a fresh tmux-server
restart), the push composer disables with a tooltip.

The schism is intentional v1 scope — migrating the rest of periscope
to `%N` is a separate refactor with its own risks. Tabulating it
here so reviewers know each surface's chosen key is the contract.

### Active-pane fallback

The UI today targets windows, not panes. For v1, "push to this
window" means "push to the window's active pane's channel, if any."
If the active pane isn't running a channel-enabled Claude, the push
UI is disabled with a tooltip ("this pane wasn't started with
channels; respawn via periscope's `+ claude` button").

## Setup: one-time `.mcp.json` registration

`claude --dangerously-load-development-channels server:periscope`
resolves `server:periscope` from the user's `~/.claude/.mcp.json`
(or project-local `.mcp.json`). Users add one entry during periscope
setup:

```json
{
  "mcpServers": {
    "periscope": {
      "command": "uv",
      "args": ["run", "--script", "/path/to/periscope/channel_server.py"],
      "env": {
        "PERISCOPE_URL": "http://127.0.0.1:8765"
      }
    }
  }
}
```

`channel_server.py` carries its own PEP-723 inline dependencies
(`mcp`, `httpx`, `websockets`) so `uv run --script` resolves them
without polluting the user's environment, matching periscope's
`server.py` convention.

A `periscope --install` (or README copy-paste snippet) writes this
entry. Implementation detail — not part of the v1 critical path.

### The "dangerously" wart

The launch flag is `--dangerously-load-development-channels`. That
string ends up in every spawned `+ claude` command (and the spawn
log, if periscope ever surfaces it). It is what it is during the
research preview, but two product implications worth naming:

1. **The default `commands[].exec` for `claude` will contain the
   word "dangerously."** Users who eyeball their `state.json` or the
   command label in the new-window tile will see it. The label
   shown in the UI defaults to the command's `label` field (e.g.
   "claude"), not the exec, so the visible UX is fine — but anyone
   who customizes commands or inspects state will encounter it.
2. **Graduation path.** Channels eventually leave research preview;
   the flag changes or disappears. Periscope's seeded default will
   need to update. The migration uses a global `channels_migration_v1_done`
   flag in `state.json` (see "Migration for existing users"); a
   future v2 migration adds its own flag and rewrites in turn.

We accept the wart for v1 because the alternatives (publishing a
channels plugin to Anthropic's allowlist, or shipping channels-free
periscope) are strictly worse for the immediate goal.

## Wire shape: channel_server ↔ periscope

Two HTTP endpoints and one WebSocket on periscope's FastAPI surface.

### `WS /ws/channel?pane=%N`

WebSocket. Long-lived. Periscope writes one JSON message per push
event:

```json
{ "content": "tests passed: 47/47", "meta": { "severity": "good" } }
```

The channel server reads from this socket and turns each message
into a `notifications/claude/channel` MCP notification with matching
`content` and `meta`. The socket is half-duplex in practice —
periscope writes, the channel server only reads — but bidirectional
WS is fine; a heartbeat ping/pong keeps the connection alive across
idle periods.

Mirrors `/ws/pane` for disconnect/reconnect semantics: on close, the
channel server reconnects with exponential backoff capped at 5s.
While disconnected, periscope queues events in-memory for that
pane; on reconnect, queued events flush in order before live events
resume.

### `POST /api/channel/push?pane=%N`

Body: `{ "content": str, "meta": object | null }`.

Periscope's UI or internal callers (future: hook into history
indexer, build watchers, etc.) call this to push to a pane. Periscope
writes the event to the pane's in-memory queue; if a subscriber is
currently connected on `/ws/channel`, the event is delivered
immediately. Otherwise it sits in the queue until reconnect.

**Meta key validation.** Channels silently drops `meta` keys that
contain characters outside `[A-Za-z_][A-Za-z0-9_]*` (see
[channels-reference](https://code.claude.com/docs/en/channels-reference)
§"Notification format"). Periscope validates server-side: keys
matching the identifier pattern pass through; any other key is
rejected with a 400 and the offending key named in the error body.
Hyphens are common in HTTP-source fields (`run-id`, `x-source`) and
silently swallowing them in Claude's context is exactly the kind of
bug worth catching at the boundary.

**Queue capacity per pane: 100 events. Overflow drops oldest.** This
is a correctness escape valve — under normal operation the queue is
empty because the channel server consumes events as they arrive.
When overflow happens, periscope coalesces a single synthetic event
on the queue:

```json
{
  "content": "periscope dropped N earlier events; oldest was at T",
  "meta": {
    "severity": "warning",
    "kind": "dropped",
    "synthetic": true
  }
}
```

The synthetic event is consumed normally on reconnect, giving Claude
a chance to notice and react. One synthetic event per overflow
window — successive drops update the same event's `N` until the
queue drains. `meta.synthetic=true` is a separate, generic flag so
future consumers can filter periscope-originated infrastructure
notices without enumerating semantic `kind` values. The prose is
unbracketed — angle brackets in `content` would land literally
inside the `<channel>...</channel>` wrapper Claude sees, which looks
like a nested pseudo-tag and is mildly confusing in transcripts.

### `POST /api/channel/reply?pane=%N`

Body: `{ "message": str, "severity": str | null, "kind": str | null }`.

The channel server's `reply` tool implementation calls this.
Periscope appends to the pane's in-memory reply log and increments
the pane's "unread" count, which the UI surfaces as a badge on the
pane card.

`kind` is a coarse classifier:

- `"info"` (default) — informational; surfaces as a normal message.
- `"need_human"` — Claude is blocked. Pane card gets a visible
  "needs attention" indicator; the modal opens to that pane on next
  focus.
- `"done"` — Claude finished its task. Pane card gets a "done"
  indicator that clears when the user opens the modal.

Three kinds is the minimum that lets the UI render meaningfully
different states. More can be added; the field is open-ended on the
wire and the UI degrades unknown kinds to `"info"`.

## MCP shape: channel_server ↔ claude

The channel server declares one experimental capability, one
`instructions` string, and one tool.

### Capability and instructions

We use the **lowlevel** `mcp.server.Server` (not `FastMCP`). FastMCP
hides `InitializationOptions` construction, which we need to control
to set `experimental` capabilities; and the notification-emission
path bypasses both APIs anyway (see next subsection). One API across
the file is cleaner than mixing flavors.

```python
from mcp.server import Server
from mcp.server.models import InitializationOptions
from mcp.types import ServerCapabilities, ToolsCapability

INSTRUCTIONS = """\
Messages from periscope arrive as <channel source="periscope" ...> blocks
on each turn. They may include severity, kind, or other meta attributes.

Use the `reply` tool to surface status back to periscope's UI:
  - kind="need_human" when blocked and waiting on the user
  - kind="done" when the current task is complete
  - kind="info" (default) for everything else

A <channel> block with meta.kind="dropped" is an infrastructure
notice — periscope's queue overflowed and dropped earlier events.
It is not a message from the user.

The pane this channel is attached to is identified by $TMUX_PANE on
the server side; you don't need to address it explicitly.
"""

server = Server("periscope")

init_options = InitializationOptions(
    server_name="periscope",
    server_version="0.1.0",
    capabilities=ServerCapabilities(
        experimental={"claude/channel": {}},
        tools=ToolsCapability(listChanged=False),
    ),
    instructions=INSTRUCTIONS,
)
```

`instructions` lands in Claude's system prompt at session start and
is the documented discoverability surface for channels (per
channels-reference §"Server options"). It explains the relationship
between inbound `<channel>` blocks, the synthetic-overflow event,
and the outbound `reply` tool — without it, Claude has no automatic
linkage between these.

`ServerCapabilities.experimental` is typed `dict[str, dict[str, Any]] | None`
in the SDK (verified against `mcp==1.27.*`'s `types.py`), so the
literal above passes through without a workaround.

### Notification emission

The Python MCP SDK's public `session.send_notification(...)` is
constrained to a closed Pydantic union of spec-defined notification
types (`mcp.types.ServerNotification`). It has no `method`/`params`
escape hatch the way the TypeScript SDK's `mcp.notification()` does.
We bypass it and write straight to the session's output stream,
mirroring what the SDK's own `send_notification` does internally:
wrap the `JSONRPCNotification` in a `JSONRPCMessage`, then in a
`SessionMessage`.

```python
from mcp.shared.message import SessionMessage
from mcp.types import JSONRPCMessage, JSONRPCNotification

async def emit_channel_event(session, content: str, meta: dict | None):
    notification = JSONRPCNotification(
        jsonrpc="2.0",
        method="notifications/claude/channel",
        params={"content": content, "meta": meta or {}},
    )
    await session._write_stream.send(
        SessionMessage(message=JSONRPCMessage(notification))
    )
```

The `JSONRPCMessage(...)` wrapper is required — `SessionMessage.message`
is typed as `JSONRPCMessage`, and the SDK's `send_notification`
(`mcp/shared/session.py:331-335` in `mcp==1.27.0`) constructs the
wrapper before sending. Skipping it fails at the dataclass
boundary.

`_write_stream` is a protected attribute; the SDK explicitly
disclaims stability for underscore-prefixed members. This is the
price of staying in Python. The implementation plan pins
`mcp==1.27.*` (where this exact path is current) and adds two
guards:

1. A **startup assertion** in `channel_server.py`:
   `hasattr(session, "_write_stream") and hasattr(session._write_stream, "send")`.
   Failing this raises a clear "upgrade required: SDK changed
   private-API surface" before anything else runs.
2. A **smoke test** in `tests/` that runs the channel server end-to-
   end against a fake peer and asserts emitted notifications arrive
   with the expected `method` and `params`. Caught-on-upgrade is the
   point of this test.

The notification surfaces in Claude's next turn as e.g.
`<channel source="periscope" severity="good">tests passed: 47/47</channel>`.
The `source` attribute defaults to the server name (`"periscope"`
here); other `meta` keys become attributes verbatim; `content` is
the block body.

### Reply tool

The lowlevel API registers tools via two decorators —
`@server.list_tools()` returning a tool schema, and
`@server.call_tool()` doing the actual dispatch. `@server.tool()`
(the FastMCP one-liner) does not exist on `mcp.server.Server`.

```python
import httpx
from mcp import types

@server.list_tools()
async def _list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="reply",
            description=(
                "Surface a message in periscope's UI for this pane. "
                "Use kind=\"need_human\" when blocked and waiting on the user, "
                "kind=\"done\" when the current task is complete, "
                "otherwise kind=\"info\"."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "message": {"type": "string"},
                    "kind": {
                        "type": "string",
                        "enum": ["info", "need_human", "done"],
                        "default": "info",
                    },
                    "severity": {
                        "type": "string",
                        "enum": ["info", "good", "warning", "bad"],
                        "default": "info",
                    },
                },
                "required": ["message"],
            },
        )
    ]

@server.call_tool()
async def _call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    if name != "reply":
        raise ValueError(f"unknown tool: {name}")

    message = arguments["message"]
    kind = arguments.get("kind", "info")
    severity = arguments.get("severity", "info")

    async with httpx.AsyncClient(timeout=5) as client:
        resp = await client.post(
            f"{PERISCOPE_URL}/api/channel/reply",
            params={"pane": TMUX_PANE},
            json={"message": message, "kind": kind, "severity": severity},
        )

    body = {
        "ok": resp.is_success,
        "status": resp.status_code,
        "kind": kind,
        "severity": severity,
    }
    return [types.TextContent(type="text", text=json.dumps(body))]
```

`async def` + `AsyncClient` is mandatory — a sync `httpx.post` would
block the MCP event loop alongside the WS consumer. The return is a
small JSON dict (serialized as `TextContent`) rather than an opaque
`"ok"`: Claude can parse it cleanly, and a human reading the
transcript sees a structured audit trail.

The tool description and the server-level `instructions` should
agree. `instructions` is the primary discoverability surface; the
tool description is what Claude sees on tool-list introspection.

## Spawn integration

Couples to the existing spawn path and to the worktree spec
(2026-05-13). Today's spawn (`/api/window/new` at `server.py:1632`)
sends `claude` (or whatever exec the user's `commands` entry
specifies). After this spec:

```
tmux new-window ... ; sleep 0.1 ; tmux send-keys 'claude --dangerously-load-development-channels server:periscope' Enter
```

For the worktree spawn from the worktree-integration spec (not yet
landed; see `/api/window/new-worktree` placeholder in that doc), the
same modification applies — the `claude` invocation gains the
dev-channels flag. No other change to the spawn flow.

### Migration for existing users

The persistent-config-layer phase 4 has already shipped: command
strings live in `state.json` under `commands[]`, and the frontend
renders one button per entry (`static/grid.js:144`). Existing users
have `commands` seeded with `[{label: "claude", exec: "claude"}, ...]`.
Changing the default seed only affects fresh installs; existing
users get no channels until they manually edit prefs.

Add a one-shot migration on server startup, gated by a global
`state.json` key (not a per-entry mark — see below):

```
if state.get("channels_migration_v1_done") is not True:
    for cmd in state["commands"]:
        if cmd.get("exec") == "claude":
            cmd["exec"] = (
                "claude --dangerously-load-development-channels "
                "server:periscope"
            )
    state["channels_migration_v1_done"] = True
    write_state()
```

The global flag is deliberate: a per-entry `version_mark` would
re-migrate any new `{exec: "claude"}` entry a user adds later
(intentionally — "I want plain claude, no channels"). A global
flag suppresses the migration after first run regardless of
`commands[]` state. The user's later edits to `commands` are
respected verbatim.

Graduation path: when channels leave research preview and the flag
name changes, a future `channels_migration_v2_done` key carries the
next rewrite. The pattern stays the same; the v1 flag just becomes
historical.

Migration runs once per startup behind the same coarse lock the
rest of `state.json` mutation uses. Idempotent — replaying it does
nothing after the first run.

### Detecting whether a pane has a channel

Periscope needs to know which panes are channel-attached so the UI
can enable/disable the push affordance. Two options:

1. **Track at spawn time.** When periscope spawns a Claude with the
   dev-channels flag, record `pane[%N].channel = true` in in-memory
   state. Lost on periscope restart, where a re-detection pass would
   re-learn from the active channel-subscribers list.
2. **Track at subscribe time.** The channel server connects to
   `/ws/channel` on startup. Periscope marks `%N` as channel-
   attached for as long as a subscriber is connected.

(2) is simpler and inherently self-healing — periscope restarts and
learns the state organically as subprocesses reconnect. The pane
card's "channel attached" indicator just reflects "is there a live
subscriber for this `%N`?" v1 uses (2).

## UI surface

### Pane card

The pane card (the grid tile) gains:

- A small dot or icon in the corner when a channel is attached
  (live subscriber). Absent = no channel.
- A red dot / count when `unread_replies > 0`.
- A visible "needs attention" treatment when any unread reply has
  `kind: "need_human"`. This is the high-signal case worth surfacing
  prominently — the rest of the grid should fade slightly so the
  attention-needing pane pops.

### Modal (open pane view)

The modal grows a **messages strip** along one edge (probably right,
above the input box), rendering replies oldest-to-newest with `kind`
controlling color/icon. Opening the modal clears `unread_replies`.

The strip also exposes a **push composer**: a small text input plus
a "push" button. Submitting calls `POST /api/channel/push?pane=%N`
with the text as `content` and no `meta`. This is the "look at this"
/ "status check" / "the build failed, FYI" path Tom can use from any
pane card.

For v1, the composer is plain text — no severity selector, no
templates. If templates ("ping with build status," "tell Claude to
commit") prove useful, add them later.

### Session-level fan-out

A small "broadcast" affordance at the session header — push to every
channel-attached pane in the session. Useful for "wrap up your task,
we're switching contexts." Implementation: iterate session's panes,
filter to those with live subscribers, post to each. Skip for v1 if
it adds non-trivial UI complexity; the per-pane composer is the
must-have.

## Failure modes

1. **Channel server can't reach periscope on startup.** Retry with
   backoff. After 30s of failure, exit nonzero — Claude reports the
   channel server crashed and continues without it.
2. **Channel server crashes mid-session.** Claude continues;
   periscope's WS closes; the pane card's "channel attached"
   indicator clears. The pane is now in the same state as a hand-
   started Claude. No auto-restart in v1.
3. **Periscope restarts.** Channel subprocesses are still alive but
   their `/ws/channel` connections are dead. Reconnect with backoff.
   Queued events on the periscope side are gone (in-memory only);
   newly-pushed events land normally once reconnected.
4. **Pane closes (tmux kill-window).** Claude exits, channel
   subprocess exits as Claude's child (see Failure mode #8 for the
   lifecycle requirement that makes this work), WS closes from the
   subprocess side. Periscope's pane-enumeration poll notices the
   pane is gone and GCs the in-memory state (queue, reply log) for
   that `%N`.
5. **tmux server restart.** `%N` values reset. Periscope's in-memory
   channel state keyed by `%N` is invalidated. This is benign:
   every Claude alive pre-restart was killed alongside the tmux
   server, so there's nothing to re-route. New channel subprocesses
   register their new `%N` on reconnect via the subscribe-as-truth
   model. The push UI's "channel attached" indicator may briefly
   flap during periscope's next poll cycle, then settles.
6. **Reply tool called with `kind` Claude invented.** Server-side
   accepts any string for `kind` and `severity`; the UI degrades
   unknown values to defaults. Forward-compatible.
7. **Claude calls `reply` while periscope is unreachable.** The
   tool call's `httpx` request times out or refuses; the tool
   returns an error string. Claude sees the tool failed and can
   decide what to do. No retry — replies are informational,
   dropping one is acceptable.
8. **Channel subprocess lifecycle on Claude exit.** The MCP stdio
   transport closes when Claude exits, which closes `stdin` on the
   subprocess side. `channel_server.py` must wire its lifecycle so
   stdin EOF terminates the process within a few seconds. Concrete
   requirement: the WS consumer task and the MCP run loop live in
   the same `anyio.create_task_group()` (the SDK's structured-
   concurrency primitive — `stdio_server` and the lowlevel `Server`
   both use anyio internally; mixing in `asyncio.TaskGroup` adds a
   backend-coupling seam that's easy to mis-cancel). Exception
   propagation from the MCP loop's natural exit on stdin EOF
   cancels the WS task. An implementation that puts the two halves
   in a bare `asyncio.gather()` and doesn't propagate cancellation
   correctly will leak channel servers. The plan includes a test:
   `claude` exits, `channel_server.py` PID gone within 5s.
9. **WS reconnect race against a stale subscriber.** After periscope
   restart, the channel subprocess reconnects to `/ws/channel`
   before periscope has finished reaping the old WS's TCP socket
   for the same `%N`. The new connection sees a 409
   "already-subscribed" against its own dead predecessor. Periscope
   resolves this by bumping the existing subscriber on a new
   subscribe — last-writer-wins, send the old socket a close frame
   with reason `"superseded"`. Simpler than retry-with-backoff on
   the subprocess side, and the subprocess's reconnect is the
   correct intent.

## Implementation notes for plan-writing time

A few wrinkles flagged up front so the implementation plan can
account for them:

1. **`channel_server.py` is its own file, not part of `server.py`.**
   The CLAUDE.md "single-file server" rule applies to FastAPI/HTTP
   surface; the channel server is a separate process started by
   Claude, not by uvicorn. A sibling file is the right shape. It
   carries its own PEP-723 header.

2. **MCP SDK pin: `mcp==1.27.*`.** This is the version verified
   against during spec review — `Server`, `InitializationOptions`,
   `ServerCapabilities`, `JSONRPCNotification`, `JSONRPCMessage`,
   `SessionMessage`, and `session._write_stream.send(...)` all
   work as the spec describes. Pin the minor in `channel_server.py`'s
   PEP-723 header; pin major-bound (`>=1.27,<2`) is too loose given
   the underscore-prefixed attribute we depend on. The startup
   assertion (see "Notification emission") plus an end-to-end smoke
   test guard against SDK churn.

3. **`websockets` SDK pin: `websockets>=15,<17`.** Use the modern
   async client: `from websockets.asyncio.client import connect`.
   The legacy `websockets.connect()` import path is being phased out
   and we want code that's still building in mid-2026. The PEP-723
   header lists `mcp==1.27.*`, `httpx`, and `websockets>=15,<17`.

4. **`%N` capture in pane parsing.** `server.py:863`'s
   `tmux list-windows -a` format string already has `#{@periscope_id}`
   (phase-2 stable id); append `#{pane_id}`. The parser needs a new
   field; `/api/state`'s window-object schema gains `pane_id`.
   Multi-pane windows are out of scope (active pane only).

5. **In-memory state survives the FastAPI app lifetime.** Pane
   queues and reply logs go in module-level dicts protected by a
   single lock (read traffic is low, contention non-issue). GC
   tied to pane enumeration — when a `%N` disappears from
   `list-windows -a`, drop its state.

6. **Channel subprocess lifecycle.** Per Failure mode #8: use
   `anyio.create_task_group()` (not `asyncio.TaskGroup` — the SDK
   transport is anyio-based and mixing primitives is a known
   structured-concurrency footgun) for the MCP loop + WS consumer,
   propagate cancellation, test the kill-claude-confirm-subprocess-
   exit path. Leaked channel servers consume FastAPI WS connection
   slots and aren't free.

7. **The `--dangerously-load-development-channels server:periscope`
   flag is required at spawn.** The spawn integration changes are
   tiny but touch `/api/window/new` at `server.py:1632` and (when
   worktree integration lands) `/api/window/new-worktree`. Both
   must pass the flag through. The seeded `commands` entry default
   and the one-shot migration handle the persisted-prefs side.

8. **The `instructions` string is product copy.** Claude's
   discoverability of `reply` depends on it. Treat its wording the
   same as user-visible docs.

9. **Meta-key validation lives at the push endpoint, not the
   subscriber.** Validating on the way in (`/api/channel/push`)
   catches the bug at the right boundary — by the time the channel
   server emits the notification, it's too late and the keys are
   silently swallowed. The Pydantic body model for the endpoint
   gets a custom validator on the `meta` dict.

## Phasing

Single-PR scope:

1. `channel_server.py` (new file): MCP server with capability,
   `instructions`, `reply` tool, WS consumer, direct-stream
   notification emit, periscope HTTP client.
2. `server.py` additions: `/ws/channel`, `/api/channel/push`,
   `/api/channel/reply`; pane-state dicts; `pane_id` capture in
   `list-windows -a`; `commands[]` migration on startup.
3. Spawn-path: `--dangerously-load-development-channels server:periscope`
   in the default seeded `+ claude` exec, applied to fresh installs
   via seed and to existing users via migration. `/api/window/new`
   passes the user's exec through verbatim — no per-endpoint flag
   injection.
4. Frontend: pane-card channel/unread indicators; modal messages
   strip + push composer; pane-card data model carries `pane_id`.
5. Setup docs: README snippet for `~/.claude/.mcp.json`.

Ship-criteria:

- Spawning a Claude via periscope's `+ claude` button produces a
  pane with a live channel (the pane card shows the channel
  indicator within one poll cycle).
- Typing a message in that pane's modal composer and clicking
  "push" causes the message to appear in Claude's next turn as a
  `<channel source="periscope">` block.
- Claude calling `reply("hi from the bot")` causes "hi from the
  bot" to appear in the pane's modal messages strip and trip an
  unread badge on the pane card.
- Claude calling `reply("ready for review", kind="need_human")`
  flags the pane card with a visible attention indicator.
- Closing the pane (`tmux kill-window`) terminates the channel
  subprocess within 5s and cleans up in-memory state on
  periscope's side.
- An existing user with `commands: [{exec: "claude"}]` from before
  this PR gets migrated to the dev-channels exec on first startup
  and gets channels on their next `+ claude` spawn.

## Open questions

1. **Should `reply` accept rich content (markdown, images)?** v1 is
   plain text. Markdown rendering in the messages strip is cheap
   if useful; defer to implementation time and revisit when we see
   actual reply content.

2. **Should periscope ever push *automatically*?** The history
   indexer already runs on a hook; it could emit a
   `notifications/claude/channel` when a long-running session
   detects its own claude-history-search transcript getting
   indexed. Out of v1 scope but worth flagging — the push direction
   is built for future programmatic emitters, not just the UI
   composer.

3. **Cross-Claude messaging.** The architecture supports "push
   from pane A's `reply` handler to pane B's push queue" with no
   new primitives — periscope is the bus, both halves already
   exist. Whether to expose this in the UI (a "forward this reply
   to pane X" action) is a v1.x question. Not blocking.

4. **How visible should the `--dangerously-load-development-channels`
   string be in the UI?** Today's plan: it lives in `commands[].exec`
   in `state.json`, which is user-inspectable but not surfaced in
   the new-window tile (which shows `label`). Could go further —
   hide the flag in a generated wrapper script (`bin/periscope-claude`,
   ~20 lines, no Claude-side complications expected) that the seeded
   exec invokes. Probably not worth it for v1, but cheap enough that
   the right call may flip if someone screenshots the spawn log and
   the word "dangerously" ends up in a public bug report.

None blocking.
