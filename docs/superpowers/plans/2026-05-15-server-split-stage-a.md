# Server split — Stage A (Peels 0–4) implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extract `server.py`'s infrastructure (logging, pidfile, config), tmux subprocess wrappers, the channels MCP subsystem, and the `state.json` store into a `periscope/` package, with a pytest test suite mirroring the package structure. Each move is preceded by a test against the symbol at its current location; each peel ends with the test passing against the moved code.

**Architecture:** uvicorn target stays `"server:app"` throughout Stage A — `server.py` keeps `app = FastAPI()` and the lifespan, importing moved symbols back. No `from server import …` at module top inside `periscope/` (would trigger a double-import of `server.py` under the name `server` separate from `__main__`). Channels needs two function-local bridges that survive into Stage B Peel 5. Tests live under `tests/` with one file per `periscope/` module, plus `tests/conftest.py` for shared fixtures (`tmp_xdg_home`, `fake_tmux`, `client`, `clean_state`).

**Tech Stack:** Python 3.11+, FastAPI, uvicorn, mcp 1.27.*, pytest 8+, pytest-mock 3+ (already declared in `pyproject.toml` dev-deps).

**Reference docs:**
- Design spec: `docs/superpowers/specs/2026-05-15-server-split-design.md` (rev 3)
- Periscope conventions: `CLAUDE.md`

---

## Verification gate (run after every peel)

```sh
# 1. Full pytest suite — authoritative gate.
uv run pytest -q

# 2. Smoke: server boots, /api/state returns 200.
uv run server.py &
SERVER_PID=$!
sleep 3
curl -fsS http://127.0.0.1:8765/api/state > /dev/null && echo "API OK"
kill $SERVER_PID
wait $SERVER_PID 2>/dev/null

# 3. Peel-specific manual smoke (see each peel below).
```

If pytest or the curl fails: revert the working tree, diagnose, retry. Do NOT commit on red.

---

## File structure (end of Stage A)

```
periscope/                          # NEW package
├── __init__.py
├── __main__.py
├── config.py                       # NEW (Peel 1)
├── log.py                          # NEW (Peel 1)
├── pidfile.py                      # NEW (Peel 1)
├── tmux.py                         # NEW (Peel 2)
├── channels.py                     # NEW (Peel 3)
├── store.py                        # NEW (Peel 4)
└── routes/                         # NEW (Peel 0, empty for Stage A)
    └── __init__.py

tests/                              # NEW (Peel 0)
├── __init__.py
├── conftest.py
├── test_smoke.py                   # NEW (Peel 0) — sanity test
├── test_config.py                  # NEW (Peel 1)
├── test_log.py                     # NEW (Peel 1)
├── test_pidfile.py                 # NEW (Peel 1)
├── test_tmux.py                    # NEW (Peel 2)
├── test_channels.py                # NEW (Peel 3)
└── test_store.py                   # NEW (Peel 4)

server.py                           # MODIFIED in every peel
                                    # End of Stage A: ~2360 lines
                                    # (down from 3543).
                                    # Stage B handles the rest.

pyproject.toml                      # MODIFIED (Peel 0) — testpaths += "tests"
test_parse_pane.py                  # UNCHANGED — Stage B Peel 5 folds into tests/test_panes.py
tests/test_channel_smoke.py         # UNCHANGED — already correctly placed
```

---

## Peel 0: scaffold + pytest infrastructure

**Goal:** Create the empty `periscope/` package, the `tests/` directory with `conftest.py` + a smoke test, and update `pyproject.toml`'s `testpaths`. No `server.py` content moves.

**Files:**
- Create: `periscope/__init__.py`, `periscope/__main__.py`, `periscope/routes/__init__.py`
- Create: `tests/__init__.py`, `tests/conftest.py`, `tests/test_smoke.py`
- Modify: `pyproject.toml`

### Task 0.1: Create empty `periscope/` package

- [ ] **Step 1: Create the package skeleton**

```sh
mkdir -p /Users/tom/dev/periscope/periscope/routes
touch /Users/tom/dev/periscope/periscope/__init__.py
touch /Users/tom/dev/periscope/periscope/routes/__init__.py
```

For `periscope/__main__.py`, write:

```python
"""Entry point for `python -m periscope`. Stage A still routes users
through `uv run server.py`; Stage B's Peel 9 replaces this with
`uvicorn.run(periscope.app:app, ...)`."""

import sys

print(
    "periscope: during the server-split migration, use `uv run server.py` "
    "from the repo root instead of `python -m periscope`.",
    file=sys.stderr,
)
sys.exit(2)
```

- [ ] **Step 2: Verify the package imports**

```sh
cd /Users/tom/dev/periscope
uv run python -c "import periscope; import periscope.routes; print('OK')"
```

Expected: `OK`.

### Task 0.2: Set up pytest infrastructure

- [ ] **Step 1: Update `pyproject.toml`**

Find the existing `[tool.pytest.ini_options]` block and change `testpaths` to include `tests`:

```toml
[tool.pytest.ini_options]
testpaths = ["tests", "history/tests"]
python_files = ["test_*.py"]
addopts = "-ra --strict-markers --strict-config"
```

- [ ] **Step 2: Create `tests/__init__.py`**

```sh
touch /Users/tom/dev/periscope/tests/__init__.py
```

- [ ] **Step 3: Create `tests/conftest.py` with the shared fixtures**

```python
"""Shared pytest fixtures for periscope's test suite.

These fixtures sandbox side-effecting helpers so tests don't write to
~/.config/periscope or bind real unix sockets.
"""

import os
from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture
def tmp_xdg_home(monkeypatch, tmp_path: Path) -> Path:
    """Redirect XDG_CONFIG_HOME so state.json, pidfile, and the log
    file all land in a per-test tempdir."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def fake_tmux(mocker):
    """Replace periscope.tmux.tmux with a Mock. Returns the mock so the
    test can configure side_effect / return_value and assert call args.

    Activates only after Peel 2 ships periscope/tmux.py. Tests written
    in Peel 0 / Peel 1 don't depend on this — kept here so later peels
    can use it without conftest churn.
    """
    # Import inside the fixture body so collection doesn't fail before
    # periscope.tmux exists (Peel 2). Once that module lands, this just
    # works.
    try:
        import periscope.tmux as tmux_mod
    except ImportError:
        pytest.skip("periscope.tmux not yet present (pre-Peel-2)")
    mock = mocker.patch.object(tmux_mod, "tmux", autospec=True)
    mock.return_value = ""
    return mock


@pytest.fixture
def clean_state(tmp_xdg_home, monkeypatch):
    """Reset periscope.store._STATE to a fresh defaults dict for the test.
    Available after Peel 4. Returns the dict so the test can prepopulate
    fields before exercising code-under-test."""
    try:
        import periscope.store as store
    except ImportError:
        pytest.skip("periscope.store not yet present (pre-Peel-4)")
    fresh = {
        "version": 1,
        "ui": {},
        "windows": {},
        "commands": [],
    }
    monkeypatch.setattr(store, "_STATE", fresh)
    return fresh
```

