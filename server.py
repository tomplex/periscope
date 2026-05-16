# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "fastapi",
#     "uvicorn[standard]",
#     "anthropic",
#     "httpx",
#     "python-dotenv",
#     "mcp==1.27.*",
# ]
# ///
"""Periscope — live tmux dashboard. Run with: uv run server.py"""

import asyncio
import atexit
import json
import os
import re
import shutil
import signal
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

from periscope.config import STATIC, MCP_SOCKET_PATH
from periscope.log import log, _bg, _task
from periscope.pidfile import (
    _reclaim_existing_instance,
    _write_pidfile,
    _remove_pidfile,
)
from periscope.tmux import (
    tmux, capture, deliver_input, _run, _tmux_mutate,
    _ANSI_SGR_RE, _FG_COLOR_RE,
)
from periscope.store import (
    _STATE, _STATE_LOCK, _write_state, _state_path,
    _seed_commands_if_empty, _channels_migration_v1, _load_state,
)
from periscope.lgtm import (
    LGTM_BASE_URL, _LGTM_LOCK, _LGTM_BY_REPO, _LGTM_SSE_TASKS,
    cached_lgtm_state, _lgtm_submitted, _lgtm_refresh_all,
    _lgtm_periodic_refresh,
)
from periscope.channels import (
    _CHANNELS_LOCK, _CHANNEL_REPLIES, _CHANNEL_UNREAD, _MCP_SESSIONS,
    _channel_gc, _mcp_listener,
)
from periscope.panes import (
    _focused_at, _acted_at, _completed_at, _prev_state, _active_per_session,
    _resuming, RESUME_EXPIRY_S,
    smooth_spinner, smooth_is_claude,
    note_focus, note_action, update_focus_from_windows,
    list_windows, parse_pane,
)
from periscope.pids import _attach_git_then_resolve_pids

# Load .env from the script's directory (existing env vars take precedence).
load_dotenv(Path(__file__).parent / ".env")


# Logging + background-task wrappers now live in periscope/log.py.


# Pidfile / single-instance reclaim now lives in periscope/pidfile.py.


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # prewarm_pr_cache, cached_scraped_usage, and kill_orphan_usage_sessions
    # are defined later; Python resolves the names at call-time, so forward
    # references are fine.
    log.info("periscope starting (pid=%d)", os.getpid())
    # Reap any periscope-usage-* tmux sessions left behind by a prior crash
    # before the new scrape thread spawns a fresh one.
    kill_orphan_usage_sessions()
    # Kick off cache prewarms eagerly so the first /api/state poll already
    # has PR badges and the usage bars populated.
    _bg("prewarm-pr", prewarm_pr_cache)
    _bg("prewarm-usage", cached_scraped_usage)
    # MCP unix-socket listener: accepts connections from channel_shim.py
    # (one per Claude pane), runs an MCP Server per connection in-process.
    mcp_task = _task(_mcp_listener(), "mcp-listener")
    # LGTM mirror: polls localhost:9900 + subscribes per-session SSE.
    # No-op while LGTM isn't running; surfaces on the dashboard the
    # moment it comes up.
    lgtm_task = _task(_lgtm_periodic_refresh(), "lgtm-refresh")
    try:
        yield
    finally:
        log.info("periscope shutting down (pid=%d)", os.getpid())
        mcp_task.cancel()
        lgtm_task.cancel()
        for t in list(_LGTM_SSE_TASKS.values()):
            t.cancel()
        try:
            await mcp_task
        except (asyncio.CancelledError, Exception):
            pass
        try:
            os.unlink(MCP_SOCKET_PATH)
        except FileNotFoundError:
            pass


app = FastAPI(lifespan=lifespan)

# Persistent state (state.json) now lives in periscope/store.py.


# Channels code now lives in periscope/channels.py.

# Panes code (focus tracking + smoothing + list_windows + parse_pane + regexes)
# now lives in periscope/panes.py.


# LGTM integration helpers now live in periscope/lgtm.py.
# (The /api/lgtm/start route stays in server.py until Peel 8.)


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
                _bg("activity-fetch", _fetch_activity_into_cache, path, branch)
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
            _bg("usage-scrape", _refresh_scrape_into_cache)
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
            _bg("pr-fetch", _fetch_pr_into_cache, path, branch)
        return cached[1] if cached else None


