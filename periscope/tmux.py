"""tmux + subprocess wrappers.

`tmux()` — read-only invocations; swallows stderr.
`_tmux_mutate()` — side-effecting; surfaces stderr on failure.
`_run()` — generic subprocess wrapper used by git_pr and sessions routes.
`capture()` — wraps `tmux capture-pane` with -e (SGR preserved).
`deliver_input()` — writes bytes into a pane via load-buffer + paste-buffer
to dodge tmux's argv parser eating semicolons that send-keys would lose.
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


def deliver_input(target: str, text: str) -> None:
    """Pipe raw bytes into a pane via tmux load-buffer + paste-buffer.

    Used rather than `send-keys -l` because tmux's argv parser treats a
    standalone `;` argument as a command separator — when xterm.js forwards
    a single semicolon keystroke as one WS message, send-keys silently
    drops it. Stdin avoids that entire parsing path.
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
