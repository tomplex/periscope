"""Claude usage tracking: two parallel paths.

(1) JSONL parsing: walks ~/.claude/projects/*/*.jsonl and sums token usage
    in the current 5-hour window. Cheap, no IO with Claude. Returns
    approximate numbers (input/output/cache_creation/cache_read tokens).

(2) TUI scrape: spawns `claude` in a hidden tmux session, sends /usage,
    captures + parses the rendered screen. Authoritative because it's
    exactly what Anthropic shows the user; expensive (tmux session + 5–12s
    of boot + render). Refreshed every 5 minutes in a background thread.

The dashboard prefers (2) when available, falls back to (1).
"""

import json
import re
import subprocess
import threading
import time
import uuid
from pathlib import Path

from periscope.config import STATIC, USAGE_SESSION_PREFIX
from periscope.log import _bg
from periscope.tmux import tmux


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


# Hidden tmux sessions we spawn to drive `claude /usage`. Named with the
# USAGE_SESSION_PREFIX (defined in periscope.config) so panes.list_windows
# can filter them out of the dashboard, and so kill_orphan_usage_sessions
# can reap leaked sessions at startup.


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
            _bg("usage-scrape", _refresh_scrape_into_cache)
        return data