- [ ] **Step 4: Create `tests/test_smoke.py`**

```python
"""Smoke test: pytest infrastructure is wired up."""


def test_periscope_package_importable():
    import periscope
    import periscope.routes
    assert periscope is not None
    assert periscope.routes is not None


def test_pyproject_testpaths_includes_tests():
    import tomllib
    from pathlib import Path
    pyproject = Path(__file__).parent.parent / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text())
    assert "tests" in data["tool"]["pytest"]["ini_options"]["testpaths"]
```

- [ ] **Step 5: Run the smoke test**

```sh
cd /Users/tom/dev/periscope
uv run pytest tests/test_smoke.py -v
```

Expected: 2 passed.

- [ ] **Step 6: Run the full suite (smoke + existing history tests)**

```sh
uv run pytest -q
```

Expected: all green. `history/tests/` should still pass (no changes there).

- [ ] **Step 7: Run the boot smoke**

```sh
uv run server.py &
SERVER_PID=$!
sleep 3
curl -fsS http://127.0.0.1:8765/api/state > /dev/null && echo "API OK"
kill $SERVER_PID
wait $SERVER_PID 2>/dev/null
```

Expected: `API OK`.

- [ ] **Step 8: Commit**

```sh
git add periscope/ tests/ pyproject.toml
git commit -m "split: scaffold periscope/ package + pytest tests/ infra (Peel 0)"
```

---

## Peel 1: infra — `config.py`, `log.py`, `pidfile.py`

**Goal:** Move logging setup, `_bg`/`_task` background-task wrappers, pidfile reclaim, and cross-cutting constants (`STATIC`, `MCP_SOCKET_PATH`) into `periscope/`. Tests written first against `server`, then re-pointed at `periscope.<mod>` after the move.

**Files:**
- Create: `periscope/config.py`, `periscope/log.py`, `periscope/pidfile.py`
- Create: `tests/test_config.py`, `tests/test_log.py`, `tests/test_pidfile.py`
- Modify: `server.py`

### Task 1.1: Write tests against the current locations

- [ ] **Step 1: Write `tests/test_config.py`**

```python
"""Constants and paths: STATIC + MCP_SOCKET_PATH.

These tests are written against `server` for Stage A's first half;
Task 1.5 re-points them at `periscope.config` after the move.
"""

from pathlib import Path

# CURRENT IMPORT (will be flipped in Task 1.5):
from server import STATIC, MCP_SOCKET_PATH


def test_STATIC_points_to_repo_static_dir():
    assert STATIC.name == "static"
    assert STATIC.is_absolute()
    # The static/ dir actually exists in the repo.
    assert STATIC.is_dir(), f"{STATIC} should exist"
    # index.html sits inside.
    assert (STATIC / "index.html").is_file()


def test_MCP_SOCKET_PATH_is_unix_socket_path():
    assert MCP_SOCKET_PATH == "/tmp/periscope-mcp.sock"
```

- [ ] **Step 2: Write `tests/test_log.py`**

```python
"""Logging setup + _bg/_task crash capture.

The logger and the background-task wrappers must surface uncaught
exceptions into the log — silent crashes are the failure mode this
module exists to prevent.
"""

import asyncio
import logging
import threading
import time

# CURRENT IMPORT (will be flipped in Task 1.5):
from server import log, _bg, _task


def test_log_is_named_periscope():
    assert isinstance(log, logging.Logger)
    assert log.name == "periscope"


def test_bg_returns_thread_and_runs_fn():
    seen = []
    t = _bg("test-thread", lambda: seen.append("ran"))
    assert isinstance(t, threading.Thread)
    t.join(timeout=2.0)
    assert seen == ["ran"]


def test_bg_logs_uncaught_exception(mocker):
    spy = mocker.spy(log, "exception")

    def crashes():
        raise RuntimeError("kaboom")

    t = _bg("crashy", crashes)
    t.join(timeout=2.0)
    spy.assert_called_once()
    # The message should mention the thread name.
    msg = spy.call_args[0][0]
    assert "crashy" in msg


def test_task_logs_uncaught_exception(mocker):
    spy = mocker.spy(log, "error")

    async def crashes():
        raise RuntimeError("async kaboom")

    async def run():
        t = _task(crashes(), "async-crashy")
        # Give the loop a tick to run + call the done callback.
        await asyncio.sleep(0.05)
        return t

    asyncio.run(run())
    spy.assert_called_once()
    assert "async-crashy" in spy.call_args[0][0]
```

- [ ] **Step 3: Write `tests/test_pidfile.py`**

