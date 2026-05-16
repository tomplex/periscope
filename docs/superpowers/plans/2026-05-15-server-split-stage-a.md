# Server split — Stage A (Peels 0–4) implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extract `server.py`'s logging/pidfile/config infrastructure, tmux subprocess wrappers, the channels (MCP) subsystem, and the `state.json` store into a `periscope/` package. After Stage A, `server.py` drops by ~1185 of 3543 lines (~33%) but still owns `app = FastAPI()`, the lifespan, all route handlers, all pane parsing, git/PR/LGTM/usage helpers, and the `__main__` entry block. Stage B (separate plan) covers panes/pids, LGTM, git_pr/usage/rename_ai, the routes split, and the final `app` move.

**Architecture:** Each peel is one atomic commit on `main`. The uvicorn import target stays `"server:app"` throughout Stage A — `server.py` keeps `app = FastAPI()` and the lifespan, and imports moved symbols back from `periscope.*`. Nothing in `periscope/` does `from server import ...` at module top (would trigger a double-import of `server.py` under the name `server` separate from `__main__`, see spec §"Critical: don't trigger a double-import"). The single exception is the channels-peel bridge: two helpers in `periscope/channels.py` do **function-local** `from server import ...` to reach `list_windows` / `_attach_git_then_resolve_pids` / `note_focus` / `note_action` which still live in `server.py` through Stage A. These bridges resolve to clean module-top imports in Stage B's Peel 5.

**Tech Stack:** Python 3.11+, FastAPI, uvicorn, mcp (1.27.\*). No new dependencies. No new tests in this stage; the verification gate is `uv run server.py` boot + `uv run test_parse_pane.py` + a targeted manual smoke per peel.

**Reference docs:**
- Design spec: `docs/superpowers/specs/2026-05-15-server-split-design.md`
- Periscope conventions: `CLAUDE.md`

---

## Verification gate (run after every peel)

```sh
# 1. Boot — must reach "Application startup complete" without exceptions.
uv run server.py &
SERVER_PID=$!
sleep 3
curl -fsS http://127.0.0.1:8765/api/state > /dev/null && echo "API OK" || echo "API FAIL"
kill $SERVER_PID
wait $SERVER_PID 2>/dev/null

# 2. Regex regression test.
uv run test_parse_pane.py
# Expected: "all PASS" tail line, exit 0.

# 3. Peel-specific manual smoke (see each peel below).
```

If any of these fails: do NOT commit. Revert the working tree, diagnose, retry.

---

## File structure (end of Stage A)

```
periscope/                          # NEW — created in Peel 0
├── __init__.py                     # NEW (empty)
├── __main__.py                     # NEW (empty placeholder)
├── config.py                       # NEW (Peel 1) — paths + constants
├── log.py                          # NEW (Peel 1) — logging + _bg/_task
├── pidfile.py                      # NEW (Peel 1) — single-instance reclaim
├── tmux.py                         # NEW (Peel 2) — subprocess wrappers
├── channels.py                     # NEW (Peel 3) — MCP server + tools
├── store.py                        # NEW (Peel 4) — state.json layer
└── routes/                         # NEW (Peel 0)
    └── __init__.py                 # NEW (empty)

server.py                           # MODIFIED in every peel
                                    # End of Stage A: ~2360 lines
                                    # (down from 3543), still has:
                                    #   - PEP-723 header
                                    #   - app = FastAPI(lifespan=lifespan)
                                    #   - lifespan() function
                                    #   - All routes (Peel 8 moves them)
                                    #   - All pane introspection (Peel 5)
                                    #   - LGTM helpers (Peel 6)
                                    #   - git_pr / usage / rename_ai (Peel 7)
                                    #   - The __main__ block (Peel 9 moves
                                    #     app+lifespan, shim stays here)
```

---

## Peel 0: scaffold

**Goal:** Create the empty `periscope/` package skeleton so later peels have somewhere to land. No content moves. No change to `server.py`.

**Files:**
- Create: `periscope/__init__.py`
- Create: `periscope/__main__.py`
- Create: `periscope/routes/__init__.py`

### Task 0.1: Create empty package

- [ ] **Step 1: Create the four empty files**

```sh
mkdir -p /Users/tom/dev/periscope/periscope/routes
touch /Users/tom/dev/periscope/periscope/__init__.py
touch /Users/tom/dev/periscope/periscope/routes/__init__.py
```

For `periscope/__main__.py`, write:

```python
"""Entry point for `python -m periscope`. After Peel 9 this delegates to
periscope.app:app via uvicorn; in Stage A it points users at `uv run server.py`
because `app` still lives in server.py."""

import sys

print(
    "periscope: during the server-split migration, use `uv run server.py` "
    "from the repo root instead of `python -m periscope`.",
    file=sys.stderr,
)
sys.exit(2)
```

- [ ] **Step 2: Verify the package imports cleanly**

```sh
cd /Users/tom/dev/periscope
uv run python -c "import periscope; import periscope.routes; print('OK')"
```

