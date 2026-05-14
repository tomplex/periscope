# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "fastapi",
#     "uvicorn[standard]",
#     "anthropic",
#     "python-dotenv",
#     "mcp==1.27.*",
# ]
# ///
"""Periscope — live tmux dashboard. Run with: uv run server.py"""

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# Load .env from the script's directory (existing env vars take precedence).
load_dotenv(Path(__file__).parent / ".env")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # prewarm_pr_cache, cached_scraped_usage, and kill_orphan_usage_sessions
    # are defined later; Python resolves the names at call-time, so forward
    # references are fine.
    # Reap any periscope-usage-* tmux sessions left behind by a prior crash
    # before the new scrape thread spawns a fresh one.
    kill_orphan_usage_sessions()
    # Kick off cache prewarms eagerly so the first /api/state poll already
    # has PR badges and the usage bars populated.
    threading.Thread(target=prewarm_pr_cache, daemon=True).start()
    threading.Thread(target=cached_scraped_usage, daemon=True).start()
    # MCP unix-socket listener: accepts connections from channel_shim.py
    # (one per Claude pane), runs an MCP Server per connection in-process.
    mcp_task = asyncio.create_task(_mcp_listener())
    try:
        yield
    finally:
        mcp_task.cancel()
        try:
            await mcp_task
        except (asyncio.CancelledError, Exception):
            pass
        try:
            os.unlink(MCP_SOCKET_PATH)
        except FileNotFoundError:
            pass


app = FastAPI(lifespan=lifespan)
STATIC = Path(__file__).parent / "static"

# --- Persistent state (state.json) ----------------------------------------
#
# Single JSON file mutated only by the server, under a threading.Lock, with
# atomic tempfile+rename writes. See
# docs/superpowers/specs/2026-05-13-persistent-config-layer-design.md.
#
# Lock primitive choice: threading.Lock (not asyncio.Lock). FastAPI runs
# sync `def` endpoints on anyio's threadpool, so two concurrent /api/state
# polls execute in parallel threads. asyncio.Lock only blocks coroutines,
# not threads — it would let sync handlers race past each other into the
# critical section. threading.Lock works correctly from both sync handlers
# and async ones (acquired synchronously; the file write is fast enough
# that briefly blocking the event loop is fine).

def _state_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "periscope" / "state.json"


_STATE_LOCK = threading.Lock()
_STATE_DEFAULTS: dict = {
    "version": 1,
    "ui": {},
    "windows": {},
    "commands": [],
}


def _load_state() -> dict:
    """Read state.json. On parse failure rename to .corrupt-<ts> and return
    defaults — the next save writes a fresh valid file, and the user can
    recover from the renamed file if they care."""
    path = _state_path()
    if not path.exists():
        return json.loads(json.dumps(_STATE_DEFAULTS))
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        # Missing keys default to their empty value — older files written by
        # earlier phases never carry `windows` or `commands`.
        for k, v in _STATE_DEFAULTS.items():
            data.setdefault(k, json.loads(json.dumps(v)))
        return data
    except (json.JSONDecodeError, OSError) as e:
        corrupt = path.with_name(f"{path.name}.corrupt-{int(time.time())}")
        try:
            path.rename(corrupt)
            print(f"periscope: state.json unreadable ({e}); renamed to {corrupt}")
        except OSError:
            pass
        return json.loads(json.dumps(_STATE_DEFAULTS))


def _write_state(data: dict) -> None:
    """Atomic write: tempfile + os.replace. Caller must hold _STATE_LOCK."""
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)


# In-memory cache — every endpoint reads from this, writes go through
# _write_state under the lock. Loaded once at startup.
_STATE: dict = _load_state()

_DEFAULT_COMMANDS = [
    {"label": "claude", "exec": "claude"},
    {"label": "shell", "exec": ""},
    {"label": "vim", "exec": "vim"},
]


def _seed_commands_if_empty() -> None:
    """If `commands` is empty (fresh install or pre-phase-4 state.json),
    seed the three legacy defaults so the new-window tile keeps working
    while phase 4 is in flight.

    Side effect: if a user deliberately drains commands to zero, the next
    server restart re-seeds the defaults. To keep zero commands, leave at
    least one no-op entry around. This tradeoff is deliberate — making
    "empty by accident" recoverable matters more than supporting a
    zero-commands configuration nobody asks for."""
    with _STATE_LOCK:
        if not _STATE["commands"]:
            _STATE["commands"] = [dict(c) for c in _DEFAULT_COMMANDS]
            _write_state(_STATE)


_seed_commands_if_empty()


def _channels_migration_v1() -> None:
    """One-shot: rewrite seeded `claude` exec entries to include the
    dev-channels flag so spawned Claudes get a channel server attached.

    Idempotent — gated by `channels_migration_v1_done`. Does not re-run on
    later restarts even if the user re-adds an `{exec: "claude"}` entry
    by hand. See docs/superpowers/specs/2026-05-14-channels-design.md
    §"Migration for existing users" for the policy rationale.
    """
    with _STATE_LOCK:
        if _STATE.get("channels_migration_v1_done"):
            return
        new_exec = (
            "claude --dangerously-load-development-channels server:periscope"
        )
        for cmd in _STATE.get("commands", []):
            if cmd.get("exec") == "claude":
                cmd["exec"] = new_exec
        _STATE["channels_migration_v1_done"] = True
        _write_state(_STATE)


_channels_migration_v1()


# --- Channels (in-process MCP server) -------------------------------------
#
# Periscope hosts an MCP server over a unix socket. Each Claude pane spawns
# a thin `channel_shim.py` subprocess (the documented stdio MCP entry point
# Claude requires), which connects to /tmp/periscope-mcp.sock and proxies
# bytes between Claude's stdio and our socket. All MCP logic — tool
# registration, capability declaration, notification emission — lives here.
#
# Locking: `_CHANNELS_LOCK` (threading.Lock) protects the reply log and
# session registry. Separate from `_STATE_LOCK` because channel state is
# touched from both sync request handlers (FastAPI threadpool) and async
# MCP handlers; threading.Lock works correctly from both whereas
# asyncio.Lock only blocks coroutines.

MCP_SOCKET_PATH = "/tmp/periscope-mcp.sock"

CHANNEL_INSTRUCTIONS = """\
Messages from periscope arrive as <channel source="periscope" ...> blocks
on each turn. They may include severity, kind, or other meta attributes.

Use the `reply` tool to surface status back to periscope's UI:
  - kind="need_human" when blocked and waiting on the user
  - kind="done" when the current task is complete
  - kind="info" (default) for everything else

The pane this channel is attached to is identified by $TMUX_PANE on
the server side; you don't need to address it explicitly.
"""

_CHANNELS_LOCK = threading.Lock()
# pane_id -> list[dict]   reply log (kind, severity, message, ts)
_CHANNEL_REPLIES: dict[str, list[dict]] = {}
# pane_id -> int          unread reply count, cleared when modal opens
_CHANNEL_UNREAD: dict[str, int] = {}
# pane_id -> MCP ServerSession reference. Presence is the "channel
# attached" indicator and the route for notification emission. Typed Any
# because the SDK's BaseSession-derived objects aren't reliably importable
# at module load (we lazy-load mcp); attribute access happens in
# emit_channel_event where the runtime shape is what matters.
_MCP_SESSIONS: dict[str, Any] = {}


def _channel_gc(known_pane_ids: set[str]) -> None:
    """Drop reply state for panes that no longer exist. Session registry is
    GC'd by the connection handler on disconnect, not here."""
    with _CHANNELS_LOCK:
        for d in (_CHANNEL_REPLIES, _CHANNEL_UNREAD):
            for stale in [k for k in d if k not in known_pane_ids]:
                d.pop(stale, None)


def _do_reply_tool(pane: str, arguments: dict):
    """Tool implementation for `reply` — appends to the per-pane reply log
    and bumps the unread count. Surfaces in periscope's UI on next poll."""
    from mcp import types

    message = arguments["message"]
    kind = arguments.get("kind", "info")
    severity = arguments.get("severity", "info")

    entry = {
        "message": message,
        "kind": kind,
        "severity": severity,
        "ts": int(time.time()),
    }
    with _CHANNELS_LOCK:
        _CHANNEL_REPLIES.setdefault(pane, []).append(entry)
        _CHANNEL_UNREAD[pane] = _CHANNEL_UNREAD.get(pane, 0) + 1

    body = {"ok": True, "kind": kind, "severity": severity}
    return [types.TextContent(type="text", text=json.dumps(body))]


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
    print(f"periscope: MCP listener bound to {MCP_SOCKET_PATH}", file=sys.stderr)
    try:
        async with server:
            await server.serve_forever()
    except asyncio.CancelledError:
        pass
    finally:
        server.close()
        try:
            await server.wait_closed()
        except Exception:
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
        print(f"periscope MCP: connection {pane or '<no-pane>'} failed: {e}", file=sys.stderr)
    finally:
        if pane:
            with _CHANNELS_LOCK:
                _MCP_SESSIONS.pop(pane, None)
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


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
            ),
        ]

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict) -> list[types.TextContent]:
        if name == "reply":
            return _do_reply_tool(pane, arguments)
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


# Server-tracked "last user-focused" per target.
# Tmux's window_activity bumps on any output (Claude streaming, build logs, dev
# servers, etc), which surprises users expecting "last accessed" semantics.
# We instead record when each window most recently became the active window in
# its session, plus any time the user acts on it via the dashboard.
_focused_at: dict[str, int] = {}
# `_acted_at` is a *user-action-only* recency stamp. Unlike `_focused_at` it
# does NOT bump on tmux active-window changes (which fire when Tom switches
# between sessions in his terminal, not when he engages a window via the
# periscope UI). The grid view's within-session card sort and the stream view
# both order by this. Bumped from the periscope-side handlers only:
#   - /ws/pane WS-connect (modal-open is the canonical "opened in periscope")
#   - /api/send, /api/paste-image, /api/rename
#   - /api/session/new, /api/window/new (creation through periscope)
# In-memory cache only; the persistent counterpart lives in
# _STATE["windows"][pid]["acked_at"] and is the source of truth for the
# done-vs-idle split (see /api/state). Reset on process restart; the
# persisted value carries forward.
_acted_at: dict[str, int] = {}
# When the parser observed a working/needs-input → idle transition, stamped
# `now`. Paired with `_acted_at` to split idle into "done" (Claude finished
# something the user hasn't acknowledged) vs "idle" (acknowledged or never
# busy). Persisted alongside acked_at under each pid's state.json entry.
_completed_at: dict[str, int] = {}
# Previous parsed state per pid, used to detect the working/needs-input →
# idle edge that drives `_completed_at`. Keyed by pid (not target) so a
# session rename doesn't lose the prior state and refire the transition.
_prev_state: dict[str, str] = {}
_active_per_session: dict[str, str] = {}

# Active resume operations, keyed by session_id. Each entry tracks where a
# `claude --resume <id>` is currently running so we can refuse concurrent
# resume requests (they'd interleave appends into the same JSONL).
_resuming: dict[str, dict] = {}
RESUME_EXPIRY_S = 30 * 60  # forget about a resume after 30 min idle

# Per-target spinner hysteresis. Tmux capture-pane occasionally catches Claude's
# TUI mid-redraw, dropping the spinner line for one cycle even when Claude is
# still working. We remember the last positive detection per target and treat
# it as sticky for SPINNER_GRACE_S so cards + modal subtitles don't flicker.
_spinner_last_seen: dict[str, tuple[str, float]] = {}
SPINNER_GRACE_S = 4.0

# Per-target "is this a Claude pane" stickiness. Detection is via STATUS_RE
# matching CC's bottom status line, but CC's interactive dialogs (e.g.
# AskUserQuestion) take over the screen and temporarily hide that line — we
# don't want the card to flip back to "shell" while the user is mid-prompt.
_claude_last_seen: dict[str, float] = {}
CLAUDE_STICKY_S = 120.0


def smooth_spinner(target: str, current: str | None) -> str | None:
    now = time.time()
    if current:
        _spinner_last_seen[target] = (current, now)
        return current
    last = _spinner_last_seen.get(target)
    if last and now - last[1] < SPINNER_GRACE_S:
        return last[0]
    _spinner_last_seen.pop(target, None)
    return None