```python
"""Pidfile reclaim: single-instance enforcement.

Tests sandbox $XDG_CONFIG_HOME so they don't touch real periscope state.
Subprocess invocations (ps, kill) are monkeypatched.
"""

import os
import subprocess
from pathlib import Path

import pytest

# CURRENT IMPORT (will be flipped in Task 1.5):
from server import (
    _pidfile_path,
    _pid_is_periscope,
    _write_pidfile,
    _remove_pidfile,
    _reclaim_existing_instance,
)


def test_pidfile_path_under_xdg(tmp_xdg_home: Path):
    assert _pidfile_path() == tmp_xdg_home / "periscope" / "periscope.pid"


def test_write_then_remove_pidfile(tmp_xdg_home: Path):
    _write_pidfile()
    path = _pidfile_path()
    assert path.is_file()
    assert path.read_text() == str(os.getpid())
    _remove_pidfile()
    assert not path.exists()


def test_remove_pidfile_ignores_other_owners(tmp_xdg_home: Path):
    """If the file holds someone else's pid, don't delete it."""
    path = _pidfile_path()
    path.parent.mkdir(parents=True)
    path.write_text("99999")
    _remove_pidfile()
    assert path.exists(), "must not delete a pidfile we don't own"
    assert path.read_text() == "99999"


def test_pid_is_periscope_true_when_command_matches(mocker):
    mocker.patch(
        "subprocess.run",
        return_value=subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout="uv run server.py\n", stderr="",
        ),
    )
    assert _pid_is_periscope(1234) is True


def test_pid_is_periscope_false_when_command_unrelated(mocker):
    mocker.patch(
        "subprocess.run",
        return_value=subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout="/usr/bin/zsh\n", stderr="",
        ),
    )
    assert _pid_is_periscope(1234) is False


def test_pid_is_periscope_false_on_ps_failure(mocker):
    mocker.patch(
        "subprocess.run",
        return_value=subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="",
        ),
    )
    assert _pid_is_periscope(1234) is False


def test_reclaim_noop_when_pidfile_missing(tmp_xdg_home: Path, mocker):
    killed = mocker.patch("os.kill")
    _reclaim_existing_instance()
    killed.assert_not_called()


def test_reclaim_signals_live_periscope(tmp_xdg_home: Path, mocker):
    path = _pidfile_path()
    path.parent.mkdir(parents=True)
    path.write_text("99999")

    # Pretend 99999 IS a periscope (return True once during reclaim, then
    # False after our "kill" so the loop exits without escalation).
    is_per = mocker.patch("server._pid_is_periscope")
    is_per.side_effect = [True, False]

    killed = mocker.patch("os.kill")
    _reclaim_existing_instance()
    # Must SIGTERM, not SIGKILL (target exited cleanly).
    import signal as _signal
    killed.assert_any_call(99999, _signal.SIGTERM)
    assert not any(
        call.args == (99999, _signal.SIGKILL)
        for call in killed.call_args_list
    )


def test_reclaim_escalates_to_sigkill_after_3s(tmp_xdg_home: Path, mocker):
    path = _pidfile_path()
    path.parent.mkdir(parents=True)
    path.write_text("99999")

    # Stays "alive" forever — forces the SIGKILL path.
    mocker.patch("server._pid_is_periscope", return_value=True)
    # Make time.time race past the 3s deadline on the first poll inside
    # the while loop. Patch time.sleep to a no-op so the test is fast.
    mocker.patch("time.sleep")
    times = iter([0.0, 0.5, 4.0, 4.0, 4.0])
    mocker.patch("time.time", side_effect=lambda: next(times))

    killed = mocker.patch("os.kill")
    _reclaim_existing_instance()
    import signal as _signal
    killed.assert_any_call(99999, _signal.SIGTERM)
    killed.assert_any_call(99999, _signal.SIGKILL)
```

- [ ] **Step 4: Run the new tests against current `server.py`**

```sh
uv run pytest tests/test_config.py tests/test_log.py tests/test_pidfile.py -v
```

Expected: ALL PASS. If any fail, the symbols don't behave as the tests claim — fix the tests before moving forward (the move shouldn't change behavior).

### Task 1.2: Create `periscope/config.py`

- [ ] **Step 1: Write `periscope/config.py`**

```python
"""Cross-cutting paths and constants. Imported widely; should never import
from any other periscope.* module — keep this a leaf."""

from pathlib import Path

# Static asset root for FastAPI's app.mount("/", StaticFiles(...)) call.
# Computed relative to the repo root, NOT to this file — server.py lives at
# the repo root, periscope/ is a subdirectory.
STATIC = Path(__file__).parent.parent / "static"

# Unix socket the in-process MCP server listens on. channel_shim.py connects
# here from each Claude pane. Lifespan unlinks this on shutdown; channels.py
# must never unlink it (see spec §"MCP_SOCKET_PATH cleanup").
MCP_SOCKET_PATH = "/tmp/periscope-mcp.sock"
```

### Task 1.3: Create `periscope/log.py`

- [ ] **Step 1: Write `periscope/log.py`**

```python
"""Logging + background-task crash capture.

Logging: rotating file at ~/.config/periscope/periscope.log + stderr. Set
up at import time so module-init, lifespan, and handlers all land in the
same sink. Background-task wrappers (_bg / _task) hoist exceptions from
fire-and-forget threads / coroutines into the log so they don't vanish.
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

### Task 1.4: Create `periscope/pidfile.py`

- [ ] **Step 1: Write `periscope/pidfile.py`**

```python
"""Pidfile / single-instance reclaim.

Called from server.py's __main__ block BEFORE uvicorn binds the port so
`uv run server.py` is idempotent — starting periscope kicks out the
previous instance.
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

### Task 1.5: Strip moved code from `server.py` and re-point tests

- [ ] **Step 1: Delete the moved blocks from `server.py`**

Using `Edit`, delete from `server.py`:
- Lines 41–100 (Logging banner through end of `_task` function).
- Lines 103–176 (Pidfile banner through end of `_remove_pidfile`).
- Line 218 (`STATIC = Path(__file__).parent / "static"`). **Do NOT delete line 217** — that's `app = FastAPI(lifespan=lifespan)`.
- Line 350 (`MCP_SOCKET_PATH = "/tmp/periscope-mcp.sock"` — physically inside the channels block, but logically config).

- [ ] **Step 2: Add the imports near the top of `server.py`**

After the existing `from pydantic import BaseModel` line, add:

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

- [ ] **Step 3: Drop now-unused stdlib imports**

```sh
grep -nE "^import logging|^import signal|^from logging" /Users/tom/dev/periscope/server.py
```

`logging.handlers` should have no remaining use; drop the import. `logging` itself: check if anything (other than the moved code) used `logging.getLogger` — if not, drop. `signal` is still used by `_on_sigterm` in `__main__`; keep it.

- [ ] **Step 4: Re-point the three test files to import from `periscope`**

In `tests/test_config.py`, change the import:

```python
# OLD: from server import STATIC, MCP_SOCKET_PATH
from periscope.config import STATIC, MCP_SOCKET_PATH
```

In `tests/test_log.py`:

```python
# OLD: from server import log, _bg, _task
from periscope.log import log, _bg, _task
```

In `tests/test_pidfile.py`:

```python
# OLD: from server import (...)
from periscope.pidfile import (
    _pidfile_path,
    _pid_is_periscope,
    _write_pidfile,
    _remove_pidfile,
    _reclaim_existing_instance,
)
```

Also update the `mocker.patch("server._pid_is_periscope")` calls inside `tests/test_pidfile.py` to `mocker.patch("periscope.pidfile._pid_is_periscope")` (there are two such patches in `test_reclaim_signals_live_periscope` and `test_reclaim_escalates_to_sigkill_after_3s`).

- [ ] **Step 5: Run the verification gate**

```sh
uv run pytest -q
```

Expected: all green (smoke + config + log + pidfile + history/tests).

Boot smoke:

```sh
uv run server.py &
sleep 3
curl -fsS http://127.0.0.1:8765/api/state > /dev/null && echo "API OK"
kill %1; wait 2>/dev/null
```

Expected: `API OK`. The startup log line should still appear in `~/.config/periscope/periscope.log`.

- [ ] **Step 6: Manual smoke — pidfile reclaim across processes**

```sh
uv run server.py &
FIRST=$!
sleep 2
uv run server.py &
SECOND=$!
sleep 4
kill -0 $FIRST 2>/dev/null && echo "FAIL: first still alive" || echo "OK: first reclaimed"
kill $SECOND
wait 2>/dev/null
```

Expected: `OK: first reclaimed`.

- [ ] **Step 7: Commit**

```sh
git add periscope/config.py periscope/log.py periscope/pidfile.py \
        tests/test_config.py tests/test_log.py tests/test_pidfile.py \
        server.py
git commit -m "split: extract config/log/pidfile + tests (Peel 1)"
```

---

## Peel 2: `tmux.py` — subprocess wrappers

**Goal:** Move all tmux/subprocess helpers into one module. Tests first.

**Files:**
- Create: `periscope/tmux.py`
- Create: `tests/test_tmux.py`
- Modify: `server.py`

### Task 2.1: Write `tests/test_tmux.py` against current `server`

- [ ] **Step 1: Write `tests/test_tmux.py`**

```python
"""tmux + subprocess wrappers.