Expected output: `OK` (no ImportError).

- [ ] **Step 3: Run the verification gate** (see top of plan)

Boot + `test_parse_pane.py`. No manual smoke needed — nothing functional changed.

- [ ] **Step 4: Commit**

```sh
git add periscope/
git commit -m "split: scaffold empty periscope/ package + routes/ subpackage"
```

---

## Peel 1: infra — `config.py`, `log.py`, `pidfile.py`

**Goal:** Move logging setup, `_bg`/`_task` background-task wrappers, pidfile reclaim, and the two cross-cutting constants (`STATIC`, `MCP_SOCKET_PATH`) into `periscope/`. `server.py` imports them back.

**Files:**
- Create: `periscope/config.py`
- Create: `periscope/log.py`
- Create: `periscope/pidfile.py`
- Modify: `server.py` (delete moved lines; add imports; update internal call sites if needed)

### Task 1.1: Create `periscope/config.py`

- [ ] **Step 1: Write `periscope/config.py`**

```python
"""Cross-cutting paths and constants. Imported widely; should never import
from any other periscope.* module — keep this a leaf."""

from pathlib import Path

# Static asset root for the FastAPI app.mount("/", StaticFiles(...)) call.
# Computed relative to the repo root, NOT to this file — server.py lives at
# the repo root, periscope/ is a subdirectory.
STATIC = Path(__file__).parent.parent / "static"

# Unix socket the in-process MCP server listens on. channel_shim.py connects
# here from each Claude pane. Lifespan unlinks this on shutdown; channels.py
# must never unlink it (see spec §"MCP_SOCKET_PATH cleanup").
MCP_SOCKET_PATH = "/tmp/periscope-mcp.sock"
```

- [ ] **Step 2: Verify import**

```sh
uv run python -c "from periscope.config import STATIC, MCP_SOCKET_PATH; print(STATIC); print(MCP_SOCKET_PATH)"
```

Expected: prints `/Users/tom/dev/periscope/static` and `/tmp/periscope-mcp.sock`.

### Task 1.2: Create `periscope/log.py`

- [ ] **Step 1: Write `periscope/log.py`**

Source: copy `server.py:41–101` verbatim, drop the section banners, fix imports.

```python
"""Logging + background-task crash capture.

Logging: rotating file at ~/.config/periscope/periscope.log + stderr. Set up
at import time so module-init, lifespan, and handlers all land in the same
sink. Without this, background-thread crashes go nowhere and "the server's
flakey" is uninvestigable.

_bg / _task: wrap fire-and-forget threads/coroutines so uncaught exceptions
hit the log instead of vanishing.
"""

import asyncio
import logging
import logging.handlers
import os
import sys
import threading
from pathlib import Path


def _log_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "periscope" / "periscope.log"


_LOG_PATH = _log_path()
_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        logging.handlers.RotatingFileHandler(
            _LOG_PATH, maxBytes=2_000_000, backupCount=3
        ),
        logging.StreamHandler(sys.stderr),
    ],
)
log = logging.getLogger("periscope")


def _bg(name: str, fn, *args, **kwargs) -> threading.Thread:
    """Start a daemon thread that logs any uncaught exception."""
    def wrapped():
        try:
            fn(*args, **kwargs)
        except Exception:
            log.exception("background thread %s crashed", name)
    t = threading.Thread(target=wrapped, daemon=True, name=name)
    t.start()
    return t


def _task(coro, name: str) -> asyncio.Task:
    """Schedule an asyncio task with a done-callback that logs crashes."""
    t = asyncio.create_task(coro, name=name)

    def _done(task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            log.error("task %s crashed", name, exc_info=exc)

    t.add_done_callback(_done)
    return t
```

- [ ] **Step 2: Verify import + idempotency**

```sh
uv run python -c "from periscope.log import log, _bg, _task; log.info('test log line'); print('OK')"
```

Expected: prints `test log line` on stderr (timestamp format), prints `OK`. Verify it appended to `~/.config/periscope/periscope.log`:

```sh
tail -1 ~/.config/periscope/periscope.log
```

Expected: shows the test line.

### Task 1.3: Create `periscope/pidfile.py`

- [ ] **Step 1: Write `periscope/pidfile.py`**

Source: copy `server.py:115–175` verbatim, drop banner, fix imports.