# list_windows now lives in periscope/panes.py.

# Periscope window-ids (@periscope_id) now live in periscope/pids.py.


# parse_pane and the pids block now live in periscope/panes.py and
# periscope/pids.py respectively.


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
        lgtm = cached_lgtm_state(w.get("cwd", ""))

        # Channel state (added by 2026-05-14-channels-design.md).
        pane_id = w.get("pane_id") or ""
        with _CHANNELS_LOCK:
            channel_attached = pane_id in _MCP_SESSIONS if pane_id else False
            channel_unread = _CHANNEL_UNREAD.get(pane_id, 0) if pane_id else 0
            channel_replies = list(_CHANNEL_REPLIES.get(pane_id, [])) if pane_id else []

        # Persisted Claude-driven links (via the link_pr / link_linear MCP
        # tools). `linked_pr` overrides the auto-detected `pr` field — when
        # Claude has explicitly told us "this pane is for PR #N", we trust
        # that over heuristic title-bar parsing.
        linked_pr = persisted.get("linked_pr")
        linked_linear = persisted.get("linked_linear")
        if linked_pr:
            pr = dict(pr)
            pr["pr"] = str(linked_pr)
            pr["pr_linked"] = True
            # `ci` (CI glyph) is keyed to the auto-detected PR; an explicit
            # linked PR may not have a fresh CI signal until a future poll
            # resolves it. Drop the stale glyph rather than mislead.
            pr.pop("ci", None)

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
                "linked_linear": linked_linear,
                "lgtm": lgtm,
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
    # Persisted links — same override semantics as /api/state.
    persisted = _STATE.get("windows", {}).get(pid or "", {})
    linked_pr = persisted.get("linked_pr")
    linked_linear = persisted.get("linked_linear")
    if linked_pr:
        pr = dict(pr)
        pr["pr"] = str(linked_pr)
        pr["pr_linked"] = True
        pr.pop("ci", None)
    lgtm = cached_lgtm_state(cwd)
    return {
        "content": content,
        "target": target,
        "name": window_name,
        "cwd": cwd_display,
        "cwd_raw": cwd,
        "session": session,
        "pid": pid,
        "pane_id": pane_id,
        "activity": activity,
        "channel_attached": channel_attached,
        "channel_unread": channel_unread,
        "channel_replies": channel_replies,
        "linked_linear": linked_linear,
        "lgtm": lgtm,
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
            # `-P -F #{window_index}` is essential: with `base-index 1` in
            # tmux.conf the first window isn't 0, and a hardcoded `:0` target
            # makes the follow-up send-keys silently no-op (tmux() discards
            # stderr) — the user sees the session appear but claude never
            # launches.
            ok, msg = _tmux_mutate(
                "new-session", "-d", "-s", session, "-c", cwd,
                "-P", "-F", "#{window_index}",
            )
            if not ok:
                return {"ok": False, "error": f"failed to create session '{session}': {msg}"}
            try:
                index = int(msg)
            except ValueError:
                return {"ok": False, "error": f"tmux returned unexpected index: {msg!r}"}
            target = f"{session}:{index}"
            time.sleep(0.1)
            tmux("send-keys", "-t", target, f"claude --resume {resume_id}", "Enter")
            _resuming[resume_id] = {"target": target, "started_at": int(time.time())}
            note_focus(target)
            note_action(target)
            return {
                "ok": True,
                "session": session,
                "index": index,
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


@app.post("/api/window/move")
def window_move(session: str, index: int, dest: str):
    """Move a window into another session via tmux move-window. The new
    index is whatever slot dest had free; tmux's move-window doesn't print
    it, so we capture the source's stable #{window_id} (e.g. `@42`) up
    front and look up its post-move index by id."""
    src = f"{session}:{index}"
    if not dest or dest == session:
        return {"ok": False, "error": "destination missing or same as source"}
    win_id = tmux("display-message", "-t", src, "-p", "#{window_id}").strip()
    if not win_id.startswith("@"):
        return {"ok": False, "error": f"unknown source window: {src!r}"}
    code, _ = _run(["tmux", "has-session", "-t", dest])
    if code != 0:
        return {"ok": False, "error": f"unknown destination session: {dest!r}"}
    ok, msg = _tmux_mutate("move-window", "-d", "-s", src, "-t", f"{dest}:")
    if not ok:
        return {"ok": False, "error": msg}
    out = tmux("list-windows", "-t", dest, "-F", "#{window_id} #{window_index}")
    new_index = None
    for line in out.splitlines():
        wid, _, idx = line.partition(" ")
        if wid == win_id and idx.isdigit():
            new_index = int(idx)
            break
    if new_index is None:
        return {"ok": False, "error": f"could not locate moved window {win_id}"}
    new_target = f"{dest}:{new_index}"
    # Carry focus / acted bookkeeping over to the new target so the moved
    # window keeps its sort position instead of dropping to the bottom.
    if src in _focused_at:
        _focused_at[new_target] = _focused_at.pop(src)
    if src in _acted_at:
        _acted_at[new_target] = _acted_at.pop(src)
    return {"ok": True, "src": src, "dest": dest, "index": new_index, "target": new_target}


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


# --- LGTM integration: start a review from the dashboard -----------------
#
# Lets the modal's Review tab register a project with LGTM without going
# through Claude. We just POST to LGTM's /projects with the pane's cwd
# and trigger an immediate cache refresh so the next /api/state poll
# carries the new session info.

class LgtmStartBody(BaseModel):
    cwd: str


@app.post("/api/lgtm/start")
async def lgtm_start(body: LgtmStartBody):
    import httpx
    cwd = os.path.expanduser((body.cwd or "").strip())
    if not cwd or not Path(cwd).is_dir():
        return {"ok": False, "error": "cwd must be an existing directory"}
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.post(
                f"{LGTM_BASE_URL}/projects",
                json={"repoPath": cwd},
            )
            r.raise_for_status()
            payload = r.json()
    except (httpx.HTTPError, OSError) as e:
        return {"ok": False, "error": f"lgtm unreachable: {e}"}

    # Refresh the cache now so the response carries the freshly-registered
    # session — the caller can use the URL immediately to mount the iframe
    # rather than waiting for the next periodic refresh tick.
    await _lgtm_refresh_all()
    slug = payload.get("slug")
    return {
        "ok": True,
        "slug": slug,
        "url": f"{LGTM_BASE_URL}/project/{slug}/" if slug else None,
    }


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

        forward_task = _task(forward_out(), "ws-forward")

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

    # Reclaim any prior periscope before binding the port. Done here (not in
    # lifespan) because uvicorn binds the socket before lifespan runs — by
    # the time the worker starts up, a port collision has already failed.
    _reclaim_existing_instance()
    _write_pidfile()
    atexit.register(_remove_pidfile)
    # SIGTERM otherwise bypasses atexit; install a handler that logs and
    # exits cleanly so atexit fires and the next start is idempotent.
    def _on_sigterm(signum, _frame):
        log.info("received signal %d; exiting", signum)
        sys.exit(0)
    signal.signal(signal.SIGTERM, _on_sigterm)

    # loop="asyncio" forces the stdlib selector loop instead of uvloop. As of
    # uvloop 0.22.1 + CPython 3.14, uvloop captures `asyncio.iscoroutinefunction`
    # at import time and calls it from `run_in_executor`, which now emits a
    # DeprecationWarning per call (loud during WS resize traffic). Revert this
    # when uvloop ships a 3.14-compatible release.
    #
    # reload=True watches server.py for changes and restarts the worker. It's
    # gated on PERISCOPE_DEV=1 because the reload supervisor adds a second
    # process to the tree (worker + supervisor + multiprocessing helpers),
    # which makes the server hard to kill cleanly and produces orphans when
    # signals don't propagate. dev.sh sets PERISCOPE_DEV=1; bare
    # `uv run server.py` runs as a single process. Needs an import string
    # (not the `app` object) when reload is on so the reloader can re-import
    # the module. reload_dirs is scoped to this file's parent so edits under
    # static/ don't bounce the server — Vite handles frontend reloads in dev,
    # and direct browser hits pick up new static files without a restart.
    dev_mode = os.environ.get("PERISCOPE_DEV") == "1"
    uvicorn.run(
        "server:app",
        host="127.0.0.1",
        port=8765,
        log_level="info",
        loop="asyncio",
        reload=dev_mode,
        reload_dirs=[str(Path(__file__).parent)] if dev_mode else None,
    )