def smooth_is_claude(target: str, current: bool) -> bool:
    now = time.time()
    if current:
        _claude_last_seen[target] = now
        return True
    last = _claude_last_seen.get(target, 0)
    if now - last < CLAUDE_STICKY_S:
        return True
    _claude_last_seen.pop(target, None)
    return False


def note_focus(target: str) -> None:
    _focused_at[target] = int(time.time())


def note_action(target: str) -> None:
    """Stamp a periscope-side user action. Separate from `note_focus`: the
    stream view orders by *only* actions the user took through periscope,
    not tmux activity. Callers that bump focus due to a user action should
    bump both; tmux-derived bumps go through `note_focus` alone."""
    _acted_at[target] = int(time.time())


def update_focus_from_windows(windows: list[dict]) -> None:
    """Walk the freshly-listed windows and stamp focus times when the active
    window for a session changes."""
    by_session_active: dict[str, str] = {}
    for w in windows:
        if w.get("active"):
            by_session_active[w["session"]] = f"{w['session']}:{w['index']}"
    for session, target in by_session_active.items():
        prev = _active_per_session.get(session)
        if prev != target or target not in _focused_at:
            note_focus(target)
            _active_per_session[session] = target

# Status line at the bottom of every Claude pane:
#   "  24% | ↑235k ↓479 | $17.04 | Opus 4.7 (1M context)"
STATUS_RE = re.compile(
    r"^\s*(?P<context>\d+)%\s*\|\s*↑\S+\s+↓\S+\s*\|\s*\$[\d.,]+\s*\|\s*(?P<model>.+?)\s*$"
)

# Branch / PR / CI used to come from a custom statusline rendered in the line
# above STATUS_RE. We now pull those from the pane's cwd directly (git +
# `gh pr list`), independent of any statusline customization.

# Active-op detection — two patterns covering the variations Claude Code's
# TUI shows for a running operation. Both are used with `.match()` so the
# spinner glyph must be at line start (after optional indent); this rejects
# prose embeds where a previous response or user message quotes the marker
# mid-sentence.
#
# An active marker is always `<non-ASCII glyph> <verb-phrase>` followed by
# either a trailing `…`, a `(timing/tokens)` parenthetical, or both.
# Glyph enumeration is intentionally avoided (Claude rotates through
# ✻ ✶ ✷ ✳ ✦ ⏺ … and adds new ones over time) — `[^\x00-\x7f]` matches any.
#
# SPINNER_RE handles the ellipsis form, single- OR multi-word phrase:
#   "✻ Envisioning…"
#   "✳ Wiring resolve_pids into endpoints…(910m 2 · ↓ 14.78 tokens · ...)"
# The phrase character class excludes `(` so it can't grow into parens —
# without that, tool-call headers like `⏺ Bash(cd /Users/tom/… --skip-glo…)`
# would match (the `…` inside the bash invocation isn't an active marker).
SPINNER_RE = re.compile(r"^\s*[^\x00-\x7f]\s+(?P<phrase>[^(\n…]+?)…")

# ACTIVE_OP_RE handles the parens form (no trailing `…`):
#   "● Bootstrapping packages (7m 29s · ↑ 22.1k tokens · thought for 2s)"
# The `↑/↓ Nk tokens` is the live uplink/downlink meter — present only while
# the op is running. Completion drops the arrow (`Done (5 tool uses · 25.5k
# tokens · 21s)`), so completed lines don't match. Distinguishable from
# STATUS_RE because the status line has both arrows on the same line, no
# `tokens` word, and no parens around the metering.
ACTIVE_OP_RE = re.compile(
    r"^\s*[^\x00-\x7f]\s+\S+.*\([^)]*[↑↓]\s*[\d.]+\w*\s+tokens[^)]*\)"
)

# Past-tense indicator: when Claude finishes a thinking phase, the spinner
# line transforms from `<glyph> Verbing…` into `<glyph> Verb-past for Xs`
# (e.g. `Cooked for 3m 42s`, `Brewed for 31s`, `Thought for 10s`). Same glyph
# rotation, different verb form.
#
# Used as a positional "stop searching" boundary: when iterating from the
# bottom, hitting this line before any active marker means Claude is idle
# and any active-marker-shape lines higher up are stale (from scrollback,
# or from the assistant's own response quoting the marker form verbatim in
# code blocks). The shape is specific enough — `<glyph> <Verb-past> for
# <digits><h|m|s>` — that prose embeds are very unlikely to match.
IDLE_INDICATOR_RE = re.compile(
    r"^\s*[^\x00-\x7f]\s+(?:\w+ed|Thought)\s+for\s+\d+\s*[hms]"
)

# Pull out a verb-shaped word for the card label (`envisioning…`,
# `planning…`). Falls back to the first word if there's no clean verb.
SPINNER_VERB_RE = re.compile(r"\b([A-Z]\w+(?:ing|ed))\b")

# Needs-input: the numbered-choice permission dialog. `❯ 1.` plus the
# "Esc to cancel" footer is Claude-Code-specific; either alone false-positives
# (shells use ❯ as a prompt; "Esc to cancel" appears in transient toasts).
# Claude's choice dialogs always render a single footer line that combines
# navigation hints with the cancel marker — e.g. one of:
#   "Enter to select · Esc to cancel"
#   "Enter to select · ↑/↓ to navigate · Esc to cancel"
#   "Submit · Esc to cancel"
# Matching the whole footer pattern on a single line is much more specific
# than scanning for the marker and a numbered option anywhere in the tail:
# prose responses (or shell output) that happen to mention both in different
# places will no longer false-positive. The dialog's options can sit far
# above the footer, so we don't need to find them — the footer is sufficient.
NEEDS_INPUT_FOOTER_RE = re.compile(
    r"(?:Enter\s+to\s+\w+|↑/↓|Submit\b).*Esc\s+to\s+cancel",
)

RECAP_RE = re.compile(
    r"※ recap:\s*(?P<text>.+?)(?=\n\s*[─❯]|\Z)", re.DOTALL
)
PROMPT_LINE_RE = re.compile(r"^❯\s*(?P<input>.*)$")


def tmux(*args: str) -> str:
    r = subprocess.run(
        ["tmux", *args], capture_output=True, text=True, timeout=5
    )
    return r.stdout


# --- Git + PR state derived from each pane's current working directory ----
#
# Independent of any custom Claude statusline. We ask tmux for the pane's
# current path, run git from there, and (if gh is installed) ask for the
# PR + CI rollup attached to that branch. Results are cached because both
# git status and gh queries cost real wall-clock time and the data changes
# slowly compared to our polling cadence.

_GIT_TTL = 15.0
_PR_TTL = 60.0
_git_cache: dict[str, tuple[float, dict | None]] = {}
_pr_cache: dict[tuple[str, str], tuple[float, dict | None]] = {}
_pr_fetching: set[tuple[str, str]] = set()
_pr_lock = threading.Lock()
_GH_AVAILABLE = shutil.which("gh") is not None


def _run(cmd: list[str], cwd: str | None = None, timeout: float = 3.0) -> tuple[int, str]:
    try:
        r = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout
        )
        return r.returncode, r.stdout.strip()
    except Exception:
        return -1, ""


def git_state_for(path: str) -> dict | None:
    """Return {branch, git} for the git repo at `path`, or None."""
    if not path or not os.path.isdir(path):
        return None
    code, _ = _run(["git", "-C", path, "rev-parse", "--git-dir"])
    if code != 0:
        return None
    _, branch = _run(["git", "-C", path, "rev-parse", "--abbrev-ref", "HEAD"])
    if not branch or branch == "HEAD":
        _, sha = _run(["git", "-C", path, "rev-parse", "--short", "HEAD"])
        branch = f"@{sha}" if sha else "?"
    # Compact diff stats vs HEAD (covers staged + unstaged together).
    _, diff = _run(["git", "-C", path, "diff", "HEAD", "--shortstat"])
    adds = int(re.search(r"(\d+) insertion", diff).group(1)) if "insertion" in diff else 0
    dels = int(re.search(r"(\d+) deletion", diff).group(1)) if "deletion" in diff else 0
    # Unpushed commits ahead of upstream.
    code, ahead_s = _run(["git", "-C", path, "rev-list", "--count", "@{u}..HEAD"])
    ahead = int(ahead_s) if code == 0 and ahead_s.isdigit() else 0
    state = "clean" if (adds == 0 and dels == 0) else f"+{adds} -{dels}"
    if ahead > 0:
        state += " *"
    return {"branch": branch, "git": state}


def cached_git_state(path: str) -> dict | None:
    if not path:
        return None
    now = time.time()
    cached = _git_cache.get(path)
    if cached and now - cached[0] < _GIT_TTL:
        return cached[1]
    data = git_state_for(path)
    _git_cache[path] = (now, data)
    return data