Tests don't require a live tmux. We monkeypatch subprocess.run to assert
the wrappers compose argv correctly and return what they claim to return.
"""

import subprocess

# CURRENT IMPORT (flipped in Task 2.3):
from server import (
    tmux, capture, deliver_input, _run, _tmux_mutate,
    _ANSI_SGR_RE, _FG_COLOR_RE,
)


def test_ansi_sgr_re_strips_color_codes():
    s = "\x1b[31mred\x1b[0m plain"
    assert _ANSI_SGR_RE.sub("", s) == "red plain"


def test_fg_color_re_matches_extended_palette():
    s = "\x1b[38;5;196mbright red\x1b[0m"
    assert _FG_COLOR_RE.search(s) is not None


def test_tmux_invokes_subprocess_with_tmux_prefix(mocker):
    mock_run = mocker.patch("subprocess.run")
    mock_run.return_value = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="hello\n", stderr="",
    )
    out = tmux("display-message", "-p", "hello")
    assert out == "hello\n"
    args, kwargs = mock_run.call_args
    assert args[0] == ["tmux", "display-message", "-p", "hello"]
    assert kwargs["capture_output"] is True
    assert kwargs["text"] is True


def test_tmux_mutate_returns_ok_on_zero_exit(mocker):
    mocker.patch(
        "subprocess.run",
        return_value=subprocess.CompletedProcess(
            args=[], returncode=0, stdout="renamed\n", stderr="",
        ),
    )
    ok, msg = _tmux_mutate("rename-window", "-t", "foo:0", "bar")
    assert ok is True
    assert msg == "renamed"


def test_tmux_mutate_surfaces_stderr_on_failure(mocker):
    mocker.patch(
        "subprocess.run",
        return_value=subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="no such window\n",
        ),
    )
    ok, msg = _tmux_mutate("rename-window", "-t", "missing:0", "bar")
    assert ok is False
    assert msg == "no such window"


def test_tmux_mutate_falls_back_to_generic_error(mocker):
    mocker.patch(
        "subprocess.run",
        return_value=subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="",
        ),
    )
    ok, msg = _tmux_mutate("bad-cmd")
    assert ok is False
    assert msg == "tmux failed"


def test_run_returns_returncode_and_stdout(mocker):
    mocker.patch(
        "subprocess.run",
        return_value=subprocess.CompletedProcess(
            args=[], returncode=0, stdout="abcdef\n", stderr="",
        ),
    )
    code, out = _run(["git", "rev-parse", "HEAD"])
    assert code == 0
    assert out == "abcdef"


def test_run_returns_minus_one_on_exception(mocker):
    mocker.patch("subprocess.run", side_effect=OSError("no such command"))
    code, out = _run(["nonexistent"])
    assert code == -1
    assert out == ""


def test_capture_calls_tmux_capture_pane_with_lines(mocker):
    mock_tmux = mocker.patch("server.tmux", return_value="pane body\n")
    out = capture("foo:0", lines=50)
    mock_tmux.assert_called_once_with(
        "capture-pane", "-t", "foo:0", "-p", "-e", "-S", "-50",
    )
    assert out == "pane body\n"


def test_deliver_input_uses_load_buffer_and_paste(mocker):
    mock_run = mocker.patch("subprocess.run")
    mock_run.return_value = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="", stderr="",
    )
    deliver_input("foo:0", "echo hi;\n")
    # Two subprocess calls: load-buffer then paste-buffer.
    assert mock_run.call_count == 2
    first_call = mock_run.call_args_list[0]
    assert first_call.args[0][:2] == ["tmux", "load-buffer"]
    assert first_call.kwargs.get("input") == "echo hi;\n"
    second_call = mock_run.call_args_list[1]
    assert second_call.args[0][:2] == ["tmux", "paste-buffer"]
    assert "foo:0" in second_call.args[0]
```

- [ ] **Step 2: Run against current `server.py`**

```sh
uv run pytest tests/test_tmux.py -v
```

Expected: all pass.

### Task 2.2: Create `periscope/tmux.py`

- [ ] **Step 1: Write `periscope/tmux.py`**

```python
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
```

### Task 2.3: Strip from `server.py`, re-point tests

- [ ] **Step 1: Delete the moved blocks from `server.py`**

Using `Edit`, delete:
- The `_ANSI_SGR_RE` / `_FG_COLOR_RE` two lines (~1806–1807).
- `def tmux(...)` and body (~1080–1085).
- `def _run(...)` and body (~1280–1289).
- `def capture(...)` and `def deliver_input(...)` (~1994–2018).
- `def _tmux_mutate(...)` (~2578–2587).

- [ ] **Step 2: Add imports to `server.py`**

Alongside the Peel 1 imports:

```python
from periscope.tmux import (
    tmux, capture, deliver_input, _run, _tmux_mutate,
    _ANSI_SGR_RE, _FG_COLOR_RE,
)
```

- [ ] **Step 3: Re-point `tests/test_tmux.py`**

```python
# OLD: from server import tmux, capture, deliver_input, _run, _tmux_mutate, _ANSI_SGR_RE, _FG_COLOR_RE
from periscope.tmux import (
    tmux, capture, deliver_input, _run, _tmux_mutate,
    _ANSI_SGR_RE, _FG_COLOR_RE,
)
```

Also update `mocker.patch("server.tmux", ...)` to `mocker.patch("periscope.tmux.tmux", ...)` (one occurrence in `test_capture_calls_tmux_capture_pane_with_lines`).

- [ ] **Step 4: Run the verification gate**

```sh
uv run pytest -q
```

Expected: all green.

Boot smoke:

```sh
uv run server.py &
sleep 3
curl -fsS http://127.0.0.1:8765/api/state > /dev/null && echo "API OK"
kill %1; wait 2>/dev/null
```

- [ ] **Step 5: Manual smoke — send + capture round-trip**

With periscope running and at least one tmux session present:

```sh
SESSION=$(tmux list-sessions -F '#S' | head -1)
curl -fsS -X POST "http://127.0.0.1:8765/api/send?session=$SESSION&index=0" \
  -H 'Content-Type: application/json' \
  -d '{"paste":"echo hi;","keys":["Enter"]}'
sleep 1
tmux capture-pane -t "$SESSION:0" -p | tail -3
```

Expected: tail shows `echo hi;` and any output.

- [ ] **Step 6: Commit**

```sh
git add periscope/tmux.py tests/test_tmux.py server.py
git commit -m "split: extract tmux + subprocess wrappers + tests (Peel 2)"
```

---

## Peel 3: `channels.py` — MCP server + tools

**Goal:** Move the channels-proper code (lines 336–896, ~560 lines) into `periscope/channels.py`. Use function-local bridge imports for the three out-edges into not-yet-moved subsystems. Tests cover the four `_do_*_tool` implementations (the live MCP listener is manual-smoke).

**Files:**
- Create: `periscope/channels.py`
- Create: `tests/test_channels.py`
- Modify: `server.py`

**Note on banner range:** the `# --- Channels ---` banner at `server.py:336` visually contains lines 895–1086 (focus/smoothing globals + regex bank), which are panes code that moves in Stage B Peel 5. This peel touches ONLY lines 336–896 (channels-proper, ending after `tg.cancel_scope.cancel()`).

**These two bridges are EXPECTED to survive Stage A:**
- `_resolve_pid_for_pane` function-local: `from server import list_windows, _attach_git_then_resolve_pids`
- `_do_spawn_claude_tool` function-local: `from server import list_windows, note_focus, note_action, _attach_git_then_resolve_pids`

Peel 5 (Stage B) replaces them with `from periscope.panes import ...` / `from periscope.pids import ...`.

**Naming note:** keep `_mcp_listener`'s leading underscore for Stage A; spec's optional rename to `mcp_listener` is discretionary and can land any time post-split.

### Task 3.1: Write `tests/test_channels.py` against current `server`

- [ ] **Step 1: Write `tests/test_channels.py`**

```python
"""Channels: tool implementations + reply log + GC.

