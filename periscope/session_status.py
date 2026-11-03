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

Imports only stdlib and periscope's leaf modules (config, resources,
agent_processes, tmux), never the resolver layer, so turns.py / window_view.py
can depend on it freely."""

import contextlib
import json
import re
import subprocess
import time
from pathlib import Path

from periscope import agent_processes, config, resources, tmux

_SESSIONS_DIR = Path.home() / ".claude" / "sessions"

# Claude status -> periscope state. Only these are trusted; everything else
# (notably "shell") yields None and the caller keeps its scraped state.
_STATE_MAP = {"busy": "working", "waiting": "needs-input", "idle": "idle"}

# Per-poll caches. build_window_view calls session_state_for once per pane every
# poll; scanning ~30 tiny files per pane would be wasteful, so the session
# index is built once and reused for the brief window a single poll spans.
# Processes come from the poll's one `ps` (agent_processes.process_snapshot).
_CACHE_TTL_S = 1.0
# (built_at, {sid: file}, {pid: [files newest-first]}) — one scan of every live
# account's sessions dir answers both directions (sessionId -> state,
# pid -> current sessionId). by_pid holds a LIST because the same OS pid can
# have a file in more than one account's dir (recycled pid, account swap).
_index_cache: tuple[float, dict, dict[int, list[dict]]] | None = None
# (pane -> root pid, process snapshot, pane -> claude pid): the walk's inputs and answer.
_pane_pids_cache: tuple[dict[str, int], dict | None, dict[str, int]] | None = None

_CONFIG_DIR_RE = re.compile(r"\bCLAUDE_CONFIG_DIR=(\S+)")
_PROFILE_RE = re.compile(rf"\b{config.PROFILE_ENV_VAR}=(\S+)")
_MODEL_RE = re.compile(rf"\b{config.MODEL_ENV_VAR}=(\S+)")
_EFFORT_ARG_RE = re.compile(r"(?:^|\s)--effort[= ](\S+)")
# Config dirs are read from process env, which is immutable for a process's
# lifetime — a short TTL would re-fork `ps eww` for an answer that cannot
# change. 15s matches the cadence window_view already used.
_CONFIG_DIRS_TTL_S = 15.0
# pane id -> the ENVIRONMENT TAIL of its live claude (command already stripped).
# Cached as the raw tail rather than as parsed values so every per-pane env
# binding periscope reads — account, wrapper profile — comes out of ONE
# `ps eww` fork instead of one fork per variable.
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
            data["_dir"] = str(d.parent)  # config dir, for the per-pane tiebreak
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


def _claude_procs() -> dict[int, int]:
    """{pid: age_s} of running `claude` processes, from the poll's process
    snapshot. The key set guards against honoring a session file whose
    process has exited or whose pid was recycled; age (now minus start) feeds
    the cycle-hint."""
    now = time.time()
    return {
        p.pid: int(now - p.started_at)
        for p in (agent_processes.process_snapshot() or {}).values()
        if agent_processes.is_claude_executable(p)
    }


def _live_claude_pids() -> set[int]:
    return set(_claude_procs())


def command_lines() -> dict[int, str]:
    """pid -> command line for every process, from the poll's process
    snapshot. Empty when ps failed."""
    return {p.pid: p.argv for p in (agent_processes.process_snapshot() or {}).values()}


def _tmux_pane_pids() -> dict[str, int]:
    """tmux pane id -> the pid of the pane's root process (usually its shell),
    from the poll's list-panes snapshot. Empty on any tmux failure (no
    server, tmux not installed)."""
    try:
        rows = tmux.panes_snapshot()
    except (OSError, subprocess.SubprocessError):
        return {}
    panes: dict[str, int] = {}
    for p in rows:
        with contextlib.suppress(ValueError):
            panes[p["pane_id"]] = int(p["pane_pid"])
    return panes


def pane_claude_pids() -> dict[str, int]:
    """tmux pane id -> the pid of the claude running in it. Panes with no
    claude have no entry.

    Nearest first through each pane's process subtree, stopping at the FIRST
    claude: that is the pane's own session. Anything claude-ish below it is a
    subagent or tool child.

    Rebuilt only when the pane map or the process snapshot changes, so it is
    never older than the two snapshots it walks (pinned for a poll, else
    1 s): it feeds a pane's CURRENT session id, which rotates mid-process.
    """
    global _pane_pids_cache
    panes = _tmux_pane_pids()
    if not panes:
        return {}  # no tmux — don't fork ps
    snapshot = agent_processes.process_snapshot()
    if _pane_pids_cache and _pane_pids_cache[0] == panes and _pane_pids_cache[1] is snapshot:
        return _pane_pids_cache[2]
    found: dict[str, int] = {}
    for pane_id, root in panes.items():
        for proc in agent_processes.descendants(root, snapshot or {}):
            if proc.pid != root and agent_processes.is_claude_executable(proc):
                found[pane_id] = proc.pid
                break
    _pane_pids_cache = (panes, snapshot, found)
    return found


# UUID-only: a captured --system-prompt can quote "--resume <word>" as text.
RESUME_RE = re.compile(r"--resume[= ]([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})")


def pane_resume_ids() -> dict[str, str]:
    """tmux pane id -> the `--resume <uuid>` in its claude's argv, for panes
    launched as resumes. The uuid names the session LINEAGE: resume currently
    preserves the sid (verified 2026-08-06), but rotation on resume/compact is
    documented from a real incident — argv still says where this claude came
    from when the live sid no longer matches any recorded entry."""
    pane_pids = pane_claude_pids()
    if not pane_pids:
        return {}  # no tmux/claudes — don't fork ps for nothing
    out: dict[str, str] = {}
    cmds = command_lines()
    for pane_id, pid in pane_pids.items():
        m = RESUME_RE.search(cmds.get(pid) or "")
        if m:
            out[pane_id] = m.group(1)
    return out


def _env_by_pid(pids: list[int]) -> dict[int, str]:
    """pid -> its `ps eww` command+environment text, in ONE fork.

    Probing each pid separately costs ~165ms on a machine with ~60 claude
    processes. The caller is `pane_config_dirs`, which sits behind a 15s
    cache (_CONFIG_DIRS_TTL_S) — one fork keeps the refresh cheap when the
    cache does expire.
    """
    if not pids:
        return {}
    try:
        out = subprocess.run(
            ["ps", "eww", "-o", "pid=,command=", "-p", ",".join(str(p) for p in pids)],
            capture_output=True,
            text=True,
            check=False,
            # Same environment as the snapshot: see agent_processes.ps_env.
            env=agent_processes.ps_env(),
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


def _env_tail(cmd_only: str, with_env: str) -> str | None:
    """The ENVIRONMENT portion of one `ps eww` line, or None when it can't be
    isolated.

    `ps eww` prints the command and the environment concatenated with no
    delimiter, so searching the whole string matches any process whose COMMAND
    merely mentions a variable (observed: a grep pattern containing the name
    was picked up as if it were a real value). Strip the command — obtained
    without `e` for the same pid — and leave callers only the tail.
    """
    cmd_only, with_env = cmd_only.strip(), with_env.strip()
    if not with_env.startswith(cmd_only):
        return None
    return with_env[len(cmd_only) :]


def _pane_claude_envs() -> dict[str, str]:
    """tmux pane id -> the environment tail of its live Claude.

    Read from the running process's environment rather than from any stored
    binding: env is what the process is actually using, and it needs no state
    to stay in sync. One `ps eww` fork serves every variable periscope reads
    off a pane (see the cache comment); parse with `pane_config_dirs` /
    `pane_profiles` rather than calling this directly.
    """
    global _config_dirs_cache
    now = time.time()
    if _config_dirs_cache and now - _config_dirs_cache[0] < _CONFIG_DIRS_TTL_S:
        return _config_dirs_cache[1]
    pane_pids = pane_claude_pids()
    if not pane_pids:
        return {}  # no tmux/claudes — don't cache the empty answer
    cmds = command_lines()
    envs = _env_by_pid(list(pane_pids.values()))
    tails: dict[str, str] = {}
    for pane_id, pid in pane_pids.items():
        # A pid missing from the command table (process exited between the two
        # snapshots) is skipped rather than passed as "": an empty command
        # makes _env_tail return the whole `ps eww` string, which is the
        # false-positive it exists to prevent.
        cmd = cmds.get(pid)
        if cmd is None:
            continue
        tail = _env_tail(cmd, envs.get(pid, ""))
        if tail:
            tails[pane_id] = tail
    _config_dirs_cache = (now, tails)
    return tails


def pane_config_dirs() -> dict[str, str]:
    """tmux pane id -> the CLAUDE_CONFIG_DIR its live Claude runs under.
    Panes on the default account have no entry.

    Consumed live by window_view's account attribution and at SAVE time by
    resurrect's save-file rewrite — a shell-level `VAR=x cmd` prefix is
    consumed by the shell and never reaches argv, so the config dir is absent
    from the command resurrect captures via ps; without this lookup every
    account-B pane silently restores onto the default account.
    """
    out = {}
    for pane_id, tail in _pane_claude_envs().items():
        m = _CONFIG_DIR_RE.search(tail)
        if m:
            out[pane_id] = m.group(1)
    return out


def pane_profiles() -> dict[str, str]:
    """tmux pane id -> the `claude` wrapper profile its live Claude runs under.
    Panes on the default profile have no entry.

    The profile is only ever observable here. The wrapper also accepts a typed
    `claude full`, but it consumes that word and execs `command claude
    --settings '{...}'`, so the profile is absent from argv — fingerprinting
    the settings JSON would couple periscope to the wrapper's exact plugin
    list. The env var both periscope and the wrapper agree on is the contract.
    """
    out = {}
    for pane_id, tail in _pane_claude_envs().items():
        m = _PROFILE_RE.search(tail)
        if m:
            out[pane_id] = m.group(1)
    return out


def pane_models() -> dict[str, str]:
    """tmux pane id -> the ANTHROPIC_MODEL its live Claude was launched with.
    Panes without an override have no entry.

    This is the SPAWN-TIME choice, not necessarily the model running now — an
    in-session `/model` changes the latter and leaves env alone. The rail's
    model chip comes from the status line (`panes.STATUS_RE`) and is the truth;
    this lookup exists for resurrect's session-lost path only.
    """
    out = {}
    for pane_id, tail in _pane_claude_envs().items():
        m = _MODEL_RE.search(tail)
        if m:
            out[pane_id] = m.group(1)
    return out


def pane_efforts() -> dict[str, str]:
    """tmux pane id -> the `--effort` its live Claude was launched with. Panes
    launched without one have no entry.

    The LAUNCH level, like pane_models: /effort changes the running session and
    leaves argv alone. window_view uses it as the per-pane effort control's
    starting value until periscope sets one.
    """
    cmds = command_lines()
    out = {}
    for pane_id, pid in pane_claude_pids().items():
        m = _EFFORT_ARG_RE.search(cmds.get(pid, ""))
        if m:
            out[pane_id] = m.group(1)
    return out


def pane_session_activity() -> dict[str, tuple[str | None, float]]:
    """tmux pane id -> (sessionId, mtime) of its live claude's session file —
    the same file live_session_id_for_pane reads. A running claude rewrites
    that file on every status change, so among panes running the same session
    the newest mtime is the one actually being used."""
    # Panes first: rewrite_save_file calls this on every save, and with no
    # claude panes (or under conftest's tmux stub) there is nothing to fork ps
    # or index ~/.claude/sessions for.
    out: dict[str, tuple[str | None, float]] = {}
    for pane_id in pane_claude_pids():
        f = _live_session_file(pane_id)
        if f:
            out[pane_id] = (f.get("sessionId"), f["_mtime"])
    return out


def _live_session_file(pane_id: str) -> dict | None:
    """The session file of the pane's RUNNING claude, or None when the pane
    has no live claude / no session file for it.

    Only honored when the pid is a live claude: a leftover <pid>.json whose pid
    has been recycled would otherwise hand back an unrelated session. Across
    accounts the same pid can have one live and one stale file (a recycled pid
    overwrites in place only within its own dir) — prefer the file from the
    pane's own CLAUDE_CONFIG_DIR; newest-mtime when that dir is unknown."""
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
        return cands[0]
    want = str(Path(cfg).expanduser())
    for c in cands:
        if c["_dir"] == want:
            return c
    return cands[0]  # pane's dir had no file — newest wins


def live_session_id_for_pane(pane_id: str) -> str | None:
    """The sessionId the pane's RUNNING claude reports right now, or None when
    the pane has no live claude / no session file for it (_live_session_file)."""
    if not pane_id:
        return None
    f = _live_session_file(pane_id)
    return f.get("sessionId") if f else None


def claude_proc_for(sid: str | None) -> dict | None:
    """Process stats for a sessionId's claude: {"pid", "mem_mb", "age_s"}, or
    None when unmapped or not a live claude (its footprint unreadable: exited
    since the ps snapshot). mem_mb is its footprint. Measurement, not state —
    applies even when the status is one session_state_for doesn't trust (a
    bloated claude deserves the cycle-hint regardless of what it's doing)."""
    if not sid:
        return None
    d = _index().get(sid)
    if not d:
        return None
    try:
        pid = int(d.get("pid"))
    except (TypeError, ValueError):
        return None
    age = _claude_procs().get(pid)
    if age is None:
        return None
    mb = resources.footprint_mb(pid)
    if mb is None:
        return None
    return {"pid": pid, "mem_mb": mb, "age_s": age}


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
    return {"state": state, "status": d.get("status"), "waiting_for": d.get("waitingFor")}
