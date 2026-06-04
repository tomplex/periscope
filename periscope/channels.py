"""Channels: in-process MCP server bound to /tmp/periscope-mcp.sock.

Each Claude pane spawns a thin `channel_shim.py` subprocess (the documented
stdio MCP entry point Claude requires), which connects to the unix socket
and proxies bytes between Claude's stdio and our socket. All MCP logic —
tool registration, capability declaration, notification emission — lives
here.

Locking: `_CHANNELS_LOCK` (threading.Lock) protects the alert log and
session registry. Separate from `_STATE_LOCK` because channel state is
touched from both sync request handlers (FastAPI threadpool) and async
MCP handlers; threading.Lock works correctly from both whereas
asyncio.Lock only blocks coroutines.

State.json writes go through `periscope.store.set_window_fields`; this
module never touches `_STATE` directly. `list_windows`/`note_focus`/
`note_action` come from `periscope.panes`, `_attach_git_then_resolve_pids`
from `periscope.pids` — no bridges to server.py remain in this module.

Socket lifecycle: this module never `os.unlink`s `MCP_SOCKET_PATH` on
shutdown. The FastAPI `lifespan` in `server.py` owns shutdown cleanup; the
listener here only removes a stale socket file on startup before binding.
"""

import asyncio
import json
import os
import threading
import time
import uuid
from typing import Any

from periscope.config import MCP_SOCKET_PATH
from periscope.log import log
from periscope.panes import list_windows, note_focus, note_action
from periscope.pids import _attach_git_then_resolve_pids
from periscope.store import set_window_fields
from periscope.tmux import tmux, _run, _tmux_mutate

CHANNEL_INSTRUCTIONS = """\
You are running inside periscope, a dashboard the user has open in their
browser that watches every tmux pane on this machine. Periscope shows the
user what every Claude session is doing across all their panes at once.
The pane this channel is attached to is identified by $TMUX_PANE on the
server side; you don't need to address it explicitly.

You have several tools that mutate periscope's UI for this pane. Use them
proactively — the user doesn't need to ask. The triggers below are
specific enough that you won't over-call them:

- link_pr(number): when you identify the user is working on a specific
  GitHub PR — a #N reference in their message, in `git status` / `git log`
  / branch name, or in CLAUDE.md — AND periscope's pane card doesn't
  already show a #PR badge. Periscope auto-detects PRs from Claude's
  title bar; if you know there's one and it isn't surfaced, link it.

- link_linear(id, title?, status?): when you identify a Linear ticket
  in the user's message, branch name, or commit history (TEAM-123
  format, e.g. FAR-456). Periscope doesn't auto-detect Linear tickets,
  so explicit linking is the only way to surface them on the card. Pass
  `title` and `status` whenever you know them — if you fetched the
  ticket through the Linear MCP, you already have both.

- notify(message, kind="done"): when you finish a substantial task the
  user asked for. One-sentence summary in `message`. Lets the user see
  at-a-glance that this pane is done and what you did, without opening
  the modal.

- notify(message, kind="need_human"): when you're blocked and waiting on
  input. Pulses the pane card with a red border so the user notices the
  alert across a busy dashboard.

- notify(message, kind="info"): for status updates worth glancing at but
  not blocking on (e.g., "tests pass, about to commit"). Use sparingly
  — this is the lowest-signal kind and adds dashboard noise if overused.

- spawn_claude(prompt, session?, cwd?, name?): launch a fresh Claude
  session in a new tmux window with the given prompt as its first
  message. The new window appears on the dashboard. Use when the user
  asks you to delegate, parallelize, or "spin up another session" — or
  when the task at hand decomposes into independent sub-tasks that
  each deserve their own focused context. Default `session` is yours,
  `cwd` is your pane's working directory; override to group sub-agents
  in a dedicated session or point them at a different repo. Keep the
  returned target/pid so you can refer to the spawned pane later.

Messages going the other direction (periscope → you) arrive as
<channel source="periscope" ...> blocks at the start of each turn. A
<channel> block with meta.kind="dropped" is an infrastructure notice
(periscope's queue overflowed); it is not a message from the user.
"""

