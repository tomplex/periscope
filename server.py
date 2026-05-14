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
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# Load .env from the script's directory (existing env vars take precedence).
load_dotenv(Path(__file__).parent / ".env")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # prewarm_pr_cache and cached_scraped_usage are defined later in the file;
    # Python resolves the names at call-time, so the forward references are
    # fine. We kick off both eagerly so the first /api/state poll already has
    # PR badges and the usage bars populated.
    threading.Thread(target=prewarm_pr_cache, daemon=True).start()
    threading.Thread(target=cached_scraped_usage, daemon=True).start()
    yield


app = FastAPI(lifespan=lifespan)
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

# Spinner: any non-ASCII glyph at line start (Claude Code rotates through
# ✻ ✶ ✷ ✳ ✦ … and others — enumerating breaks every time Claude adds a new
# one) + whitespace + a single-word verb + a literal `…`. The ellipsis is
# what distinguishes an active spinner from past-tense status lines like
# "✻ Brewed for 31s" (no `…`).
#
# The phrase is `\S+?` (single token, no whitespace) on purpose: Claude Code
# tool-call headers look like `⏺ Bash(cd /Users/tom/… --skip-glo…)`, with the
# `…` truncating the command inside the parens. A laxer pattern that allowed
# whitespace inside the phrase would match those scrollback lines and falsely
# promote idle Claude panes to the "working" state.
SPINNER_RE = re.compile(r"^\s*[^\x00-\x7f]\s+(?P<phrase>\S+?)…")

# Newer Claude Code task UI dropped the trailing `…`. An active operation now
# renders as `<glyph> <verb> <noun?> (Xm Ys · ↑Nk tokens · thought for Zs)`
# and signals completion by dropping the up-arrow — e.g. `Done (5 tool uses ·
# 25.5k tokens · 21s)` is a finished agent. The `↑ Nk tokens` inside parens
# is the live uplink meter; only the running op shows it. Distinguishable
# from STATUS_RE because the status line has both ↑ and ↓ and no `tokens`
# word, and it isn't wrapped in parens.
ACTIVE_OP_RE = re.compile(r"\([^)]*↑\s*[\d.]+\w*\s+tokens[^)]*\)")

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


def scrape_usage_via_tmux() -> dict | None:
    """Drive `claude` in a hidden tmux session to capture its /usage output."""
    sess = f"periscope-usage-{uuid.uuid4().hex[:8]}"
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

    # Spinner: most recent active-op signal in the bottom rows. Two forms,
    # tried at each line so whichever sits closer to the bottom wins (a fresh
    # active op should override an older `…` line lingering in scrollback):
    #   - old: "<glyph> <phrase>…"           (SPINNER_RE)
    #   - new: "<glyph> <verb> ... (↑Nk tokens ...)"  (ACTIVE_OP_RE)
    # Verb extraction is a display nicety — detection only requires the match.
    #
    # Window is tight (15 lines) because both patterns are short enough strings
    # that Claude assistant prose can incidentally render them — a prior
    # response quoting `✻ Envisioning…` or `(...↑22.1k tokens...)` as example
    # text would false-positive against a wider window. The actual TUI marker
    # sits ~7-11 rows above the prompt even with a long subtask list, so the
    # 15-line ceiling never excludes a real one.
    spinner = None
    for line in reversed(lines[-15:]):
        m = SPINNER_RE.match(line)
        if m:
            phrase = m.group("phrase").strip()
            vm = SPINNER_VERB_RE.search(phrase)
            if vm:
                spinner = vm.group(1)
            else:
                first = phrase.split(None, 1)[0] if phrase else ""
                spinner = first or "working"
            break
        if ACTIVE_OP_RE.search(line):
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
    pending_input = None
    if not needs_input:
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

    # State priority: needs-input wins over working (a spinner glyph can
    # linger in scrollback above the dialog), working wins over waiting.
    if not is_claude:
        state = "shell"
    elif needs_input:
        state = "needs-input"
    elif spinner:
        state = "working"
    else:
        state = "waiting"

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
        git = cached_git_state(w.get("cwd", "")) or {}
        pr = cached_pr_state(w.get("cwd", ""), git.get("branch")) or {}
        result.append(
            {
                **w, **parsed, **git, **pr,
                "target": target,
                "focused_at": _focused_at.get(target, 0),
            }
        )
    return {
        "windows": result,
        "ts": int(time.time()),
        "usage": cached_claude_usage(),
        "usage_scrape": cached_scraped_usage(),
    }


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
    pr = cached_pr_state(cwd, git.get("branch")) or {}
    # Shorten $HOME → ~ for display. Done server-side because the browser
    # doesn't know the user's home dir.
    home = os.path.expanduser("~")
    cwd_display = cwd
    if cwd and (cwd == home or cwd.startswith(home + "/")):
        cwd_display = "~" + cwd[len(home):]
    return {
        "content": content,
        "target": target,
        "name": window_name,
        "cwd": cwd_display,
        "session": session,
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
    # Stamp focus so the new session sorts to the top on next poll.
    note_focus(f"{name}:0")
    return {"ok": True, "session": name}


@app.delete("/api/session")
def session_delete(session: str):
    ok, msg = _tmux_mutate("kill-session", "-t", session)
    if not ok:
        return {"ok": False, "error": msg}
    prefix = f"{session}:"
    for t in [t for t in _focused_at if t.startswith(prefix)]:
        _focused_at.pop(t, None)
    _active_per_session.pop(session, None)
    return {"ok": True, "session": session}


@app.post("/api/window/new")
def window_new(session: str, mode: str = "shell"):
    """Spawn a window in `session`. `mode=claude` types `claude\\n` into the
    new pane so the window comes up running Claude Code; `mode=shell` leaves
    it at a bare prompt. cwd is inherited from the session's active pane —
    without `-c`, tmux would use the periscope server's cwd, which is never
    what you want."""
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
    if mode == "claude":
        # Let the shell finish its rc before the `claude` line arrives, so the
        # command runs as a real prompt entry rather than mid-rc echoed text.
        time.sleep(0.1)
        tmux("send-keys", "-t", target, "claude", "Enter")
    note_focus(target)
    return {"ok": True, "session": session, "index": index, "target": target, "mode": mode}


@app.delete("/api/window")
def window_delete(session: str, index: int):
    target = f"{session}:{index}"
    ok, msg = _tmux_mutate("kill-window", "-t", target)
    if not ok:
        return {"ok": False, "error": msg}
    _focused_at.pop(target, None)
    return {"ok": True, "target": target}


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
        return {"ok": True, "applied": False, "old": current_name, "new": current_name}
    tmux("rename-window", "-t", target, new_name)
    return {"ok": True, "applied": True, "old": current_name, "new": new_name}


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