def pr_state_for(path: str, branch: str) -> dict | None:
    """Return PR metadata + CI rollup for the PR open against `branch` in
    repo at `path`. Modal sidebar surfaces title/draft/+/−/reviewers; the
    grid card uses {pr, ci} as before."""
    if not _GH_AVAILABLE or not path or not branch:
        return None
    code, out = _run(
        [
            "gh", "pr", "list",
            "--head", branch,
            "--state", "open",
            "--json",
            "number,title,isDraft,additions,deletions,reviewRequests,statusCheckRollup",
            "--limit", "1",
        ],
        cwd=path,
        timeout=8.0,
    )
    if code != 0 or not out:
        return None
    try:
        prs = json.loads(out)
    except Exception:
        return None
    if not prs:
        return None
    pr = prs[0]
    rollup = pr.get("statusCheckRollup") or []
    states = {(c.get("conclusion") or c.get("status") or "").upper() for c in rollup}
    states.discard("")
    ci = None
    if states & {"FAILURE", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED"}:
        ci = "✗"
    elif states & {"PENDING", "QUEUED", "IN_PROGRESS", "WAITING"}:
        ci = "⟳"
    elif states and states <= {"SUCCESS", "NEUTRAL", "SKIPPED"}:
        ci = "✓"
    # gh exposes requested reviewers as either users (with `login`) or teams
    # (with `name`) — take the login for users, name for teams, and trim to
    # the leading letters as the avatar text (2 chars max).
    reviewers: list[str] = []
    for r in pr.get("reviewRequests") or []:
        handle = r.get("login") or r.get("name") or ""
        if handle:
            reviewers.append(handle)
    return {
        "pr": pr.get("number"),
        "ci": ci,
        "pr_title": pr.get("title") or "",
        "pr_draft": bool(pr.get("isDraft")),
        "pr_additions": int(pr.get("additions") or 0),
        "pr_deletions": int(pr.get("deletions") or 0),
        "pr_reviewers": reviewers,
    }


def _fetch_pr_into_cache(path: str, branch: str) -> None:
    try:
        data = pr_state_for(path, branch)
    except Exception:
        data = None
    with _pr_lock:
        _pr_cache[(path, branch)] = (time.time(), data)
        _pr_fetching.discard((path, branch))


# --- Activity timeline (for modal sidebar) -------------------------------
#
# Per pane, surface a short timeline of recent events: commits on the repo
# in the last 24h, CI runs on the branch, and a single "opened in periscope"
# anchor sourced from _acted_at. Repo+branch events are cached by
# (cwd, branch) since they're the same for every window on the same branch;
# the per-target open event is layered in fresh on each call.

_ACTIVITY_TTL = 60.0
_activity_cache: dict[tuple[str, str], tuple[float, list[dict]]] = {}
_activity_fetching: set[tuple[str, str]] = set()
_activity_lock = threading.Lock()


def _gh_run_state(run: dict) -> str | None:
    """Map a gh run record to one of 'passed' / 'failed' / 'running', or
    None for runs we don't surface (skipped, neutral)."""
    s = (run.get("status") or "").upper()
    c = (run.get("conclusion") or "").upper()
    if c == "SUCCESS":
        return "passed"
    if c in ("FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED"):
        return "failed"
    if c in ("NEUTRAL", "SKIPPED"):
        return None
    if s in ("QUEUED", "IN_PROGRESS", "WAITING"):
        return "running"
    return None


def shared_activity_for(path: str, branch: str) -> list[dict]:
    """Repo/branch-scoped events: commits in last 24h + CI runs on branch."""
    events: list[dict] = []
    if not path or not os.path.isdir(path):
        return events
    code, _ = _run(["git", "-C", path, "rev-parse", "--git-dir"])
    if code != 0:
        return events
    # %ct = committer date as unix seconds; %s = subject. Tab-separated so
    # subjects with spaces don't confuse the split.
    code, out = _run(
        ["git", "-C", path, "log", "-10", "--since=24h", "--pretty=format:%ct%x09%s"],
        timeout=3.0,
    )
    if code == 0 and out:
        for line in out.split("\n"):
            tab = line.find("\t")
            if tab < 0:
                continue
            try:
                at = int(line[:tab])
            except ValueError:
                continue
            subj = line[tab + 1 :].strip()
            if subj:
                events.append({"kind": "commit", "at": at, "text": subj})

    if _GH_AVAILABLE and branch:
        code, out = _run(
            [
                "gh", "run", "list",
                "--branch", branch,
                "--limit", "5",
                "--json", "conclusion,status,createdAt,displayTitle,name",
            ],
            cwd=path,
            timeout=5.0,
        )
        if code == 0 and out:
            try:
                runs = json.loads(out)
            except Exception:
                runs = []
            from datetime import datetime
            for run in runs:
                state = _gh_run_state(run)
                if state is None:
                    continue
                created = run.get("createdAt") or ""
                try:
                    # GitHub timestamps are RFC3339 with a trailing Z.
                    at = int(
                        datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp()
                    )
                except Exception:
                    continue
                name = run.get("displayTitle") or run.get("name") or "workflow"
                events.append(
                    {"kind": "ci", "at": at, "text": name, "state": state}
                )
    return events


def _fetch_activity_into_cache(path: str, branch: str) -> None:
    try:
        events = shared_activity_for(path, branch)
    except Exception:
        events = []
    with _activity_lock:
        _activity_cache[(path, branch)] = (time.time(), events)
        _activity_fetching.discard((path, branch))


def cached_pane_activity(target: str, path: str, branch: str | None) -> list[dict]:
    """Return up to 8 timeline events for this pane, newest-first. Shared
    (repo+branch) events come from a stale-while-revalidate cache; the
    per-target 'open' event is layered in fresh from _acted_at."""
    events: list[dict] = []
    if path and branch:
        key = (path, branch)
        now = time.time()
        with _activity_lock:
            cached = _activity_cache.get(key)
            stale = cached is None or (now - cached[0] >= _ACTIVITY_TTL)
            if stale and key not in _activity_fetching:
                _activity_fetching.add(key)
                threading.Thread(
                    target=_fetch_activity_into_cache,
                    args=(path, branch),
                    daemon=True,
                ).start()
            shared = cached[1] if cached else []
        events.extend(shared)

    opened_at = _acted_at.get(target, 0)
    if opened_at:
        events.append(
            {"kind": "open", "at": opened_at, "text": "opened in periscope"}
        )

    events.sort(key=lambda e: e.get("at", 0), reverse=True)
    return events[:8]


# --- Claude Code plan usage (parsed from session JSONL files) -------------
#
# Claude Code logs every assistant message to ~/.claude/projects/<encoded-cwd>/
# <session-id>.jsonl. Each line is a JSON record; assistant lines carry a
# `message.usage` block with input_tokens, cache_creation_input_tokens,
# cache_read_input_tokens, and output_tokens. Summing across files in a
# rolling 5h window gives a real measurement of plan token usage, no API
# subscription / billing endpoint required.

_USAGE_TTL = 30.0
_usage_cache: tuple[float, dict] | None = None
_usage_lock = threading.Lock()
_CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"


def compute_claude_usage(window_hours: float = 5.0) -> dict:
    """Walk every recent session JSONL and sum token usage in the window."""
    if not _CLAUDE_PROJECTS.exists():
        return {"available": False}

    from datetime import datetime
    cutoff = time.time() - window_hours * 3600
    fresh = cache_w = cache_r = out = msgs = 0
    earliest_msg_ts: float | None = None

    for jsonl in _CLAUDE_PROJECTS.glob("*/*.jsonl"):
        try:
            if jsonl.stat().st_mtime < cutoff:
                continue
        except OSError:
            continue
        try:
            with jsonl.open(encoding="utf-8", errors="replace") as f:
                for line in f:
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    ts_str = rec.get("timestamp")
                    if not isinstance(ts_str, str):
                        continue
                    try:
                        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp()
                    except Exception:
                        continue
                    if ts < cutoff:
                        continue
                    usage = ((rec.get("message") or {}).get("usage")) or {}
                    if not usage:
                        continue
                    fresh += int(usage.get("input_tokens") or 0)
                    cache_w += int(usage.get("cache_creation_input_tokens") or 0)
                    cache_r += int(usage.get("cache_read_input_tokens") or 0)
                    out += int(usage.get("output_tokens") or 0)
                    msgs += 1
                    if earliest_msg_ts is None or ts < earliest_msg_ts:
                        earliest_msg_ts = ts
        except OSError:
            continue

    # The plan's 5h rolling reset is anchored at the *first* message of the
    # window, so the next reset is window_hours after the earliest in-window
    # message (not "now + 5h"). If we found nothing, the window is wide open.
    reset_at = int(earliest_msg_ts + window_hours * 3600) if earliest_msg_ts else None
    return {
        "available": True,
        "window_hours": window_hours,
        "messages": msgs,
        "input_tokens": fresh,
        "cache_creation_tokens": cache_w,
        "cache_read_tokens": cache_r,
        "output_tokens": out,
        "total_tokens": fresh + cache_w + cache_r + out,
        "reset_at": reset_at,
    }


def cached_claude_usage() -> dict:
    global _usage_cache
    now = time.time()
    with _usage_lock:
        if _usage_cache and now - _usage_cache[0] < _USAGE_TTL:
            return _usage_cache[1]
    data = compute_claude_usage()
    with _usage_lock:
        _usage_cache = (now, data)
    return data


# --- Authoritative plan usage scraped from `claude` TUI's /usage screen ---
#
# The JSONL aggregation above is a free local approximation. The real numbers
# (session %, week-all-models %, week-Sonnet %) only live server-side at
# Anthropic and only render inside `claude`'s interactive TUI. We spawn a
# headless tmux session, run claude, send /usage, capture the rendered screen,
# and parse out the three progress bars. Refreshed every 5 minutes in a
# background thread; that interval bounds the cost (a tiny haiku call per
# scrape) without making the bars feel stale.

USAGE_SCRAPE_REFRESH_S = 300.0
USAGE_SCRAPE_BOOT_TIMEOUT_S = 30.0
USAGE_SCRAPE_RENDER_TIMEOUT_S = 12.0
_scrape_cache: tuple[float, dict | None] = (0.0, None)
_scrape_in_flight = False
_scrape_lock = threading.Lock()


_USAGE_LABELS = {
    "Current session": "session",
    "Current week (all models)": "week_all",
    "Current week (Sonnet only)": "week_sonnet",
}


def parse_usage_screen(text: str) -> dict:
    """Walk the captured /usage screen line-by-line, picking out each meter's
    percentage and reset string. The TUI lays each meter out as three lines:
    label, bar+percent, "Resets ...". Three known labels."""
    lines = text.split("\n")
    meters: dict[str, dict] = {}
    for i, line in enumerate(lines):
        stripped = line.strip()
        key = _USAGE_LABELS.get(stripped)
        if not key or i + 2 >= len(lines):
            continue
        pct_match = re.search(r"(\d+)%\s+used", lines[i + 1])
        if not pct_match:
            continue
        resets = ""
        rs = re.search(r"Resets\s+(.+?)\s*$", lines[i + 2])
        if rs:
            resets = rs.group(1).strip()
        meters[key] = {
            "label": stripped,
            "percent": int(pct_match.group(1)),
            "resets": resets,
        }
    return {"available": bool(meters), "meters": meters}


# Hidden tmux sessions we spawn to drive `claude /usage`. Named with this
# prefix so we can filter them out of the dashboard and reap any leaked ones
# on startup (if the server died before its `finally: kill-session` ran).
USAGE_SESSION_PREFIX = "periscope-usage-"


def kill_orphan_usage_sessions() -> None:
    """Kill any leftover periscope-usage-* sessions from a prior server run.
    Idempotent; safe to call at startup before the scrape thread launches."""
    try:
        out = tmux("list-sessions", "-F", "#{session_name}")
    except Exception:
        return
    for name in out.strip().split("\n"):
        if name.startswith(USAGE_SESSION_PREFIX):
            subprocess.run(
                ["tmux", "kill-session", "-t", name],
                capture_output=True, check=False, timeout=5,
            )


def scrape_usage_via_tmux() -> dict | None:
    """Drive `claude` in a hidden tmux session to capture its /usage output."""
    sess = f"{USAGE_SESSION_PREFIX}{uuid.uuid4().hex[:8]}"
    empty_mcp = STATIC.parent / ".empty-mcp.json"
    if not empty_mcp.exists():
        empty_mcp.write_text('{"mcpServers":{}}')

    def cap() -> str:
        return subprocess.run(
            ["tmux", "capture-pane", "-t", sess, "-p"],
            capture_output=True, text=True, timeout=5,
        ).stdout

    try:
        subprocess.run(
            [
                "tmux", "new-session", "-d", "-s", sess, "-x", "200", "-y", "60",
                f"claude --strict-mcp-config {empty_mcp}",
            ],
            check=True, capture_output=True, timeout=5,
        )

        # Wait for the prompt chevron to indicate claude is ready for input.
        deadline = time.time() + USAGE_SCRAPE_BOOT_TIMEOUT_S
        booted = False
        while time.time() < deadline:
            time.sleep(0.5)
            if "❯" in cap():
                booted = True
                break
        if not booted:
            return None

        # Send /usage and wait for the bars to render.
        subprocess.run(
            ["tmux", "send-keys", "-t", sess, "/usage", "Enter"],
            check=False, capture_output=True, timeout=5,
        )
        deadline = time.time() + USAGE_SCRAPE_RENDER_TIMEOUT_S
        usage_text = ""
        while time.time() < deadline:
            time.sleep(0.5)
            content = cap()
            if "% used" in content and "Resets" in content:
                usage_text = content
                break
        if not usage_text:
            return None
        return parse_usage_screen(usage_text)
    except Exception:
        return None
    finally:
        subprocess.run(
            ["tmux", "kill-session", "-t", sess],
            capture_output=True, check=False,
        )


def _refresh_scrape_into_cache() -> None:
    global _scrape_cache, _scrape_in_flight
    try:
        result = scrape_usage_via_tmux()
    except Exception:
        result = None
    with _scrape_lock:
        if result:
            _scrape_cache = (time.time(), result)
        _scrape_in_flight = False


def cached_scraped_usage() -> dict | None:
    """Stale-while-revalidate: serves the last successful scrape immediately
    and kicks off a background refresh whenever the cache is older than
    USAGE_SCRAPE_REFRESH_S. First-ever call returns None; the dashboard's
    next poll will see the freshly-cached result."""
    global _scrape_in_flight
    now = time.time()
    with _scrape_lock:
        ts, data = _scrape_cache
        if now - ts < USAGE_SCRAPE_REFRESH_S:
            return data
        if not _scrape_in_flight:
            _scrape_in_flight = True
            threading.Thread(target=_refresh_scrape_into_cache, daemon=True).start()
        return data


def cached_pr_state(path: str, branch: str | None) -> dict | None:
    """Stale-while-revalidate. Returns cached data instantly; kicks off a
    refresh in a background thread if the cache is missing or expired. The
    next poll picks up the fresh value."""
    if not branch:
        return None
    key = (path, branch)
    now = time.time()
    with _pr_lock:
        cached = _pr_cache.get(key)
        if cached and now - cached[0] < _PR_TTL:
            return cached[1]
        if key not in _pr_fetching:
            _pr_fetching.add(key)
            threading.Thread(
                target=_fetch_pr_into_cache, args=(path, branch), daemon=True
            ).start()
        return cached[1] if cached else None


def list_windows() -> list[dict]:
    out = tmux(
        "list-windows",
        "-a",
        "-F",
        "#{session_name}\t#{window_index}\t#{window_name}\t#{window_active}\t#{pane_current_path}\t#{@periscope_id}\t#{pane_id}",
    )
    rows = []
    for line in out.strip().split("\n"):
        if not line:
            continue
        parts = line.split("\t")
        # pane_current_path is the active pane's cwd; safe even when missing.
        # @periscope_id is empty for unmanaged windows — `resolve_pids` mints
        # one on first sighting and stamps it onto the window.
        s, idx, name, active = parts[:4]
        # Hide the hidden `/usage`-scraper sessions from every caller; they're
        # our internal scaffolding, not user-visible tmux state.
        if s.startswith(USAGE_SESSION_PREFIX):
            continue
        cwd = parts[4] if len(parts) > 4 else ""
        pid_raw = parts[5] if len(parts) > 5 else ""
        # pane_id (%N) is tmux's stable handle for the active pane within the
        # current server lifetime — the addressing key for channel pushes.
        pane_id = parts[6] if len(parts) > 6 else ""
        rows.append(
            {
                "session": s,
                "index": int(idx),
                "name": name,
                "active": active == "1",
                "cwd": cwd,
                "pid_raw": pid_raw,
                "pane_id": pane_id,
            }
        )
    return rows


# --- Periscope window-ids (@periscope_id) ---------------------------------
#
# Every window we see acquires a periscope-assigned 8-char hex id, stamped
# onto the window as a tmux user option `@periscope_id`. The id survives
# rename / move / reorder. When the tmux server restarts (reboot,
# kill-server, OOM) and the option is gone, `_rebind_pid` recovers it from
# the (session, name) hint in `last_seen` within a 30-day window — see the
# rebind heuristic in the design spec.

_PID_TTL_S = 30 * 86400  # 30 days


def _mint_pid() -> str:
    return uuid.uuid4().hex[:8]


def _stamp_pid(target: str, pid: str) -> None:
    """Fire-and-forget set-option. If it fails (window gone, tmux racy),
    the next poll repeats the attempt. Uses the project's read-style
    `tmux()` helper because we don't need stderr-surfacing here."""
    tmux("set-option", "-w", "-t", target, "@periscope_id", pid)


def _rebind_pid(
    windows_block: dict,
    session: str,
    name: str,
    branch: str | None,
    cwd: str | None,
    taken_pids: set[str],
) -> str | None:
    """Look for an orphan id in state's `windows` block that matches the
    sighted window on (session, name) — or as a softer fallback,
    (branch, cwd). Returns the matched pid, or None if no candidate
    matches."""
    now = time.time()
    # Pass 1: strong match on (session, name).
    # Pass 2: secondary match on (branch, cwd) when both are set.
    for pass_n in (1, 2):
        for pid, entry in windows_block.items():
            if pid in taken_pids:
                continue
            ls = entry.get("last_seen") or {}
            ts = ls.get("ts")
            if not ts or now - ts > _PID_TTL_S:
                continue
            if pass_n == 1:
                if ls.get("session") == session and ls.get("name") == name:
                    return pid
            else:
                if not branch or not cwd:
                    continue
                if ls.get("branch") == branch and ls.get("cwd") == cwd:
                    return pid
    return None


def resolve_pids(windows: list[dict]) -> None:
    """Mutates `windows` in place, adding a `pid` field to every entry.

    For each window:
      1. If @periscope_id is non-empty, use it.
      2. Else attempt rebind from state.json's `windows` block.
      3. Else mint a fresh id.
    In cases 2 and 3, stamp the chosen id onto the tmux window (`set-option
    -w @periscope_id`) so subsequent polls take the fast path.

    Always updates the pid's `last_seen` block with (session, name, branch,
    cwd, now) — but only flags `dirty` when something other than the `ts`
    field changed, to avoid thrashing state.json on every 3s poll.

    Callers MUST have populated each window's `branch` (from
    cached_git_state) before calling, or rebind falls back to the
    session/name-only path.
    """
    if not windows:
        return
    now_ts = int(time.time())
    # Everything that reads/writes _STATE goes through _STATE_LOCK. We hold
    # the lock for the full resolve pass — it's cheap (kilobyte-scale JSON
    # write at the end) and gives us a single consistent snapshot of the
    # windows block to score rebinds against.
    with _STATE_LOCK:
        wblock = _STATE.setdefault("windows", {})
        taken: set[str] = set()
        dirty = False
        for w in windows:
            target = f"{w['session']}:{w['index']}"
            pid_raw = (w.get("pid_raw") or "").strip()
            pid: str | None = None
            if pid_raw and len(pid_raw) == 8 and all(c in "0123456789abcdef" for c in pid_raw):
                pid = pid_raw
            if pid is None:
                pid = _rebind_pid(
                    wblock,
                    session=w["session"],
                    name=w["name"],
                    branch=w.get("branch"),
                    cwd=w.get("cwd"),
                    taken_pids=taken,
                )
            if pid is None:
                pid = _mint_pid()
            # Stamp tmux only when we synthesized the id (mint or rebind).
            if pid != pid_raw:
                _stamp_pid(target, pid)
                dirty = True
            taken.add(pid)
            w["pid"] = pid
            # `pid_raw` was internal — strip it before emit.
            w.pop("pid_raw", None)
            # Refresh last_seen. Only flag dirty if something *other than*
            # `ts` changed — a pure ts bump every 3s would thrash state.json
            # to disk thousands of times an hour for no semantic gain.
            entry = wblock.setdefault(pid, {})
            prev = entry.get("last_seen") or {}
            new_seen = {
                "session": w["session"],
                "name": w["name"],
                "branch": w.get("branch"),
                "cwd": w.get("cwd"),
                "ts": now_ts,
            }
            identity_changed = (
                "last_seen" not in entry
                or any(prev.get(k) != new_seen[k] for k in ("session", "name", "branch", "cwd"))
            )
            entry["last_seen"] = new_seen
            if identity_changed:
                dirty = True
        # GC: drop windows entries that (a) carry no notes and no tags, AND
        # (b) weren't refreshed this pass, AND (c) have a last_seen older
        # than 30 days. Annotated entries are immune — losing one would
        # lose notes.
        cutoff = now_ts - _PID_TTL_S
        for pid in list(wblock.keys()):
            if pid in taken:
                continue
            entry = wblock[pid]
            if entry.get("notes") or entry.get("tags"):
                continue
            ts = (entry.get("last_seen") or {}).get("ts") or 0
            if ts < cutoff:
                del wblock[pid]
                dirty = True
        if dirty:
            _write_state(_STATE)


def _attach_git_then_resolve_pids(windows: list[dict]) -> None:
    """resolve_pids relies on `branch` for its secondary match. Populate it
    via cached_git_state before calling so the rebind heuristic has
    everything it needs."""
    for w in windows:
        git = cached_git_state(w.get("cwd", "")) or {}
        if "branch" in git:
            w["branch"] = git["branch"]
    resolve_pids(windows)

# SGR (Select Graphic Rendition) escape codes from capture-pane -e. Stripped
# for the bulk of parse_pane, but the prompt-line detection inspects the raw
# colored line to distinguish real user input from Claude's ghost-text
# suggestion — the two differ only in fg color.
_ANSI_SGR_RE = re.compile(r"\x1b\[[\d;]*m")
_FG_COLOR_RE = re.compile(r"\x1b\[38(?:;\d+)+m")


def capture(target: str, lines: int = 100) -> str:
    # -e preserves SGR escapes; parse_pane strips them for content parsing
    # but uses the raw prompt-line color info to filter ghost-text input.
    return tmux("capture-pane", "-t", target, "-p", "-e", "-S", f"-{lines}")


def deliver_input(target: str, text: str) -> None:
    """Pipe raw bytes into a pane via tmux load-buffer + paste-buffer.

    We use this rather than `send-keys -l` because tmux's argv parser treats a
    standalone `;` argument as a command separator — when xterm.js forwards a
    single semicolon keystroke as one WS message, send-keys silently drops it.
    Stdin avoids that entire parsing path.
    """
    buf = f"wd-in-{uuid.uuid4().hex[:8]}"
    subprocess.run(
        ["tmux", "load-buffer", "-b", buf, "-"],
        input=text, text=True, check=False, timeout=5,
    )
    subprocess.run(
        ["tmux", "paste-buffer", "-d", "-b", buf, "-t", target],
        check=False, timeout=5,
    )


def parse_pane(content: str) -> dict:
    # `content` from capture() includes SGR escape sequences (-e). Strip them
    # for the bulk of parsing; keep the raw rows for the prompt-line check
    # below, which needs the color info to distinguish real input from
    # Claude's ghost-text suggestion.
    raw_rows = content.rstrip("\n").split("\n")
    plain_rows = [_ANSI_SGR_RE.sub("", row) for row in raw_rows]
    lines = [p for p in plain_rows if p.strip() != ""]

    status = None
    # Claude Code's bottom status line ("X% | ↑n ↓n | $cost | model") signals
    # both "this is a Claude pane" and gives us the context+model fields.
    # Branch/PR/CI used to be parsed from a custom statusline rendered above
    # this — we now derive them from the pane's cwd via git/gh instead.
    tail = lines[-4:]
    for line in reversed(tail):
        m = STATUS_RE.match(line)
        if m:
            status = m.groupdict()
            break

    is_claude = status is not None

    # Iterate the bottom rows looking for a state signal. Whichever signal
    # is closest to the prompt wins:
    #   - IDLE_INDICATOR_RE: Claude finished thinking → spinner stays None.
    #     Stops the search so we never reach quoted/stale markers in
    #     scrollback or in the assistant's own response code blocks.
    #   - SPINNER_RE / ACTIVE_OP_RE: an active marker is below the past-tense
    #     line (or there is no past-tense line) → spinner gets the verb.
    #
    # Verb extraction always falls back to the string "working" if no clean
    # [A-Z]\w+(ing|ed) match — a phrase like "3 reasons why" used to surface
    # "3…" via a "first word" fallback, which was uninformative noise.
    spinner = None
    for line in reversed(lines[-15:]):
        if IDLE_INDICATOR_RE.match(line):
            break
        m = SPINNER_RE.match(line)
        if m:
            phrase = m.group("phrase").strip()
            vm = SPINNER_VERB_RE.search(phrase)
            spinner = vm.group(1) if vm else "working"
            break
        if ACTIVE_OP_RE.match(line):
            vm = SPINNER_VERB_RE.search(line)
            spinner = vm.group(1) if vm else "working"
            break

    # Needs-input: look for the dialog's footer line in the last few lines.
    # The footer is always a single line at the bottom of the pane when a
    # dialog is active, so restricting the search to a tight tail avoids
    # matching prose that happens to discuss dialog UI.
    needs_input = any(
        NEEDS_INPUT_FOOTER_RE.search(line) for line in lines[-5:]
    )
    # The dialog footer is Claude-specific UI; if we see it the pane IS
    # Claude even if STATUS_RE missed (the dialog occupies the bottom rows
    # where the status line normally lives).
    if needs_input:
        is_claude = True

    # Pending input: ❯ followed by some text the user has typed but not
    # submitted. Skip when needs_input is true — `❯ 1.` is the dialog's
    # selection line, not user typing.
    #
    # Ghost-text filter: Claude Code shows a greyed-out suggestion in the
    # input slot when nothing's been typed. The suggestion looks like real
    # input in plain text, but in the colored row it shares the prompt
    # prefix's fg color (single distinct fg code on the line). Real typed
    # input switches to a different fg color (≥2 distinct fg codes). When
    # the row carries no SGR escapes at all (e.g. test fixtures), we have
    # no color info and trust the visible text.
    pending_input = None
    if not needs_input:
        for raw, plain in zip(reversed(raw_rows), reversed(plain_rows)):
            if not plain.strip():
                continue
            m = PROMPT_LINE_RE.match(plain.strip())
            if not m:
                continue
            input_text = m.group("input").strip()
            if not input_text:
                break
            if "\x1b[" in raw:
                fg_codes = set(_FG_COLOR_RE.findall(raw))
                if len(fg_codes) >= 2:
                    pending_input = input_text
                # else: ghost text — leave pending_input as None
            else:
                pending_input = input_text
            break

    # Most recent recap block
    full = "\n".join(lines)
    recap = None
    matches = list(RECAP_RE.finditer(full))
    if matches:
        recap = matches[-1].group("text").strip()
        recap = re.sub(r"\s+", " ", recap)[:400]

    # Last meaningful line for shell panes / card snippet fallback. Walk up
    # from the bottom skipping TUI chrome — what's left is the closest
    # "real" content (recent prose, subtask line, or past-tense indicator).
    #   ─ / ❯           separator and empty prompt
    #   ⏵               `⏵⏵ auto mode on (shift+tab to cycle)` footer hint
    #   STATUS_RE       `XX% | ↑Nk ↓N | $cost | model` status line
    #   title bar       `<repo> | <branch> | <diff> | github.com/<path>…`
    #                   (Claude Code renders this inline above the convo)
    #   SPINNER/ACTIVE  active spinner line — the verb is already shown as
    #                   the card's state label, so re-rendering the full
    #                   spinner line as the snippet would be redundant.
    last_line = ""
    for line in reversed(lines):
        s = line.strip()
        if not s:
            continue
        if s.startswith(("─", "❯", "⏵")):
            continue
        if STATUS_RE.match(line):
            continue
        if "github.com/" in line and line.count("|") >= 3:
            continue
        if SPINNER_RE.match(line) or ACTIVE_OP_RE.match(line):
            continue
        last_line = s[:200]
        break

    # State priority: needs-input wins over working (a spinner glyph can
    # linger in scrollback above the dialog), working wins over idle.
    # `idle` is the parse-level neutral state — /api/state may refine it to
    # `done` when there's an unacknowledged completion stamp.
    if not is_claude:
        state = "shell"
    elif needs_input:
        state = "needs-input"
    elif spinner:
        state = "working"
    else:
        state = "idle"

    return {
        "is_claude": is_claude,
        "state": state,
        "spinner": spinner,
        "needs_input": needs_input,
        "pending_input": pending_input,
        "recap": recap,
        "last_line": last_line,
        "context_pct": int(status["context"]) if status else None,
        "model": status["model"].strip() if status else None,
    }


@app.get("/api/state")
def state():
    windows = list_windows()
    update_focus_from_windows(windows)
    _attach_git_then_resolve_pids(windows)
    now_ts = int(time.time())
    # Accumulate (pid, completed_at, acked_at) tuples for stamp persistence
    # at the end of the loop. Single lock acquisition + single write covers
    # every pane in this poll.
    stamp_updates: list[tuple[str, int, int]] = []
    result = []
    for w in windows:
        target = f"{w['session']}:{w['index']}"
        pid = w.get("pid") or ""
        try:
            content = capture(target)
            parsed = parse_pane(content)
        except Exception as e:
            parsed = {"error": str(e), "state": "error", "is_claude": False}
        # Hysteresis: smooth out per-poll detection gaps so cards / modal
        # subtitles don't flicker between "thinking" and idle.
        parsed["spinner"] = smooth_spinner(target, parsed.get("spinner"))
        # is_claude stickiness: dialogs hide the bottom status line; without
        # this the card would flip to "shell" mid-prompt and lose its state
        # coloring + needs-input classification.
        parsed["is_claude"] = smooth_is_claude(target, parsed.get("is_claude", False))
        if not parsed["is_claude"]:
            parsed["state"] = "shell"
        # Spinner hysteresis can promote a momentarily-blank parse back to
        # "working" — but only if we're not already in a louder state.
        # needs-input must never be downgraded back to working: the dialog
        # commonly lingers below a stale spinner glyph in scrollback.
        if (
            parsed.get("is_claude")
            and parsed.get("spinner")
            and parsed.get("state") not in ("working", "needs-input")
        ):
            parsed["state"] = "working"

        # done-vs-idle refinement. Uses per-pid stamps (persisted via
        # state.json) so a server restart preserves the "Claude finished
        # something you haven't looked at" signal across the gap.
        #
        # Edge detection: if the previous parse was busy and now we're idle,
        # stamp `_completed_at` so the refinement below promotes us to
        # "done" until the user acknowledges via a periscope action.
        # Targets without a pid (rare — only if pid resolution failed)
        # skip persistence; the in-memory value still works for the
        # current process lifetime.
        prev = _prev_state.get(pid) if pid else None
        cur = parsed.get("state")
        if pid and prev in ("working", "needs-input") and cur == "idle":
            _completed_at[target] = now_ts
        if pid:
            _prev_state[pid] = cur

        # Pull persisted stamps; in-memory may be ahead (just bumped) or
        # behind (fresh process, never observed a transition this run).
        wblock = _STATE.get("windows", {})
        persisted = wblock.get(pid, {}) if pid else {}
        completed = max(_completed_at.get(target, 0), int(persisted.get("completed_at") or 0))
        acked = max(_acted_at.get(target, 0), int(persisted.get("acked_at") or 0))

        if cur == "idle" and parsed.get("is_claude") and completed > acked:
            parsed["state"] = "done"

        # Schedule a state.json write if either stamp is newer than what's
        # on disk. The write itself runs once, under the lock, after the
        # loop.
        if pid and (
            completed > int(persisted.get("completed_at") or 0)
            or acked > int(persisted.get("acked_at") or 0)
        ):
            stamp_updates.append((pid, completed, acked))

        git = cached_git_state(w.get("cwd", "")) or {}
        pr = cached_pr_state(w.get("cwd", ""), git.get("branch")) or {}

        # Channel state (added by 2026-05-14-channels-design.md).
        pane_id = w.get("pane_id") or ""
        with _CHANNELS_LOCK:
            channel_attached = pane_id in _MCP_SESSIONS if pane_id else False
            channel_unread = _CHANNEL_UNREAD.get(pane_id, 0) if pane_id else 0
            channel_replies = list(_CHANNEL_REPLIES.get(pane_id, [])) if pane_id else []

        result.append(
            {
                **w, **parsed, **git, **pr,
                "target": target,
                "focused_at": _focused_at.get(target, 0),
                # 0 means "never engaged through periscope" — stream view
                # filters these out; grid view sorts cards within each session
                # by acted_at desc (most-recently-opened leftmost).
                "acted_at": acked,
                "completed_at": completed,
                "channel_attached": channel_attached,
                "channel_unread": channel_unread,
                "channel_replies": channel_replies,
            }
        )
    _channel_gc({w["pane_id"] for w in windows if w.get("pane_id")})
    if stamp_updates:
        with _STATE_LOCK:
            wblock = _STATE.setdefault("windows", {})
            dirty = False
            for pid, completed, acked in stamp_updates:
                entry = wblock.setdefault(pid, {})
                if int(entry.get("completed_at") or 0) != completed:
                    entry["completed_at"] = completed
                    dirty = True
                if int(entry.get("acked_at") or 0) != acked:
                    entry["acked_at"] = acked
                    dirty = True
            if dirty:
                _write_state(_STATE)
    # Garbage-collect stale resumes: targets that are no longer in tmux's
    # list-windows output, or older than 30 min.
    now = int(time.time())
    live_targets = {f"{w['session']}:{w['index']}" for w in windows}
    for sid in list(_resuming):
        entry = _resuming[sid]
        if entry["target"] not in live_targets or now - entry["started_at"] > RESUME_EXPIRY_S:
            del _resuming[sid]
    return {
        "windows": result,
        "ts": int(time.time()),
        "usage": cached_claude_usage(),
        "usage_scrape": cached_scraped_usage(),
    }



# --- /api/prefs endpoints -------------------------------------------------

@app.get("/api/prefs")
def get_prefs():
    """Full state blob, for client boot. Reads from the in-memory cache —
    every mutation refreshes the cache atomically, so this is safe to call
    without the lock."""
    return _STATE

class UIPatch(BaseModel):
    session_order: list[str] | None = None
    collapsed_sessions: list[str] | None = None
    view: str | None = None  # "grid" or "stream"


@app.patch("/api/prefs/ui")
async def patch_prefs_ui(body: UIPatch):
    """Merge partial UI prefs. Only fields present in the body get written."""
    patch = body.model_dump(exclude_none=True)
    # `view` is validated against a fixed enum to keep junk out of the file.
    if "view" in patch and patch["view"] not in ("grid", "stream"):
        return {"ok": False, "error": f"invalid view: {patch['view']!r}"}
    with _STATE_LOCK:
        _STATE["ui"].update(patch)
        _write_state(_STATE)
    return {"ok": True, "ui": _STATE["ui"]}


class WindowAnnotation(BaseModel):
    notes: str | None = None
    tags: list[str] | None = None


@app.put("/api/prefs/windows/{pid}")
async def put_window_annotation(pid: str, body: WindowAnnotation):
    """Set/replace the annotation fields on a window. `last_seen` is left
    intact — only notes/tags are managed via this endpoint."""
    if not pid or not pid.isalnum():
        return {"ok": False, "error": "invalid pid"}
    patch = body.model_dump(exclude_none=True)
    # Coerce tags to a trimmed unique list, preserving order.
    if "tags" in patch:
        seen: set[str] = set()
        clean: list[str] = []
        for t in patch["tags"]:
            t = (t or "").strip()
            if t and t not in seen:
                seen.add(t)
                clean.append(t)
        patch["tags"] = clean
    with _STATE_LOCK:
        entry = _STATE["windows"].setdefault(pid, {})
        for k in ("notes", "tags"):
            if k in patch:
                entry[k] = patch[k]
        # Drop empty notes / empty tag list to keep the file tidy.
        if entry.get("notes") == "":
            entry.pop("notes", None)
        if entry.get("tags") == []:
            entry.pop("tags", None)
        _write_state(_STATE)
    return {"ok": True, "pid": pid, "annotation": {
        "notes": entry.get("notes"),
        "tags": entry.get("tags") or [],
    }}


@app.delete("/api/prefs/windows/{pid}")
async def delete_window_annotation(pid: str):
    """Remove notes + tags. last_seen is preserved (it's the rebind hint)."""
    if not pid or not pid.isalnum():
        return {"ok": False, "error": "invalid pid"}
    with _STATE_LOCK:
        entry = _STATE["windows"].get(pid)
        if entry:
            entry.pop("notes", None)
            entry.pop("tags", None)
            _write_state(_STATE)
    return {"ok": True, "pid": pid}

class Command(BaseModel):
    label: str
    exec: str = ""


class CommandPatch(BaseModel):
    """For PUT: both fields are optional. Sending only `label` renames
    without clobbering `exec`; sending only `exec` updates the command
    without renaming. The frontend always sends both, but keeping them
    optional protects against curl-from-shell footguns."""
    label: str | None = None
    exec: str | None = None


@app.post("/api/prefs/commands")
async def add_command(body: Command):
    label = body.label.strip()
    if not label:
        return {"ok": False, "error": "empty label"}
    with _STATE_LOCK:
        if any(c["label"] == label for c in _STATE["commands"]):
            return {"ok": False, "error": f"duplicate label: {label!r}"}
        _STATE["commands"].append({"label": label, "exec": body.exec or ""})
        _write_state(_STATE)
    return {"ok": True, "commands": _STATE["commands"]}


@app.put("/api/prefs/commands/{label}")
async def update_command(label: str, body: CommandPatch):
    with _STATE_LOCK:
        for c in _STATE["commands"]:
            if c["label"] == label:
                new_label = (body.label or label).strip()
                if not new_label:
                    return {"ok": False, "error": "empty label"}
                if new_label != label and any(
                    other["label"] == new_label for other in _STATE["commands"] if other is not c
                ):
                    return {"ok": False, "error": f"duplicate label: {new_label!r}"}
                c["label"] = new_label
                if body.exec is not None:
                    c["exec"] = body.exec
                _write_state(_STATE)
                return {"ok": True, "commands": _STATE["commands"]}
    return {"ok": False, "error": f"unknown label: {label!r}"}


@app.delete("/api/prefs/commands/{label}")
async def delete_command(label: str):
    with _STATE_LOCK:
        before = len(_STATE["commands"])
        _STATE["commands"] = [c for c in _STATE["commands"] if c["label"] != label]
        if len(_STATE["commands"]) == before:
            return {"ok": False, "error": f"unknown label: {label!r}"}
        _write_state(_STATE)
    return {"ok": True, "commands": _STATE["commands"]}


class CommandsReorder(BaseModel):
    labels: list[str]


@app.put("/api/prefs/commands")
async def reorder_commands(body: CommandsReorder):
    """Reorder the commands list to match `labels`. Unknown labels are
    ignored; missing labels stay in place at the end."""
    with _STATE_LOCK:
        by_label = {c["label"]: c for c in _STATE["commands"]}
        ordered = [by_label[l] for l in body.labels if l in by_label]
        leftover = [c for c in _STATE["commands"] if c["label"] not in {l for l in body.labels if l in by_label}]
        _STATE["commands"] = ordered + leftover
        _write_state(_STATE)
    return {"ok": True, "commands": _STATE["commands"]}


@app.get("/api/pane")
def pane(session: str, index: int, lines: int = 200):
    """Capture last N lines of a pane plus parsed status fields. Session/index
    passed as query params so slash-bearing session names (e.g. 'tc/foo/bar')
    don't conflict with path routing."""
    target = f"{session}:{index}"
    # -e preserves ANSI escape sequences for the modal viewer. parse_pane
    # handles the colored content itself — it strips for content parsing
    # but uses the raw prompt-line color info to filter ghost-text input.
    content = tmux("capture-pane", "-t", target, "-p", "-e", "-S", f"-{lines}")
    parsed = parse_pane(content)
    parsed["spinner"] = smooth_spinner(target, parsed.get("spinner"))
    parsed["is_claude"] = smooth_is_claude(target, parsed.get("is_claude", False))
    if not parsed["is_claude"]:
        parsed["state"] = "shell"
    if parsed.get("is_claude") and parsed.get("spinner") and parsed.get("state") not in ("working", "needs-input"):
        parsed["state"] = "working"
    try:
        meta = tmux(
            "display-message", "-t", target, "-p",
            "#{window_name}\t#{pane_current_path}",
        ).strip()
        window_name, _, cwd = meta.partition("\t")
    except Exception:
        window_name, cwd = "", ""
    git = cached_git_state(cwd) or {}
    one = [{"session": session, "index": index, "name": window_name, "active": False, "cwd": cwd, "pid_raw": ""}]
    _attach_git_then_resolve_pids(one)
    pid = one[0].get("pid")
    pr = cached_pr_state(cwd, git.get("branch")) or {}
    activity = cached_pane_activity(target, cwd, git.get("branch"))
    # Shorten $HOME → ~ for display. Done server-side because the browser
    # doesn't know the user's home dir.
    home = os.path.expanduser("~")
    cwd_display = cwd
    if cwd and (cwd == home or cwd.startswith(home + "/")):
        cwd_display = "~" + cwd[len(home):]
    # Channel data: look up pane_id via list_windows since the route doesn't
    # take it directly. Single iteration is fine — list_windows is cached at
    # tmux's speed (sub-ms) and we already pay it on every state() poll.
    pane_id = ""
    for w in list_windows():
        if w["session"] == session and w["index"] == index:
            pane_id = w.get("pane_id", "")
            break
    with _CHANNELS_LOCK:
        channel_attached = pane_id in _MCP_SESSIONS if pane_id else False
        channel_unread = _CHANNEL_UNREAD.get(pane_id, 0) if pane_id else 0
        channel_replies = list(_CHANNEL_REPLIES.get(pane_id, [])) if pane_id else []
    return {
        "content": content,
        "target": target,
        "name": window_name,
        "cwd": cwd_display,
        "session": session,
        "pid": pid,
        "pane_id": pane_id,
        "activity": activity,
        "channel_attached": channel_attached,
        "channel_unread": channel_unread,
        "channel_replies": channel_replies,
        **parsed,
        **git,
        **pr,
    }


class SendBody(BaseModel):
    keys: list[str] = []
    paste: str | None = None  # bracketed-pasted into the pane before `keys`


class SendBulkBody(BaseModel):
    targets: list[str]            # ["session:index", ...]
    keys: list[str] = []
    paste: str | None = None


class RenameBody(BaseModel):
    name: str


class NewSessionBody(BaseModel):
    name: str
    cwd: str | None = None


def _tmux_mutate(*args: str) -> tuple[bool, str]:
    """Run a tmux command for its side effects. Surfaces stderr on failure
    instead of swallowing it like the read-only `tmux()` helper does."""
    r = subprocess.run(
        ["tmux", *args], capture_output=True, text=True, timeout=5
    )
    if r.returncode != 0:
        return False, (r.stderr.strip() or r.stdout.strip() or "tmux failed")
    return True, r.stdout.strip()


@app.post("/api/rename")
def rename(session: str, index: int, body: RenameBody):
    target = f"{session}:{index}"
    name = body.name.strip()
    if not name:
        return {"ok": False, "error": "empty name"}
    tmux("rename-window", "-t", target, name)
    note_action(target)
    return {"ok": True, "target": target, "name": name}


@app.post("/api/session/new")
def session_new(body: NewSessionBody):
    name = body.name.strip()
    if not name:
        return {"ok": False, "error": "empty name"}
    cwd = body.cwd or os.path.expanduser("~")
    ok, msg = _tmux_mutate("new-session", "-d", "-s", name, "-c", cwd)
    if not ok:
        return {"ok": False, "error": msg}
    # Stamp focus so the new session sorts to the top on next poll. Stamping
    # `acted_at` too: creating a session through periscope is a user action,
    # so the new window earns a slot in the stream view.
    note_focus(f"{name}:0")
    note_action(f"{name}:0")
    return {"ok": True, "session": name}


@app.delete("/api/session")
def session_delete(session: str):
    ok, msg = _tmux_mutate("kill-session", "-t", session)
    if not ok:
        return {"ok": False, "error": msg}
    prefix = f"{session}:"
    for t in [t for t in _focused_at if t.startswith(prefix)]:
        _focused_at.pop(t, None)
    for t in [t for t in _acted_at if t.startswith(prefix)]:
        _acted_at.pop(t, None)
    _active_per_session.pop(session, None)
    return {"ok": True, "session": session}


@app.post("/api/window/new")
def window_new(session: str, exec_cmd: str = Query("", alias="exec"), mode: str = "shell", resume_id: str | None = None):
    """Spawn a window in `session`. `exec` param sends a command to the new window;
    legacy `mode` maps to `exec` for backwards-compat. `mode=resume` runs
    `claude --resume <resume_id>` in the original session's project dir.
    cwd is inherited from the session's active pane — without `-c`,
    tmux would use the periscope server's cwd, which is never what you want."""
    # Legacy `mode` → exec_cmd mapping for callers still on the old contract.
    # `mode=resume` synthesizes the actual command from resume_id; the
    # _resuming registration happens after the spawn (below) so the existing-
    # session fall-through path doesn't lose either side-effect.
    if not exec_cmd:
        if mode in ("claude", "vim", "shell"):
            exec_cmd = {"claude": "claude", "vim": "vim", "shell": ""}.get(mode, "")
        elif mode == "resume" and resume_id:
            exec_cmd = f"claude --resume {resume_id}"
    
    # mode=resume looks up the original project_path and runs claude --resume
    # there; we resolve cwd up front so the rest of the spawn path is shared.
    resume_sess = None
    if mode == "resume":
        if not resume_id:
            return {"ok": False, "error": "resume_id required for mode=resume"}
        from history.search import get_session
        resume_sess = get_session(resume_id)
        if resume_sess is None:
            return {"ok": False, "error": f"unknown session_id: {resume_id}"}
        # Liveness guard: refuse if the jsonl was written to in the last 60s
        # (the session may be currently active in another window/process,
        # and two concurrent appenders would interleave into the same JSONL).
        if resume_sess["jsonl_path"] and os.path.isfile(resume_sess["jsonl_path"]):
            mtime_age = time.time() - os.path.getmtime(resume_sess["jsonl_path"])
            if mtime_age < 60:
                return {"ok": False, "error": "session looks live; wait a minute or pick another"}
        # Already resumed elsewhere in this periscope process?
        if resume_id in _resuming:
            existing = _resuming[resume_id]
            return {"ok": False,
                    "error": f"already resumed in {existing['target']}",
                    "existing_target": existing["target"]}
        cwd = resume_sess["project_path"] or os.path.expanduser("~")
        if not os.path.isdir(cwd):
            cwd = os.path.expanduser("~")
        # Resume convention: the frontend always sends `session=resumes`
        # (or any sentinel). If that session doesn't exist yet, create it
        # on first use so the resume button doesn't bounce. Side-effect-
        # only when actually missing; existing sessions pass through.
        code, _ = _run(["tmux", "has-session", "-t", session])
        if code != 0:
            ok, msg = _tmux_mutate("new-session", "-d", "-s", session, "-c", cwd)
            if not ok:
                return {"ok": False, "error": f"failed to create session '{session}': {msg}"}
            # tmux new-session creates window 0; turn that into our resume
            # window directly rather than spawning a second one.
            target = f"{session}:0"
            time.sleep(0.1)
            tmux("send-keys", "-t", target, f"claude --resume {resume_id}", "Enter")
            _resuming[resume_id] = {"target": target, "started_at": int(time.time())}
            note_focus(target)
            note_action(target)
            return {
                "ok": True,
                "session": session,
                "index": 0,
                "target": target,
                "mode": mode,
                "resumed_session_id": resume_id,
            }
    else:
        cwd = tmux(
            "display-message", "-t", f"{session}:", "-p", "#{pane_current_path}",
        ).strip() or os.path.expanduser("~")
    ok, msg = _tmux_mutate(
        "new-window", "-t", f"{session}:", "-c", cwd,
        "-P", "-F", "#{window_index}",
    )
    if not ok:
        return {"ok": False, "error": msg}
    try:
        index = int(msg)
    except ValueError:
        return {"ok": False, "error": f"tmux returned unexpected index: {msg!r}"}
    target = f"{session}:{index}"
    
    # Execute the command if provided via exec or legacy mode mapping.
    cmd = exec_cmd.strip()
    if cmd:
        # Let the shell finish its rc before the command line arrives, so
        # the command runs as a real prompt entry rather than mid-rc
        # echoed text. (See CLAUDE.md "Key invariants" note 5.)
        time.sleep(0.1)
        tmux("send-keys", "-t", target, cmd, "Enter")

    # Resume bookkeeping for the fall-through path (existing `resumes`
    # session). The new-session branch above already set this inline before
    # its early return.
    if mode == "resume" and resume_id and resume_id not in _resuming:
        _resuming[resume_id] = {"target": target, "started_at": int(time.time())}

    note_focus(target)
    note_action(target)
    result = {"ok": True, "session": session, "index": index, "target": target, "mode": mode, "exec": cmd}
    if mode == "resume":
        result["resumed_session_id"] = resume_id
    return result


@app.delete("/api/window")
def window_delete(session: str, index: int):
    target = f"{session}:{index}"
    ok, msg = _tmux_mutate("kill-window", "-t", target)
    if not ok:
        return {"ok": False, "error": msg}
    _focused_at.pop(target, None)
    _acted_at.pop(target, None)
    return {"ok": True, "target": target}


# --- history index API ----------------------------------------------------


@app.get("/api/history/search")
def history_search(
    q: str,
    project: str | None = None,
    branch: str | None = None,
    since: int | None = None,
    until: int | None = None,
    include_trivial: bool = False,
    rerank: bool = False,
    limit: int = 50,
):
    """FTS5-ranked search across the history index. Empty q falls back to
    `recent()` (newest-first) so the UI can populate before the user types."""
    import history
    started = time.time()
    if q and q.strip():
        results = history.search(
            q,
            project=project,
            branch=branch,
            since=since,
            until=until,
            include_trivial=include_trivial,
            rerank=rerank,
            limit=limit,
        )
    else:
        # branch / until aren't part of recent's filter set (the UI doesn't
        # surface them either); plumb through what the route accepts.
        results = history.recent(
            project=project,
            since=since,
            include_trivial=include_trivial,
            limit=limit,
        )
    # is_resuming belongs to periscope's in-process _resuming dict, not to
    # the history index — apply it here so the frontend can render guards.
    for r in results:
        r["is_resuming"] = r["session_id"] in _resuming
    return {
        "query": q,
        "rerank_used": rerank,
        "results": results,
        "took_ms": int((time.time() - started) * 1000),
    }


@app.get("/api/history/session/{session_id}")
def history_session(session_id: str):
    """Full session record + parsed conversation messages.

    Returns 404 if the session_id is not in the index. The `jsonl_missing`
    field is true if the row exists but the underlying JSONL has been
    deleted (search will hide it until `history clean` removes the row)."""
    from fastapi.responses import JSONResponse
    from history.search import get_session
    data = get_session(session_id)
    if data is None:
        return JSONResponse({"ok": False, "error": "unknown session_id"}, status_code=404)
    data["is_resuming"] = session_id in _resuming
    return data


@app.get("/api/history/stats")
def history_stats():
    """Index summary for the /history route header + footer."""
    import history
    return history.stats()


@app.get("/history")
def history_page():
    """Serve the /history single-page route. The StaticFiles mount below
    can't resolve /history without a trailing slash + an index.html in a
    subdir; this explicit route keeps the URL clean."""
    from fastapi.responses import FileResponse
    return FileResponse(STATIC / "history.html")


# --- auto-rename via the Anthropic SDK ------------------------------------

_anthropic_client = None


def get_anthropic():
    global _anthropic_client
    if _anthropic_client is None:
        import os
        from anthropic import Anthropic

        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set. Export it in your shell "
                "(e.g. add to ~/.zshenv) before starting the dashboard."
            )
        _anthropic_client = Anthropic()
    return _anthropic_client