_CHANNELS_LOCK = threading.Lock()
# pane_id -> list[dict]   alert log (kind, severity, message, ts)
_CHANNEL_ALERTS: dict[str, list[dict]] = {}
# pane_id -> int          unread alert count, cleared when modal opens
_CHANNEL_UNREAD: dict[str, int] = {}
# pane_id -> MCP ServerSession reference. Presence is the "channel
# attached" indicator and the route for notification emission. Typed Any
# because the SDK's BaseSession-derived objects aren't reliably importable
# at module load (we lazy-load mcp); attribute access happens in
# emit_channel_event where the runtime shape is what matters.
_MCP_SESSIONS: dict[str, Any] = {}


def channel_state_for(pane_id: str) -> dict:
    """Channel state for a pane: whether an MCP session is attached, the
    unread-alert count, and a copy of the alert log. Empty when `pane_id`
    is blank. Holds `_CHANNELS_LOCK` internally so callers (window_view,
    routes/pane) need not reach into the channel dicts directly."""
    if not pane_id:
        return {"attached": False, "unread": 0, "alerts": []}
    with _CHANNELS_LOCK:
        return {
            "attached": pane_id in _MCP_SESSIONS,
            "unread": _CHANNEL_UNREAD.get(pane_id, 0),
            "alerts": list(_CHANNEL_ALERTS.get(pane_id, [])),
        }


def _tool_result(body: dict) -> list:
    """Wrap a tool-result dict in the MCP TextContent list shape every tool
    handler returns. One place to change if the wire format ever grows an
    `isError` flag or a structured-content field."""
    from mcp import types

    return [types.TextContent(type="text", text=json.dumps(body))]


def _channel_gc(known_pane_ids: set[str]) -> None:
    """Drop alert state for panes that no longer exist. Session registry is
    GC'd by the connection handler on disconnect, not here."""
    with _CHANNELS_LOCK:
        for d in (_CHANNEL_ALERTS, _CHANNEL_UNREAD):
            for stale in [k for k in d if k not in known_pane_ids]:
                d.pop(stale, None)


def _do_notify_tool(pane: str, arguments: dict):
    """Tool implementation for `notify` — appends to the per-pane alert log
    and bumps the unread count. Surfaces in periscope's UI on next poll."""
    message = arguments["message"]
    kind = arguments.get("kind", "info")
    severity = arguments.get("severity", "info")

    entry = {
        "id": uuid.uuid4().hex,
        "message": message,
        "kind": kind,
        "severity": severity,
        "ts": int(time.time()),
    }
    with _CHANNELS_LOCK:
        _CHANNEL_ALERTS.setdefault(pane, []).append(entry)
        _CHANNEL_UNREAD[pane] = _CHANNEL_UNREAD.get(pane, 0) + 1

    # Durable mirror (survives restart, feeds the modal's merged Activity
    # stream). _CHANNEL_ALERTS above stays as the write-through cache for
    # the unread badge. record()'s positional args are
    # (scope_kind, scope_key, event_kind, text); the alert kind
    # (done/need_human/info) rides in `detail`.
    try:
        from periscope import activity
        activity.record("pane", pane, "alert", message,
                        detail=kind, at=entry["ts"])
    except Exception:
        log.warning("activity.record failed for notify()", exc_info=True)

    body = {"ok": True, "kind": kind, "severity": severity}
    return _tool_result(body)


def _resolve_window(match) -> tuple[str, str]:
    """Find the first `list_windows()` entry satisfying `match`, resolve its
    persistent @periscope_id (minting one if the window is new), and return
    `(pid, pane_id)`. Returns `("", "")` if no window matches — e.g. the
    pane has vanished from tmux's list-windows.

    `match` is a predicate over a window dict; callers vary only in the
    lookup key (pane_id %N vs (session, index))."""
    for w in list_windows():
        if match(w):
            _attach_git_then_resolve_pids([w])
            return w.get("pid") or "", w.get("pane_id") or ""
    return "", ""


def _resolve_pid_for_pane(pane_id: str) -> str:
    """Find the persistent @periscope_id (pid) for a tmux %N pane id.
    Mints a fresh pid if the window hasn't been seen before. Returns ""
    if the pane has vanished from tmux's list-windows."""
    pid, _ = _resolve_window(lambda w: w.get("pane_id") == pane_id)
    return pid