The live MCP listener (binds /tmp/periscope-mcp.sock, runs an MCP Server
per connection) is exercised by `tests/test_channel_smoke.py` against the
sibling `channel_server.py` and by per-peel manual smoke (attach a Claude
pane, invoke the tools). These pytest tests cover the pure-logic surface
that the listener dispatches into.
"""

import time
import pytest

# CURRENT IMPORTS (flipped in Task 3.3):
from server import (
    _CHANNELS_LOCK, _CHANNEL_REPLIES, _CHANNEL_UNREAD, _MCP_SESSIONS,
    _channel_gc,
    _do_reply_tool, _do_link_pr_tool, _do_link_linear_tool,
)


@pytest.fixture(autouse=True)
def reset_channel_state():
    """Clear the in-memory channel dicts between tests so each one starts
    from a clean slate."""
    with _CHANNELS_LOCK:
        _CHANNEL_REPLIES.clear()
        _CHANNEL_UNREAD.clear()
        _MCP_SESSIONS.clear()
    yield
    with _CHANNELS_LOCK:
        _CHANNEL_REPLIES.clear()
        _CHANNEL_UNREAD.clear()
        _MCP_SESSIONS.clear()


def test_reply_tool_appends_reply_and_bumps_unread():
    _do_reply_tool("%5", {"message": "done", "kind": "done"})
    assert len(_CHANNEL_REPLIES["%5"]) == 1
    entry = _CHANNEL_REPLIES["%5"][0]
    assert entry["message"] == "done"
    assert entry["kind"] == "done"
    assert "ts" in entry
    assert _CHANNEL_UNREAD["%5"] == 1


def test_reply_tool_multiple_replies_accumulate():
    _do_reply_tool("%5", {"message": "first", "kind": "info"})
    _do_reply_tool("%5", {"message": "second", "kind": "done"})
    assert len(_CHANNEL_REPLIES["%5"]) == 2
    assert _CHANNEL_UNREAD["%5"] == 2


def test_channel_gc_drops_unknown_panes():
    _CHANNEL_REPLIES["%5"] = [{"message": "x", "kind": "info", "ts": 0}]
    _CHANNEL_UNREAD["%5"] = 1
    _CHANNEL_REPLIES["%99"] = [{"message": "y", "kind": "info", "ts": 0}]
    _CHANNEL_UNREAD["%99"] = 1

    _channel_gc({"%5"})

    assert "%5" in _CHANNEL_REPLIES
    assert "%5" in _CHANNEL_UNREAD
    assert "%99" not in _CHANNEL_REPLIES
    assert "%99" not in _CHANNEL_UNREAD


def test_link_pr_tool_writes_to_state(clean_state, mocker):
    # _do_link_pr_tool reads _STATE/_STATE_LOCK/_write_state from `server`
    # in Stage A. After Peel 4 it imports from periscope.store.
    # Mock _resolve_pid_for_pane to return a known pid; otherwise it tries
    # to call list_windows which spawns a real tmux subprocess.
    mocker.patch("server._resolve_pid_for_pane", return_value="abc123")
    mocker.patch("server._write_state")  # don't actually write disk

    # Seed _STATE so the channels code can read/mutate it
    import server
    server._STATE.update(clean_state)

    _do_link_pr_tool("%5", {"number": 1234})

    assert clean_state["windows"]["abc123"]["linked_pr"] == 1234


def test_link_linear_tool_writes_to_state(clean_state, mocker):
    mocker.patch("server._resolve_pid_for_pane", return_value="abc123")
    mocker.patch("server._write_state")
    import server
    server._STATE.update(clean_state)

    _do_link_linear_tool("%5", {"id": "FAR-456"})

    assert clean_state["windows"]["abc123"]["linked_linear"] == "FAR-456"


def test_link_pr_rejects_non_integer(clean_state, mocker):
    """Loose: the tool MAY validate, or MAY accept and stringify. Just
    verify it doesn't crash on a numeric string input — the MCP schema
    is the source of truth for arg shape."""
    mocker.patch("server._resolve_pid_for_pane", return_value="abc123")
    mocker.patch("server._write_state")
    import server
    server._STATE.update(clean_state)

    # If it accepts strings, fine; if it raises, also fine — but document.
    try:
        _do_link_pr_tool("%5", {"number": "1234"})
    except (TypeError, ValueError):
        pass  # acceptable: tool rejects non-int input
```

- [ ] **Step 2: Run against current `server`**

```sh
uv run pytest tests/test_channels.py -v
```

Expected: all pass. If `test_link_pr_tool_writes_to_state` fails because `_resolve_pid_for_pane` does something unexpected with the mocked return, inspect the implementation in `server.py:448` and adjust the test fixture wiring.

### Task 3.2: Create `periscope/channels.py`

- [ ] **Step 1: Write `periscope/channels.py`**

Layout: module-top imports, then verbatim copy of `server.py:336–896` channels code (replacing the `MCP_SOCKET_PATH` definition at ~350, which moved to `config.py` in Peel 1, with an import).

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

Bridges in _resolve_pid_for_pane and _do_spawn_claude_tool do function-
local `from server import …` to reach panes / pids / note_* helpers that
still live in server.py through Stage A. Stage B Peel 5 replaces these
with module-top imports.
"""

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

