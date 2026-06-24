"""Authoritative Claude pane state from ~/.claude/sessions/<pid>.json.

Claude Code writes a small per-session status file (named by OS pid) carrying a
live `status` — busy / waiting / idle / shell — plus a best-effort `waitingFor`
string. Periscope reads it to replace TUI scraping for the working / needs-input
/ idle signal, which previously came from fragile regexes over capture-pane (see
docs/prompts/replace-tui-scraping-with-data-sources.md).

Resolution is pane-driven: a caller resolves a tmux pane to its sessionId
(periscope.turns.session_id_for_pane), then `session_state_for` maps that
sessionId to its file. We never enumerate session files and trust pid-liveness
alone — pids outlive sessions and can recycle — so a file is only honored when
its pid is a currently-running `claude` process.

Only busy/waiting/idle are mapped; `shell` and any unknown status return None so
the caller falls back to scraping. The schema is undocumented Claude Code
internals (version-tagged), so every read degrades to None on a shape change.

Imports only stdlib — no periscope.* — so it stays a leaf the resolver layer
(turns.py / window_view.py) can depend on freely."""
import contextlib
import json
import subprocess
import time
from pathlib import Path

_SESSIONS_DIR = Path.home() / ".claude" / "sessions"

# Claude status -> periscope state. Only these are trusted; everything else
# (notably "shell") yields None and the caller keeps its scraped state.
_STATE_MAP = {"busy": "working", "waiting": "needs-input", "idle": "idle"}

# Per-poll caches. build_window_view calls session_state_for once per pane every
# ~3s poll; scanning ~30 tiny files and one `ps` per pane would be wasteful, so
# both are built once and reused for the brief window a single poll spans.
_CACHE_TTL_S = 1.0
_index_cache: tuple[float, dict] | None = None        # (built_at, {sid: file})
_claude_pids_cache: tuple[float, set[int]] | None = None  # (built_at, {pid})


def _build_index() -> dict:
    idx: dict = {}
    try:
        files = list(_SESSIONS_DIR.glob("*.json"))
    except OSError:
        return idx
    for f in files:
        try:
            d = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        sid = d.get("sessionId")
        if sid:
            idx[sid] = d
    return idx


def _index() -> dict:
    global _index_cache
    now = time.time()
    if _index_cache and now - _index_cache[0] < _CACHE_TTL_S:
        return _index_cache[1]
    idx = _build_index()
    _index_cache = (now, idx)
    return idx


def _live_claude_pids() -> set[int]:
    """PIDs of running `claude` processes, snapshotted once per poll. Guards
    against honoring a session file whose process has exited or whose pid was
    recycled by something else. `comm` is the full executable path; claude's
    launcher (`.../bin/claude`) and versioned binary
    (`.../share/claude/versions/<v>`) both contain "claude"."""
    global _claude_pids_cache
    now = time.time()
    if _claude_pids_cache and now - _claude_pids_cache[0] < _CACHE_TTL_S:
        return _claude_pids_cache[1]
    pids: set[int] = set()
    try:
        out = subprocess.run(
            ["ps", "-axo", "pid=,comm="],
            capture_output=True, text=True, timeout=5,
        ).stdout
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue
            pid_s, _, comm = line.partition(" ")
            if "claude" in comm.lower():
                with contextlib.suppress(ValueError):
                    pids.add(int(pid_s))
    except (OSError, subprocess.SubprocessError):
        pass
    _claude_pids_cache = (now, pids)
    return pids


def session_state_for(sid: str | None) -> dict | None:
    """Authoritative state for a sessionId, or None when unmapped, not a live
    claude, or in a status we don't trust (shell/unknown).

    Returns {"state": <periscope state>, "status": <raw>, "waiting_for": str|None}.
    None means "no opinion — caller keeps its scraped state."
    """
    if not sid:
        return None
    d = _index().get(sid)
    if not d:
        return None
    pid = d.get("pid")
    try:
        pid_i = int(pid)
    except (TypeError, ValueError):
        return None
    if pid_i not in _live_claude_pids():
        return None
    state = _STATE_MAP.get(d.get("status"))
    if state is None:
        return None
    return {"state": state, "status": d.get("status"),
            "waiting_for": d.get("waitingFor")}
