# /// script
# requires-python = ">=3.11"
# dependencies = ["fastapi", "uvicorn[standard]"]
# ///
"""Live tmux dashboard. Run with: uv run server.py"""

import re
import subprocess
import time
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

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
        "#{session_name}\t#{window_index}\t#{window_name}\t#{window_active}\t#{window_activity}",
    )
    rows = []
    for line in out.strip().split("\n"):
        if not line:
            continue
        s, idx, name, active, activity = line.split("\t")
        rows.append(
            {
                "session": s,
                "index": int(idx),
                "name": name,
                "active": active == "1",
                "activity": int(activity),
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
    """Capture last N lines of a pane. Session/index passed as query params so
    slash-bearing session names (e.g. "tc/foo/bar") don't conflict with path
    routing."""
    target = f"{session}:{index}"
    # -e preserves ANSI color escape sequences for the modal viewer.
    content = tmux("capture-pane", "-t", target, "-p", "-e", "-S", f"-{lines}")
    return {"content": content, "target": target}


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
    if body.keys:
        tmux("send-keys", "-t", target, *body.keys)
    if not body.keys and body.paste is None:
        return {"ok": False, "error": "no keys or paste"}
    note_focus(target)
    return {"ok": True, "target": target}


app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def root():
    return FileResponse(STATIC / "index.html")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8765, log_level="warning")