```python
"""Pidfile / single-instance reclaim.

Periscope's uvicorn reload supervisor + worker dance plus Claude-driven
one-off uvicorn invocations mean orphan periscopes accumulate on adjacent
ports. Reclaim solves the common case: starting periscope kicks out the
previous instance so `uv run server.py` is idempotent.

Called from server.py's __main__ block BEFORE uvicorn binds the port.
"""

import os
import signal
import subprocess
import time
from pathlib import Path

from periscope.log import log


def _pidfile_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "periscope" / "periscope.pid"


def _pid_is_periscope(pid: int) -> bool:
    """True if `pid` is alive and looks like a periscope process. Checks
    the command line for 'server.py' to avoid SIGTERMing some unrelated
    process that happens to have inherited an old pid."""
    try:
        out = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True, text=True, timeout=2.0,
        )
    except (subprocess.SubprocessError, OSError):
        return False
    if out.returncode != 0:
        return False
    return "server.py" in out.stdout


def _reclaim_existing_instance() -> None:
    """If the pidfile points at a live periscope, SIGTERM it (escalate to
    SIGKILL after 3s) so we can bind the port cleanly."""
    path = _pidfile_path()
    try:
        prev = int(path.read_text().strip())
    except (OSError, ValueError):
        return
    if prev == os.getpid() or not _pid_is_periscope(prev):
        return
    log.info("reclaiming previous periscope instance pid=%d", prev)
    try:
        os.kill(prev, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.time() + 3.0
    while time.time() < deadline:
        if not _pid_is_periscope(prev):
            return
        time.sleep(0.1)
    log.warning("pid=%d ignored SIGTERM; sending SIGKILL", prev)
    try:
        os.kill(prev, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _write_pidfile() -> None:
    path = _pidfile_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(os.getpid()))


def _remove_pidfile() -> None:
    path = _pidfile_path()
    try:
        if path.read_text().strip() == str(os.getpid()):
            path.unlink()
    except (OSError, ValueError):
        pass
```

- [ ] **Step 2: Verify import**

```sh
uv run python -c "from periscope.pidfile import _reclaim_existing_instance, _write_pidfile, _remove_pidfile; print('OK')"
```

Expected: prints `OK`.

### Task 1.4: Strip moved code from `server.py` and add imports

- [ ] **Step 1: Delete the moved blocks**

Using `Edit`, delete from `server.py`:
- Lines 41–100 (Logging banner through end of `_task` function).
- Lines 103–176 (Pidfile banner through end of `_remove_pidfile`).
- Line 218 (`STATIC = Path(__file__).parent / "static"`). Do NOT delete line 217 — that's `app = FastAPI(lifespan=lifespan)`.
- Line 350 (`MCP_SOCKET_PATH = "/tmp/periscope-mcp.sock"` — currently inside the channels block but logically config).

For STATIC and MCP_SOCKET_PATH, the cleanest move is to Edit out just those two lines and add them to the import list (see step 2).

- [ ] **Step 2: Add the imports near the top of `server.py`**

After the existing `from pydantic import BaseModel` line (currently around line 32), add:

```python
from periscope.config import STATIC, MCP_SOCKET_PATH
from periscope.log import log, _bg, _task
from periscope.pidfile import (
    _reclaim_existing_instance,
    _write_pidfile,
    _remove_pidfile,
)
```

`_reclaim_existing_instance`, `_write_pidfile`, `_remove_pidfile` are referenced inside the `if __name__ == "__main__":` block at the bottom of `server.py`, so the imports are live — keep them.

- [ ] **Step 3: Remove the now-unused stdlib imports**

`server.py`'s top block currently imports `logging`, `logging.handlers`, and `signal` for the moved code. Check the remaining uses:

```sh
grep -n "logging\.\|signal\." /Users/tom/dev/periscope/server.py | head -20
```

If `logging.handlers` has no remaining use, drop it. `signal` is still used by `_on_sigterm` in `__main__`, keep it. `logging` is still used by `log = logging.getLogger(...)` — wait, that's now in `periscope.log`. Drop the bare `logging` import if no other uses. Keep `subprocess`, `time`, etc.

- [ ] **Step 4: Run the verification gate**

Boot + `test_parse_pane.py`. Confirm `~/.config/periscope/periscope.log` still gets a startup line.

- [ ] **Step 5: Manual smoke — pidfile reclaim**

```sh
# Boot periscope in background
uv run server.py &
FIRST_PID=$!
sleep 2

# Boot a second one — it should reclaim the first.
uv run server.py &
SECOND_PID=$!
sleep 4

# First should be dead.
kill -0 $FIRST_PID 2>/dev/null && echo "FAIL: first still alive" || echo "OK: first reclaimed"

# Cleanup
kill $SECOND_PID
wait 2>/dev/null
```

Expected: `OK: first reclaimed`.

- [ ] **Step 6: Commit**

```sh
git add periscope/config.py periscope/log.py periscope/pidfile.py server.py
git commit -m "split: extract config/log/pidfile to periscope/ (Peel 1)"
```

---

## Peel 2: `tmux.py` — subprocess wrappers

**Goal:** Move all tmux/subprocess helpers into one module. `server.py` imports them back.

**Files:**
- Create: `periscope/tmux.py`
- Modify: `server.py`

### Task 2.1: Create `periscope/tmux.py`

- [ ] **Step 1: Write `periscope/tmux.py`**

Source: `server.py:1080–1085` (`tmux`), `1280–1289` (`_run`), `1806–1807` (the two ANSI regexes), `1994–1998` (`capture`), `2000–2018` (`deliver_input`), `2578–2587` (`_tmux_mutate`).