def _do_link_pr_tool(pane: str, arguments: dict):
    """Persist a linked PR number on the window's state.json entry.
    Overrides the auto-detected `pr` field when present."""
    try:
        number = int(arguments["number"])
    except (KeyError, TypeError, ValueError):
        body = {"ok": False, "error": "number must be an integer"}
        return _tool_result(body)

    pid = _resolve_pid_for_pane(pane)
    if not pid:
        body = {"ok": False, "error": f"could not resolve pid for pane {pane}"}
        return _tool_result(body)

    set_window_fields(pid, linked_pr=number)
    body = {"ok": True, "linked_pr": number, "pid": pid}
    return _tool_result(body)


def _do_link_linear_tool(pane: str, arguments: dict):
    """Persist a linked Linear ticket on the window's state.json entry.

    `id` is required; `title` and `status` are optional metadata. Each call
    fully describes the link — omitted title/status clear any prior value
    (set_window_fields drops keys set to None), so a re-link with just an id
    won't leave stale metadata pointing at a different ticket."""
    ticket_id = str(arguments.get("id", "")).strip()
    if not ticket_id:
        body = {"ok": False, "error": "id is required and must be non-empty"}
        return _tool_result(body)

    title = str(arguments.get("title", "")).strip()
    status = str(arguments.get("status", "")).strip()

    pid = _resolve_pid_for_pane(pane)
    if not pid:
        body = {"ok": False, "error": f"could not resolve pid for pane {pane}"}
        return _tool_result(body)

    set_window_fields(
        pid,
        linked_linear=ticket_id,
        linked_linear_title=title or None,
        linked_linear_status=status or None,
    )
    body = {
        "ok": True,
        "linked_linear": ticket_id,
        "linked_linear_title": title,
        "linked_linear_status": status,
        "pid": pid,
    }
    return _tool_result(body)


