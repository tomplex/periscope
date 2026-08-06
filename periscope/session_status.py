"""Authoritative Claude pane state from each account's sessions/<pid>.json —
~/.claude/sessions plus every live CLAUDE_CONFIG_DIR's sessions/ (see
`_session_dirs`).

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

The same files answer the reverse question: `live_session_id_for_pane` walks a
pane's process subtree to its claude pid and reads back the sessionId that
process reports RIGHT NOW. That is what makes it the authority over the
recorded pane_sessions row — see the comment on `session_id_for_pane`.

Only busy/waiting/idle are mapped; `shell` and any unknown status return None so
the caller falls back to scraping. The schema is undocumented Claude Code
internals (version-tagged), so every read degrades to None on a shape change.

Imports only stdlib — no periscope.* — so it stays a leaf the resolver layer
(turns.py / window_view.py) can depend on freely."""
import contextlib
import json
import re
import subprocess
import time
from collections import deque
from pathlib import Path

_SESSIONS_DIR = Path.home() / ".claude" / "sessions"

# Claude status -> periscope state. Only these are trusted; everything else
# (notably "shell") yields None and the caller keeps its scraped state.
_STATE_MAP = {"busy": "working", "waiting": "needs-input", "idle": "idle"}

# Per-poll caches. build_window_view calls session_state_for once per pane every
# ~3s poll; scanning ~30 tiny files and one `ps` per pane would be wasteful, so
# both are built once and reused for the brief window a single poll spans.
_CACHE_TTL_S = 1.0
# (built_at, {sid: file}, {pid: [files newest-first]}) — one scan of every live
# account's sessions dir answers both directions (sessionId -> state,
# pid -> current sessionId). by_pid holds a LIST because the same OS pid can
# have a file in more than one account's dir (recycled pid, account swap).
_index_cache: tuple[float, dict, dict[int, list[dict]]] | None = None
# (built_at, {pid: (rss_kb, age_s)}) — one ps snapshot serves both pid
# liveness (the key set) and the memory/uptime cycle-hint.
_claude_procs_cache: tuple[float, dict[int, tuple[int, int]]] | None = None
_proc_table_cache: tuple[float, tuple[dict[int, list[int]], dict[int, str]]] | None = None
_pane_pids_cache: tuple[float, dict[str, int]] | None = None

_CONFIG_DIR_RE = re.compile(r"\bCLAUDE_CONFIG_DIR=(\S+)")
# Config dirs are read from process env, which is immutable for a process's
# lifetime — a short TTL would re-fork `ps eww` for an answer that cannot
# change. 15s matches the cadence window_view already used.
_CONFIG_DIRS_TTL_S = 15.0
_config_dirs_cache: tuple[float, dict[str, str]] | None = None


def _session_dirs() -> list[Path]:
    """Every live account's sessions dir. The default account first, then each
    distinct CLAUDE_CONFIG_DIR a live claude runs under — panes on a secondary
    account write <config_dir>/sessions/<pid>.json, invisible to a single-dir
    scan (the account-swap blindness in the 2026-08-05 identity merge)."""
    dirs = [_SESSIONS_DIR]
    for cfg in dict.fromkeys(pane_config_dirs().values()):
        p = Path(cfg).expanduser() / "sessions"
        if p != _SESSIONS_DIR:
            dirs.append(p)
    return dirs


def _build_index() -> tuple[dict, dict[int, list[dict]]]:
    by_sid: dict = {}
    by_pid: dict[int, list[dict]] = {}
    for d in _session_dirs():
        try:
            files = list(d.glob("*.json"))
        except OSError:
            continue
        for f in files:
            try:
                data = json.loads(f.read_text())
                mtime = f.stat().st_mtime
            except (OSError, ValueError):
                continue
            data["_dir"] = str(d.parent)   # config dir, for the per-pane tiebreak
            data["_mtime"] = mtime
            sid = data.get("sessionId")
            if sid:
                by_sid[sid] = data
            with contextlib.suppress(TypeError, ValueError):
                by_pid.setdefault(int(data.get("pid")), []).append(data)
    for cands in by_pid.values():
        cands.sort(key=lambda c: c["_mtime"], reverse=True)
    return by_sid, by_pid


def _indexes() -> tuple[dict, dict[int, list[dict]]]:
    global _index_cache
    now = time.time()
    if _index_cache and now - _index_cache[0] < _CACHE_TTL_S:
        return _index_cache[1], _index_cache[2]
    by_sid, by_pid = _build_index()
    _index_cache = (now, by_sid, by_pid)
    return by_sid, by_pid