def claude_complete(prompt: str, model: str = "claude-haiku-4-5") -> str:
    """Single-shot completion via the Anthropic SDK. Much faster than the
    claude CLI (no MCP / hooks / settings load — just an HTTP round-trip)."""
    client = get_anthropic()
    msg = client.messages.create(
        model=model,
        max_tokens=2048,
        messages=[{"role": "user", "content": prompt}],
    )
    # Concatenate all text blocks (Haiku usually returns just one)
    return "".join(b.text for b in msg.content if b.type == "text")


def build_rename_prompt(windows: list[dict]) -> str:
    lines = [
        "You are renaming tmux windows in a senior developer's terminal session.",
        "",
        "For each window below, suggest a SHORT descriptive name that captures what",
        "is currently happening in that window. Constraints:",
        "  - 1-3 words, lowercase-with-dashes preferred (e.g. 'fs-build', 'cohort-inv')",
        "  - Max 25 characters",
        "  - Concept-focused, not generic. Bad: 'claude', 'shell', 'zsh', 'work'.",
        "    Good: 'postcode-ingestion', 'monitoring-cert', 'rust-port'",
        "  - If the existing name is still accurate, KEEP IT (don't change for the sake of changing)",
        "",
        "Windows in this session:",
    ]
    for w in windows:
        lines.append("")
        lines.append(f"[index {w['index']}] current_name='{w['current_name']}'")
        if w.get("branch"):
            pr = f", PR #{w['pr']}" if w.get("pr") else ""
            lines.append(f"  branch: {w['branch']}{pr}")
        if w.get("recap"):
            lines.append(f"  recap: {w['recap'][:300]}")
        if w.get("pending_input"):
            lines.append(f"  pending input: {w['pending_input'][:120]}")
        snippet = w.get("recent_excerpt", "")
        if snippet:
            lines.append(f"  recent terminal excerpt:\n    {snippet}")
    lines.append("")
    lines.append(
        'Return ONLY a JSON object mapping window index (as a string) to the new name. '
        'Example: {"1": "fs-build", "2": "cohort-inv"}. '
        "No markdown fences, no commentary, just the JSON object."
    )
    return "\n".join(lines)