```python
"""tmux + subprocess wrappers.

`tmux()` — read-only invocations; swallows stderr (most callers want stdout
or nothing). `_tmux_mutate()` — side-effecting invocations; surfaces stderr
on failure. `_run()` — generic subprocess wrapper used by git_pr and
sessions routes; logically subprocess plumbing but lives here because
there's no other home for it.

`capture()` — wraps `tmux capture-pane` with the -e (SGR preserved) flag
parse_pane relies on. `deliver_input()` — writes bytes into a pane via
load-buffer + paste-buffer to dodge tmux's argv parser eating semicolons
that send-keys would lose.
"""

import re
import subprocess
import uuid

# Strip SGR escape sequences from captured pane text before parsing.
# parse_pane wants the colored prompt-line bytes (to filter ghost text),
# but everything else needs to be plain ASCII for the regexes to match.
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
    instead of swallowing it like the read-only `tmux()` helper does."""
    r = subprocess.run(
        ["tmux", *args], capture_output=True, text=True, timeout=5
    )
    if r.returncode != 0:
        return False, (r.stderr.strip() or r.stdout.strip() or "tmux failed")
    return True, r.stdout.strip()


def capture(target: str, lines: int = 100) -> str:
    # -e preserves SGR escapes; parse_pane strips them for content parsing
    # but uses the raw prompt-line color info to filter ghost-text input.
    return tmux("capture-pane", "-t", target, "-p", "-e", "-S", f"-{lines}")


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
```

- [ ] **Step 2: Verify import**

```sh
uv run python -c "from periscope.tmux import tmux, capture, deliver_input, _run, _tmux_mutate, _ANSI_SGR_RE, _FG_COLOR_RE; print(tmux('display-message', '-p', 'hello').strip())"
```

Expected: prints `hello` (requires a tmux server running; if not, prints empty string — also acceptable).

### Task 2.2: Strip moved code from `server.py` and add imports

- [ ] **Step 1: Delete the moved blocks**

Using `Edit`, delete from `server.py`:
- The two `_ANSI_SGR_RE` / `_FG_COLOR_RE` lines (~1806–1807).
- `def tmux(...)` and its body (~1080–1085).
- `def _run(...)` and its body (~1280–1289).
- `def capture(...)` and `def deliver_input(...)` (~1994–2018).
- `def _tmux_mutate(...)` (~2578–2587).

- [ ] **Step 2: Add the import near the top of `server.py`**

Add alongside the Peel 1 imports:

```python
from periscope.tmux import (
    tmux, capture, deliver_input, _run, _tmux_mutate,
    _ANSI_SGR_RE, _FG_COLOR_RE,
)
```

- [ ] **Step 3: Drop now-unused stdlib imports**

`uuid` may still be needed for `_send_to_target` / `/ws/pane` paths — verify:

```sh
grep -n "uuid\." /Users/tom/dev/periscope/server.py
```

If `uuid.uuid4()` is still referenced, keep `import uuid`. Same check for `subprocess`. Both are likely still used elsewhere; verify before removing.

- [ ] **Step 4: Run the verification gate**

Boot + `test_parse_pane.py`.

- [ ] **Step 5: Manual smoke — send + capture round-trip**

With periscope running (`uv run server.py` in another shell), open the dashboard and:
1. Open the modal on any pane (exercises `capture` via `/ws/pane`).
2. Type a few characters including a semicolon `;` into the modal (exercises `deliver_input`).
3. Confirm the characters appear in the pane.

If you don't want to open a browser, exercise `/api/send` from curl:

```sh
# Send "echo hi;" to your first session's window 0 (adjust target).
SESSION=$(tmux list-sessions -F '#S' | head -1)
curl -fsS -X POST "http://127.0.0.1:8765/api/send?session=$SESSION&index=0" \
  -H 'Content-Type: application/json' \
  -d '{"paste":"echo hi;","keys":["Enter"]}'
tmux capture-pane -t "$SESSION:0" -p | tail -3
```

Expected: the captured tail shows `echo hi;` and any output.

- [ ] **Step 6: Commit**

```sh
git add periscope/tmux.py server.py
git commit -m "split: extract tmux + subprocess wrappers to periscope/tmux.py (Peel 2)"
```

---

## Peel 3: `channels.py` — MCP server + tools

**Goal:** Move the entire channels subsystem (lines 336–896, ~560 lines of channels-proper code) into `periscope/channels.py`. Use function-local bridge imports for the three out-edges into not-yet-moved subsystems. `server.py` imports back what its lifespan and routes need.

**Files:**
- Create: `periscope/channels.py`
- Modify: `server.py`

**Note on banner range:** the `# --- Channels ---` banner at `server.py:336` visually contains lines 895–1085, which are NOT channels code (focus/smoothing globals, regex bank, `tmux()` — Peel 2 already moved `tmux()`). This peel touches ONLY lines 336–896 (channels-proper).

