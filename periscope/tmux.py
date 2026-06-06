"""tmux + subprocess wrappers.

`tmux()` — read-only invocations; swallows stderr.
`_tmux_mutate()` — side-effecting; surfaces stderr on failure.
`_run()` — generic subprocess wrapper used by git_pr and sessions routes.
`capture()` — wraps `tmux capture-pane` with -e (SGR preserved).
`deliver_input()` — writes bytes into a pane. Small inputs (keystrokes
and normal-size pastes) use a single `send-keys -H` subprocess; large
inputs fall back to load-buffer + paste-buffer over stdin to dodge ARG_MAX.
"""

import re
import subprocess
import uuid

# Strip SGR escape sequences from captured pane text before parsing.
_ANSI_SGR_RE = re.compile(r"\x1b\[[\d;]*m")
_FG_COLOR_RE = re.compile(r"\x1b\[38(?:;\d+)+m")


def tmux(*args: str) -> str:
    r = subprocess.run(
        ["tmux", *args], capture_output=True, text=True, timeout=5
    )
    return r.stdout


def _run(cmd: list[str], cwd: str | None = None, timeout: float = 3.0) -> tuple[int, str]:
    try:
        r = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout
        )
        return r.returncode, r.stdout.strip()
    except Exception:
        return -1, ""


def _tmux_mutate(*args: str) -> tuple[bool, str]:
    """Run a tmux command for its side effects. Surfaces stderr on failure
    instead of swallowing it like `tmux()` does."""
    r = subprocess.run(
        ["tmux", *args], capture_output=True, text=True, timeout=5
    )
    if r.returncode != 0:
        return False, (r.stderr.strip() or r.stdout.strip() or "tmux failed")
    return True, r.stdout.strip()


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
        "display-message", "-t", target, "-p",
        "#{window_name}\t#{pane_current_path}",
    ).strip()
    window_name, _, cwd = meta.partition("\t")
    return window_name, cwd


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
            check=False, timeout=5,
        )
        return
    buf = f"wd-in-{uuid.uuid4().hex[:8]}"
    subprocess.run(
        ["tmux", "load-buffer", "-b", buf, "-"],
        input=text, text=True, check=False, timeout=5,
    )
    subprocess.run(
        ["tmux", "paste-buffer", "-d", "-b", buf, "-t", target],
        check=False, timeout=5,
    )