async def _do_spawn_claude_tool(pane: str, arguments: dict):
    """Spawn a new tmux window running `claude`, deliver an initial prompt.

    Async because Claude's TUI needs ~1.5s to mount before it can absorb a
    paste — using time.sleep here would block the event loop for every
    other pane's MCP connection sharing it."""
    prompt = str(arguments.get("prompt", "")).strip()
    if not prompt:
        body = {"ok": False, "error": "prompt is required and must be non-empty"}
        return _tool_result(body)

    # Caller's pane → its session + cwd. If the pane has vanished (rare —
    # the connection would normally drop first), tmux returns empty and we
    # fall through to defaults.
    info = tmux(
        "display-message", "-t", pane, "-p", "#{session_name}|#{pane_current_path}",
    ).strip()
    caller_session, _, caller_cwd = info.partition("|")

    session = str(arguments.get("session") or caller_session or "spawned").strip()
    cwd = str(arguments.get("cwd") or caller_cwd or os.path.expanduser("~")).strip()
    if not os.path.isdir(cwd):
        cwd = os.path.expanduser("~")
    name = str(arguments.get("name") or "").strip()

    # Create the session if missing, otherwise add a window to it. Both
    # paths use `-P -F #{window_index}` so we know the spawned slot — with
    # base-index 1 in tmux.conf, hardcoding :0 would silently target the
    # wrong window (see /api/window/new resume notes).
    code, _ = _run(["tmux", "has-session", "-t", session])
    if code != 0:
        ok, msg = _tmux_mutate(
            "new-session", "-d", "-s", session, "-c", cwd,
            "-P", "-F", "#{window_index}",
            *(["-n", name] if name else []),
        )
    else:
        ok, msg = _tmux_mutate(
            "new-window", "-t", f"{session}:", "-c", cwd,
            "-P", "-F", "#{window_index}",
            *(["-n", name] if name else []),
        )
    if not ok:
        body = {"ok": False, "error": msg}
        return _tool_result(body)
    try:
        index = int(msg)
    except ValueError:
        body = {"ok": False, "error": f"tmux returned unexpected index: {msg!r}"}
        return _tool_result(body)
    target = f"{session}:{index}"

    # Let the shell rc finish before the `claude` command line arrives, so
    # it runs as a real prompt entry rather than mid-rc echoed text. The
    # dev-channels flag is what makes the spawned Claude connect back to
    # periscope's MCP socket.
    from periscope.config import CLAUDE_EXEC
    await asyncio.sleep(0.1)
    tmux("send-keys", "-t", target, CLAUDE_EXEC, "Enter")

    # Claude Code shows a one-time-per-process consent prompt for the
    # --dangerously-load-development-channels flag:
    #     WARNING: Loading development channels
    #     ...
    #     ❯ 1. I am using this for local development
    #        2. Exit
    # Default selection is option 1; bare Enter confirms. Without
    # dismissing it, paste-buffer lands on the chooser (which only accepts
    # digit keys) and is silently dropped, leaving the input box empty.
    #
    # Poll a plain (non-`-e`) capture-pane — `capture()` preserves SGR
    # escapes, which interleave the dialog text with ANSI codes and break
    # substring matching.
    def _plain_snapshot() -> str:
        return tmux("capture-pane", "-t", target, "-p", "-S", "-30")

    for _ in range(50):  # up to 5s
        await asyncio.sleep(0.1)
        if "Loading development channels" in _plain_snapshot():
            tmux("send-keys", "-t", target, "Enter")
            break

    # Even after dismissal the React TUI takes another beat to mount its
    # keyboard handler — `❯` (the input glyph) and `auto mode on` (status
    # line) both render before paste-buffer is reliably accepted. Wait
    # for the post-dialog state, then a small settle window.
    for _ in range(50):  # up to 5s
        await asyncio.sleep(0.1)
        snap = _plain_snapshot()
        if "auto mode on" in snap and "Loading development channels" not in snap:
            await asyncio.sleep(0.5)
            break

    # Deliver the prompt via paste-buffer because send-keys strips embedded
    # newlines (see CLAUDE.md key invariant 5). Same buffer-name uuid trick
    # as `_send_to_target` so concurrent spawns don't trample each other.
    buf = f"spawn-{uuid.uuid4().hex[:8]}"
    tmux("set-buffer", "-b", buf, prompt)
    tmux("paste-buffer", "-d", "-p", "-b", buf, "-t", target)
    # Same 100ms bracketed-paste delay as /api/send — without it the Enter
    # can land before the paste applies, submitting empty input.
    await asyncio.sleep(0.1)
    tmux("send-keys", "-t", target, "Enter")

    note_focus(target)
    note_action(target)

    # Resolve the spawned window's stable @periscope_id so the caller can
    # reference it across restarts. Matched on (session, index) rather than
    # pane_id %N because we don't have the pane_id yet.
    pid, pane_id = _resolve_window(
        lambda w: w.get("session") == session and w.get("index") == index
    )

    body = {
        "ok": True,
        "target": target,
        "session": session,
        "index": index,
        "pid": pid,
        "pane_id": pane_id,
    }
    return _tool_result(body)


async def emit_channel_event(pane: str, content: str, meta: dict | None = None) -> bool:
    """Push a `notifications/claude/channel` event to the Claude connected
    on `pane`. Returns True on send, False if no session attached.

    The push direction has no current consumer in periscope's UI (Tom's
    framing: this is plumbing for future external producers like webhooks
    or autonomous TODO loops). Built so it's there when needed."""
    from mcp.shared.message import SessionMessage
    from mcp.types import JSONRPCMessage, JSONRPCNotification

    with _CHANNELS_LOCK:
        session = _MCP_SESSIONS.get(pane)
    if session is None:
        return False

    notification = JSONRPCNotification(
        jsonrpc="2.0",
        method="notifications/claude/channel",
        params={"content": content, "meta": meta or {}},
    )
    try:
        await session._write_stream.send(  # type: ignore[attr-defined]
            SessionMessage(message=JSONRPCMessage(notification))
        )
        return True
    except Exception:
        return False