@app.post("/api/auto-rename-session")
def auto_rename_session(session: str):
    all_windows = list_windows()
    target_windows = [w for w in all_windows if w["session"] == session]
    _attach_git_then_resolve_pids(target_windows)
    if not target_windows:
        return {"ok": False, "error": f"session {session!r} not found"}

    # Build per-window context
    context = []
    for w in target_windows:
        target = f"{w['session']}:{w['index']}"
        try:
            content = capture(target, lines=80)
            parsed = parse_pane(content)
        except Exception:
            content, parsed = "", {}
        # Strip ANSI from snippet so the prompt isn't full of escape codes
        plain = re.sub(r"\x1b\[[\d;]*m", "", content)
        snippet_lines = [ln for ln in plain.split("\n") if ln.strip()][-20:]
        snippet = "\n    ".join(snippet_lines)[-1200:]
        # branch/pr no longer live in parse_pane output — they're derived
        # from the pane's cwd via git/gh. Fetching here (cached) gives the
        # prompt actually-useful context.
        git = cached_git_state(w.get("cwd", "")) or {}
        pr = cached_pr_state(w.get("cwd", ""), git.get("branch")) or {}
        context.append(
            {
                "index": w["index"],
                "current_name": w["name"],
                "branch": git.get("branch"),
                "pr": pr.get("pr"),
                "recap": parsed.get("recap"),
                "pending_input": parsed.get("pending_input"),
                "recent_excerpt": snippet,
            }
        )

    prompt = build_rename_prompt(context)
    try:
        result = claude_complete(prompt)
    except Exception as e:
        return {"ok": False, "error": str(e)}

    # Claude sometimes wraps JSON in code fences despite instructions; strip.
    cleaned = result.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.MULTILINE)
    try:
        new_names = json.loads(cleaned)
    except json.JSONDecodeError as e:
        return {"ok": False, "error": f"claude returned invalid JSON: {e}", "raw": result[:500]}

    applied = []
    for index_str, new_name in new_names.items():
        try:
            index = int(index_str)
        except ValueError:
            continue
        new_name = (new_name or "").strip()
        if not new_name:
            continue
        old = next((w["name"] for w in target_windows if w["index"] == index), None)
        if old is None or new_name == old:
            continue
        target = f"{session}:{index}"
        tmux("rename-window", "-t", target, new_name)
        applied.append({"index": index, "old": old, "new": new_name})

    return {"ok": True, "applied": applied, "session": session}