**Naming note:** the spec mentions optionally renaming `_mcp_listener` → `mcp_listener` (drop the underscore now that it's cross-module). This plan keeps the underscore for Stage A to minimize churn — the rename is discretionary and can land any time post-Stage-A. Keep the original name throughout this peel.

### Task 3.1: Create `periscope/channels.py`

- [ ] **Step 1: Write `periscope/channels.py`**

Layout:

```python
"""Channels: in-process MCP server + tool implementations.

Periscope hosts an MCP server over a unix socket. Each Claude pane spawns
channel_shim.py, which connects to /tmp/periscope-mcp.sock and proxies
bytes between Claude's stdio and our socket. This module owns:
  - tool implementations (reply, link_pr, link_linear, spawn_claude)
  - the per-pane session registry (_MCP_SESSIONS)
  - the reply log + unread counter (_CHANNEL_REPLIES / _CHANNEL_UNREAD)
  - the unix-socket listener loop

Cleanup invariant: this module never unlinks MCP_SOCKET_PATH. The lifespan
in server.py owns socket cleanup. See spec §"MCP_SOCKET_PATH cleanup."

Bridge imports: _resolve_pid_for_pane and _do_spawn_claude_tool reach into
panes/pids/note_* helpers that still live in server.py through Stage A.
The bridges are function-local to dodge double-import of server.py under
the name `server` vs `__main__`. Stage B's Peel 5 replaces these with
module-top imports from periscope.panes / periscope.pids.
"""

# --- imports (module top) -------------------------------------------------

import asyncio
import json
import os
import socket
import threading
import time
from typing import Any

from periscope.config import MCP_SOCKET_PATH
from periscope.log import log, _bg, _task
from periscope.tmux import tmux

# _STATE / _STATE_LOCK / _write_state come from server.py during Peel 3.
# Peel 4 will flip these to `from periscope.store import ...`.
# See `# BRIDGE: replace in Peel 4` comments inside the tool functions.

# --- everything from server.py:336–896 goes here (verbatim) --------------
# Copy these blocks in order:
#   1. CHANNEL_INSTRUCTIONS         (~349)
#   2. MCP_SOCKET_PATH constant     (~350) — SKIP, already in config.py
#   3. The four module-level dicts: _CHANNELS_LOCK, _CHANNEL_REPLIES,
#      _CHANNEL_UNREAD, _MCP_SESSIONS (~404–414)
#   4. _channel_gc(...)             (~408)
#   5. _do_reply_tool(...)          (~425)
#   6. _resolve_pid_for_pane(...)   (~448)  ← function-local bridge
#   7. _do_link_pr_tool(...)        (~460)  ← uses _STATE
#   8. _do_link_linear_tool(...)    (~486)  ← uses _STATE
#   9. _do_spawn_claude_tool(...)   (~510)  ← function-local bridge
#  10. emit_channel_event(...)
#  11. _handle_mcp_connection(...) / _run_mcp_for_pane(...) / _mcp_listener(...)
#      and any helpers ending at ~894 (right before `_focused_at` at 895)
```

- [ ] **Step 2: Add the bridge helpers**

Inside `_resolve_pid_for_pane`, replace the existing references to `list_windows()` and `_attach_git_then_resolve_pids()` with function-local imports:

```python
def _resolve_pid_for_pane(pane_id: str) -> str:
    # BRIDGE: removed in Peel 5 when panes/pids move to periscope/.
    # Local import dodges double-import of server.py — sys.modules["server"]
    # is populated lazily by the uvicorn worker, and by the time this
    # function runs, it's already there.
    from server import list_windows, _attach_git_then_resolve_pids
    # ... rest of original body, using the locally-imported names ...
```

Inside `_do_spawn_claude_tool`, do the same for `list_windows`, `note_focus`, `note_action`, and `_attach_git_then_resolve_pids` (called at server.py:597):

```python
def _do_spawn_claude_tool(pane: str, arguments: dict):
    # BRIDGE: removed in Peel 5.
    from server import (
        list_windows, note_focus, note_action,
        _attach_git_then_resolve_pids,
    )
    # ... rest of original body ...
```

**These two bridges are EXPECTED to survive Stage A.** Peel 5 in Stage B
replaces them with module-top `from periscope.panes import ...` /
`from periscope.pids import ...` once those modules exist.

For `_STATE` / `_STATE_LOCK` / `_write_state` (used by `_do_link_pr_tool`, `_do_link_linear_tool`, and possibly elsewhere): use a function-local `from server import _STATE, _STATE_LOCK, _write_state` inside each function that needs them. Tag with `# BRIDGE: removed in Peel 4`. Local imports here serve double duty: they avoid module-top circular risk AND they make Peel 4's flip a trivial find-and-replace of two lines.

### Task 3.2: Strip the channels block from `server.py`

- [ ] **Step 1: Delete lines 336–896**

Using `Edit`, remove the entire `# --- Channels ---` block content, EXCEPT for the banner comment itself (keep that as a "see periscope.channels" marker) and the `MCP_SOCKET_PATH` constant (already moved in Peel 1, should already be gone).

Replace the deleted block with a single line:

```python
# Channels code now lives in periscope/channels.py.
```

- [ ] **Step 2: Add the import near the top of `server.py`**

```python
from periscope.channels import (
    _CHANNELS_LOCK,
    _CHANNEL_REPLIES,
    _CHANNEL_UNREAD,
    _MCP_SESSIONS,
    _channel_gc,
    _mcp_listener,
)
```

Why each one is needed in `server.py`:
- `_CHANNELS_LOCK`, `_CHANNEL_REPLIES`, `_CHANNEL_UNREAD`, `_MCP_SESSIONS` — read by `/api/state` (per-window channel block) and `/api/channel/clear-unread`.
- `_channel_gc` — called by `/api/state` to GC stale pane entries.
- `_mcp_listener` — called by lifespan.

If a grep shows other channels-internal symbols referenced from `server.py` (e.g. `CHANNEL_INSTRUCTIONS`), import those too:

```sh
grep -n "CHANNEL_INSTRUCTIONS\|_do_reply_tool\|_do_link_pr_tool\|_do_link_linear_tool\|_do_spawn_claude_tool\|emit_channel_event" /Users/tom/dev/periscope/server.py
```

If any hit, add to the import list. Likely only `_mcp_listener` and the four dicts/lock are needed externally; the tool dispatch happens inside `_mcp_listener`'s call graph.

- [ ] **Step 3: Run the verification gate**

Boot + `test_parse_pane.py`. The boot log should show `mcp-listener` task starting (look for log line `MCP listener bound to /tmp/periscope-mcp.sock` or similar).

- [ ] **Step 4: Manual smoke — channels end-to-end**

This is the gate `tests/test_channel_smoke.py` does NOT cover (it imports `channel_server.py`, a separate file). Do it manually.

```sh
# In one shell: periscope running
uv run server.py
```

In another shell, with tmux:

```sh
# Spawn a Claude session attached to periscope's channel
tmux new-window -n channel-smoke \
  "claude --dangerously-load-development-channels server:periscope"
```

Inside that Claude session, issue:

```
Use the periscope reply tool to send a "done" reply with message "smoke test".
```

Wait for Claude to call the tool. Then in periscope's dashboard (or via curl), confirm the pane card shows the reply. Example via curl:

```sh
curl -fsS http://127.0.0.1:8765/api/state \
  | python3 -c "import json,sys; s=json.load(sys.stdin); print([{'target':w['target'],'replies':w.get('channel_replies',[])} for w in s['windows'] if w.get('channel_replies')])"
```

Expected: at least one entry with the "smoke test" message.

Also exercise `link_pr` and `link_linear` if you have time:
```
Use periscope's link_pr tool with number 1234.
Use periscope's link_linear tool with id FAR-456.
```

Confirm the dashboard shows the linked PR and Linear ticket on the pane card.

- [ ] **Step 5: Run `tests/test_channel_smoke.py`**

This doesn't exercise the new code, but confirms `channel_server.py` (the separate shim Claude actually spawns) still works:

```sh
uv run tests/test_channel_smoke.py
```

Expected: `OK` / exit 0.

- [ ] **Step 6: Commit**

```sh
git add periscope/channels.py server.py
git commit -m "split: extract channels (in-process MCP) to periscope/channels.py (Peel 3)"
```

---

## Peel 4: `store.py` — state.json layer

**Goal:** Move the persistent state subsystem (lines 220–335) into `periscope/store.py`. Flip the channels bridges from `from server import _STATE, ...` to `from periscope.store import _STATE, ...`.

**Files:**
- Create: `periscope/store.py`
- Modify: `server.py`
- Modify: `periscope/channels.py` (flip 3 bridge imports)

### Task 4.1: Create `periscope/store.py`

- [ ] **Step 1: Write `periscope/store.py`**

Source: copy `server.py:220–335` verbatim, drop banner, fix imports.

```python
"""state.json: persistent UI prefs, per-window annotations, command palette.

Single JSON file at $XDG_CONFIG_HOME/periscope/state.json (default
~/.config/periscope/state.json), mutated only by the server, under
threading.Lock, with atomic tempfile+rename writes.

Lock choice: threading.Lock (not asyncio.Lock). FastAPI runs sync `def`
endpoints on anyio's threadpool, so two concurrent /api/state polls
execute in parallel threads. asyncio.Lock only blocks coroutines, not
threads — would let sync handlers race past each other into the critical
section. threading.Lock works correctly from both sync and async (acquired
synchronously; the file write is fast enough that briefly blocking the
event loop is fine).

Import-time side effect: `_STATE = _load_state()` runs on import, and
`_seed_commands_if_empty()` + `_channels_migration_v1()` run after that.
This means importing periscope.store mutates ~/.config/periscope/state.json
(creates it if missing, runs migrations). Matches today's behavior in
server.py:283; acknowledged in spec §"_load_state() runs on import."
"""

import json
import os
import time
from pathlib import Path
import threading

from periscope.log import log


def _state_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "periscope" / "state.json"


_STATE_LOCK = threading.Lock()
_STATE_DEFAULTS: dict = {
    "version": 1,
    "ui": {},
    "windows": {},
    "commands": [],
}


def _load_state() -> dict:
    """Read state.json. On parse failure rename to .corrupt-<ts> and return
    defaults — the next save writes a fresh valid file, and the user can
    recover from the renamed file if they care."""
    path = _state_path()
    if not path.exists():
        return json.loads(json.dumps(_STATE_DEFAULTS))
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        # Missing keys default to their empty value — older files written by
        # earlier phases never carry `windows` or `commands`.
        for k, v in _STATE_DEFAULTS.items():
            data.setdefault(k, json.loads(json.dumps(v)))
        return data
    except (json.JSONDecodeError, OSError) as e:
        corrupt = path.with_name(f"{path.name}.corrupt-{int(time.time())}")
        try:
            path.rename(corrupt)
            log.warning("state.json unreadable (%s); renamed to %s", e, corrupt)
        except OSError:
            pass
        return json.loads(json.dumps(_STATE_DEFAULTS))


def _write_state(data: dict) -> None:
    """Atomic write: tempfile + os.replace. Caller must hold _STATE_LOCK."""
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)


# In-memory cache — every endpoint reads from this, writes go through
# _write_state under the lock. Loaded once at import.
_STATE: dict = _load_state()

_DEFAULT_COMMANDS = [
    {"label": "claude", "exec": "claude"},
    {"label": "shell", "exec": ""},
    {"label": "vim", "exec": "vim"},
]


def _seed_commands_if_empty() -> None:
    """If `commands` is empty (fresh install or pre-phase-4 state.json),
    seed the three legacy defaults so the new-window tile keeps working.

    Side effect: if a user deliberately drains commands to zero, the next
    server restart re-seeds the defaults. To keep zero commands, leave at
    least one no-op entry around."""
    with _STATE_LOCK:
        if not _STATE["commands"]:
            _STATE["commands"] = [dict(c) for c in _DEFAULT_COMMANDS]
            _write_state(_STATE)


_seed_commands_if_empty()


def _channels_migration_v1() -> None:
    """One-shot: rewrite seeded `claude` exec entries to include the
    dev-channels flag so spawned Claudes get a channel server attached.

    Idempotent — gated by `channels_migration_v1_done`. See
    docs/superpowers/specs/2026-05-14-channels-design.md
    §"Migration for existing users" for the policy rationale.
    """
    with _STATE_LOCK:
        if _STATE.get("channels_migration_v1_done"):
            return
        new_exec = (
            "claude --dangerously-load-development-channels server:periscope"
        )
        for cmd in _STATE.get("commands", []):
            if cmd.get("exec") == "claude":
                cmd["exec"] = new_exec
        _STATE["channels_migration_v1_done"] = True
        _write_state(_STATE)


_channels_migration_v1()
```

- [ ] **Step 2: Verify import idempotency**

```sh
uv run python -c "from periscope.store import _STATE, _STATE_LOCK, _write_state; print(sorted(_STATE.keys()))"
```

Expected: prints something like `['channels_migration_v1_done', 'commands', 'ui', 'version', 'windows']`.

Confirm `~/.config/periscope/state.json` is unchanged from before (the migrations are idempotent):

```sh
cat ~/.config/periscope/state.json | python3 -m json.tool | head
```

### Task 4.2: Strip the store block from `server.py` and add imports

- [ ] **Step 1: Delete lines 220–335**

Using `Edit`, remove the entire `# --- Persistent state (state.json) ---` block in `server.py`. Keep the banner as a one-line marker:

```python
# Persistent state (state.json) now lives in periscope/store.py.
```

- [ ] **Step 2: Add the import near the top of `server.py`**

```python
from periscope.store import (
    _STATE, _STATE_LOCK, _write_state,
    _STATE_DEFAULTS, _DEFAULT_COMMANDS,
)
```

`_STATE_DEFAULTS` and `_DEFAULT_COMMANDS` might not be referenced from `server.py` after the move — grep to confirm:

```sh
grep -n "_STATE_DEFAULTS\|_DEFAULT_COMMANDS" /Users/tom/dev/periscope/server.py
```

If neither shows up (other than the import line), drop them from the import.

### Task 4.3: Flip the channels bridges from `server` to `periscope.store`

- [ ] **Step 1: Update `periscope/channels.py`**

Find every `# BRIDGE: removed in Peel 4` marker inside `periscope/channels.py`. Each one wraps a `from server import _STATE, _STATE_LOCK, _write_state` (or subset). Replace each with `from periscope.store import _STATE, _STATE_LOCK, _write_state`. Delete the BRIDGE marker comment in the same edit.

**Expected: exactly 2 markers** (one each in `_do_link_pr_tool` at original server.py lines 476–480 and `_do_link_linear_tool` at 500–504). If the count differs, something went wrong in Peel 3.

The function-local-vs-module-top decision: now that `periscope.store` exists and doesn't import from `server`, the imports can move to `periscope/channels.py`'s module top. Pick one of:
  - (a) Leave them function-local for consistency with the panes/pids bridges that survive into Peel 5.
  - (b) Promote to module-top — cleaner, no circular risk.

Recommendation: (b). Add to `periscope/channels.py`'s module-top imports:

```python
from periscope.store import _STATE, _STATE_LOCK, _write_state
```

And delete every function-local import line that the bridge markers tagged.

- [ ] **Step 2: Verify no remaining `from server import _STATE` lines in periscope/**

```sh
grep -rn "from server import" /Users/tom/dev/periscope/periscope/
```

Expected output should show ONLY the `_resolve_pid_for_pane` and `_do_spawn_claude_tool` bridges (the panes/pids ones, which Peel 5 in Stage B will resolve). Specifically:
```
periscope/channels.py:NNN:    from server import list_windows, _attach_git_then_resolve_pids
periscope/channels.py:NNN:    from server import list_windows, note_focus, note_action
```
Anything else → fix.

### Task 4.4: Verification + commit

- [ ] **Step 1: Run the verification gate**

Boot + `test_parse_pane.py`. Note: `test_parse_pane.py` still does `import server`, which now triggers `periscope.store` via the new import. Verify state.json is not corrupted by the test run:

```sh
md5sum ~/.config/periscope/state.json
uv run test_parse_pane.py
md5sum ~/.config/periscope/state.json
```

Expected: same hash both times (idempotent migrations).

- [ ] **Step 2: Manual smoke — prefs round-trip**

```sh
# Read current prefs.
curl -fsS http://127.0.0.1:8765/api/prefs | python3 -m json.tool | head -20

# Toggle a UI pref and read it back.
curl -fsS -X PATCH http://127.0.0.1:8765/api/prefs/ui \
  -H 'Content-Type: application/json' \
  -d '{"foo":"bar"}'
curl -fsS http://127.0.0.1:8765/api/prefs | python3 -c "import json,sys; print(json.load(sys.stdin)['ui'].get('foo'))"
```

Expected: prints `bar`. State.json on disk:

```sh
grep '"foo"' ~/.config/periscope/state.json
```

Expected: shows `"foo": "bar"`.

Clean up the test pref:
```sh
curl -fsS -X PATCH http://127.0.0.1:8765/api/prefs/ui \
  -H 'Content-Type: application/json' -d '{"foo":null}'
```

- [ ] **Step 3: Re-run channels smoke (regression check)**

After flipping the bridges, re-do Peel 3's Step 4 manual smoke — confirm `link_pr` / `link_linear` still write to `_STATE` correctly (they go through the moved import path now):

```
# In a Claude session attached to periscope:
Use periscope's link_pr tool with number 9999.
```

Then:

```sh
curl -fsS http://127.0.0.1:8765/api/state \
  | python3 -c "import json,sys; print([w.get('pr') for w in json.load(sys.stdin)['windows'] if w.get('pr',{}).get('pr_linked')])"
```

Expected: shows `9999` in at least one entry.

- [ ] **Step 4: Commit**

```sh
git add periscope/store.py periscope/channels.py server.py
git commit -m "split: extract state.json layer to periscope/store.py + flip channels bridges (Peel 4)"
```

---

## End of Stage A — checkpoint

After Peel 4 commits, `server.py` should be ~2360 lines (down from 3543). Verify:

```sh
wc -l /Users/tom/dev/periscope/server.py
wc -l /Users/tom/dev/periscope/periscope/*.py
```

Expected approximate totals:
- `server.py`: ~2350–2400 lines
- `periscope/config.py`: ~20 lines
- `periscope/log.py`: ~70 lines
- `periscope/pidfile.py`: ~85 lines
- `periscope/tmux.py`: ~70 lines
- `periscope/channels.py`: ~560 lines
- `periscope/store.py`: ~115 lines

### Pause and run for a week

Per the spec's staged-rollout recommendation: stop here. Use periscope on its normal day-to-day workload. Watch for:

- Anything that boots slower, logs differently, or behaves differently from before.
- Channel attachments — `claude --dangerously-load-development-channels server:periscope` should still attach cleanly; `reply` / `link_pr` / `link_linear` / `spawn_claude` should still surface on the dashboard.
- Pidfile reclaim — `uv run server.py` should still be idempotent (each invocation kicks out the previous).
- state.json — should accumulate prefs normally, no spurious migrations.

If any of these regress, the bisect target is at most 5 commits (Peels 0–4). If a week of normal use surfaces nothing, proceed to Stage B.

---

## What's NOT in this plan

- **Stage B (Peels 5–9):** panes/pids, LGTM, git_pr/usage/rename_ai, routes split, final `app` move. Separate plan.
- **Behavior changes.** Every endpoint behaves bit-identically.
- **New tests.** `test_parse_pane.py` still imports `server`; its rewrite happens in Stage B Peel 5.
- **The shim refactor of `server.py`.** Through Stage A, `server.py` is still the FastAPI app and entry point. Peel 9 moves `app` to `periscope/app.py` and shrinks `server.py` to ~30 lines.