async def _mcp_listener() -> None:
    """Bind the unix socket and accept connections from channel_shim.py.
    Each connection runs a fresh per-pane MCP Server in _handle_mcp_connection."""
    try:
        os.unlink(MCP_SOCKET_PATH)
    except FileNotFoundError:
        pass

    server = await asyncio.start_unix_server(
        _handle_mcp_connection, path=MCP_SOCKET_PATH
    )
    os.chmod(MCP_SOCKET_PATH, 0o600)
    log.info("MCP listener bound to %s", MCP_SOCKET_PATH)
    try:
        async with server:
            await server.serve_forever()
    finally:
        # Tearing the listener down on uvicorn reload needs all three steps:
        # close() stops accept, close_clients() closes the transports, and a
        # bounded wait_closed() lets the per-pane handlers unwind. The bound
        # is essential — anyio-bridged MCP handlers don't always notice their
        # underlying socket is gone, and an unbounded wait_closed() wedges
        # the lifespan handler so every dev save hangs the server.
        server.close()
        server.close_clients()
        try:
            await asyncio.wait_for(server.wait_closed(), timeout=1.0)
        except (asyncio.TimeoutError, Exception):
            pass


async def _handle_mcp_connection(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    """Per-connection MCP handler: read hello frame, dispatch to
    _run_mcp_for_pane, clean up session registry on close."""
    pane = ""
    try:
        # Hello frame: a single JSON line {"pane": "%N"}.
        hello_line = await reader.readline()
        if not hello_line:
            return
        try:
            hello = json.loads(hello_line.decode().strip())
            pane = hello.get("pane", "")
        except (json.JSONDecodeError, UnicodeDecodeError):
            return
        if not pane.startswith("%"):
            return

        await _run_mcp_for_pane(reader, writer, pane)
    except Exception as e:
        log.warning("MCP connection %s failed: %s", pane or "<no-pane>", e)
    finally:
        if pane:
            with _CHANNELS_LOCK:
                _MCP_SESSIONS.pop(pane, None)
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


# --- MCP tool registry ---
# Each record co-locates a tool's name, JSON schema, and handler. `_list_tools`
# maps it to `types.Tool` objects (mcp `types` is lazy-imported, so the registry
# itself stays plain data); `_call_tool` dispatches by iterating it. Adding a
# tool is one record here plus one `_do_*` handler — no separate schema list and
# dispatch branch to keep in sync.
_CHANNEL_TOOLS = [
    {
        "name": "notify",
        "description": (
            "Surface a message in periscope's UI for this pane. "
            "Use kind=\"need_human\" when blocked and waiting on the user, "
            "kind=\"done\" when the current task is complete, "
            "otherwise kind=\"info\"."
        ),
        "inputSchema": {
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
        "handler": _do_notify_tool,
    },
    {
        "name": "link_pr",
        "description": (
            "Link this pane to a GitHub PR by number. Use when the user "
            "is working on a specific PR and periscope's auto-detection "
            "hasn't surfaced it on the pane card (periscope reads PR "
            "URLs from Claude's title-bar status line; if the card "
            "shows no #PR badge but you know there is one, link it). "
            "Overrides the auto-detected PR until the user removes the "
            "link."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "number": {"type": "integer", "minimum": 1},
            },
            "required": ["number"],
        },
        "handler": _do_link_pr_tool,
    },
    {
        "name": "link_linear",
        "description": (
            "Link this pane to a Linear ticket. Use when the user is "
            "working on a Linear ticket. Periscope doesn't auto-detect "
            "Linear tickets, so this is the only way to surface one on "
            "the pane card. Pass `title` and `status` when you know "
            "them (e.g. you fetched the ticket via the Linear MCP) so "
            "the card shows what the ticket is, not just its id. Each "
            "call fully describes the link — omitting title/status "
            "clears any previously set value."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {
                    "type": "string",
                    "pattern": r"^[A-Z]+-\d+$",
                    "description": "Ticket id, TEAM-123 format (e.g. FAR-456).",
                },
                "title": {
                    "type": "string",
                    "description": "Ticket title, shown alongside the id on the pane card.",
                },
                "status": {
                    "type": "string",
                    "description": (
                        "Workflow state, free-form (e.g. 'In Progress', "
                        "'In Review', 'Done'). Rendered as a pill in the modal."
                    ),
                },
            },
            "required": ["id"],
        },
        "handler": _do_link_linear_tool,
    },
    {
        "name": "spawn_claude",
        "description": (
            "Spawn a fresh Claude Code session in a new tmux window "
            "and deliver an initial prompt to it. The new window "
            "appears on periscope's dashboard alongside this one. "
            "Use when: (1) the user explicitly asks to delegate, "
            "parallelize, or spin up another Claude session; "
            "(2) the current task decomposes into independent "
            "sub-tasks that benefit from focused, isolated "
            "contexts running concurrently. Returns target / "
            "session / index / pid / pane_id for the spawned pane "
            "— keep them so you can address it again later."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "Initial message to send to the spawned Claude session.",
                },
                "session": {
                    "type": "string",
                    "description": "tmux session to spawn into. Defaults to the caller's session. Created if it doesn't exist.",
                },
                "cwd": {
                    "type": "string",
                    "description": "Working directory for the spawned window. Defaults to the caller's pane cwd.",
                },
                "name": {
                    "type": "string",
                    "description": "Optional name for the new tmux window.",
                },
            },
            "required": ["prompt"],
        },
        "handler": _do_spawn_claude_tool,
    },
]