@app.post("/api/auto-rename-window")
def auto_rename_window(session: str, index: int):
    """Single-window variant of auto_rename_session. Same prompt machinery, but
    scoped to one window so the user can refresh a single card's name without
    perturbing siblings."""
    target = f"{session}:{index}"
    try:
        meta = tmux(
            "display-message", "-t", target, "-p",
            "#{window_name}\t#{pane_current_path}",
        ).strip()
        current_name, _, cwd = meta.partition("\t")
    except Exception as e:
        return {"ok": False, "error": str(e)}

    # Single-window pid resolution: build a one-element list and reuse the
    # batch helper so `last_seen` stays current for this window too.
    one = [{"session": session, "index": index, "name": current_name, "active": False, "cwd": cwd, "pid_raw": ""}]
    _attach_git_then_resolve_pids(one)
    pid = one[0].get("pid")
    if not current_name:
        return {"ok": False, "error": f"target {target!r} not found"}

    try:
        content = capture(target, lines=80)
        parsed = parse_pane(content)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    plain = re.sub(r"\x1b\[[\d;]*m", "", content)
    snippet_lines = [ln for ln in plain.split("\n") if ln.strip()][-20:]
    snippet = "\n    ".join(snippet_lines)[-1200:]
    git = cached_git_state(cwd) or {}
    pr = cached_pr_state(cwd, git.get("branch")) or {}

    ctx = [{
        "index": index,
        "current_name": current_name,
        "branch": git.get("branch"),
        "pr": pr.get("pr"),
        "recap": parsed.get("recap"),
        "pending_input": parsed.get("pending_input"),
        "recent_excerpt": snippet,
    }]
    prompt = build_rename_prompt(ctx)
    try:
        result = claude_complete(prompt)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    cleaned = result.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.MULTILINE)
    try:
        new_names = json.loads(cleaned)
    except json.JSONDecodeError as e:
        return {"ok": False, "error": f"claude returned invalid JSON: {e}", "raw": result[:500]}
    new_name = (new_names.get(str(index)) or "").strip()
    if not new_name:
        return {"ok": False, "error": "claude returned empty name"}
    if new_name == current_name:
        return {"ok": True, "applied": False, "old": current_name, "new": current_name, "pid": pid}
    tmux("rename-window", "-t", target, new_name)
    return {"ok": True, "applied": True, "old": current_name, "new": new_name, "pid": pid}


