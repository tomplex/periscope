# /// script
# requires-python = ">=3.11"
# dependencies = ["fastapi", "uvicorn[standard]", "anthropic", "python-dotenv"]
# ///
"""Periscope — live tmux dashboard. Run with: uv run server.py"""

import asyncio
import json
import os
import re
import shutil
import subprocess
import threading
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

# Per-target spinner hysteresis. Tmux capture-pane occasionally catches Claude's
# TUI mid-redraw, dropping the spinner line for one cycle even when Claude is
# still working. We remember the last positive detection per target and treat
# it as sticky for SPINNER_GRACE_S so cards + modal subtitles don't flicker.
_spinner_last_seen: dict[str, tuple[str, float]] = {}
SPINNER_GRACE_S = 4.0


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

# Branch / PR / CI used to come from a custom statusline rendered in the line
# above STATUS_RE. We now pull those from the pane's cwd directly (git +
# `gh pr list`), independent of any statusline customization.

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
    """Return {pr, ci} for the PR open against `branch` in repo at `path`."""
    if not _GH_AVAILABLE or not path or not branch:
        return None
    code, out = _run(
        [
            "gh", "pr", "list",
            "--head", branch,
            "--state", "open",
            "--json", "number,statusCheckRollup",
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
    return {"pr": pr.get("number"), "ci": ci}


def _fetch_pr_into_cache(path: str, branch: str) -> None:
    try:
        data = pr_state_for(path, branch)
    except Exception:
        data = None
    with _pr_lock:
        _pr_cache[(path, branch)] = (time.time(), data)
        _pr_fetching.discard((path, branch))


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
        "#{session_name}\t#{window_index}\t#{window_name}\t#{window_active}\t#{pane_current_path}",
    )
    rows = []
    for line in out.strip().split("\n"):
        if not line:
            continue
        # pane_current_path is the active pane's cwd; safe even when missing.
        parts = line.split("\t")
        s, idx, name, active = parts[:4]
        cwd = parts[4] if len(parts) > 4 else ""
        rows.append(
            {
                "session": s,
                "index": int(idx),
                "name": name,
                "active": active == "1",
                "cwd": cwd,
            }
        )
    return rows


def capture(target: str, lines: int = 100) -> str:
    return tmux("capture-pane", "-t", target, "-p", "-S", f"-{lines}")


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
    raw_lines = content.rstrip("\n").split("\n")
    lines = [ln for ln in raw_lines if ln.strip() != ""]

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

    # Last meaningful line for shell panes / fallback
    last_line = ""
    for line in reversed(lines):
        s = line.strip()
        if not s:
            continue
        if s.startswith("─") or s.startswith("❯"):
            continue
        if STATUS_RE.match(line):
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
        # Hysteresis: smooth out per-poll detection gaps so cards / modal
        # subtitles don't flicker between "thinking" and idle.
        parsed["spinner"] = smooth_spinner(target, parsed.get("spinner"))
        if parsed.get("is_claude") and parsed.get("spinner") and parsed.get("state") != "working":
            parsed["state"] = "working"
        git = cached_git_state(w.get("cwd", "")) or {}
        pr = cached_pr_state(w.get("cwd", ""), git.get("branch")) or {}
        result.append(
            {
                **w, **parsed, **git, **pr,
                "target": target,
                "focused_at": _focused_at.get(target, 0),
            }
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
    parsed["spinner"] = smooth_spinner(target, parsed.get("spinner"))
    if parsed.get("is_claude") and parsed.get("spinner") and parsed.get("state") != "working":
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
    pr = cached_pr_state(cwd, git.get("branch")) or {}
    return {
        "content": content,
        "target": target,
        "name": window_name,
        **parsed,
        **git,
        **pr,
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


@app.on_event("startup")
def _on_startup() -> None:
    threading.Thread(target=prewarm_pr_cache, daemon=True).start()


app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def root():
    return FileResponse(STATIC / "index.html")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8765, log_level="warning")