def _index() -> dict:
    return _indexes()[0]


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


def proc_table() -> tuple[dict[int, list[int]], dict[int, str]]:
    """(ppid -> child pids, pid -> command) for every process. Empty on any ps
    failure. Snapshotted for _CACHE_TTL_S so one poll's worth of subtree walks
    costs a single fork."""
    global _proc_table_cache
    now = time.time()
    if _proc_table_cache and now - _proc_table_cache[0] < _CACHE_TTL_S:
        return _proc_table_cache[1]
    kids: dict[int, list[int]] = {}
    cmds: dict[int, str] = {}
    try:
        out = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,command="],
            capture_output=True, text=True, check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return kids, cmds   # not cached: a transient ps failure shouldn't stick
    for line in out.splitlines():
        bits = line.split(maxsplit=2)
        if len(bits) < 3:
            continue
        try:
            pid, ppid = int(bits[0]), int(bits[1])
        except ValueError:
            continue
        kids.setdefault(ppid, []).append(pid)
        cmds[pid] = bits[2]
    _proc_table_cache = (now, (kids, cmds))
    return kids, cmds


def _tmux_pane_pids() -> dict[str, int]:
    """tmux pane id -> the pid of the pane's root process (usually its shell).
    Empty on any tmux failure (no server, tmux not installed)."""
    try:
        out = subprocess.run(
            ["tmux", "list-panes", "-a", "-F", "#{pane_id}\t#{pane_pid}"],
            capture_output=True, text=True, check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    panes: dict[str, int] = {}
    for line in out.splitlines():
        pane_id, tab, pid_raw = line.partition("\t")
        if not tab:
            continue
        with contextlib.suppress(ValueError):
            panes[pane_id] = int(pid_raw)
    return panes


def pane_claude_pids() -> dict[str, int]:
    """tmux pane id -> the pid of the claude running in it. Panes with no
    claude have no entry.

    Breadth-first through each pane's process subtree, stopping at the FIRST
    claude: that is the pane's own session. Anything claude-ish below it is a
    subagent or tool child.

    Cached for _CACHE_TTL_S — the same per-poll window the rest of this module
    uses. Deliberately NOT the 15s of window_view's account scan: that scan
    reads process env, which is immutable for a process's lifetime, whereas
    this map feeds a pane's CURRENT session id — which rotates mid-process —
    and a pid can be recycled by an unrelated claude. One /api/state poll
    resolves ~20 panes, so a one-second window already collapses the cost to
    two forks per poll; a longer one buys nothing but staleness.
    """
    global _pane_pids_cache
    now = time.time()
    if _pane_pids_cache and now - _pane_pids_cache[0] < _CACHE_TTL_S:
        return _pane_pids_cache[1]
    panes = _tmux_pane_pids()
    if not panes:
        return {}   # no tmux — don't fork ps, and don't cache the empty answer
    kids, cmds = proc_table()
    found: dict[str, int] = {}
    for pane_id, root in panes.items():
        seen: set[int] = set()
        queue = deque([root])
        while queue:
            pid = queue.popleft()
            if pid in seen:
                continue
            seen.add(pid)
            if pid != root and "claude" in cmds.get(pid, ""):
                found[pane_id] = pid
                break
            queue.extend(kids.get(pid, ()))
    _pane_pids_cache = (now, found)
    return found


_RESUME_RE = re.compile(r"--resume[= ]([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})")


def pane_resume_ids() -> dict[str, str]:
    """tmux pane id -> the `--resume <uuid>` in its claude's argv, for panes
    launched as resumes. The uuid names the session LINEAGE: resume currently
    preserves the sid (verified 2026-08-06), but rotation on resume/compact is
    documented from a real incident — argv still says where this claude came
    from when the live sid no longer matches any recorded entry."""
    pane_pids = pane_claude_pids()
    if not pane_pids:
        return {}   # no tmux/claudes — don't fork ps for nothing
    out: dict[str, str] = {}
    _, cmds = proc_table()
    for pane_id, pid in pane_pids.items():
        m = _RESUME_RE.search(cmds.get(pid) or "")
        if m:
            out[pane_id] = m.group(1)
    return out


def _env_by_pid(pids: list[int]) -> dict[int, str]:
    """pid -> its `ps eww` command+environment text, in ONE fork.

    Probing each pid separately costs ~165ms on a machine with ~60 claude
    processes, and window_view runs this on every /api/state poll — one fork
    keeps it off the dashboard's hot path.
    """
    if not pids:
        return {}
    try:
        out = subprocess.run(
            ["ps", "eww", "-o", "pid=,command=", "-p", ",".join(str(p) for p in pids)],
            capture_output=True, text=True, check=False,
        ).stdout
    except OSError:
        return {}
    envs: dict[int, str] = {}
    for line in out.splitlines():
        bits = line.split(maxsplit=1)
        if len(bits) != 2:
            continue
        try:
            envs[int(bits[0])] = bits[1]
        except ValueError:
            continue
    return envs


def _config_dir_from_ps(cmd_only: str, with_env: str) -> str | None:
    """Extract CLAUDE_CONFIG_DIR from `ps eww` output.

    `ps eww` prints the command and the environment concatenated with no
    delimiter, so searching the whole string matches any process whose COMMAND
    merely mentions the variable (observed: a grep pattern containing the name
    was picked up as if it were a real value). Strip the command — obtained
    without `e` for the same pid — and search only the environment tail.
    """
    cmd_only, with_env = cmd_only.strip(), with_env.strip()
    if not with_env.startswith(cmd_only):
        return None
    env_tail = with_env[len(cmd_only):]
    m = _CONFIG_DIR_RE.search(env_tail)
    return m.group(1) if m else None


def pane_config_dirs() -> dict[str, str]:
    """tmux pane id -> the CLAUDE_CONFIG_DIR its live Claude runs under.

    Read from the running process's environment rather than from any stored
    binding: env is what the process is actually using, and it needs no state to
    stay in sync. Panes on the default account have no entry.

    Consumed live by window_view's account attribution and at SAVE time by
    resurrect's save-file rewrite — a shell-level `VAR=x cmd` prefix is
    consumed by the shell and never reaches argv, so the config dir is absent
    from the command resurrect captures via ps; without this lookup every
    account-B pane silently restores onto the default account.
    """
    global _config_dirs_cache
    now = time.time()
    if _config_dirs_cache and now - _config_dirs_cache[0] < _CONFIG_DIRS_TTL_S:
        return _config_dirs_cache[1]
    pane_pids = pane_claude_pids()
    if not pane_pids:
        return {}   # no tmux/claudes — don't cache the empty answer
    _, cmds = proc_table()
    envs = _env_by_pid(list(pane_pids.values()))
    dirs: dict[str, str] = {}
    for pane_id, pid in pane_pids.items():
        # A pid missing from the command table (process exited between the two
        # snapshots) is skipped rather than passed as "": an empty command
        # makes _config_dir_from_ps search the whole `ps eww` string, which is
        # the false-positive it exists to prevent.
        cmd = cmds.get(pid)
        if cmd is None:
            continue
        cfg = _config_dir_from_ps(cmd, envs.get(pid, ""))
        if cfg:
            dirs[pane_id] = cfg
    _config_dirs_cache = (now, dirs)
    return dirs


def live_session_id_for_pane(pane_id: str) -> str | None:
    """The sessionId the pane's RUNNING claude reports right now, or None when
    the pane has no live claude / no session file for it.

    Only honored when the pid is a live claude: a leftover <pid>.json whose pid
    has been recycled would otherwise hand back an unrelated session. Across
    accounts the same pid can have one live and one stale file (a recycled pid
    overwrites in place only within its own dir) — prefer the file from the
    pane's own CLAUDE_CONFIG_DIR; newest-mtime when that dir is unknown."""
    if not pane_id:
        return None
    pid = pane_claude_pids().get(pane_id)
    if pid is None or pid not in _live_claude_pids():
        return None
    cands = _indexes()[1].get(pid)
    if not cands:
        return None
    cfg = pane_config_dirs().get(pane_id)
    if cfg is None:
        # pane_config_dirs has entries only for panes on a NON-default account
        # (env var absent → no entry) — so "dir unknown" covers every
        # default-account pane. Newest mtime is the live file there: a running
        # claude rewrites its session file; a stale one never does.
        return cands[0].get("sessionId")
    want = str(Path(cfg).expanduser())
    for c in cands:
        if c["_dir"] == want:
            return c.get("sessionId")
    return cands[0].get("sessionId")   # pane's dir had no file — newest wins


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