def _send_to_target(target: str, paste: str | None, keys: list[str]) -> dict:
    """Core paste-buffer + send-keys logic. Used by `/api/send` and the bulk
    variant; both bump focus + acted_at on the target. Returns a result dict
    suitable for inclusion in the endpoint response."""
    if not keys and (paste is None or paste == ""):
        return {"target": target, "ok": False, "error": "no keys or paste"}
    try:
        if paste is not None and paste != "":
            # Unique buffer name so concurrent calls (including bulk fan-out)
            # never trample each other.
            import uuid
            buf = f"wd-{uuid.uuid4().hex[:8]}"
            tmux("set-buffer", "-b", buf, paste)
            tmux("paste-buffer", "-d", "-p", "-b", buf, "-t", target)
            # Give the receiving TUI (especially Claude Code) time to apply
            # state for the paste before the submit key arrives. Without this,
            # Enter can land before React renders and submits empty input,
            # leaving the pasted text visibly stranded in the prompt area.
            if keys:
                time.sleep(0.10)
        if keys:
            tmux("send-keys", "-t", target, *keys)
    except subprocess.CalledProcessError as e:
        return {"target": target, "ok": False, "error": (e.stderr or str(e)).strip()}
    except Exception as e:
        return {"target": target, "ok": False, "error": str(e)}
    note_focus(target)
    note_action(target)
    return {"target": target, "ok": True}


@app.post("/api/send")
def send(session: str, index: int, body: SendBody):
    """Send input to a tmux pane.

    `paste`, if set, is sent first via tmux's bracketed-paste mechanism — this
    is the only reliable way to deliver multi-line text, since tmux send-keys
    silently strips embedded newlines.

    `keys` is then sent via send-keys. Each item is either a tmux key name
    (Enter, Escape, C-c, S-Tab, Up, F1, …) or a literal string.
    """
    target = f"{session}:{index}"
    result = _send_to_target(target, body.paste, body.keys)
    if not result["ok"]:
        return result
    return {"ok": True, "target": target}


@app.post("/api/send-bulk")
def send_bulk(body: SendBulkBody):
    """Fan out the same paste/keys to multiple panes concurrently.

    Each target is processed in its own thread so the per-pane 100ms
    bracketed-paste delay overlaps across panes — broadcasting `/reload-plugins`
    to 30 claudes finishes in ~100ms wall time instead of 3s sequential.

    Buffer-name collisions are avoided by `_send_to_target` minting a fresh
    uuid'd buf per call.
    """
    if not body.targets:
        return {"ok": False, "error": "no targets"}
    if not body.keys and (body.paste is None or body.paste == ""):
        return {"ok": False, "error": "no keys or paste"}
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=min(32, len(body.targets))) as pool:
        results = list(
            pool.map(
                lambda t: _send_to_target(t, body.paste, body.keys),
                body.targets,
            )
        )
    ok_count = sum(1 for r in results if r["ok"])
    return {"ok": True, "sent": ok_count, "total": len(results), "results": results}


@app.post("/api/channel/clear-unread")
def channel_clear_unread(pane: str = Query(...)):
    if not pane.startswith("%"):
        return {"ok": False, "error": "pane must be a %N tmux pane id"}
    with _CHANNELS_LOCK:
        _CHANNEL_UNREAD[pane] = 0
    return {"ok": True}


