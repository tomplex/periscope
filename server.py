# /// script
# requires-python = ">=3.11"
# dependencies = ["fastapi", "uvicorn[standard]", "anthropic", "python-dotenv"]
# ///
"""Periscope — live tmux dashboard. Run with: uv run server.py"""

import asyncio
import json
import os
import re
import subprocess
import time
import uuid
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# Load .env from the script's directory (existing env vars take precedence).
load_dotenv(Path(__file__).parent / ".env")

app = FastAPI()
STATIC = Path(__file__).parent / "static"

# Server-tracked "last user-focused" per target.
# Tmux's window_activity bumps on any output (Claude streaming, build logs, dev
# servers, etc), which surprises users expecting "last accessed" semantics.
# We instead record when each window most recently became the active window in
# its session, plus any time the user acts on it via the dashboard.
_focused_at: dict[str, int] = {}
_active_per_session: dict[str, str] = {}


def note_focus(target: str) -> None:
    _focused_at[target] = int(time.time())


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

# Title line above the status line:
#   "  fdy | master | clean | github.com/.../pull/1234 ✓"
TITLE_RE = re.compile(
    r"^\s*(?P<title>.+?)\s+\|\s+(?P<branch>[^|]+?)\s+\|\s+(?P<git>[^|]+?)\s+\|\s+(?P<repo>.+?)\s*$"
)

PR_RE = re.compile(r"github\.com/[^/]+/[^/]+/pull/(?P<num>\d+)\s*(?P<ci>[⟳✓✗])?")
SPINNER_RE = re.compile(r"^[\s✻✶*·]+(?P<verb>[A-Z]\w+(?:ing|ed))[…\.]")
RECAP_RE = re.compile(
    r"※ recap:\s*(?P<text>.+?)(?=\n\s*[─❯]|\Z)", re.DOTALL
)
PROMPT_LINE_RE = re.compile(r"^❯\s*(?P<input>.*)$")


def tmux(*args: str) -> str:
    r = subprocess.run(
        ["tmux", *args], capture_output=True, text=True, timeout=5
    )
    return r.stdout


def list_windows() -> list[dict]:
    out = tmux(
        "list-windows",
        "-a",
        "-F",
        "#{session_name}\t#{window_index}\t#{window_name}\t#{window_active}",
    )
    rows = []
    for line in out.strip().split("\n"):
        if not line:
            continue
        s, idx, name, active = line.split("\t")
        rows.append(
            {
                "session": s,
                "index": int(idx),
                "name": name,
                "active": active == "1",
            }
        )
    return rows


def capture(target: str, lines: int = 100) -> str:
    return tmux("capture-pane", "-t", target, "-p", "-S", f"-{lines}")


def parse_pane(content: str) -> dict:
    raw_lines = content.rstrip("\n").split("\n")
    lines = [ln for ln in raw_lines if ln.strip() != ""]

    status = None
    title = None
    # The Claude UI keeps its status block (title + context-line) at the very
    # bottom of the pane. If we don't see it in the last 4 non-empty lines, the
    # user is at a shell prompt and the pane shouldn't be marked as Claude even
    # if scrollback contains old status lines.
    tail = lines[-4:]
    for i in range(len(tail) - 1, -1, -1):
        m = STATUS_RE.match(tail[i])
        if m:
            status = m.groupdict()
            if i > 0:
                t = TITLE_RE.match(tail[i - 1])
                if t:
                    title = t.groupdict()
            break

    is_claude = status is not None

    # Spinner: most recent "✻ Verbing…" line in the pane.
    spinner = None
    for line in reversed(lines[-40:]):
        m = SPINNER_RE.match(line)
        if m:
            spinner = m.group("verb")
            break

    # Pending input: ❯ followed by some text (not just empty prompt)
    pending_input = None
    for line in reversed(lines[-15:]):
        m = PROMPT_LINE_RE.match(line.strip())
        if m and m.group("input").strip():
            pending_input = m.group("input").strip()
            break

    # Most recent recap block
    full = "\n".join(lines)
    recap = None
    matches = list(RECAP_RE.finditer(full))
    if matches:
        recap = matches[-1].group("text").strip()
        recap = re.sub(r"\s+", " ", recap)[:400]

    # PR + CI state
    pr_num = None
    ci_state = None
    if title:
        m = PR_RE.search(title["repo"])
        if m:
            pr_num = int(m.group("num"))
            ci_state = m.group("ci")

    # Last meaningful line for shell panes / fallback
    last_line = ""
    for line in reversed(lines):
        s = line.strip()
        if not s:
            continue
        if s.startswith("─") or s.startswith("❯"):
            continue
        if STATUS_RE.match(line) or TITLE_RE.match(line):
            continue
        last_line = s[:200]
        break

    if not is_claude:
        state = "shell"
    elif spinner:
        state = "working"
    else:
        state = "waiting"

    return {
        "is_claude": is_claude,
        "state": state,
        "spinner": spinner,
        "pending_input": pending_input,
        "recap": recap,
        "last_line": last_line,
        "title": title["title"].strip() if title else None,
        "branch": title["branch"].strip() if title else None,
        "git": title["git"].strip() if title else None,
        "pr": pr_num,
        "ci": ci_state,
        "context_pct": int(status["context"]) if status else None,
        "model": status["model"].strip() if status else None,
    }


