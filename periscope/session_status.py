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
# (built_at, {pid: (rss_kb, age_s)}) — one ps snapshot serves both pid
# liveness (the key set) and the memory/uptime cycle-hint.
_claude_procs_cache: tuple[float, dict[int, tuple[int, int]]] | None = None


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


def parse_etime(s: str) -> int:
    """ps `etime` ([[dd-]hh:]mm:ss) → seconds; 0 on any malformed input."""
    s = s.strip()
    days = 0
    if "-" in s:
        d, _, s = s.partition("-")
        try:
            days = int(d)
        except ValueError:
            return 0
    parts = s.split(":")
    if not 2 <= len(parts) <= 3:
        return 0
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return 0
    while len(nums) < 3:
        nums.insert(0, 0)
    h, m, sec = nums
    return ((days * 24 + h) * 60 + m) * 60 + sec


def _claude_procs() -> dict[int, tuple[int, int]]:
    """{pid: (rss_kb, age_s)} of running `claude` processes, snapshotted once
    per poll. The key set guards against honoring a session file whose process
    has exited or whose pid was recycled; rss/etime feed the memory cycle-hint.
    `comm` is the full executable path; claude's launcher (`.../bin/claude`)
    and versioned binary (`.../share/claude/versions/<v>`) both contain
    "claude". comm may itself contain spaces, hence maxsplit=3."""
    global _claude_procs_cache
    now = time.time()
    if _claude_procs_cache and now - _claude_procs_cache[0] < _CACHE_TTL_S:
        return _claude_procs_cache[1]
    procs: dict[int, tuple[int, int]] = {}
    try:
        out = subprocess.run(
            ["ps", "-axo", "pid=,rss=,etime=,comm="],
            capture_output=True, text=True, timeout=5,
        ).stdout
        for line in out.splitlines():
            parts = line.split(None, 3)
            if len(parts) != 4:
                continue
            pid_s, rss_s, etime, comm = parts
            if "claude" in comm.lower():
                with contextlib.suppress(ValueError):
                    procs[int(pid_s)] = (int(rss_s), parse_etime(etime))
    except (OSError, subprocess.SubprocessError):
        pass
    _claude_procs_cache = (now, procs)
    return procs


def _live_claude_pids() -> set[int]:
    return set(_claude_procs())


def claude_proc_for(sid: str | None) -> dict | None:
    """Process stats for a sessionId's claude: {"pid", "rss_kb", "age_s"}, or
    None when unmapped or not a live claude. Measurement, not state — applies
    even when the status is one session_state_for doesn't trust (a bloated
    claude deserves the cycle-hint regardless of what it's doing)."""
    if not sid:
        return None
    d = _index().get(sid)
    if not d:
        return None
    try:
        pid = int(d.get("pid"))
    except (TypeError, ValueError):
        return None
    info = _claude_procs().get(pid)
    if info is None:
        return None
    return {"pid": pid, "rss_kb": info[0], "age_s": info[1]}


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