# --- Paste image (screenshot) → temp file → @path into pane --------------
#
# xterm.js has no way to carry image bytes through to Claude Code, and tmux
# has no image protocol either. So we shortcut: the browser intercepts a
# paste event with an image in the clipboard, POSTs the bytes here, we write
# them to /tmp, and bracketed-paste "@/tmp/foo.png " into the pane. Claude
# Code resolves @-paths against the filesystem on submit.
#
# Files are best-effort GC'd on each paste (anything older than an hour).
# Same-machine only by construction — server binds 127.0.0.1.
_PASTE_IMG_DIR = Path("/tmp")
_PASTE_IMG_PREFIX = "periscope-paste-"
_PASTE_IMG_MAX_AGE_S = 3600.0
_PASTE_IMG_MAX_BYTES = 25 * 1024 * 1024
_EXT_BY_MIME = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/heic": "heic",
}


def _sweep_old_paste_images() -> None:
    cutoff = time.time() - _PASTE_IMG_MAX_AGE_S
    for p in _PASTE_IMG_DIR.glob(f"{_PASTE_IMG_PREFIX}*"):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
        except Exception:
            pass


@app.post("/api/paste-image")
async def paste_image(session: str, index: int, request: Request):
    target = f"{session}:{index}"
    body = await request.body()
    if not body:
        return {"ok": False, "error": "empty body"}
    if len(body) > _PASTE_IMG_MAX_BYTES:
        return {"ok": False, "error": f"image too large ({len(body)} bytes)"}
    mime = (request.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
    ext = _EXT_BY_MIME.get(mime, "png")
    _sweep_old_paste_images()
    path = _PASTE_IMG_DIR / f"{_PASTE_IMG_PREFIX}{uuid.uuid4().hex}.{ext}"
    path.write_bytes(body)
    # Trailing space so Claude Code commits the @-reference (its file picker
    # closes on whitespace) and the user can keep typing immediately after.
    buf = f"wd-img-{uuid.uuid4().hex[:8]}"
    tmux("set-buffer", "-b", buf, f"@{path} ")
    tmux("paste-buffer", "-d", "-p", "-b", buf, "-t", target)
    note_focus(target)
    note_action(target)
    return {"ok": True, "path": str(path), "bytes": len(body)}


# --- Live terminal: WebSocket bridge to a tmux pane ----------------------
#
# Architecture:
#   - tmux pipe-pane -O writes the pane's output stream to a named pipe
#   - we read from the FIFO and forward bytes to the WebSocket
#   - we receive keystroke messages from the WebSocket and pass them through
#     to tmux send-keys -l (literal) so escape sequences (arrow keys, etc.)
#     reach the pane's PTY untouched
#   - on disconnect we stop the pipe-pane and remove the FIFO
#
# pipe-pane duplicates the output, so the user's actual tmux terminal keeps
# rendering normally alongside the browser-side terminal.


@app.websocket("/ws/pane")
async def ws_pane(websocket: WebSocket, session: str, index: int):
    await websocket.accept()
    target = f"{session}:{index}"
    # Modal-open is the canonical "opened in periscope" event. The grid view's
    # `focused_at` doesn't move (no tmux focus shift here), but the stream view
    # should treat this as engagement with the window.
    note_action(target)
    fifo_path = f"/tmp/periscope.{uuid.uuid4().hex}.fifo"
    fd = None
    loop = asyncio.get_running_loop()
    pipe_active = False

    # On the first {type:"resize"} message we save the window's original size
    # and window-size mode so we can restore them when the connection closes.
    # Tmux refuses to honor resize-window unless window-size is "manual".
    saved_window_size: str | None = None
    saved_dims: tuple[int, int] | None = None

    try:
        # 1) Get tmux's view of the pane: size, cursor position, alt-screen
        #    state. We need all three to render the initial blob into an xterm
        #    state that matches what tmux thinks the pane currently looks like.
        #    If we don't, incremental updates from the stream (e.g. "cursor to
        #    row 5 col 15, write '20s'") land at xterm's stale cursor and
        #    leave ghost text from the old buffer.
        try:
            meta = tmux(
                "display-message", "-t", target, "-p",
                "#{pane_width}|#{pane_height}|#{cursor_x}|#{cursor_y}|#{alternate_on}",
            ).strip()
            cols_s, rows_s, cx_s, cy_s, alt_s = meta.split("|")
            cols, rows = int(cols_s), int(rows_s)
            cx, cy = int(cx_s), int(cy_s)
            alt_on = alt_s == "1"
        except Exception:
            cols, rows, cx, cy, alt_on = 120, 40, 0, 0, False

        await websocket.send_text(
            json.dumps({"type": "size", "cols": cols, "rows": rows})
        )

        initial = tmux("capture-pane", "-t", target, "-p", "-e", "-S", "-200")
        # capture-pane separates lines with \n AND appends one more \n after
        # the final line. We strip exactly that final terminator (not any
        # blank-line content above it) and convert internal \n to \r\n so
        # xterm wraps each new line back to column 0 instead of staircasing.
        # If we strip too many, blank lines at the bottom of the pane vanish
        # and the cursor lands one row above where tmux says it is. If we
        # strip too few, the trailing \r\n scrolls xterm one row past the
        # bottom and the cursor lands one row below.
        if initial:
            if initial.endswith("\n"):
                initial = initial[:-1]
            body = initial.replace("\n", "\r\n")
        else:
            body = ""
        # Build a prefix that puts xterm into the same screen mode tmux is in,
        # clears any stale rendering, then a suffix that parks the cursor
        # where tmux thinks it is.
        prefix = ""
        if alt_on:
            prefix += "\x1b[?1049h"  # enter alt-screen buffer
        prefix += "\x1b[2J\x1b[H"     # clear screen, home cursor
        # ANSI cursor positioning is 1-indexed; tmux's #{cursor_x/y} are 0-indexed.
        suffix = f"\x1b[{cy + 1};{cx + 1}H"
        blob = (prefix + body + suffix).encode("utf-8", errors="replace")
        await websocket.send_bytes(blob)

        # 2) Set up the pipe. mkfifo + open(O_RDONLY|O_NONBLOCK) returns
        #    immediately; tmux opens the write end via the cat subprocess.
        os.mkfifo(fifo_path)
        tmux("pipe-pane", "-O", "-t", target, f"cat > {fifo_path}")
        pipe_active = True
        fd = os.open(fifo_path, os.O_RDONLY | os.O_NONBLOCK)

        # 3) Bridge FIFO → queue → websocket. asyncio.add_reader notifies us
        #    when the fd is readable; we drain in non-blocking chunks.
        out_queue: asyncio.Queue[bytes] = asyncio.Queue()

        def on_readable():
            try:
                chunk = os.read(fd, 8192)
            except BlockingIOError:
                return
            if chunk:
                out_queue.put_nowait(chunk)

        loop.add_reader(fd, on_readable)

        async def forward_out():
            while True:
                chunk = await out_queue.get()
                await websocket.send_bytes(chunk)

        forward_task = asyncio.create_task(forward_out())

        # 4) Main loop: receive keystrokes from the client and push to tmux.
        #    xterm.js's onData sends raw input including escape sequences
        #    (e.g. "\x1b[A" for up arrow). We deliver them via load-buffer +
        #    paste-buffer rather than send-keys -l because tmux's argv parser
        #    treats a standalone ";" as a command separator — typing a single
        #    semicolon would otherwise be silently dropped.
        try:
            while True:
                msg = await websocket.receive()
                if msg.get("type") == "websocket.disconnect":
                    break
                text = msg.get("text")
                if text is None and msg.get("bytes") is not None:
                    text = msg["bytes"].decode("utf-8", errors="replace")
                if not text:
                    continue
                # Resize control message: client measured how many cols/rows
                # fit in its modal and asks tmux to match. Sent as JSON text
                # ({"type":"resize","cols":N,"rows":M}). Plain keystrokes are
                # always non-JSON, so the json.loads path filters them out.
                if text.startswith("{"):
                    try:
                        ctrl = json.loads(text)
                    except Exception:
                        ctrl = None
                    if isinstance(ctrl, dict) and ctrl.get("type") == "resize":
                        cols = int(ctrl.get("cols") or 0)
                        rows = int(ctrl.get("rows") or 0)
                        if cols > 0 and rows > 0:
                            def do_resize(c=cols, r=rows):
                                nonlocal saved_window_size, saved_dims
                                if saved_window_size is None:
                                    # First resize: snapshot the window's
                                    # current size + mode so we can restore
                                    # on disconnect, then switch to manual so
                                    # resize-window actually takes effect.
                                    try:
                                        wsz = tmux(
                                            "show-option", "-t", target,
                                            "-w", "-v", "window-size",
                                        ).strip() or "latest"
                                        dims = tmux(
                                            "display-message", "-t", target,
                                            "-p", "#{window_width} #{window_height}",
                                        ).strip().split()
                                        saved_window_size = wsz
                                        saved_dims = (int(dims[0]), int(dims[1]))
                                        tmux("setw", "-t", target, "window-size", "manual")
                                    except Exception:
                                        pass
                                tmux("resize-window", "-t", target, "-x", str(c), "-y", str(r))
                            await loop.run_in_executor(None, do_resize)
                        continue
                await loop.run_in_executor(
                    None, lambda t=text: deliver_input(target, t)
                )
        except WebSocketDisconnect:
            pass
        finally:
            forward_task.cancel()
    finally:
        # Cleanup in reverse setup order. Each step is best-effort because
        # any of them could fail mid-teardown if the pane already died.
        # Restore the original window size + mode if we ever resized.
        if saved_window_size is not None and saved_dims is not None:
            try:
                tmux(
                    "resize-window", "-t", target,
                    "-x", str(saved_dims[0]), "-y", str(saved_dims[1]),
                )
                tmux("setw", "-t", target, "window-size", saved_window_size)
            except Exception:
                pass
        if pipe_active:
            try:
                tmux("pipe-pane", "-t", target)  # no command = stop piping
            except Exception:
                pass
        if fd is not None:
            try:
                loop.remove_reader(fd)
            except Exception:
                pass
            try:
                os.close(fd)
            except Exception:
                pass
        if os.path.exists(fifo_path):
            try:
                os.unlink(fifo_path)
            except Exception:
                pass


def prewarm_pr_cache() -> None:
    """Walk every current tmux pane, resolve its branch via git, and kick off
    background gh PR queries for each unique (cwd, branch) pair. Runs once at
    startup so PR badges populate on the first /api/state poll instead of
    waiting for the second poll's stale-while-revalidate to fire them."""
    if not _GH_AVAILABLE:
        return
    try:
        windows = list_windows()
    except Exception:
        return
    pairs: set[tuple[str, str]] = set()
    for w in windows:
        cwd = w.get("cwd") or ""
        if not cwd:
            continue
        git = cached_git_state(cwd)
        if git and git.get("branch"):
            pairs.add((cwd, git["branch"]))
    for cwd, branch in pairs:
        # cached_pr_state spawns a daemon thread per (cwd, branch) miss.
        cached_pr_state(cwd, branch)


# Mounted last so the API/WS routes above take precedence. `html=True` serves
# index.html for `/` (and any directory request) without needing a separate
# route. Asset paths in index.html are root-relative (`/styles.css`, `/app.js`,
# `/vendor/xterm.js`) so they resolve identically here and under Vite's dev
# server on :5174.
app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")


if __name__ == "__main__":
    import uvicorn

    # loop="asyncio" forces the stdlib selector loop instead of uvloop. As of
    # uvloop 0.22.1 + CPython 3.14, uvloop captures `asyncio.iscoroutinefunction`
    # at import time and calls it from `run_in_executor`, which now emits a
    # DeprecationWarning per call (loud during WS resize traffic). Revert this
    # when uvloop ships a 3.14-compatible release.
    #
    # reload=True watches server.py for changes and restarts the worker. Needs
    # an import string (not the `app` object) so the reloader can re-import the
    # module. reload_dirs is scoped to this file's parent so edits under
    # static/ don't bounce the server — Vite handles frontend reloads in dev,
    # and direct browser hits pick up new static files without a restart.
    uvicorn.run(
        "server:app",
        host="127.0.0.1",
        port=8765,
        log_level="warning",
        loop="asyncio",
        reload=True,
        reload_dirs=[str(Path(__file__).parent)],
    )