# _STATE / _STATE_LOCK / _write_state come from server.py in Peel 3.
# Peel 4 flips these BRIDGE imports to `from periscope.store import …`.

# ===== Copy server.py:344–896 verbatim, EXCEPT skip the now-redundant =====
# ===== `MCP_SOCKET_PATH = "/tmp/periscope-mcp.sock"` line (Peel 1 moved it). =====
#
# The block contains, in order:
#   - CHANNEL_INSTRUCTIONS (~344)
#   - module-level dicts: _CHANNELS_LOCK, _CHANNEL_REPLIES,
#     _CHANNEL_UNREAD, _MCP_SESSIONS (~404–414)
#   - _channel_gc(...)             (~408)
#   - _do_reply_tool(...)          (~425)
#   - _resolve_pid_for_pane(...)   (~448)  ← function-local BRIDGE
#   - _do_link_pr_tool(...)        (~460)  ← uses _STATE
#   - _do_link_linear_tool(...)    (~486)  ← uses _STATE
#   - _do_spawn_claude_tool(...)   (~510)  ← function-local BRIDGE
#   - emit_channel_event(...)
#   - _handle_mcp_connection / _run_mcp_for_pane / _mcp_listener
#     ending at line 896 (tg.cancel_scope.cancel()).
```

- [ ] **Step 2: Add the bridge helpers**

Inside `_resolve_pid_for_pane`, replace any references to `list_windows()` / `_attach_git_then_resolve_pids()` with a function-local import:

```python
def _resolve_pid_for_pane(pane_id: str) -> str:
    # BRIDGE: removed in Stage B Peel 5. Local import dodges double-import
    # of server.py — sys.modules["server"] is populated lazily by the
    # uvicorn worker, and by the time this function runs, it's already
    # there.
    from server import list_windows, _attach_git_then_resolve_pids
    # ... rest of original body, using locally-imported names ...
```

Inside `_do_spawn_claude_tool`, do the same for all four symbols (verified call sites at server.py:586–587 and ~597):

```python
def _do_spawn_claude_tool(pane: str, arguments: dict):
    # BRIDGE: removed in Stage B Peel 5.
    from server import (
        list_windows, note_focus, note_action,
        _attach_git_then_resolve_pids,
    )
    # ... rest of original body ...
```

For `_STATE` / `_STATE_LOCK` / `_write_state` (used by `_do_link_pr_tool` at ~476–480 and `_do_link_linear_tool` at ~500–504): use function-local `from server import _STATE, _STATE_LOCK, _write_state` inside each function, tagged with `# BRIDGE: removed in Peel 4`. Two such markers total.

### Task 3.3: Strip from `server.py`, re-point tests

- [ ] **Step 1: Delete lines 336–896 from `server.py`**

Using `Edit`, remove the entire `# --- Channels ---` block content (banner + body), EXCEPT preserve a one-line marker:

```python
# Channels code now lives in periscope/channels.py.
```

- [ ] **Step 2: Add imports to `server.py`**

```python
from periscope.channels import (
    _CHANNELS_LOCK, _CHANNEL_REPLIES, _CHANNEL_UNREAD, _MCP_SESSIONS,
    _channel_gc, _mcp_listener,
)
```

Why each is needed externally:
- `_CHANNELS_LOCK`, `_CHANNEL_REPLIES`, `_CHANNEL_UNREAD`, `_MCP_SESSIONS` — read by `/api/state` (per-window channel block) and `/api/channel/clear-unread`.
- `_channel_gc` — called by `/api/state`.
- `_mcp_listener` — called by lifespan.

Grep for anything else channels-internal still referenced from `server.py`:

```sh
grep -nE "CHANNEL_INSTRUCTIONS|_do_reply_tool|_do_link_pr_tool|_do_link_linear_tool|_do_spawn_claude_tool|emit_channel_event" /Users/tom/dev/periscope/server.py
```

If any hit (other than the import line you just added), add them to the import too.

- [ ] **Step 3: Re-point `tests/test_channels.py`**

Change all `from server import ...` of channels symbols to `from periscope.channels import ...`. Note the `mocker.patch("server._resolve_pid_for_pane", ...)` and `mocker.patch("server._write_state", ...)` calls need to flip to `periscope.channels._resolve_pid_for_pane`. And `import server; server._STATE.update(clean_state)` stays for Peel 3 (channels still imports `_STATE` from `server`); Peel 4 flips it.

- [ ] **Step 4: Run the verification gate**

```sh
uv run pytest -q
```

Expected: all green.

Boot smoke (look for `mcp-listener` task in the log):

```sh
uv run server.py &
sleep 3
curl -fsS http://127.0.0.1:8765/api/state > /dev/null && echo "API OK"
grep -i "listener" ~/.config/periscope/periscope.log | tail -3
kill %1; wait 2>/dev/null
```

- [ ] **Step 5: Run the existing channel-shim test (regression)**

```sh
uv run pytest tests/test_channel_smoke.py -v
```

Expected: pass (this exercises `channel_server.py`, not our moved code — should be unaffected).

- [ ] **Step 6: Manual smoke — channels end-to-end**

In one shell, periscope running. In another shell:

```sh
tmux new-window -n channel-smoke \
  "claude --dangerously-load-development-channels server:periscope"
```

Inside that Claude session, ask:
> Use the periscope reply tool to send a "done" reply with message "smoke test".

Then verify:

```sh
curl -fsS http://127.0.0.1:8765/api/state \
  | python3 -c "import json,sys; s=json.load(sys.stdin); print([{'target':w['target'],'replies':w.get('channel_replies',[])} for w in s['windows'] if w.get('channel_replies')])"
```

Expected: at least one entry containing the "smoke test" message.

Also exercise `link_pr` if you have time:
> Use periscope's link_pr tool with number 1234.

Then:

```sh
curl -fsS http://127.0.0.1:8765/api/state \
  | python3 -c "import json,sys; print([w.get('pr') for w in json.load(sys.stdin)['windows'] if w.get('pr',{}).get('pr_linked')])"
```

Expected: shows `'1234'` somewhere.

- [ ] **Step 7: Commit**

```sh
git add periscope/channels.py tests/test_channels.py server.py
git commit -m "split: extract channels (in-process MCP) + tests (Peel 3)"
```

---

## Peel 4: `store.py` — state.json layer

**Goal:** Move the persistent state subsystem (lines 220–335) into `periscope/store.py`. Flip the channels bridges from `from server import _STATE, ...` to `from periscope.store import _STATE, ...`. Tests cover load/write atomicity + migration idempotency.

**Files:**
- Create: `periscope/store.py`
- Create: `tests/test_store.py`
- Modify: `server.py`, `periscope/channels.py`

### Task 4.1: Write `tests/test_store.py` against current `server`

- [ ] **Step 1: Write `tests/test_store.py`**

```python
"""state.json: load/write atomicity + migration idempotency.

Tests redirect XDG_CONFIG_HOME so they don't touch ~/.config/periscope.
"""

import importlib
import json
import os
import sys
from pathlib import Path

import pytest


def _reimport_store(monkeypatch, tmp_xdg_home):
    """Force a fresh import of the store module under the patched XDG.
    Necessary because _STATE is loaded once at import time."""
    # Remove cached `server` first — its module-level _load_state ran
    # under the unpatched XDG and we want a clean slate.
    sys.modules.pop("server", None)
    # Re-import under the patched env.
    import importlib
    import server
    importlib.reload(server)
    return server


def test_state_path_under_xdg(tmp_xdg_home: Path):
    from server import _state_path
    assert _state_path() == tmp_xdg_home / "periscope" / "state.json"


def test_load_state_returns_defaults_when_file_missing(tmp_xdg_home: Path):
    from server import _load_state
    data = _load_state()
    assert data == {"version": 1, "ui": {}, "windows": {}, "commands": []}


def test_load_state_fills_missing_defaults(tmp_xdg_home: Path):
    """An older state.json missing newer keys should get the defaults
    merged in without losing existing data."""
    from server import _load_state, _state_path
    path = _state_path()
    path.parent.mkdir(parents=True)
    path.write_text('{"version": 1, "ui": {"theme": "dark"}}')
    data = _load_state()
    assert data["version"] == 1
    assert data["ui"] == {"theme": "dark"}
    assert data["windows"] == {}
    assert data["commands"] == []


def test_load_state_renames_corrupt_file(tmp_xdg_home: Path):
    from server import _load_state, _state_path
    path = _state_path()
    path.parent.mkdir(parents=True)
    path.write_text("{not valid json")
    data = _load_state()
    assert data == {"version": 1, "ui": {}, "windows": {}, "commands": []}
    # The corrupt file should have been renamed.
    corrupts = list(path.parent.glob("state.json.corrupt-*"))
    assert len(corrupts) == 1


def test_write_state_writes_atomically(tmp_xdg_home: Path):
    from server import _write_state, _state_path
    payload = {"version": 1, "ui": {"x": 1}, "windows": {}, "commands": []}
    _write_state(payload)
    assert json.loads(_state_path().read_text()) == payload
    # The tempfile should be gone (rename succeeded).
    assert not list(_state_path().parent.glob("state.json.tmp"))


def test_write_state_creates_parent_dir(tmp_xdg_home: Path):
    from server import _write_state, _state_path
    assert not _state_path().parent.exists()
    _write_state({"version": 1, "ui": {}, "windows": {}, "commands": []})
    assert _state_path().parent.is_dir()


def test_seed_commands_fills_empty_list(tmp_xdg_home: Path, monkeypatch):
    """When _STATE.commands is empty, the seed adds the three defaults."""
    import server
    monkeypatch.setattr(server, "_STATE", {
        "version": 1, "ui": {}, "windows": {}, "commands": [],
    })
    server._seed_commands_if_empty()
    labels = [c["label"] for c in server._STATE["commands"]]
    assert labels == ["claude", "shell", "vim"]


def test_seed_commands_noop_when_nonempty(tmp_xdg_home: Path, monkeypatch):
    import server
    existing = [{"label": "custom", "exec": "my-cmd"}]
    monkeypatch.setattr(server, "_STATE", {
        "version": 1, "ui": {}, "windows": {}, "commands": existing,
    })
    server._seed_commands_if_empty()
    assert server._STATE["commands"] == existing


def test_channels_migration_v1_rewrites_claude_exec(tmp_xdg_home: Path, monkeypatch):
    import server
    monkeypatch.setattr(server, "_STATE", {
        "version": 1, "ui": {}, "windows": {},
        "commands": [
            {"label": "claude", "exec": "claude"},
            {"label": "shell", "exec": ""},
        ],
    })
    server._channels_migration_v1()
    assert server._STATE["commands"][0]["exec"] == (
        "claude --dangerously-load-development-channels server:periscope"
    )
    assert server._STATE["channels_migration_v1_done"] is True


def test_channels_migration_v1_is_idempotent(tmp_xdg_home: Path, monkeypatch):
    import server
    monkeypatch.setattr(server, "_STATE", {
        "version": 1, "ui": {}, "windows": {},
        "commands": [{"label": "claude", "exec": "claude"}],
        "channels_migration_v1_done": True,
    })
    server._channels_migration_v1()
    # Migration shouldn't re-run; exec stays as-is.
    assert server._STATE["commands"][0]["exec"] == "claude"
```

- [ ] **Step 2: Run against current `server`**

```sh
uv run pytest tests/test_store.py -v
```

Expected: all pass.

### Task 4.2: Create `periscope/store.py`

- [ ] **Step 1: Write `periscope/store.py`**

Source: copy `server.py:220–335` verbatim, drop banner, fix imports.

