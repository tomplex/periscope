"""tmux + subprocess wrappers.

`tmux()` — read-only invocations; swallows stderr.
`_tmux_mutate()` — side-effecting; surfaces stderr on failure.
`_run()` — generic subprocess wrapper used by git_pr and sessions routes.
`capture()` — wraps `tmux capture-pane` with -e (SGR preserved).
`deliver_input()` — writes bytes into a pane. Small inputs (keystrokes
and normal-size pastes) use a single `send-keys -H` subprocess; large
inputs fall back to load-buffer + paste-buffer over stdin to dodge ARG_MAX.
"""

import os
import re
import subprocess
import threading
import time
import uuid

from periscope import config

# Strip SGR escape sequences from captured pane text before parsing.
_ANSI_SGR_RE = re.compile(r"\x1b\[[\d;]*m")


def _tmux_argv(*args: str) -> list[str]:
    sock = os.environ.get("PERISCOPE_TMUX_SOCKET")
    return ["tmux", *(("-L", sock) if sock else ()), *args]


def tmux(*args: str) -> str:
    r = subprocess.run(_tmux_argv(*args), capture_output=True, text=True, timeout=5)
    return r.stdout


# Fields of every `list-panes -a` row. window_* and the @periscope_* window
# options resolve against the pane's window, so one call answers both the
# per-window questions (panes.list_windows reads each window's active pane) and
# the per-pane ones (panes.all_pane_ids, session_status's pane -> pid map).
PANE_FIELDS = (
    "session_name",
    "window_index",
    "window_name",
    "window_active",
    "pane_current_path",
    "@periscope_id",
    "pane_id",
    "window_activity",
    "window_id",
    "pane_pid",
    "pane_current_command",
    "@periscope_opened",
    "pane_active",
)
# \x1f (unit separator) between fields: tmux prints a tab inside a window name
# or path verbatim, which a tab separator would split out of shape.
_PANES_FMT = "\x1f".join(f"#{{{f}}}" for f in PANE_FIELDS)
_PANES_TTL_S = 1.0
_panes_lock = threading.Lock()
# (monotonic time, rows) from the last list_panes().
_panes_snapshot: tuple[float, list[dict[str, str]]] | None = None
_unparsed_logged = False


def _log_skipped_row(buf_lines: list[str]) -> None:
    global _unparsed_logged
    if not _unparsed_logged:
        from periscope.log import log

        log.warning("list-panes row skipped (a \\x1f in a window name or path, or a malformed row?): %r", buf_lines)
        _unparsed_logged = True


def list_panes() -> list[dict[str, str]]:
    """Every pane as {field: value} over PANE_FIELDS, from a fresh
    `list-panes -a` that becomes the snapshot `panes_snapshot()` serves. []
    when tmux has no server."""
    global _panes_snapshot
    raw_lines = tmux("list-panes", "-a", "-F", _PANES_FMT).split("\n")
    if raw_lines and raw_lines[-1] == "":
        raw_lines.pop()  # the trailing newline tmux's output ends with
    rows = []
    buf_lines: list[str] = []
    for line in raw_lines:
        if not buf_lines and not line:
            continue
        buf_lines.append(line)
        # A newline in a window name (only given with `new-window -n`; paths
        # and @-options reach the output raw) splits its pane's row across
        # raw lines — rejoin with "\n" until the field count is whole again.
        fields = "\n".join(buf_lines).split("\x1f")
        if len(fields) == len(PANE_FIELDS):
            rows.append(dict(zip(PANE_FIELDS, fields, strict=True)))
            buf_lines = []
        elif len(fields) > len(PANE_FIELDS):
            # Overshot: a \x1f inside a value. Not a newline — drop it and
            # resume scanning from the next raw line.
            _log_skipped_row(buf_lines)
            buf_lines = []
    if buf_lines:
        _log_skipped_row(buf_lines)  # never reached the field count
    _panes_snapshot = (time.monotonic(), rows)
    return rows