async def _run_mcp_for_pane(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, pane: str
) -> None:
    """Run a per-pane MCP Server over the given asyncio socket streams.

    Bridges asyncio byte streams ↔ anyio object streams that the MCP SDK
    consumes. Tool handlers close over `pane` so they target the right
    per-pane state without needing thread-local lookups."""
    # Lazy-imported — MCP SDK is heavy; only loaded once any pane connects.
    import anyio
    from mcp.server import Server
    from mcp.server.models import InitializationOptions
    from mcp.shared.message import SessionMessage
    from mcp import types
    from mcp.types import JSONRPCMessage, ServerCapabilities, ToolsCapability

    server = Server("periscope")

    @server.list_tools()
    async def _list_tools() -> list[types.Tool]:
        # Capture this connection's session for notification emission. Claude
        # always calls tools/list during init, so this fires reliably and
        # before any tools/call could need it.
        try:
            sess = server.request_context.session  # type: ignore[attr-defined]
            with _CHANNELS_LOCK:
                _MCP_SESSIONS[pane] = sess
        except LookupError:
            pass
        return [
            types.Tool(
                name=t["name"],
                description=t["description"],
                inputSchema=t["inputSchema"],
            )
            for t in _CHANNEL_TOOLS
        ]

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict) -> list[types.TextContent]:
        for tool in _CHANNEL_TOOLS:
            if tool["name"] == name:
                handler = tool["handler"]
                if asyncio.iscoroutinefunction(handler):
                    return await handler(pane, arguments)
                return handler(pane, arguments)
        raise ValueError(f"unknown tool: {name}")

    # Bridge: asyncio socket → anyio object stream of SessionMessage.
    read_send, read_recv = anyio.create_memory_object_stream[SessionMessage | Exception](0)
    write_send, write_recv = anyio.create_memory_object_stream[SessionMessage](0)

    async def socket_reader_loop() -> None:
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                try:
                    msg = JSONRPCMessage.model_validate_json(line.decode())
                    await read_send.send(SessionMessage(message=msg))
                except Exception as e:
                    await read_send.send(e)
        finally:
            await read_send.aclose()

    async def socket_writer_loop() -> None:
        try:
            async with write_recv:
                async for sm in write_recv:
                    data = sm.message.model_dump_json(
                        by_alias=True, exclude_none=True
                    ) + "\n"
                    writer.write(data.encode())
                    await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            pass

    init_options = InitializationOptions(
        server_name="periscope",
        server_version="0.1.0",
        capabilities=ServerCapabilities(
            experimental={"claude/channel": {}},
            tools=ToolsCapability(listChanged=False),
        ),
        instructions=CHANNEL_INSTRUCTIONS,
    )

    async with anyio.create_task_group() as tg:
        tg.start_soon(socket_reader_loop)
        tg.start_soon(socket_writer_loop)
        try:
            await server.run(read_recv, write_send, init_options)
        finally:
            tg.cancel_scope.cancel()