@app.get("/api/state")
def state():
    windows = list_windows()
    update_focus_from_windows(windows)
    result = []
    for w in windows:
        target = f"{w['session']}:{w['index']}"
        try:
            content = capture(target)
            parsed = parse_pane(content)
        except Exception as e:
            parsed = {"error": str(e), "state": "error", "is_claude": False}
        result.append(
            {**w, **parsed, "target": target, "focused_at": _focused_at.get(target, 0)}
        )
    return {"windows": result, "ts": int(time.time())}


@app.get("/api/pane")
def pane(session: str, index: int, lines: int = 200):
    """Capture last N lines of a pane plus parsed status fields. Session/index
    passed as query params so slash-bearing session names (e.g. 'tc/foo/bar')
    don't conflict with path routing."""
    target = f"{session}:{index}"
    # -e preserves ANSI escape sequences for the modal viewer.
    content = tmux("capture-pane", "-t", target, "-p", "-e", "-S", f"-{lines}")
    # Parse the same buffer (after stripping ANSI) so the modal can render a
    # live status header alongside the pane content from one request.
    plain = re.sub(r"\x1b\[[\d;]*m", "", content)
    parsed = parse_pane(plain)
    try:
        window_name = tmux(
            "display-message", "-t", target, "-p", "#{window_name}"
        ).strip()
    except Exception:
        window_name = ""
    return {
        "content": content,
        "target": target,
        "name": window_name,
        **parsed,
    }


@app.post("/api/focus")
def focus(session: str, index: int):
    target = f"{session}:{index}"
    clients = tmux("list-clients", "-F", "#{client_name}").strip().split("\n")
    switched = []
    for c in clients:
        if c:
            tmux("switch-client", "-c", c, "-t", target)
            switched.append(c)
    note_focus(target)
    return {"ok": True, "switched": switched, "target": target}


class SendBody(BaseModel):
    keys: list[str] = []
    paste: str | None = None  # bracketed-pasted into the pane before `keys`


class RenameBody(BaseModel):
    name: str


@app.post("/api/rename")
def rename(session: str, index: int, body: RenameBody):
    target = f"{session}:{index}"
    name = body.name.strip()
    if not name:
        return {"ok": False, "error": "empty name"}
    tmux("rename-window", "-t", target, name)
    return {"ok": True, "target": target, "name": name}


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
        context.append(
            {
                "index": w["index"],
                "current_name": w["name"],
                "branch": parsed.get("branch"),
                "pr": parsed.get("pr"),
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
    if body.paste is not None and body.paste != "":
        # Use a unique buffer name so concurrent calls don't trample each other.
        import uuid
        buf = f"wd-{uuid.uuid4().hex[:8]}"
        tmux("set-buffer", "-b", buf, body.paste)
        tmux("paste-buffer", "-d", "-p", "-b", buf, "-t", target)
        # Give the receiving TUI (especially Claude Code) time to apply state
        # for the paste before the submit key arrives. Without this, Enter can
        # land before React renders and submits empty input, leaving the pasted
        # text visibly stranded in the prompt area.
        if body.keys:
            time.sleep(0.10)
    if body.keys:
        tmux("send-keys", "-t", target, *body.keys)
    if not body.keys and body.paste is None:
        return {"ok": False, "error": "no keys or paste"}
    note_focus(target)
    return {"ok": True, "target": target}


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
    fifo_path = f"/tmp/periscope.{uuid.uuid4().hex}.fifo"
    fd = None
    loop = asyncio.get_running_loop()
    pipe_active = False

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
        # capture-pane separates lines with bare \n. xterm needs \r\n to
        # return each new line to column 0 — without the \r every line would
        # staircase rightward.
        body = initial.replace("\n", "\r\n") if initial else ""
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
        #    (e.g. "\x1b[A" for up arrow). send-keys -l preserves them.
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
                # Run send-keys in a thread so it doesn't block the loop on the
                # ~1-2ms subprocess fork.
                await loop.run_in_executor(
                    None, lambda t=text: tmux("send-keys", "-t", target, "-l", t)
                )
        except WebSocketDisconnect:
            pass
        finally:
            forward_task.cancel()
    finally:
        # Cleanup in reverse setup order. Each step is best-effort because
        # any of them could fail mid-teardown if the pane already died.
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


app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def root():
    return FileResponse(STATIC / "index.html")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8765, log_level="warning")