def panes_snapshot() -> list[dict[str, str]]:
    """list_panes' rows if they are under _PANES_TTL_S old, else a fresh call.
    Locked, so a refresh's fan-out threads wait for one fork instead of each
    forking."""
    with _panes_lock:
        if _panes_snapshot and time.monotonic() - _panes_snapshot[0] < _PANES_TTL_S:
            return _panes_snapshot[1]
        return list_panes()


def _run(
    cmd: list[str], cwd: str | None = None, timeout: float = 3.0, env_overrides: dict[str, str] | None = None
) -> tuple[int, str]:
    """Run `cmd`. `env_overrides`, if given, is merged over `os.environ` here
    so a caller wanting e.g. `LC_ALL=C` doesn't rebuild the whole environment
    (and can't accidentally run the child with PATH/HOME missing)."""
    env = {**os.environ, **env_overrides} if env_overrides else None
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env)
        if r.returncode != 0:
            # git and gh explain failures on stderr, with stdout empty: every
            # "git worktree add failed: " the open surfaces raised ended blank,
            # and the "already in use" / "not found" matches on `out` never hit.
            return r.returncode, (r.stderr.strip() or r.stdout.strip())
        return r.returncode, r.stdout.strip()
    except Exception:
        return -1, ""


def _tmux_mutate(*args: str) -> tuple[bool, str]:
    """Run a tmux command for its side effects. Surfaces stderr on failure
    instead of swallowing it like `tmux()` does."""
    r = subprocess.run(_tmux_argv(*args), capture_output=True, text=True, timeout=5)
    if r.returncode != 0:
        return False, (r.stderr.strip() or r.stdout.strip() or "tmux failed")
    # Every periscope-initiated structural change (new/kill/rename window,
    # kill-session) flows through here — kick the state hub so the dashboard
    # reflects it on the next event-loop tick instead of waiting out the
    # steady interval. Thread-safe + a no-op when the hub isn't running.
    from periscope import state_hub

    state_hub.kick()
    return True, r.stdout.strip()


def env_args(
    config_dir: str | None,
    profile: str | None = None,
    model: str | None = None,
) -> list[str]:
    """`-e VAR=...` args for new-window/new-session, or [].

    Three bindings ride here: CLAUDE_CONFIG_DIR (which subscription the pane
    bills), the wrapper profile (which plugin set it runs under), and the
    model override (ANTHROPIC_MODEL). Effort is deliberately NOT one of them —
    see config.EFFORT_LEVELS.

    Set on the window rather than prefixed onto the command string so the
    binding lives in the pane's process environment: a user re-running
    `claude` by hand in that pane keeps the same account and profile, and
    resurrect reads both back off the live process at save time.
    """
    args: list[str] = []
    if config_dir:
        args += ["-e", f"CLAUDE_CONFIG_DIR={config_dir}"]
    if profile:
        args += ["-e", f"{config.PROFILE_ENV_VAR}={profile}"]
    if model:
        args += ["-e", f"{config.MODEL_ENV_VAR}={model}"]
    return args


def scrub_session_env(session: str) -> bool:
    """Unset the env_args vars from a SESSION's environment. True on success.

    `new-session -e` sets the session env, not just the first window's — so
    every later window in periscope's single shared session would inherit the
    first window's account, silently billing account-A panes to account B (and,
    for the profile, silently running them on the wrong plugin set). The first
    window's shell has already forked with the values, so scrubbing immediately
    after creation keeps that pane correct and leaves the session clean for the
    next one.

    A failure here is the exact outcome this feature exists to prevent, and its
    blast radius is every LATER pane rather than this one — so it must never be
    silent. `_tmux_mutate` only returns stderr, it does not log, so log here.
    """
    ok = True
    for var in ("CLAUDE_CONFIG_DIR", config.PROFILE_ENV_VAR, config.MODEL_ENV_VAR):
        got, msg = _tmux_mutate("set-environment", "-t", f"={session}", "-u", var)
        if not got:
            ok = False
            from periscope.log import log

            log.error(
                "FAILED to scrub %s from session %s (%s) — later panes in this session may silently inherit it",
                var,
                session,
                msg,
            )
    return ok