```python
"""state.json: persistent UI prefs, per-window annotations, command palette.

Single JSON file at $XDG_CONFIG_HOME/periscope/state.json (default
~/.config/periscope/state.json), mutated only by the server, under
threading.Lock, with atomic tempfile+rename writes.

Lock choice: threading.Lock (not asyncio.Lock). FastAPI runs sync `def`
endpoints on anyio's threadpool, so two concurrent /api/state polls
execute in parallel threads. asyncio.Lock only blocks coroutines.

Import-time side effect: `_STATE = _load_state()` runs on import, then
`_seed_commands_if_empty()` + `_channels_migration_v1()` run. Importing
periscope.store mutates ~/.config/periscope/state.json (creates it if
missing, runs migrations). Matches today's server.py:283 behavior.
"""

import json
import os
import time
import threading
from pathlib import Path

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
    defaults — the next save writes a fresh valid file."""
    path = _state_path()
    if not path.exists():
        return json.loads(json.dumps(_STATE_DEFAULTS))
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
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


_STATE: dict = _load_state()

_DEFAULT_COMMANDS = [
    {"label": "claude", "exec": "claude"},
    {"label": "shell", "exec": ""},
    {"label": "vim", "exec": "vim"},
]


def _seed_commands_if_empty() -> None:
    """If `commands` is empty, seed the three legacy defaults so the
    new-window tile keeps working."""
    with _STATE_LOCK:
        if not _STATE["commands"]:
            _STATE["commands"] = [dict(c) for c in _DEFAULT_COMMANDS]
            _write_state(_STATE)


_seed_commands_if_empty()


def _channels_migration_v1() -> None:
    """One-shot: rewrite seeded `claude` exec entries to include the
    dev-channels flag. Idempotent — gated by `channels_migration_v1_done`."""
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

### Task 4.3: Strip from `server.py`, add imports, flip bridges, re-point tests

- [ ] **Step 1: Delete lines 220–335 from `server.py`**

Replace the block with a marker:

```python
# Persistent state (state.json) now lives in periscope/store.py.
```

- [ ] **Step 2: Add the import to `server.py`**

```python
from periscope.store import (
    _STATE, _STATE_LOCK, _write_state,
    _DEFAULT_COMMANDS,
)
```

Grep to confirm `_STATE_DEFAULTS` is no longer referenced from `server.py`:

```sh
grep -n "_STATE_DEFAULTS\|_DEFAULT_COMMANDS" /Users/tom/dev/periscope/server.py
```

If `_DEFAULT_COMMANDS` isn't referenced, drop from the import.

- [ ] **Step 3: Flip channels bridges**

In `periscope/channels.py`, find every `# BRIDGE: removed in Peel 4` marker. **Expected: exactly 2 markers** (one each in `_do_link_pr_tool` and `_do_link_linear_tool`). For each, replace the function-local `from server import _STATE, _STATE_LOCK, _write_state` with the module-top equivalent. Add to `periscope/channels.py`'s module-top imports:

```python
from periscope.store import _STATE, _STATE_LOCK, _write_state
```

Delete the function-local lines (now redundant) and the `# BRIDGE` markers.

Verify no stray `from server import _STATE` remain in `periscope/`:

```sh
grep -rn "from server import" /Users/tom/dev/periscope/periscope/
```

Expected output should show ONLY the two surviving bridges (panes / pids ones from Peel 3):
```
periscope/channels.py:NNN:    from server import list_windows, _attach_git_then_resolve_pids
periscope/channels.py:NNN:    from server import list_windows, note_focus, note_action, _attach_git_then_resolve_pids
```

- [ ] **Step 4: Re-point `tests/test_store.py`**

Change all `from server import ...` and `import server` to use `periscope.store`. The `_reimport_store` helper at the top of the file can be deleted — module reloading isn't needed when the test directly seeds `monkeypatch.setattr(store, "_STATE", ...)`. Update the per-test imports:

```python
# Examples:
def test_state_path_under_xdg(tmp_xdg_home):
    from periscope.store import _state_path
    assert _state_path() == tmp_xdg_home / "periscope" / "state.json"

def test_seed_commands_fills_empty_list(tmp_xdg_home, monkeypatch):
    import periscope.store as store
    monkeypatch.setattr(store, "_STATE", {
        "version": 1, "ui": {}, "windows": {}, "commands": [],
    })
    store._seed_commands_if_empty()
    labels = [c["label"] for c in store._STATE["commands"]]
    assert labels == ["claude", "shell", "vim"]
```

Drop the now-unused `_reimport_store` helper and the `importlib`/`sys` imports at the top.

- [ ] **Step 5: Also update `tests/test_channels.py`**

The link-tool tests do `import server; server._STATE.update(clean_state)`. After Peel 4, `_STATE` lives in `periscope.store`. Change to:

```python
import periscope.store as store
store._STATE.update(clean_state)
```

And the `mocker.patch("server._write_state", ...)` → `mocker.patch("periscope.store._write_state", ...)`. Also `mocker.patch("server._resolve_pid_for_pane", ...)` → `mocker.patch("periscope.channels._resolve_pid_for_pane", ...)` (this was changed in Peel 3 Task 3.3 Step 3 — verify it's already pointing at `periscope.channels`).

- [ ] **Step 6: Run the verification gate**

```sh
uv run pytest -q
```

Expected: all green.

Boot smoke:

```sh
uv run server.py &
sleep 3
curl -fsS http://127.0.0.1:8765/api/state > /dev/null && echo "API OK"
kill %1; wait 2>/dev/null
```

- [ ] **Step 7: Manual smoke — prefs round-trip + channels regression**

```sh
# Prefs:
curl -fsS -X PATCH http://127.0.0.1:8765/api/prefs/ui \
  -H 'Content-Type: application/json' -d '{"foo":"bar"}'
curl -fsS http://127.0.0.1:8765/api/prefs \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['ui'].get('foo'))"
# Expected: bar

# Clean up:
curl -fsS -X PATCH http://127.0.0.1:8765/api/prefs/ui \
  -H 'Content-Type: application/json' -d '{"foo":null}'
```

If you have a Claude session attached to periscope from Peel 3's smoke, re-run a `link_pr` to confirm the channels→store wiring survives the bridge flip.

- [ ] **Step 8: Commit**

```sh
git add periscope/store.py periscope/channels.py tests/test_store.py tests/test_channels.py server.py
git commit -m "split: extract state.json layer + tests, flip channels bridges (Peel 4)"
```

---

## End of Stage A — checkpoint

After Peel 4:

```sh
wc -l /Users/tom/dev/periscope/server.py
wc -l /Users/tom/dev/periscope/periscope/*.py
uv run pytest -q
```

Expected:
- `server.py`: ~2350–2400 lines (from 3543)
- `periscope/`: config (~20) + log (~70) + pidfile (~85) + tmux (~70) + channels (~560) + store (~115) = ~920 lines
- pytest: all green across `tests/` + `history/tests/`

Proceed to Stage B (separate plan).

---

## What's NOT in this plan

- **Stage B (Peels 5–9):** panes/pids, LGTM, git_pr/usage/rename_ai, routes split, final `app` move. Separate plan with its own plan-review.
- **Behavior changes.** Every endpoint behaves bit-identically.
- **Folding `test_parse_pane.py` into `tests/test_panes.py`.** Happens in Stage B Peel 5.
- **The shim refactor of `server.py`.** Through Stage A, `server.py` is still the FastAPI app and entry point. Stage B Peel 9 moves `app` to `periscope/app.py`.