def capture(target: str, lines: int = 100) -> str:
    # -e preserves SGR escapes; parse_pane strips them for content parsing
    # but uses raw prompt-line color info to filter ghost-text input.
    return tmux("capture-pane", "-t", target, "-p", "-e", "-S", f"-{lines}")


def pane_meta(target: str) -> tuple[str, str]:
    """`(window_name, cwd)` for a tmux target via display-message. Raises on
    tmux failure — callers decide whether to swallow it (pane detail) or
    surface it (auto-rename). For `(pane_id, cwd)` see turns.py, which queries
    a different field."""
    meta = tmux(
        "display-message",
        "-t",
        target,
        "-p",
        "#{window_name}\t#{pane_current_path}",
    ).strip()
    window_name, _, cwd = meta.partition("\t")
    return window_name, cwd


def window_identity(target: str) -> tuple[str, str, str]:
    """`(@periscope_id, pane_id, window_id)` for a tmux target, "" each on
    failure. Synthetic single-window rows fed to resolve_pids MUST carry the
    real stamp: a hard-coded pid_raw="" reads as an unstamped window, so every
    call rebinds (log spam at detail-pane poll cadence) and last_seen.pane_id
    flaps None↔%N against the real poll, dirtying state.json on each flip
    (observed 2026-08-06, post session-keyed-identity deploy)."""
    meta = tmux(
        "display-message",
        "-t",
        target,
        "-p",
        "#{@periscope_id}\t#{pane_id}\t#{window_id}",
    )
    # rstrip newline ONLY — .strip() would eat the leading tab of an
    # unstamped window's empty first field and shift every field left.
    parts = (meta.rstrip("\n").split("\t") + ["", "", ""])[:3]
    return parts[0], parts[1], parts[2]


# Threshold below which `send-keys -H` is used (single subprocess, fast path).
# Above this we go via load-buffer + paste-buffer to avoid argv bloat — each
# input byte becomes a 2-char hex arg, so ARG_MAX caps the -H path well below
# the OS limit. 4 KiB covers single keystrokes and normal clipboard pastes.
_SEND_KEYS_H_MAX = 4096


def deliver_input(target: str, text: str) -> None:
    """Pipe raw bytes into a pane.

    Fast path (the common case — keystrokes from xterm.js, short escape
    sequences, normal pastes): one `send-keys -H` subprocess with each
    byte as a hex arg. Hex args are never parsed as commands, so the
    standalone-`;` problem that motivated the old load-buffer dance
    doesn't apply here. Halves per-keystroke fork+exec cost compared to
    load-buffer + paste-buffer.

    Fallback (inputs above `_SEND_KEYS_H_MAX`): load-buffer + paste-buffer
    over stdin. Slower (two subprocesses) but unbounded in size — needed
    for multi-KB pastes that would otherwise overflow ARG_MAX.
    """
    encoded = text.encode("utf-8")
    if len(encoded) <= _SEND_KEYS_H_MAX:
        hexes = [f"{b:02x}" for b in encoded]
        subprocess.run(
            ["tmux", "send-keys", "-t", target, "-H", *hexes],
            check=False,
            timeout=5,
        )
        return
    buf = f"wd-in-{uuid.uuid4().hex[:8]}"
    subprocess.run(
        ["tmux", "load-buffer", "-b", buf, "-"],
        input=text,
        text=True,
        check=False,
        timeout=5,
    )
    subprocess.run(
        ["tmux", "paste-buffer", "-d", "-b", buf, "-t", target],
        check=False,
        timeout=5,
    )
