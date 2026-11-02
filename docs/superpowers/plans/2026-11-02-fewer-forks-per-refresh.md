# Fewer Forks Per Refresh Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `build_state` measures its own cost, forks one `tmux` and one `ps` per refresh instead of six processes, keeps git off the request path, and runs every `gh` without a keychain read (THI-450; spec `docs/superpowers/specs/2026-11-02-fewer-forks-per-refresh-design.md`).

**Architecture:** A Python audit hook (`periscope/forks.py`) counts every `subprocess.Popen` by command over a rolling minute; `build_state` and the state hub record their timings there and `/api/state` reports them as `refresh`. One `list-panes -a` (`tmux.list_panes`, refreshed by every `list_windows`) and one `ps` (`agent_processes.process_snapshot`) feed every per-refresh reader; git reads two commands per cwd behind a jittered stale-while-revalidate cache, and `gh` goes through one `gh.run` that carries a token read once an hour.

**Tech Stack:** Python 3.14 / FastAPI / pytest + pytest-mock; Preact + `@preact/signals`, Vite, vitest; tmux, git (porcelain v2), gh.

---

## Conventions

- **Worktree:** `/Users/thibaultdody/dev/worktrees/periscope/thi-450-fewer-forks` (branch `thibaultdody/thi-450-build_state-runs-too-many-subprocesses-per-tick-so-the`). Every path below is relative to it. Never touch the prod tree `/Users/thibaultdody/github/tomplex/periscope` or anything on `:17374`.
- **Read first:** the spec, `docs/invariants.md` (#1, #8, #11, #13, #18, #19), `docs/testing.md`, `tests/conftest.py`.
- **Python tests:** `uv run pytest -q <paths>`; full suite `uv run pytest -q`.
- **Frontend:** the worktree has no `node_modules` — run `npm install` once before any `npm`/`npx` command. Tests `npm test`; build `npm run build`.
- **Gate:** `bin/check; echo "exit=$?"` — read the real exit code, never pipe it through `tail`/`head`.
- **Before each commit:** `uv run ruff check --fix <py paths> && uv run ruff format <py paths>` on the task's Python files (sorts imports the steps add).
- **Commits:** one per task, single-line subject `area: what (THI-450)`, a blank line, then the implementing model's attribution line. Commit only the task's paths: new files need `git add <file>` first, then `git commit -m "<subject>" -m "Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>" -- <paths>`. Replace the Co-Authored-By line with your own model's if you are not Claude Opus 5.5.
- **Background work** goes through `periscope.log._bg` (invariant #8). Tests never start a real background thread for it: they patch the module's `_bg`, as `test_reviews.py` already does.
- **No test forks real tmux, ps or gh.** `tests/conftest.py` seeds each new snapshot cache with an unexpiring empty entry (the `_config_dirs_cache` pattern `test_session_status.py` already uses); a test of the real path resets the seed itself.

## File structure

| File | Change | Responsibility |
|---|---|---|
| `periscope/forks.py` | Create | Audit hook, rolling fork counter, build/gap timings, the `refresh` block |
| `periscope/app.py` | Modify (lifespan, ~L54–58) | Install the hook |
| `periscope/routes/state.py` | Modify (L16, L69, L167–208) | Time `build_state`; add `refresh` to the blob |
| `periscope/state_hub.py` | Modify (imports, `_run_loop` L96–111) | Record the gap between broadcasts |
| `periscope/session_status.py` | Modify (L29–58, L128–241, L243–259) | `proc_table` / `_claude_procs` from `process_snapshot`; `_tmux_pane_pids` from `tmux.recent_panes`; `parse_etime` deleted |
| `periscope/window_view.py` | Modify (comment L164–171) | Comment names the shared snapshot |
| `periscope/tmux.py` | Modify (imports, `_run` L33–43, new section) | `list_panes` / `recent_panes`; `_run(env=)` |
| `periscope/panes.py` | Modify (L21, L385–449) | `list_windows` and `all_pane_ids` read `list_panes` / `recent_panes` |
| `periscope/git_pr.py` | Modify (L18–43, L65–128, gh call sites) | Two-command `git_state_for`, repo-identity cache, SWR `cached_git_state`, `gh.run` |
| `periscope/gh.py` | Create | The one way periscope runs `gh` |
| `periscope/reviews.py`, `periscope/cleanup.py`, `periscope/projects.py` | Modify (their `gh` call) | Use `gh.run` |
| `static/src/store.js`, `static/src/poll.js` | Modify | `refresh` signal |
| `static/src/chrome/resources.js`, `static/src/chrome/ResourcePill.jsx`, `static/styles.css` | Modify | The popover's refresh line |
| `static/dist/*` | Rebuild | Committed bundle |
| `tests/conftest.py` | Modify | Seed tmux / ps / gh-token caches; fresh git caches |
| `tests/test_forks.py`, `tests/test_gh.py` | Create | Hook, counter, stats; gh environment |
| `tests/test_panes.py`, `tests/test_tmux.py`, `tests/test_session_status.py`, `tests/test_git_pr.py`, `tests/test_reviews.py`, `tests/test_state_hub.py`, `tests/routes/test_state.py` | Modify | Cases per task |
| `static/src/chrome/__tests__/resources.test.js`, `resourcePillRender.test.jsx` | Modify | Refresh line |
| `docs/invariants.md`, `docs/testing.md`, `CLAUDE.md` | Modify | Snapshot note, conftest seeds, module table |

`periscope/gitutil.py` does not change: the 10-minute identity cache lives in `git_pr.py`, because `resolve_repo` / `github_slug` have other callers (open_ops, reviews) that need a live answer.

---

## Task 1: The fork counter

**Files:**
- Create: `periscope/forks.py`
- Modify: `periscope/app.py` (lifespan, the first lines after `async def lifespan`)
- Test: `tests/test_forks.py` (new)

- [ ] **Step 1: Write the failing tests** — create `tests/test_forks.py`:

```python
"""forks — the audit hook that counts every fork the server makes, and the
per-refresh cost /api/state reports."""

import subprocess
import sys
from collections import deque
from types import SimpleNamespace

import pytest

from periscope import forks


@pytest.fixture(autouse=True)
def _fresh_counters(monkeypatch):
    monkeypatch.setattr(forks, "_forks", deque())


def _popen(*argv):
    forks._on_audit("subprocess.Popen", (argv[0], list(argv), None, None))


@pytest.mark.parametrize(
    ("argv", "key"),
    [
        (["git", "-C", "/repo", "status", "--porcelain=v2"], "git status"),
        (["git", "--no-optional-locks", "-C", "repo", "diff", "HEAD"], "git diff"),
        (["/opt/homebrew/bin/tmux", "-L", "periscope-x", "list-panes", "-a"], "tmux list-panes"),
        (["ps", "-axo", "pid=,ppid=,lstart=,state=,comm=,args="], "ps"),
        (["ps", "eww", "-o", "pid=,command=", "-p", "1,2"], "ps eww"),
        (["gh", "pr", "list", "--head", "x"], "gh pr"),
        (["/bin/sh", "-c", "echo hi"], "sh"),
        ([b"/usr/bin/true"], "true"),
    ],
)
def test_command_key_is_the_binary_and_its_subcommand(argv, key):
    assert forks._command_key(argv) == key


def test_hook_ignores_every_other_audit_event():
    forks._on_audit("open", ("/etc/hosts", "r", 0))
    # An argv the key can't be read from is dropped, never raised: raising
    # would abort the Popen being audited.
    forks._on_audit("subprocess.Popen", ("x", [], None, None))
    assert forks.fork_counts() == {}


def test_counts_roll_off_after_a_minute(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(forks, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    _popen("git", "-C", "/r", "status")
    clock[0] += 30
    _popen("git", "-C", "/r", "status")
    _popen("ps", "-axo", "pid=")
    assert forks.fork_counts() == {"git status": 2, "ps": 1}
    clock[0] += 31
    assert forks.fork_counts() == {"git status": 1, "ps": 1}


def test_installed_hook_counts_run_and_popen():
    forks.install()
    subprocess.run(["true"], check=True)
    subprocess.Popen(["true"]).wait()
    assert forks.fork_counts()["true"] == 2


def test_install_adds_the_hook_once(monkeypatch):
    added = []
    monkeypatch.setattr(forks, "_installed", False)
    monkeypatch.setattr(sys, "addaudithook", added.append)
    forks.install()
    forks.install()
    assert added == [forks._on_audit]
```

- [ ] **Step 2: Run them and see them fail**

Run: `uv run pytest -q tests/test_forks.py`
Expected: collection error `ImportError: cannot import name 'forks' from 'periscope'`.

- [ ] **Step 3: Implement** — create `periscope/forks.py`:

```python
"""Every fork the server makes, counted by command over a rolling minute.

Counted by a Python audit hook on `subprocess.Popen` — the event
`subprocess.run`, `check_output` and asyncio's subprocesses all raise — so no
call site changes and background threads count too."""

import os
import re
import sys
import threading
import time
from collections import Counter, deque
from collections.abc import Sequence
from typing import Any

_WINDOW_S = 60.0
# The subcommand is the first word-like argument after the binary, so
# `git -C <dir> status` counts as `git status` and `ps -axo pid=,…` as `ps`.
_SUBCOMMAND_RE = re.compile(r"[a-z][a-z0-9-]*")
# Options whose value comes next and could pass for a subcommand
# (`tmux -L <socket>`, `git -C <relative dir>`, `sh -c <script>`).
_VALUE_FLAGS = frozenset({"-C", "-L", "-S", "-c"})

_lock = threading.Lock()
# (monotonic time, command key), oldest first.
_forks: deque[tuple[float, str]] = deque()
_installed = False


def _command_key(argv: Sequence[Any]) -> str:
    args = [os.fsdecode(a) for a in argv]
    key = os.path.basename(args[0])
    skip = False
    for a in args[1:]:
        if skip:
            skip = False
        elif a in _VALUE_FLAGS:
            skip = True
        elif _SUBCOMMAND_RE.fullmatch(a):
            return f"{key} {a}"
    return key


def _prune(q: deque, now: float) -> None:
    while q and q[0][0] < now - _WINDOW_S:
        q.popleft()


def _on_audit(event: str, args: tuple[Any, ...]) -> None:
    if event != "subprocess.Popen":
        return
    # args is (executable, argv, cwd, env). An exception here would abort the
    # Popen being audited, so an argv the key can't be read from goes uncounted.
    try:
        key = _command_key(args[1])
    except Exception:
        return
    now = time.monotonic()
    with _lock:
        _forks.append((now, key))
        _prune(_forks, now)


def install() -> None:
    """Start counting. An audit hook can't be removed, and the lifespan runs
    more than once in one process under tests — so add it once."""
    global _installed
    if not _installed:
        sys.addaudithook(_on_audit)
        _installed = True


def fork_counts() -> Counter[str]:
    """Forks per command over the last minute."""
    now = time.monotonic()
    with _lock:
        _prune(_forks, now)
        return Counter(key for _, key in _forks)
```

In `periscope/app.py`, the lifespan's first lines become:

```python
@asynccontextmanager
async def lifespan(_app: FastAPI):
    from periscope import config, forks

    forks.install()
    log.info("periscope starting (pid=%d, port=%d)", os.getpid(), config.PORT)
```

(replacing `from periscope import config` + the blank line before `log.info`; nothing else in the lifespan changes).

- [ ] **Step 4: Run the tests and see them pass**

Run: `uv run pytest -q tests/test_forks.py tests/test_app.py`
Expected: all pass.

- [ ] **Step 5: Commit**

```sh
uv run ruff check --fix periscope/forks.py periscope/app.py tests/test_forks.py && uv run ruff format periscope/forks.py periscope/app.py tests/test_forks.py
git add periscope/forks.py tests/test_forks.py
git commit -m "forks: an audit hook counts every fork the server makes, by command, over a rolling minute (THI-450)" -m "Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>" -- periscope/forks.py periscope/app.py tests/test_forks.py
```

---

## Task 2: Each refresh reports its cost

**Files:**
- Modify: `periscope/forks.py` (append), `periscope/routes/state.py` (import L16; `build_state` L69 and L167–208), `periscope/state_hub.py` (imports; `_run_loop` L96–111)
- Test: `tests/test_forks.py`, `tests/routes/test_state.py`, `tests/test_state_hub.py`

- [ ] **Step 1: Write the failing tests**

In `tests/test_forks.py`, add `import logging` to the imports and replace the `_fresh_counters` fixture with:

```python
@pytest.fixture(autouse=True)
def _fresh_counters(monkeypatch):
    monkeypatch.setattr(forks, "_forks", deque())
    monkeypatch.setattr(forks, "_builds", deque())
    monkeypatch.setattr(forks, "_last_gap", None)
```

and append:

```python
def test_refresh_stats_report_the_last_minute(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(forks, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    forks.record_build(2.0)
    clock[0] += 30
    for _ in range(3):
        _popen("git", "-C", "/r", "status")
    _popen("tmux", "list-panes", "-a")
    forks.record_build(0.4)
    forks.record_gap(1.1)
    assert forks.refresh_stats() == {
        "build_s": 0.4,
        "build_max_s": 2.0,
        "gap_s": 1.1,
        "forks_per_min": 4,
        "top_forks": [("git status", 3), ("tmux list-panes", 1)],
    }
    clock[0] += 31
    assert forks.refresh_stats()["build_max_s"] == 0.4  # the 2.0 s build rolled off


def test_gap_is_none_until_the_hub_has_broadcast_twice():
    forks.record_build(0.1)
    assert forks.refresh_stats()["gap_s"] is None


def test_top_forks_are_the_five_largest():
    for n, cmd in enumerate(["a", "b", "c", "d", "e", "f"], start=1):
        for _ in range(n):
            _popen(cmd)
    forks.record_build(0.1)
    assert [k for k, _ in forks.refresh_stats()["top_forks"]] == ["f", "e", "d", "c", "b"]


def test_a_slow_build_is_logged_with_its_top_forkers(caplog):
    for _ in range(3):
        _popen("git", "-C", "/r", "status")
    with caplog.at_level(logging.WARNING, logger="periscope"):
        forks.record_build(3.4)
    assert "3.4 s" in caplog.text
    assert "git status" in caplog.text


def test_a_normal_build_logs_nothing(caplog):
    with caplog.at_level(logging.WARNING, logger="periscope"):
        forks.record_build(2.9)
    assert caplog.text == ""
```

In `tests/routes/test_state.py`, append:

```python
def test_state_reports_what_the_refresh_cost(client, mocker, clean_state):
    _patch(mocker, "list_windows", return_value=[])
    _patch(mocker, "update_focus_from_windows")
    _patch(mocker, "_attach_git_then_resolve_pids")
    _patch(mocker, "cached_claude_usage", return_value={})
    _patch(mocker, "cached_plan_usage", return_value={})
    _patch(mocker, "all_pane_ids", return_value=set())

    r = client.get("/api/state").json()["refresh"]
    assert set(r) == {"build_s", "build_max_s", "gap_s", "forks_per_min", "top_forks"}
    assert 0 <= r["build_s"] <= r["build_max_s"]
```

In `tests/test_state_hub.py`, add `import contextlib` to the imports and append:

```python
def test_run_loop_records_gaps_between_broadcasts_but_not_across_idle(monkeypatch, mocker):
    gaps = mocker.patch("periscope.state_hub.forks.record_gap")
    monkeypatch.setattr(state_hub, "_TICK_INTERVAL_S", 0.01)
    monkeypatch.setattr(state_hub, "_IDLE_POLL_S", 0.01)
    builds = []

    async def until(n):
        while len(builds) < n:
            await asyncio.sleep(0.005)

    async def main():
        # The module's Event binds to the first loop that waits on it; give
        # this test's loop its own.
        monkeypatch.setattr(state_hub, "_wake", asyncio.Event())
        q = state_hub.subscribe()
        task = asyncio.create_task(state_hub._run_loop(asyncio.get_running_loop(), lambda: builds.append(1) or {}))
        await until(3)
        state_hub.unsubscribe(q)
        await asyncio.sleep(0.5)  # idle: nothing built, and no gap spans it
        state_hub.subscribe()
        await until(len(builds) + 2)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(main())
    recorded = [c.args[0] for c in gaps.call_args_list]
    assert len(recorded) >= 3
    assert all(0 < g < 0.4 for g in recorded)
```

- [ ] **Step 2: Run them and see them fail**

Run: `uv run pytest -q tests/test_forks.py tests/routes/test_state.py tests/test_state_hub.py`
Expected: `AttributeError: <module 'periscope.forks'> does not have the attribute '_builds'` from the fixture, `KeyError: 'refresh'` in the route test, and `AttributeError: ... 'periscope.state_hub' ... has no attribute 'forks'` in the hub test.

- [ ] **Step 3: Implement**

Append to `periscope/forks.py` (and add `from periscope.log import log` to its imports; update the module docstring's first line to "Every fork the server makes, counted by command over a rolling minute, and what each dashboard refresh costs (/api/state `refresh`)."):

```python
# A build this slow is logged with the commands that forked most.
_SLOW_BUILD_S = 3.0
# (monotonic time, build_state seconds), oldest first.
_builds: deque[tuple[float, float]] = deque()
# The state hub's last gap between two broadcasts; None until it has made two.
_last_gap: float | None = None


def _secs(s: float | None) -> float | None:
    return None if s is None else round(s, 2)


def record_build(seconds: float) -> None:
    """Note one build_state's duration; a slow one is logged with the commands
    that forked most over the last minute."""
    with _lock:
        _builds.append((time.monotonic(), seconds))
    if seconds > _SLOW_BUILD_S:
        log.warning(
            "slow refresh: build_state took %.1f s; top forks in the last minute: %s",
            seconds,
            fork_counts().most_common(5),
        )


def record_gap(seconds: float) -> None:
    """Note the state hub's gap between two broadcasts — how often a dashboard
    actually updates."""
    global _last_gap
    _last_gap = seconds


def refresh_stats() -> dict:
    """/api/state's `refresh` block: the last build, the slowest over the last
    minute, the hub's last gap, and the minute's forks — total and the five
    largest commands."""
    counts = fork_counts()
    now = time.monotonic()
    with _lock:
        _prune(_builds, now)
        last = _builds[-1][1] if _builds else None
        slowest = max((s for _, s in _builds), default=None)
    return {
        "build_s": _secs(last),
        "build_max_s": _secs(slowest),
        "gap_s": _secs(_last_gap),
        "forks_per_min": sum(counts.values()),
        "top_forks": counts.most_common(5),
    }
```

`periscope/routes/state.py`:
- L16: `from periscope import forks, resources, store, updater, usage`
- In `build_state`, replace

```python
    windows = list_windows()
    update_focus_from_windows(windows)
```

with

```python
    started = time.monotonic()
    windows = list_windows()
    update_focus_from_windows(windows)
```

- replace

```python
    walls = usage.walls()
    return {
```

with

```python
    walls = usage.walls()
    blob = {
```

- replace the dict's closing

```python
        "resources": measured.totals if measured else None,
    }
```

with

```python
        "resources": measured.totals if measured else None,
    }
    forks.record_build(time.monotonic() - started)
    # What this refresh cost and how often the hub pushes one (forks.py) — the
    # resource popover's last line.
    blob["refresh"] = forks.refresh_stats()
    return blob
```

These three edits are the whole change to `build_state`.

`periscope/state_hub.py`: add `import time` to the stdlib imports and `from periscope import forks` above `from periscope.log import log`; replace `_run_loop` with:

```python
async def _run_loop(loop: asyncio.AbstractEventLoop, build_state: Callable[[], dict]) -> None:
    # When the last broadcast went out. Reset by an idle stretch: the gap
    # across one is how long nobody watched, not a refresh interval.
    last_at: float | None = None
    while True:
        if not _subscribers:
            # Nobody watching: compute nothing. Wait for a subscriber (or kick).
            last_at = None
            _wake.clear()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(_wake.wait(), timeout=_IDLE_POLL_S)
            continue
        try:
            blob = await loop.run_in_executor(None, build_state)
            _broadcast(json.dumps(blob))
            now = time.monotonic()
            if last_at is not None:
                forks.record_gap(now - last_at)
            last_at = now
        except Exception:
            log.warning("state broadcast tick failed", exc_info=True)
        # Sleep the interval, but cut it short if an event kicks us.
        _wake.clear()
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(_wake.wait(), timeout=_TICK_INTERVAL_S)
```

- [ ] **Step 4: Run the tests and see them pass**

Run: `uv run pytest -q tests/test_forks.py tests/routes/test_state.py tests/test_state_hub.py`
Expected: all pass.

- [ ] **Step 5: Commit** (Task 9 measures "before" at this commit — keep the subject exactly)

```sh
uv run ruff check --fix periscope/forks.py periscope/routes/state.py periscope/state_hub.py tests/test_forks.py tests/routes/test_state.py tests/test_state_hub.py && uv run ruff format periscope/forks.py periscope/routes/state.py periscope/state_hub.py tests/test_forks.py tests/routes/test_state.py tests/test_state_hub.py
git commit -m "state: /api/state reports what each refresh costs (THI-450)" -m "Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>" -- periscope/forks.py periscope/routes/state.py periscope/state_hub.py tests/test_forks.py tests/routes/test_state.py tests/test_state_hub.py
```

---

## Task 3: One process table per refresh

Done before the tmux snapshot (the suggested order swapped) so that when Task 4 drops conftest's `_tmux_pane_pids` stub, `proc_table` already reads a seeded `ps` snapshot and no test can fork real `ps`.

**Files:**
- Modify: `periscope/session_status.py` (imports L37; docstring last paragraph; cache block L44–58; delete `parse_etime` L128–148; `_claude_procs` L151–181; `proc_table` L188–218), `periscope/window_view.py` (comment L164–171), `tests/conftest.py` (`_no_live_session_scan`)
- Test: `tests/test_session_status.py`

- [ ] **Step 1: Write the failing tests** — in `tests/test_session_status.py`:
  - add `from datetime import datetime`, `from types import SimpleNamespace` and `from periscope import agent_processes` to the imports;
  - in `_reset_caches`, delete the two lines `monkeypatch.setattr(ss, "_claude_procs_cache", None)` and `monkeypatch.setattr(ss, "_proc_table_cache", None)`;
  - delete `_PS_SAMPLE`, `test_live_claude_pids_matches_claude_paths`, `test_claude_procs_carry_age`, the `── etime parsing` header and `test_parse_etime` (L122–169), and put in their place:

```python
# ── live claudes and the process table come off process_snapshot ─────────

# The processes the old `ps -axo pid=,etime=,comm=` sample listed (etimes
# 01-02:08:58, 22:09:53, 05:33, 91-10:00:00), as process_snapshot's
# `pid ppid lstart state comm args` rows read at _PS_NOW.
_PS_SNAPSHOT = (
    " 9448     1 Thu Jul 30 10:00:00 2026 S    /Users/tom/.local/bin/claude claude --resume 0b6f2a1e-1111-2222-3333-444455556666\n"
    " 9876  9448 Thu Jul 30 13:59:05 2026 S    /Users/tom/.local/share/claude/versions/2.1.161 claude\n"
    "  329     1 Fri Jul 31 12:03:25 2026 Ss   /usr/libexec/logd /usr/libexec/logd\n"
    "    1     0 Fri May  1 02:08:58 2026 Ss   /sbin/launchd /sbin/launchd\n"
)
_PS_NOW = datetime(2026, 7, 31, 12, 8, 58).timestamp()


def _snapshot(monkeypatch, text=_PS_SNAPSHOT):
    monkeypatch.setattr(agent_processes, "process_snapshot", lambda: agent_processes.parse_ps_snapshot(text))
    monkeypatch.setattr(ss, "time", SimpleNamespace(time=lambda: _PS_NOW))


def test_live_claude_pids_matches_claude_paths(monkeypatch):
    _snapshot(monkeypatch)
    assert ss._live_claude_pids() == {9448, 9876}


def test_claude_procs_age_from_start_time(monkeypatch):
    # The same ages `ps etime` reported: now minus each process's start.
    _snapshot(monkeypatch)
    assert ss._claude_procs() == {
        9448: 26 * 3600 + 8 * 60 + 58,
        9876: 22 * 3600 + 9 * 60 + 53,
    }


def test_proc_table_matches_the_command_table(monkeypatch):
    # What `ps -axo pid=,ppid=,command=` gave: children by parent in ps order,
    # and each pid's full argv.
    _snapshot(monkeypatch)
    kids, cmds = ss.proc_table()
    assert kids == {1: [9448, 329], 9448: [9876], 0: [1]}
    assert cmds == {
        9448: "claude --resume 0b6f2a1e-1111-2222-3333-444455556666",
        9876: "claude",
        329: "/usr/libexec/logd",
        1: "/sbin/launchd",
    }


def test_ps_failure_reads_as_no_processes(monkeypatch):
    monkeypatch.setattr(agent_processes, "process_snapshot", lambda: None)
    assert ss._claude_procs() == {}
    assert ss.proc_table() == ({}, {})


def test_one_ps_serves_the_whole_refresh(monkeypatch, mocker):
    monkeypatch.setattr(agent_processes, "_snapshot_cache", None)
    take = mocker.patch.object(
        agent_processes, "_take_snapshot", return_value=agent_processes.parse_ps_snapshot(_PS_SNAPSHOT)
    )
    mocker.patch("subprocess.run", side_effect=AssertionError("session_status forked its own ps"))
    ss.proc_table()
    ss._claude_procs()
    agent_processes.process_snapshot()
    take.assert_called_once()
```

- [ ] **Step 2: Run them and see them fail**

Run: `uv run pytest -q tests/test_session_status.py`
Expected: the five new tests fail — today's `_claude_procs` / `proc_table` fork their own `ps` and never read `process_snapshot`: the fixture pids are absent from the machine's real `ps` output, and in `test_one_ps_serves_the_whole_refresh` the patched `subprocess.run` raises `AssertionError: session_status forked its own ps`.

- [ ] **Step 3: Implement** — `periscope/session_status.py`:
  - L37: `from periscope import agent_processes, config, resources`
  - Docstring's last paragraph becomes: "Imports only stdlib and periscope's leaf modules (config, resources, agent_processes), never the resolver layer, so turns.py / window_view.py can depend on it freely."
  - Replace the cache block (L44–58) with:

```python
# Per-poll caches. build_window_view calls session_state_for once per pane every
# refresh; scanning ~30 tiny files per pane would be wasteful, so the session
# index and the pane -> claude walk are built once and reused for the brief
# window a single refresh spans. Processes come from the refresh's one `ps`
# (agent_processes.process_snapshot).
_CACHE_TTL_S = 1.0
# (built_at, {sid: file}, {pid: [files newest-first]}) — one scan of every live
# account's sessions dir answers both directions (sessionId -> state,
# pid -> current sessionId). by_pid holds a LIST because the same OS pid can
# have a file in more than one account's dir (recycled pid, account swap).
_index_cache: tuple[float, dict, dict[int, list[dict]]] | None = None
_pane_pids_cache: tuple[float, dict[str, int]] | None = None
```

  - Delete `parse_etime` entirely (its only callers were `_claude_procs` and its test).
  - Replace `_claude_procs` and `proc_table` with:

```python
def _claude_procs() -> dict[int, int]:
    """{pid: age_s} of running `claude` processes, from the refresh's process
    snapshot. The key set guards against honoring a session file whose
    process has exited or whose pid was recycled; age (now minus start) feeds
    the cycle-hint. `comm` is the executable path; claude's launcher
    (`.../bin/claude`) and versioned binary (`.../share/claude/versions/<v>`)
    both contain "claude"."""
    now = time.time()
    return {
        p.pid: int(now - p.started_at)
        for p in (agent_processes.process_snapshot() or {}).values()
        if "claude" in p.comm.lower()
    }
```

```python
def proc_table() -> tuple[dict[int, list[int]], dict[int, str]]:
    """(ppid -> child pids, pid -> command) for every process, from the
    refresh's process snapshot. Empty when ps failed."""
    kids: dict[int, list[int]] = {}
    cmds: dict[int, str] = {}
    for p in (agent_processes.process_snapshot() or {}).values():
        kids.setdefault(p.ppid, []).append(p.pid)
        cmds[p.pid] = p.argv
    return kids, cmds
```

`periscope/window_view.py`, replace the comment lines

```python
# One account scan per POLL, not per window. `pane_config_dirs` forks `ps`
# once for the process table plus once more per candidate claude;
# build_window_view runs across ~20 windows on a 32-thread fan-out every 3s, so
# per-window would be hundreds of forks a poll. Same TTL-snapshot shape as
# session_status._claude_procs, plus a lock — without one the fan-out's cold
# start stampedes 32 concurrent scans instead of 31 waiting on the first.
# The scan costs ~100ms — two `ps` forks, and the cost is the forks themselves,
# not the number of processes probed. That lands on the /api/state hot path,
```

with

```python
# One account scan per POLL, not per window. `pane_config_dirs` forks one
# `ps eww` over every candidate claude (the process table is the refresh's
# shared snapshot); build_window_view runs across ~20 windows on a 32-thread
# fan-out, so per-window would be dozens of forks a poll. Same TTL-snapshot
# shape as agent_processes.process_snapshot, plus a lock — without one the
# fan-out's cold start stampedes 32 concurrent scans instead of 31 waiting on
# the first. The cost is the fork itself, not the number of processes
# probed. That lands on the /api/state hot path,
```

`tests/conftest.py`: add `import math` to the imports and replace `_no_live_session_scan` with:

```python
@pytest.fixture(autouse=True)
def _no_live_session_scan(monkeypatch):
    """Keep turns.session_id_for_pane's live lookup off the real machine.

    It resolves a pane to its running claude by walking the process tree
    (`tmux list-panes` + `ps`), and it runs on every /api/state poll — so any
    test that builds a window view would otherwise fork against the developer's
    real tmux server and read their real ~/.claude/sessions. Stubbing the tmux
    seam makes pane_claude_pids() return {} by its own no-panes path, and an
    unexpiring empty process snapshot answers proc_table() / _claude_procs()
    without forking `ps`; every test falls back to the pane_sessions row unless
    it opts in. Tests of the live path patch these same seams themselves, which
    overrides this."""
    try:
        from periscope import agent_processes, session_status
    except ImportError:
        return
    monkeypatch.setattr(session_status, "_tmux_pane_pids", dict, raising=False)
    monkeypatch.setattr(session_status, "_pane_pids_cache", None, raising=False)
    monkeypatch.setattr(agent_processes, "_snapshot_cache", (math.inf, {}), raising=False)
    # pane_config_dirs' 15s TTL outlives a test — a populated cache would bleed
    # one test's pane->config-dir map into every later view build.
    monkeypatch.setattr(session_status, "_config_dirs_cache", None, raising=False)
```

- [ ] **Step 4: Run the tests and see them pass**

Run: `uv run pytest -q tests/test_session_status.py tests/test_agent_processes.py tests/test_window_view.py tests/test_resources.py tests/routes/test_state.py tests/test_channels.py tests/test_resurrect.py`
Expected: all pass. Then `uv run pytest -q` — all pass.

- [ ] **Step 5: Commit**

```sh
uv run ruff check --fix periscope/session_status.py periscope/window_view.py tests/conftest.py tests/test_session_status.py && uv run ruff format periscope/session_status.py periscope/window_view.py tests/conftest.py tests/test_session_status.py
git commit -m "session_status: proc_table and live claudes read the refresh's one process snapshot (THI-450)" -m "Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>" -- periscope/session_status.py periscope/window_view.py tests/conftest.py tests/test_session_status.py
```

---

## Task 4: One tmux call per refresh

`list_windows` keeps forking a fresh `list-panes -a` on every call: open_ops reads freshly created windows straight back ("list_windows() is a live shell-out, so freshly-stamped windows are visible synchronously"), and `pids` stamps `@periscope_id` with a bare `tmux set-option`, so a cached answer would show a just-stamped window unstamped and re-mint it (invariants #11, #13). Each call refreshes the shared snapshot, and `all_pane_ids` and `session_status._tmux_pane_pids` read that snapshot (1 s, locked) — so a refresh forks tmux once, at its `list_windows`.

**Files:**
- Modify: `periscope/tmux.py` (imports; new section after `_tmux_argv`/`tmux`), `periscope/panes.py` (L21; `all_pane_ids` + `list_windows` L385–449), `periscope/session_status.py` (imports; docstring; `_tmux_pane_pids` L221–241; `pane_claude_pids` docstring), `tests/conftest.py` (`_no_live_session_scan`)
- Test: `tests/test_tmux.py`, `tests/test_panes.py`, `tests/test_session_status.py`

- [ ] **Step 1: Write the failing tests**

`tests/test_tmux.py` — add `import periscope.tmux as tmux_mod` and `from types import SimpleNamespace` to the imports; append:

```python
_ROW = "main\t0\tshell\t1\t/home/tom\t\t%5\t1717250000\t@1\t4242\tzsh\t\t1"


def test_list_panes_asks_for_every_field_in_one_call(monkeypatch):
    calls = []
    monkeypatch.setattr(tmux_mod, "tmux", lambda *a: calls.append(a) or "")
    tmux_mod.list_panes()
    assert calls == [("list-panes", "-a", "-F", "\t".join(f"#{{{f}}}" for f in tmux_mod.PANE_FIELDS))]


def test_list_panes_reads_every_field(monkeypatch):
    monkeypatch.setattr(tmux_mod, "tmux", lambda *a: _ROW + "\n")
    assert tmux_mod.list_panes() == [dict(zip(tmux_mod.PANE_FIELDS, _ROW.split("\t"), strict=True))]


def test_list_panes_skips_a_row_a_newline_in_a_path_split(monkeypatch):
    broken = _ROW.replace("/home/tom", "/home/to\nm")
    monkeypatch.setattr(tmux_mod, "tmux", lambda *a: broken + "\n" + _ROW.replace("%5", "%6") + "\n")
    assert [p["pane_id"] for p in tmux_mod.list_panes()] == ["%6"]


def test_recent_panes_reuses_a_snapshot_under_a_second_old(monkeypatch):
    clock = [100.0]
    calls = []
    monkeypatch.setattr(tmux_mod, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(tmux_mod, "_panes_snapshot", None)
    monkeypatch.setattr(tmux_mod, "tmux", lambda *a: calls.append(a) or _ROW + "\n")
    tmux_mod.list_panes()
    clock[0] += 0.5
    assert [p["pane_id"] for p in tmux_mod.recent_panes()] == ["%5"]
    assert len(calls) == 1
    clock[0] += 0.6
    tmux_mod.recent_panes()
    assert len(calls) == 2
```

`tests/test_panes.py` — add to the top import block: `import subprocess`, `import periscope.tmux as tmux_mod`, `from periscope import panes, session_status`, `from periscope.config import INPUT_CTL_SESSION`, `from tests.conftest import needs_tmux`. Delete `test_list_windows_parses_tmux_list_output`, `test_list_windows_appends_process_metadata`, `test_all_pane_ids_parses_and_dedupes`, `test_all_pane_ids_empty_on_blank`, `test_list_windows_parses_window_activity`, `test_list_windows_activity_defaults_zero_when_absent` (L1049–1114) and `test_list_windows_filters_input_ctl_session` (L1133–1139); keep `test_update_focus_ignores_window_activity`. Add in their place:

```python
# main:0 is split with its SECOND pane active; main:1 carries an open stamp;
# periscope's own input session comes last.
_PANES = (
    "\n".join(
        [
            "main\t0\tshell\t1\t/home/tom/dev/foo\tpid-a\t%5\t1717250000\t@1\t4241\tzsh\t\t0",
            "main\t0\tshell\t1\t/home/tom/dev/bar\tpid-a\t%6\t1717250000\t@1\t4242\tnvim\t\t1",
            "main\t1\tclaude\t0\t/home/tom/dev/baz\tpid-b\t%7\t1717250100\t@2\t4243\tclaude\t1717249990 /home/tom/dev/my baz\t1",
            f"{INPUT_CTL_SESSION}\t0\tctl\t1\t/tmp\t\t%9\t1717250200\t@3\t4244\tsh\t\t1",
        ]
    )
    + "\n"
)


def test_list_windows_reads_each_windows_active_pane(monkeypatch):
    monkeypatch.setattr(tmux_mod, "tmux", lambda *a: _PANES)
    assert list_windows() == [
        {
            "session": "main",
            "index": 0,
            "name": "shell",
            "active": True,
            "cwd": "/home/tom/dev/bar",
            "pid_raw": "pid-a",
            "pane_id": "%6",
            "activity": 1717250000,
            "window_id": "@1",
            "pane_pid": "4242",
            "current_command": "nvim",
            "opened": "",
        },
        {
            "session": "main",
            "index": 1,
            "name": "claude",
            "active": False,
            "cwd": "/home/tom/dev/baz",
            "pid_raw": "pid-b",
            "pane_id": "%7",
            "activity": 1717250100,
            "window_id": "@2",
            "pane_pid": "4243",
            "current_command": "claude",
            "opened": "1717249990 /home/tom/dev/my baz",
        },
    ]


def test_all_pane_ids_includes_a_split_windows_background_pane(monkeypatch):
    # Channel-alert GC keys on this: a background Claude pane (%5) must stay.
    monkeypatch.setattr(tmux_mod, "tmux", lambda *a: _PANES)
    monkeypatch.setattr(tmux_mod, "_panes_snapshot", None)
    assert panes.all_pane_ids() == {"%5", "%6", "%7", "%9"}


def test_no_tmux_server_means_no_windows_and_no_panes(monkeypatch):
    monkeypatch.setattr(tmux_mod, "tmux", lambda *a: "")
    monkeypatch.setattr(tmux_mod, "_panes_snapshot", None)
    assert list_windows() == []
    assert panes.all_pane_ids() == set()


def test_list_windows_never_serves_the_snapshot(monkeypatch):
    # A window created or stamped a moment ago must read back at once.
    calls = []
    monkeypatch.setattr(tmux_mod, "tmux", lambda *a: calls.append(a) or _PANES)
    list_windows()
    list_windows()
    assert len(calls) == 2


def test_one_list_panes_serves_the_whole_refresh(monkeypatch):
    calls = []
    monkeypatch.setattr(tmux_mod, "tmux", lambda *a: calls.append(a) or _PANES)
    windows = list_windows()
    assert panes.all_pane_ids() == {"%5", "%6", "%7", "%9"}
    assert session_status._tmux_pane_pids() == {"%5": 4241, "%6": 4242, "%7": 4243, "%9": 4244}
    assert len(windows) == 2
    assert len(calls) == 1


# Today's list-windows read, kept as the oracle the list-panes read reproduces.
_LIST_WINDOWS_FMT = (
    "#{session_name}\t#{window_index}\t#{window_name}\t#{window_active}\t#{pane_current_path}\t"
    "#{@periscope_id}\t#{pane_id}\t#{window_activity}\t#{window_id}\t#{pane_pid}\t"
    "#{pane_current_command}\t#{@periscope_opened}"
)


def _list_windows_rows(out):
    rows = []
    for line in out.strip().split("\n"):
        if not line:
            continue
        p = line.split("\t")
        if p[0] == INPUT_CTL_SESSION:
            continue
        rows.append(
            {
                "session": p[0],
                "index": int(p[1]),
                "name": p[2],
                "active": p[3] == "1",
                "cwd": p[4],
                "pid_raw": p[5],
                "pane_id": p[6],
                "activity": int(p[7]) if p[7] else 0,
                "window_id": p[8],
                "pane_pid": p[9],
                "current_command": p[10],
                "opened": p[11] if len(p) > 11 else "",
            }
        )
    return rows


@needs_tmux
def test_list_windows_matches_list_windows_on_a_live_server(tmux_test_server):
    def t(*args):
        return subprocess.run(
            ["tmux", "-L", tmux_test_server, *args], check=True, capture_output=True, text=True
        ).stdout

    t("new-session", "-d", "-s", "alpha", "-n", "one", "cat")
    t("split-window", "-t", "alpha:one", "cat")  # the new, second pane is active
    t("new-window", "-t", "alpha", "-n", "two", "cat")
    t("split-window", "-t", "alpha:two", "cat")
    t("select-pane", "-t", "alpha:two.0")  # the first pane of a split is active
    t("set-option", "-w", "-t", "alpha:two", "@periscope_id", "p-two")
    t("set-option", "-w", "-t", "alpha:two", "@periscope_opened", "1717249990 /tmp/my dir")
    t("new-session", "-d", "-s", "beta", "cat")
    t("new-session", "-d", "-s", INPUT_CTL_SESSION, "cat")
    t("select-window", "-t", "alpha:one")
    expected = _list_windows_rows(t("list-windows", "-a", "-F", _LIST_WINDOWS_FMT))
    assert [(w["session"], w["index"]) for w in expected] == [("alpha", 0), ("alpha", 1), ("beta", 0)]
    assert list_windows() == expected
    assert panes.all_pane_ids() == set(t("list-panes", "-a", "-F", "#{pane_id}").split())
```

`tests/test_session_status.py` — add `import subprocess` and `import periscope.tmux as tmux_mod` to the imports; append:

```python
# ── pane -> root pid comes off the list-panes snapshot ───────────────────


def test_tmux_pane_pids_read_the_snapshot(monkeypatch):
    rows = [{"pane_id": "%5", "pane_pid": "4241"}, {"pane_id": "%6", "pane_pid": "4242"}]
    monkeypatch.setattr(tmux_mod, "_panes_snapshot", (time.monotonic(), rows))
    assert ss._tmux_pane_pids() == {"%5": 4241, "%6": 4242}


def test_tmux_pane_pids_empty_without_tmux(monkeypatch):
    def no_tmux(*args):
        raise FileNotFoundError("tmux")

    monkeypatch.setattr(tmux_mod, "_panes_snapshot", None)
    monkeypatch.setattr(tmux_mod, "tmux", no_tmux)
    assert ss._tmux_pane_pids() == {}


def test_tmux_pane_pids_use_the_configured_socket(monkeypatch, mocker):
    # The old bare `tmux list-panes` ignored PERISCOPE_TMUX_SOCKET.
    monkeypatch.setenv("PERISCOPE_TMUX_SOCKET", "periscope-x")
    monkeypatch.setattr(tmux_mod, "_panes_snapshot", None)
    run = mocker.patch("subprocess.run", return_value=subprocess.CompletedProcess([], 0, stdout="", stderr=""))
    ss._tmux_pane_pids()
    assert run.call_args.args[0][:3] == ["tmux", "-L", "periscope-x"]
```

These exercise the real `_tmux_pane_pids`: Step 3 drops conftest's stub of it (the seeded snapshot takes its place), so before Step 3 they fail against the stub's `{}`.

- [ ] **Step 2: Run them and see them fail**

Run: `uv run pytest -q tests/test_tmux.py tests/test_panes.py tests/test_session_status.py`
Expected: `AttributeError: module 'periscope.tmux' has no attribute 'list_panes'` / `PANE_FIELDS` / `_panes_snapshot`; the list_windows fixture tests fail because `list_windows` still asks `list-windows` (the patched `tmux_mod.tmux` isn't the `tmux` name `panes.py` imported).

- [ ] **Step 3: Implement**

`periscope/tmux.py` — add `import threading` and `import time` to the imports; add after `def tmux(...)`:

```python
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
_PANES_FMT = "\t".join(f"#{{{f}}}" for f in PANE_FIELDS)
_PANES_TTL_S = 1.0
_panes_lock = threading.Lock()
# (monotonic time, rows) from the last list_panes().
_panes_snapshot: tuple[float, list[dict[str, str]]] | None = None


def list_panes() -> list[dict[str, str]]:
    """Every pane as {field: value} over PANE_FIELDS, from a fresh
    `list-panes -a` that becomes the snapshot recent_panes serves. [] when
    tmux has no server."""
    global _panes_snapshot
    rows = []
    for line in tmux("list-panes", "-a", "-F", _PANES_FMT).split("\n"):
        fields = line.split("\t")
        # tmux prints a tab or newline inside a name or path verbatim, which
        # splits that row out of shape; skip it rather than misread it.
        if len(fields) == len(PANE_FIELDS):
            rows.append(dict(zip(PANE_FIELDS, fields, strict=True)))
    _panes_snapshot = (time.monotonic(), rows)
    return rows


def recent_panes() -> list[dict[str, str]]:
    """list_panes' rows if they are under _PANES_TTL_S old, else a fresh call.
    Locked, so a refresh's fan-out threads wait for one fork instead of each
    forking."""
    with _panes_lock:
        if _panes_snapshot and time.monotonic() - _panes_snapshot[0] < _PANES_TTL_S:
            return _panes_snapshot[1]
        return list_panes()
```

`periscope/panes.py` — L21 becomes two lines:

```python
from periscope import tmux
from periscope.tmux import _ANSI_SGR_RE
```

and replace `all_pane_ids` and `list_windows` (L385–449) with:

```python
def all_pane_ids() -> set[str]:
    """Every live pane id across all sessions, from the refresh's list-panes
    snapshot. `list_windows` keeps one pane per window (the active one), so a
    split window's background panes are invisible to it; channel-alert GC keys
    on pane id, so using the window set there would delete a background Claude
    pane's alerts every poll. This is the full set."""
    return {p["pane_id"] for p in tmux.recent_panes()}


def list_windows() -> list[dict]:
    """One row per window, read off its active pane — the pane `pane_*`
    resolves against in `list-windows` — from a FRESH `list-panes -a`: callers
    that just created or stamped a window read it back at once, so this never
    serves the snapshot. The call refreshes the snapshot that all_pane_ids and
    session_status read for the rest of the refresh."""
    rows = []
    for p in tmux.list_panes():
        # Hide periscope's internal session from every caller — the
        # control-mode input client's session is scaffolding, not
        # user-visible tmux state.
        if p["pane_active"] != "1" or p["session_name"] == INPUT_CTL_SESSION:
            continue
        rows.append(
            {
                "session": p["session_name"],
                "index": int(p["window_index"]),
                "name": p["window_name"],
                "active": p["window_active"] == "1",
                # The active pane's cwd.
                "cwd": p["pane_current_path"],
                # Empty for unmanaged windows — `resolve_pids` mints one on
                # first sighting and stamps it onto the window.
                "pid_raw": p["@periscope_id"],
                # pane_id (%N) is tmux's stable handle for the active pane within
                # the current server lifetime — the addressing key for channel pushes.
                "pane_id": p["pane_id"],
                # window_activity is tmux's last-output timestamp. Used ONLY as a
                # skip-recapture hint in window_view; it must NEVER feed focus
                # (invariant #1 — focus is keyed on window_active above).
                "activity": int(p["window_activity"] or 0),
                # window_id (@N) is tmux's stable handle for the WINDOW. Unlike
                # session:index it survives move-window/renumbering, so it is the
                # only safe target for writing @periscope_id back (see pids._resolve_one).
                "window_id": p["window_id"],
                "pane_pid": p["pane_pid"],
                "current_command": p["pane_current_command"],
                # "<epoch> <dir>" on windows an open created (pids.stamp_new_window);
                # open_ops trusts it over the live cwd while the pane is fresh.
                "opened": p["@periscope_opened"],
            }
        )
    return rows
```

`periscope/session_status.py`:
- imports: `from periscope import agent_processes, config, resources, tmux`
- docstring's last paragraph: "(config, resources, agent_processes, tmux)" in place of "(config, resources, agent_processes)".
- replace `_tmux_pane_pids` with:

```python
def _tmux_pane_pids() -> dict[str, int]:
    """tmux pane id -> the pid of the pane's root process (usually its shell),
    from the refresh's list-panes snapshot. Empty on any tmux failure (no
    server, tmux not installed)."""
    try:
        rows = tmux.recent_panes()
    except (OSError, subprocess.SubprocessError):
        return {}
    panes: dict[str, int] = {}
    for p in rows:
        with contextlib.suppress(ValueError):
            panes[p["pane_id"]] = int(p["pane_pid"])
    return panes
```

- in `pane_claude_pids`' docstring, replace the last sentence "One /api/state poll resolves ~20 panes, so a one-second window already collapses the cost to two forks per poll; a longer one buys nothing but staleness." with "The pane and process tables it walks are the refresh's shared tmux and ps snapshots; a longer window buys nothing but staleness."

`tests/conftest.py` — replace `_no_live_session_scan` with:

```python
@pytest.fixture(autouse=True)
def _no_live_session_scan(monkeypatch):
    """Keep turns.session_id_for_pane's live lookup off the real machine.

    It resolves a pane to its running claude by walking the process tree
    (`tmux list-panes` + `ps`), and it runs on every /api/state poll — so any
    test that builds a window view would otherwise fork against the developer's
    real tmux server and read their real ~/.claude/sessions. An unexpiring
    empty list-panes snapshot makes pane_claude_pids() return {} by its own
    no-panes path (a test's own list_windows() replaces it, as a refresh
    would), and an unexpiring empty process snapshot answers proc_table() /
    _claude_procs() without forking `ps`; every test falls back to the
    pane_sessions row unless it opts in. Tests of the live path patch these
    same seams themselves, which overrides this."""
    try:
        from periscope import agent_processes, session_status, tmux
    except ImportError:
        return
    monkeypatch.setattr(tmux, "_panes_snapshot", (math.inf, []), raising=False)
    monkeypatch.setattr(session_status, "_pane_pids_cache", None, raising=False)
    monkeypatch.setattr(agent_processes, "_snapshot_cache", (math.inf, {}), raising=False)
    # pane_config_dirs' 15s TTL outlives a test — a populated cache would bleed
    # one test's pane->config-dir map into every later view build.
    monkeypatch.setattr(session_status, "_config_dirs_cache", None, raising=False)
```

- [ ] **Step 4: Run the tests and see them pass**

Run: `uv run pytest -q tests/test_tmux.py tests/test_panes.py tests/test_session_status.py tests/test_pids.py tests/routes/test_state.py tests/test_open_ops.py tests/test_channels.py`
Expected: all pass. Then `uv run pytest -q`. A test that patches `periscope.tmux.tmux` with capture text and now reaches `list_windows` gets `[]` (the field-count rule skips its rows); if one asserted on windows it got from the real server through `list_windows`, patch `list_windows` at its import site instead, as `tests/routes/test_state.py` does.

- [ ] **Step 5: Commit**

```sh
uv run ruff check --fix periscope/tmux.py periscope/panes.py periscope/session_status.py tests/conftest.py tests/test_tmux.py tests/test_panes.py tests/test_session_status.py && uv run ruff format periscope/tmux.py periscope/panes.py periscope/session_status.py tests/conftest.py tests/test_tmux.py tests/test_panes.py tests/test_session_status.py
git commit -m "panes: one list-panes per refresh serves list_windows, all_pane_ids and the pane pid map (THI-450)" -m "Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>" -- periscope/tmux.py periscope/panes.py periscope/session_status.py tests/conftest.py tests/test_tmux.py tests/test_panes.py tests/test_session_status.py
```

---

## Task 5: Git in two commands, repo identity cached for 10 minutes

**Files:**
- Modify: `periscope/git_pr.py` (imports L18–31, constants L33–43, `git_state_for` L65–116), `tests/conftest.py` (new fixture)
- Test: `tests/test_git_pr.py` (imports L7–23, `_reset_caches` L26–36, L50–122 replaced, L411–445 replaced)

- [ ] **Step 1: Write the failing tests** — in `tests/test_git_pr.py`:
  - imports: add `import os`, `import re`, `from types import SimpleNamespace`, `from periscope import git_pr, gitutil`; remove `_git_cache` from the `from periscope.git_pr import (...)` list (conftest clears it now) and drop its two `.clear()` lines from `_reset_caches`.
  - Replace `test_git_state_for_returns_branch_for_mocked_git_repo`, `_mock_git` and the four `test_git_state_*` tests after it (L50–122) with:

```python
def _mock_git(mocker, *, status="# branch.oid 0123456789abcdef0123456789abcdef01234567\n# branch.head main", diff="", code=0):
    """Patch _run so git_state_for sees one `status --porcelain=v2 --branch`
    answer and one `diff --shortstat` answer; repo identity falls back."""

    def fake_run(cmd, cwd=None, timeout=3.0, env=None):
        if "status" in cmd:
            return (code, status)
        if "diff" in cmd:
            return (0, diff)
        return (1, "")  # rev-parse --git-common-dir / remote get-url

    mocker.patch("periscope.git_pr._run", side_effect=fake_run)
    mocker.patch("periscope.gitutil._run", side_effect=fake_run)


_HEAD = "# branch.oid 0123456789abcdef0123456789abcdef01234567\n# branch.head main\n"


def test_git_state_counts_untracked_files(tmp_path, mocker):
    """`git diff HEAD` ignores untracked files, so a worktree whose only change
    was brand-new files used to report "clean" — and isDirty() then suppressed
    the chip entirely, hiding the work."""
    _mock_git(mocker, status=_HEAD + "? brand_new.py")
    assert git_state_for(str(tmp_path))["git"] == "?1"


def test_git_state_combines_tracked_and_untracked(tmp_path, mocker):
    _mock_git(
        mocker,
        status=_HEAD + "1 .M N... 100644 100644 100644 aaa bbb a.py\n? new.py\n? newdir/",
        diff=" 1 file changed, 12 insertions(+), 3 deletions(-)",
    )
    assert git_state_for(str(tmp_path))["git"] == "+12 -3 ?2"


def test_git_state_untracked_with_unpushed_commits(tmp_path, mocker):
    """The `*` suffix means unpushed commits and composes with the ? count."""
    _mock_git(mocker, status=_HEAD + "# branch.upstream origin/main\n# branch.ab +2 -0\n? a.py")
    assert git_state_for(str(tmp_path))["git"] == "?1 *"


def test_git_state_clean_stays_clean(tmp_path, mocker):
    """No tracked diff, no untracked, no unpushed -> the literal isDirty() checks."""
    _mock_git(mocker)
    assert git_state_for(str(tmp_path))["git"] == "clean"
    _mock_git(mocker, status=_HEAD + "# branch.upstream origin/main\n# branch.ab +3 -1")
    assert git_state_for(str(tmp_path))["git"] == "clean *"


def test_git_state_detached_head_is_the_short_sha(tmp_path, mocker):
    _mock_git(mocker, status="# branch.oid 0123456789abcdef0123456789abcdef01234567\n# branch.head (detached)")
    assert git_state_for(str(tmp_path))["branch"] == "@0123456"


def test_git_state_none_when_status_fails(tmp_path, mocker):
    _mock_git(mocker, status="fatal: not a git repository", code=128)
    assert git_state_for(str(tmp_path)) is None


# --- the two-command read against real repos -------------------------------


@pytest.fixture
def git_identity(monkeypatch):
    """Commit in fixture repos without the developer's git config or signing."""
    for k, v in {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
    }.items():
        monkeypatch.setenv(k, v)


def _seven_command_state(path):
    """The pre-THI-450 seven-command read of branch + git, kept as the oracle."""

    def run(*args):
        r = subprocess.run(["git", "-C", path, *args], capture_output=True, text=True)
        return r.returncode, r.stdout.strip()

    if run("rev-parse", "--git-dir")[0] != 0:
        return None
    _, branch = run("rev-parse", "--abbrev-ref", "HEAD")
    if not branch or branch == "HEAD":
        _, sha = run("rev-parse", "--short", "HEAD")
        branch = f"@{sha}" if sha else "?"
    _, diff = run("diff", "HEAD", "--shortstat")
    adds_m = re.search(r"(\d+) insertion", diff)
    dels_m = re.search(r"(\d+) deletion", diff)
    adds = int(adds_m.group(1)) if adds_m else 0
    dels = int(dels_m.group(1)) if dels_m else 0
    _, untracked = run("ls-files", "-o", "--exclude-standard", "--directory", "--no-empty-directory")
    new = sum(1 for line in untracked.splitlines() if line.strip())
    code, ahead_s = run("rev-list", "--count", "@{u}..HEAD")
    ahead = int(ahead_s) if code == 0 and ahead_s.isdigit() else 0
    bits = []
    if adds or dels:
        bits.append(f"+{adds} -{dels}")
    if new:
        bits.append(f"?{new}")
    state = " ".join(bits) if bits else "clean"
    if ahead > 0:
        state += " *"
    return {"branch": branch, "git": state}


def _committed(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "a.txt").write_text("one\ntwo\n")
    (repo / ".gitignore").write_text("*.log\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "init")
    return repo


def _clean(tmp_path):
    return _committed(tmp_path)


def _dirty(tmp_path):
    repo = _committed(tmp_path)
    (repo / "a.txt").write_text("one\nTWO\nthree\n")
    (repo / "b.txt").write_text("staged\n")
    _git(repo, "add", "b.txt")
    return repo


def _untracked_only(tmp_path):
    # A file, a wholly-untracked tree, a dir holding only ignored files, a dir
    # mixing ignored and untracked, and an empty dir.
    repo = _committed(tmp_path)
    (repo / "new.py").write_text("x\n")
    (repo / "newdir" / "deep").mkdir(parents=True)
    (repo / "newdir" / "deep" / "f.txt").write_text("x\n")
    (repo / "logs").mkdir()
    (repo / "logs" / "run.log").write_text("x\n")
    (repo / "mixed").mkdir()
    (repo / "mixed" / "keep.txt").write_text("x\n")
    (repo / "mixed" / "skip.log").write_text("x\n")
    (repo / "empty").mkdir()
    return repo


def _detached(tmp_path):
    repo = _committed(tmp_path)
    _git(repo, "checkout", "-q", "--detach")
    return repo


def _ahead(tmp_path):
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "-q", "--bare", str(remote))
    repo = _committed(tmp_path)
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "-q", "-u", "origin", "main")
    for n in (1, 2):
        (repo / "a.txt").write_text(f"v{n}\n")
        _git(repo, "commit", "-qam", f"c{n}")
    return repo


def _no_upstream(tmp_path):
    repo = _committed(tmp_path)
    _git(repo, "checkout", "-q", "-b", "feature")
    (repo / "a.txt").write_text("feature\n")
    _git(repo, "commit", "-qam", "f")
    return repo


def _non_repo(tmp_path):
    d = tmp_path / "plain"
    d.mkdir()
    return d


@pytest.mark.parametrize(
    ("build", "want"),
    [
        (_clean, ("main", "clean")),
        (_dirty, ("main", "+3 -1")),
        (_untracked_only, ("main", "?3")),
        (_detached, (None, "clean")),  # branch is "@<sha7>", checked against the oracle
        (_ahead, ("main", "clean *")),
        (_no_upstream, ("feature", "clean")),
        (_non_repo, None),
    ],
)
def test_two_command_read_matches_the_seven_command_read(build, want, tmp_path, git_identity):
    path = str(build(tmp_path))
    new = git_state_for(path)
    got = None if new is None else {"branch": new["branch"], "git": new["git"]}
    assert got == _seven_command_state(path)
    if want is None:
        assert got is None
    else:
        assert got["git"] == want[1]
        assert got["branch"] == want[0] or (want[0] is None and got["branch"].startswith("@"))


def test_unborn_branch_reads_its_name(tmp_path, git_identity):
    # `rev-parse --abbrev-ref HEAD` fails before the first commit, and _run
    # handed its stderr back as the branch; porcelain v2 names the branch.
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "trunk")
    (repo / "new.py").write_text("x\n")
    assert git_state_for(str(repo)) == {
        "branch": "trunk",
        "git": "?1",
        "repo_slug": None,
        "repo_key": os.path.realpath(repo),
        "repo_label": "repo",
    }


def test_git_state_forks_two_commands_once_identity_is_cached(tmp_path, monkeypatch, git_identity):
    repo = str(_committed(tmp_path))
    clock = [1000.0]
    monkeypatch.setattr(git_pr, "time", SimpleNamespace(time=lambda: clock[0]))
    real = git_pr._run
    calls = []

    def rec(cmd, **kw):
        calls.append(cmd)
        return real(cmd, **kw)

    monkeypatch.setattr(git_pr, "_run", rec)
    monkeypatch.setattr(gitutil, "_run", rec)

    def subcommands():
        out = [c[c.index("-C") + 2] for c in calls]
        calls.clear()
        return out

    git_state_for(repo)
    assert subcommands() == ["status", "diff", "rev-parse", "remote"]
    git_state_for(repo)
    assert subcommands() == ["status", "diff"]
    clock[0] += 601
    git_state_for(repo)
    assert subcommands() == ["status", "diff", "rev-parse", "remote"]


def test_git_reads_take_no_optional_locks(tmp_path, mocker):
    # A dashboard refresh must not take the index lock from under the user's
    # own git commands; `git diff` refreshes the index too.
    _mock_git(mocker)
    git_state_for(str(tmp_path))
    git_cmds = [c.args[0] for c in git_pr._run.call_args_list]
    assert all("--no-optional-locks" in c for c in git_cmds)
```

  - Replace `test_git_state_includes_repo_key_and_label` (L411–445) with:

```python
def test_git_state_includes_repo_key_and_label(tmp_path, mocker):
    """git_state_for() returns repo_key (full path) + repo_label (basename)."""
    repo_dir = tmp_path / "foo"
    worktree_dir = repo_dir / "branch-a"
    worktree_dir.mkdir(parents=True)
    common_dir = repo_dir / ".git"
    common_dir.mkdir()

    def fake_run(args, cwd=None, timeout=None, env=None):
        if "status" in args:
            return (0, _HEAD)
        if "--git-common-dir" in args:
            return (0, str(common_dir))
        if "get-url" in args:
            return (1, "")  # no github slug
        return (0, "")

    # Both modules import _run from periscope.tmux at module load — patch both.
    mocker.patch("periscope.git_pr._run", side_effect=fake_run)
    mocker.patch("periscope.gitutil._run", side_effect=fake_run)
    out = git_pr.git_state_for(str(worktree_dir))
    assert out["repo_key"] == str(repo_dir)
    assert out["repo_label"] == "foo"
```

`tests/conftest.py` — add after `_no_live_session_scan`:

```python
@pytest.fixture(autouse=True)
def _fresh_git_caches():
    """git_pr's per-path caches are module globals: an entry one test left
    behind would answer the next test's read of the same path."""
    from periscope import git_pr

    git_pr._git_cache.clear()
    git_pr._repo_identity_cache.clear()
```

- [ ] **Step 2: Run them and see them fail**

Run: `uv run pytest -q tests/test_git_pr.py`
Expected: every test errors in conftest with `AttributeError: module 'periscope.git_pr' has no attribute '_repo_identity_cache'`.

- [ ] **Step 3: Implement** — `periscope/git_pr.py`:
  - L28: `from periscope.gitutil import github_slug, resolve_repo`
  - after `_GIT_TTL = 15.0` add:

```python
# A repo's root and its GitHub remote: re-read rarely, they change only when a
# checkout is re-pointed.
_REPO_IDENTITY_TTL_S = 600.0
# path -> (read at, repo_key, repo_slug)
_repo_identity_cache: dict[str, tuple[float, str, str | None]] = {}
```

  - replace `git_state_for` with:

```python
def git_state_for(path: str) -> dict | None:
    """Return {branch, git, repo_slug, repo_key, repo_label} for the git repo at
    `path`, or None. Two forks: one status for branch, ahead and untracked, one
    diff for line counts; repo_key and repo_slug come from a per-path cache."""
    if not path or not os.path.isdir(path):
        return None
    # --no-optional-locks: a dashboard read must not take the index lock from
    # under the user's own git commands (status and diff both refresh the index).
    code, status = _run(
        ["git", "--no-optional-locks", "-C", path, "status", "--porcelain=v2", "--branch", "--untracked-files=normal"]
    )
    if code != 0:
        return None
    head = oid = ""
    ahead = new = 0
    for line in status.splitlines():
        if line.startswith("# branch.head "):
            head = line.removeprefix("# branch.head ")
        elif line.startswith("# branch.oid "):
            oid = line.removeprefix("# branch.oid ")
        elif line.startswith("# branch.ab "):
            # Present only when the upstream exists: "+<ahead> -<behind>".
            ahead = int(line.split()[2].lstrip("+"))
        elif line.startswith("? "):
            # Untracked; `git diff HEAD` ignores these, so a worktree whose only
            # change is brand-new files would read "clean". A wholly-untracked
            # directory is one entry.
            new += 1
    branch = f"@{oid[:7]}" if head == "(detached)" else head
    # Compact diff stats vs HEAD (covers staged + unstaged together).
    _, diff = _run(["git", "--no-optional-locks", "-C", path, "diff", "HEAD", "--shortstat"])
    adds_m = re.search(r"(\d+) insertion", diff)
    dels_m = re.search(r"(\d+) deletion", diff)
    adds = int(adds_m.group(1)) if adds_m else 0
    dels = int(dels_m.group(1)) if dels_m else 0
    bits = []
    if adds or dels:
        bits.append(f"+{adds} -{dels}")
    if new:
        bits.append(f"?{new}")
    state = " ".join(bits) if bits else "clean"
    if ahead > 0:
        state += " *"
    # `repo_slug` (owner/repo) lets the frontend build PR URLs without
    # hardcoding a repo — periscope watches panes across many repos.
    # repo_key is the full repo path (handles both sibling and inline
    # worktree layouts via gitutil.resolve_repo). repo_label is the
    # basename for human-readable display in the rail.
    now = time.time()
    identity = _repo_identity_cache.get(path)
    if identity is None or now - identity[0] >= _REPO_IDENTITY_TTL_S:
        identity = (now, resolve_repo(path), github_slug(path))
        _repo_identity_cache[path] = identity
    _, repo_key, repo_slug = identity
    return {
        "branch": branch,
        "git": state,
        "repo_slug": repo_slug,
        "repo_key": repo_key,
        "repo_label": os.path.basename(repo_key.rstrip("/")) if repo_key else "",
    }
```

- [ ] **Step 4: Run the tests and see them pass**

Run: `uv run pytest -q tests/test_git_pr.py tests/test_gitutil.py tests/test_pids.py tests/test_window_view.py`
Expected: all pass.

- [ ] **Step 5: Commit**

```sh
uv run ruff check --fix periscope/git_pr.py tests/conftest.py tests/test_git_pr.py && uv run ruff format periscope/git_pr.py tests/conftest.py tests/test_git_pr.py
git commit -m "git_pr: git state in two commands, repo root and remote cached per cwd for 10 minutes (THI-450)" -m "Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>" -- periscope/git_pr.py tests/conftest.py tests/test_git_pr.py
```

---

## Task 6: Git state refreshed in the background

**Files:**
- Modify: `periscope/git_pr.py` (imports; constants after `_GIT_TTL`; `cached_git_state` L119–128 + new `_next_git_due`, `_refresh_git_state`), `tests/conftest.py` (`_fresh_git_caches`)
- Test: `tests/test_git_pr.py` (replace `test_cached_git_state_uses_ttl`, `test_cached_git_state_returns_none_for_empty_path`, `test_cached_git_state_refetches_after_ttl`)

- [ ] **Step 1: Write the failing tests** — in `tests/test_git_pr.py`, remove `_GIT_TTL` and `cached_git_state` from the `from periscope.git_pr import (...)` list and replace the three `cached_git_state` tests with:

```python
@pytest.fixture
def swr(monkeypatch, mocker):
    """A fixed clock, no jitter, a fake git read, and no real refresh thread
    (docs/testing.md): _bg is recorded, and a test runs the refresh itself."""
    clock = [1000.0]
    monkeypatch.setattr(git_pr, "time", SimpleNamespace(time=lambda: clock[0]))
    monkeypatch.setattr(git_pr, "random", SimpleNamespace(uniform=lambda a, b: 0.0))
    inner = mocker.patch("periscope.git_pr.git_state_for", return_value={"branch": "main", "git": "clean"})
    bg = mocker.patch("periscope.git_pr._bg")
    return SimpleNamespace(clock=clock, inner=inner, bg=bg)


def test_first_sight_reads_synchronously(swr):
    # The pid resolver matches panes on (branch, cwd) after a tmux restart, so
    # a cwd's first read can't come back without its branch.
    assert git_pr.cached_git_state("/foo") == {"branch": "main", "git": "clean"}
    assert swr.inner.call_count == 1
    swr.bg.assert_not_called()


def test_fresh_entry_is_served_without_git(swr):
    git_pr.cached_git_state("/foo")
    swr.clock[0] += 14
    git_pr.cached_git_state("/foo")
    assert swr.inner.call_count == 1
    swr.bg.assert_not_called()


def test_stale_entry_is_served_and_refreshed_once_in_the_background(swr):
    git_pr.cached_git_state("/foo")
    swr.clock[0] += 16
    swr.inner.return_value = {"branch": "feat", "git": "+1 -0"}
    assert git_pr.cached_git_state("/foo") == {"branch": "main", "git": "clean"}
    assert git_pr.cached_git_state("/foo") == {"branch": "main", "git": "clean"}
    swr.bg.assert_called_once_with("git-state", git_pr._refresh_git_state, "/foo")
    assert swr.inner.call_count == 1  # nothing ran on the request path


def test_background_refresh_lands_and_frees_the_path(swr):
    git_pr.cached_git_state("/foo")
    swr.clock[0] += 16
    git_pr.cached_git_state("/foo")
    swr.inner.return_value = {"branch": "feat", "git": "+1 -0"}
    git_pr._refresh_git_state("/foo")
    assert "/foo" not in git_pr._git_refreshing
    assert git_pr.cached_git_state("/foo") == {"branch": "feat", "git": "+1 -0"}
    swr.bg.assert_called_once()  # the landed entry is fresh again


def test_empty_path_is_none(swr):
    assert git_pr.cached_git_state("") is None
    swr.inner.assert_not_called()


def test_expiry_is_jittered(monkeypatch, mocker):
    # cwds first seen in one refresh must not all expire in the same later one.
    monkeypatch.setattr(git_pr, "time", SimpleNamespace(time=lambda: 1000.0))
    mocker.patch("periscope.git_pr.git_state_for", return_value=None)
    for n in range(50):
        git_pr.cached_git_state(f"/p{n}")
    dues = [due for due, _ in git_pr._git_cache.values()]
    assert all(1000.0 + 10 <= d <= 1000.0 + 20 for d in dues)
    assert len(set(dues)) > 1
```

In `tests/conftest.py`'s `_fresh_git_caches`, add `git_pr._git_refreshing.clear()` and extend the docstring: "…and a stale one would start a real `_bg` git refresh thread."

- [ ] **Step 2: Run them and see them fail**

Run: `uv run pytest -q tests/test_git_pr.py`
Expected: conftest error `AttributeError: module 'periscope.git_pr' has no attribute '_git_refreshing'`.

- [ ] **Step 3: Implement** — `periscope/git_pr.py`:
  - add `import random` to the imports.
  - replace the `_git_cache` line (L39) with:

```python
# ± this much on each entry's next refresh, so cwds first read in one refresh
# don't all expire in the same later one.
_GIT_JITTER_S = 5.0
# path -> (refresh due at, state)
_git_cache: dict[str, tuple[float, dict | None]] = {}
_git_refreshing: set[str] = set()
_git_lock = threading.Lock()
```

  - replace `cached_git_state` with:

```python
def _next_git_due() -> float:
    return time.time() + _GIT_TTL + random.uniform(-_GIT_JITTER_S, _GIT_JITTER_S)


def cached_git_state(path: str) -> dict | None:
    """Stale-while-revalidate: the cached state at once, refreshed in the
    background once due. A path seen for the first time is read synchronously:
    the pid resolver matches panes on (branch, cwd) after a tmux restart
    (pids._attach_git_then_resolve_pids), so a first-sight branch can't be
    missing."""
    if not path:
        return None
    with _git_lock:
        cached = _git_cache.get(path)
        if cached is not None:
            if time.time() >= cached[0] and path not in _git_refreshing:
                _git_refreshing.add(path)
                _bg("git-state", _refresh_git_state, path)
            return cached[1]
    data = git_state_for(path)
    with _git_lock:
        _git_cache[path] = (_next_git_due(), data)
    return data


def _refresh_git_state(path: str) -> None:
    try:
        data = git_state_for(path)
        with _git_lock:
            _git_cache[path] = (_next_git_due(), data)
    finally:
        with _git_lock:
            _git_refreshing.discard(path)
```

- [ ] **Step 4: Run the tests and see them pass**

Run: `uv run pytest -q tests/test_git_pr.py tests/test_pids.py tests/routes/test_state.py tests/test_window_view.py tests/test_narrator.py`
Expected: all pass; then `uv run pytest -q`.

- [ ] **Step 5: Commit**

```sh
uv run ruff check --fix periscope/git_pr.py tests/conftest.py tests/test_git_pr.py && uv run ruff format periscope/git_pr.py tests/conftest.py tests/test_git_pr.py
git commit -m "git_pr: cached git state refreshes in the background on a jittered 15 s, first sight still synchronous (THI-450)" -m "Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>" -- periscope/git_pr.py tests/conftest.py tests/test_git_pr.py
```

---

## Task 7: gh reads its token once

gh's error for a bad token was checked against gh 2.103.3: `HTTP 401: Bad credentials (https://api.github.com/graphql)`, exit 1. `gh help environment` documents `GH_TOKEN` (takes precedence over stored credentials), `GH_TELEMETRY=false` and `GH_NO_UPDATE_NOTIFIER`.

**Files:**
- Create: `periscope/gh.py`
- Modify: `periscope/tmux.py` (`_run` L33–43), `periscope/git_pr.py` (`pr_state_for` L231–247, `shared_activity_for` L347–362, `linked_pr_state_for` L486–509), `periscope/reviews.py` (`_gh_list`), `periscope/cleanup.py` (`_pr_state`), `periscope/projects.py` (`_resolve_pr_metadata`), `tests/conftest.py` (new fixture)
- Test: `tests/test_gh.py` (new), `tests/test_tmux.py`, `tests/test_git_pr.py` (L314, L338, `_fake_gh` L346–358), `tests/test_reviews.py` (L210–218, L322)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_gh.py`:

```python
"""gh — every `gh` periscope runs carries a token read once, not a keychain
read per call, and runs with telemetry and the update check off."""

import os
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from periscope import gh


@pytest.fixture(autouse=True)
def _no_gh_token(monkeypatch):
    """Overrides conftest's seeded "no token" entry: these tests read it."""
    monkeypatch.setattr(gh, "_token_cache", None)


def _fake(monkeypatch, *, token="tok-1", answers=None):
    calls = []
    answers = list(answers or [])

    def run(cmd, cwd=None, timeout=3.0, env=None):
        calls.append((cmd, env))
        if cmd[1:3] == ["auth", "token"]:
            return (0, token) if token else (1, "You are not logged into any GitHub hosts.")
        return answers.pop(0) if answers else (0, "[]")

    monkeypatch.setattr(gh, "_run", run)
    return calls


def _token_reads(calls):
    return sum(1 for cmd, _ in calls if cmd == ["gh", "auth", "token"])


def test_a_call_carries_the_token_and_runs_quiet(monkeypatch):
    calls = _fake(monkeypatch)
    assert gh.run("pr", "list", "--limit", "1", cwd="/r", timeout=8.0) == (0, "[]")
    cmd, env = calls[-1]
    assert cmd == ["gh", "pr", "list", "--limit", "1"]
    assert env["GH_TOKEN"] == "tok-1"
    assert env["GH_TELEMETRY"] == "false"
    assert env["GH_NO_UPDATE_NOTIFIER"] == "1"
    assert env["PATH"] == os.environ["PATH"]  # the rest of the environment rides along


def test_the_token_is_read_once_an_hour(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(gh, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    calls = _fake(monkeypatch)
    gh.run("pr", "list")
    gh.run("pr", "view", "1")
    assert _token_reads(calls) == 1
    clock[0] += 3601
    gh.run("pr", "list")
    assert _token_reads(calls) == 2


def test_an_auth_failure_rereads_the_token(monkeypatch):
    calls = _fake(monkeypatch, answers=[(1, "HTTP 401: Bad credentials (https://api.github.com/graphql)")])
    assert gh.run("pr", "list")[0] == 1
    gh.run("pr", "list")
    assert _token_reads(calls) == 2


def test_other_failures_keep_the_token(monkeypatch):
    calls = _fake(monkeypatch, answers=[(1, "no pull requests found for branch \"x\"")])
    gh.run("pr", "view", "9")
    gh.run("pr", "list")
    assert _token_reads(calls) == 1


def test_without_a_token_calls_still_run(monkeypatch):
    monkeypatch.delenv("GH_TOKEN", raising=False)
    calls = _fake(monkeypatch, token=None)
    assert gh.run("pr", "list") == (0, "[]")
    cmd, env = calls[-1]
    assert cmd == ["gh", "pr", "list"]
    assert "GH_TOKEN" not in env


def test_every_gh_call_goes_through_gh_run():
    pkg = Path(__file__).resolve().parents[1] / "periscope"
    offenders = [
        p.name for p in pkg.rglob("*.py") if p.name != "gh.py" and re.search(r'\[\s*"gh"\s*,', p.read_text())
    ]
    assert offenders == []
```

`tests/test_tmux.py` — append:

```python
def test_run_passes_env_through(mocker):
    run = mocker.patch(
        "subprocess.run",
        return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout="ok\n", stderr=""),
    )
    assert _run(["gh", "pr", "list"], env={"GH_TOKEN": "t"}) == (0, "ok")
    assert run.call_args.kwargs["env"] == {"GH_TOKEN": "t"}
```

`tests/test_git_pr.py` — at L314 and L338 change `mocker.patch("periscope.git_pr._run", return_value=(0, payload))` to `mocker.patch("periscope.gh.run", return_value=(0, payload))`; in `_fake_gh`, replace the inner `fake_run` and its patch with `mocker.patch("periscope.gh.run", return_value=(0, payload))` (keep the `_GH_AVAILABLE` patch).

`tests/test_reviews.py` — replace `test_open_prs_merges_both_lists_and_assigns_roles`'s patch and final asserts:

```python
    run = mocker.patch("periscope.gh.run", side_effect=[(0, MINE), (0, REQUESTED)])
    got = reviews.open_prs("/dev/fdy")
    assert [(p["number"], p["role"], p["branch"], p["author"], p["draft"]) for p in got] == [
        (1234, "mine", "feat/x", "tom", False),
        (99, "review-requested", "feat/theirs", "sam", True),
    ]
    first, second = run.call_args_list
    assert "--author" in first.args and "@me" in first.args and first.kwargs["cwd"] == "/dev/fdy"
    assert "--search" in second.args and "review-requested:@me" in second.args
```

and at L322: `mocker.patch("periscope.gh.run", return_value=(1, "auth error"))`.

`tests/conftest.py` — add:

```python
@pytest.fixture(autouse=True)
def _no_gh_token(monkeypatch):
    """Keep gh.run from forking `gh auth token` against the developer's real
    keychain: an unexpiring "no token" entry makes every call run on gh's own
    credentials. tests/test_gh.py overrides this fixture by name."""
    from periscope import gh

    monkeypatch.setattr(gh, "_token_cache", (math.inf, None))
```

- [ ] **Step 2: Run them and see them fail**

Run: `uv run pytest -q tests/test_gh.py tests/test_tmux.py tests/test_git_pr.py tests/test_reviews.py`
Expected: conftest `ImportError: cannot import name 'gh' from 'periscope'`; `test_run_passes_env_through` fails with `TypeError: _run() got an unexpected keyword argument 'env'`.

- [ ] **Step 3: Implement**

`periscope/tmux.py` — replace `_run`:

```python
def _run(
    cmd: list[str], cwd: str | None = None, timeout: float = 3.0, env: dict[str, str] | None = None
) -> tuple[int, str]:
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
```

Create `periscope/gh.py`:

```python
"""Every `gh` periscope runs (git_pr, reviews, cleanup, projects).

gh's macOS keyring execs `/usr/bin/security` on every call to read its stored
token. GH_TOKEN takes precedence over stored credentials, so the token is read
once with `gh auth token` and handed to each call; telemetry and the update
check are off, each of which forks again per call."""

import os
import threading
import time

from periscope.tmux import _run

_TOKEN_TTL_S = 3600.0
_QUIET = {"GH_TELEMETRY": "false", "GH_NO_UPDATE_NOTIFIER": "1"}
_lock = threading.Lock()
# (read at, token); None when `gh auth token` failed — calls then run on gh's
# own credentials, as before.
_token_cache: tuple[float, str | None] | None = None


def _token() -> str | None:
    global _token_cache
    with _lock:
        if _token_cache is None or time.monotonic() - _token_cache[0] >= _TOKEN_TTL_S:
            code, out = _run(["gh", "auth", "token"], env={**os.environ, **_QUIET})
            _token_cache = (time.monotonic(), out if code == 0 and out else None)
        return _token_cache[1]


def run(*args: str, cwd: str | None = None, timeout: float = 3.0) -> tuple[int, str]:
    """`gh <args>` — (returncode, stdout, or stderr on failure), as tmux._run."""
    global _token_cache
    token = _token()
    env = {**os.environ, **_QUIET}
    if token:
        env["GH_TOKEN"] = token
    code, out = _run(["gh", *args], cwd=cwd, timeout=timeout, env=env)
    # A revoked or rotated token answers HTTP 401; drop it so the next call
    # reads it again.
    if code != 0 and token and "HTTP 401" in out:
        with _lock:
            _token_cache = None
    return code, out
```

`periscope/git_pr.py` — add `from periscope import config, gh` (replacing `from periscope import config`). In `pr_state_for`, replace the `_run([...], cwd=path, timeout=8.0)` call with:

```python
    code, out = gh.run(
        "pr",
        "list",
        "--head",
        branch,
        "--state",
        "open",
        "--json",
        "number,title,isDraft,additions,deletions,reviewRequests,statusCheckRollup,baseRefName",
        "--limit",
        "1",
        cwd=path,
        timeout=8.0,
    )
```

In `shared_activity_for`, replace the `gh run list` call with:

```python
        code, out = gh.run(
            "run",
            "list",
            "--branch",
            branch,
            "--limit",
            "10",
            "--json",
            "conclusion,status,createdAt,displayTitle,name,url",
            cwd=path,
            timeout=5.0,
        )
```

Replace `linked_pr_state_for` with:

```python
def linked_pr_state_for(path: str, number: int) -> dict | None:
    """Resolve an explicitly-linked PR's lifecycle state + CI by number.
    Returns {pr_state: 'open'|'merged'|'closed', ci: glyph|None} or None if gh
    can't answer. Distinct from pr_state_for, which only sees OPEN PRs on the
    current branch."""
    if not _GH_AVAILABLE or not path or not number:
        return None
    code, out = gh.run("pr", "view", str(number), "--json", "state,statusCheckRollup,baseRefName", cwd=path, timeout=8.0)
    if code != 0 or not out:
        return None
    try:
        pr = json.loads(out)
    except Exception:
        return None
    return {
        "pr_state": (pr.get("state") or "").lower() or None,  # open/merged/closed
        "ci": _ci_glyph(pr.get("statusCheckRollup")),
        "ci_checks": _ci_checks(pr.get("statusCheckRollup")),
        "pr_base": pr.get("baseRefName") or None,
    }
```

`periscope/reviews.py` — `from periscope import gh, git_pr, gitutil, worktrees`; replace `_gh_list` with:

```python
def _gh_list(repo_root: str, *extra: str) -> list[dict] | None:
    """None on any failure (offline, auth, bad JSON) so the caller can keep
    the previous cache entry instead of blanking the queue for 60 s."""
    code, out = gh.run(
        "pr", "list", "--state", "open", "--json", _PR_FIELDS, "--limit", "50", *extra, cwd=repo_root, timeout=10.0
    )
    if code != 0:
        return None
    try:
        return json.loads(out) if out else []
    except ValueError:
        return None
```

`periscope/cleanup.py` — `from periscope import gh, worktrees`; replace `_pr_state` with:

```python
def _pr_state(repo: str, pr_number: int) -> str | None:
    """Return 'OPEN' / 'CLOSED' / 'MERGED' / None. Cached 5min."""
    key = (repo, pr_number)
    with _lock:
        cached = _pr_state_cache.get(key)
        if cached and _now() - cached[0] < _PR_STATE_TTL:
            return cached[1]
    code, out = gh.run("pr", "view", str(pr_number), "--json", "state", cwd=repo, timeout=10.0)
    state: str | None = None
    if code == 0 and out:
        try:
            import json

            state = json.loads(out).get("state") or None
        except Exception as e:
            log.warning("cleanup: gh pr view %s parse error: %s", pr_number, e)
    with _lock:
        _pr_state_cache[key] = (_now(), state)
    return state
```

`periscope/projects.py` — add `from periscope import gh` (above `from periscope import store as _store`); replace `_resolve_pr_metadata` with:

```python
def _resolve_pr_metadata(repo: str, pr: int) -> dict:
    """`gh pr view` for PR #pr in `repo`, returning the parsed metadata.

    Raises HTTPException: 404 if the PR doesn't exist, 400 if the gh call
    fails for any other reason, 500 if gh returns unparseable JSON.
    """
    code, out = gh.run(
        "pr",
        "view",
        str(pr),
        "--json",
        "headRefName,isCrossRepository,headRepository,baseRefName,state",
        cwd=repo,
        timeout=15.0,
    )
    if code != 0:
        # `out` is gh's stderr on failure; map "not found" variants to 404,
        # anything else to 400.
        if "no pull requests found" in out.lower() or "could not resolve" in out.lower():
            raise HTTPException(404, f"PR #{pr} not found in {repo}: {out}")
        raise HTTPException(400, f"gh pr view failed: {out}")
    try:
        return json.loads(out)
    except json.JSONDecodeError as e:
        raise HTTPException(500, f"gh pr view returned invalid JSON: {e}") from e
```

- [ ] **Step 4: Run the tests and see them pass**

Run: `uv run pytest -q tests/test_gh.py tests/test_tmux.py tests/test_git_pr.py tests/test_reviews.py tests/test_cleanup.py tests/test_open_ops.py tests/routes/test_reviews.py tests/routes/test_projects.py`
Expected: all pass; then `uv run pytest -q`.

- [ ] **Step 5: Commit**

```sh
uv run ruff check --fix periscope/gh.py periscope/tmux.py periscope/git_pr.py periscope/reviews.py periscope/cleanup.py periscope/projects.py tests/conftest.py tests/test_gh.py tests/test_tmux.py tests/test_git_pr.py tests/test_reviews.py && uv run ruff format periscope/gh.py periscope/tmux.py periscope/git_pr.py periscope/reviews.py periscope/cleanup.py periscope/projects.py tests/conftest.py tests/test_gh.py tests/test_tmux.py tests/test_git_pr.py tests/test_reviews.py
git add periscope/gh.py tests/test_gh.py
git commit -m "gh: every gh call carries a token read once an hour, telemetry and update check off (THI-450)" -m "Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>" -- periscope/gh.py periscope/tmux.py periscope/git_pr.py periscope/reviews.py periscope/cleanup.py periscope/projects.py tests/conftest.py tests/test_gh.py tests/test_tmux.py tests/test_git_pr.py tests/test_reviews.py
```

---

## Task 8: The popover's refresh line

**Files:**
- Modify: `static/src/store.js` (after the `resources` signal, L36), `static/src/poll.js` (import list; `applyState`), `static/src/chrome/resources.js` (append), `static/src/chrome/ResourcePill.jsx`, `static/styles.css` (after `.res-machine`, L327)
- Rebuild: `static/dist/`
- Test: `static/src/chrome/__tests__/resources.test.js`, `static/src/chrome/__tests__/resourcePillRender.test.jsx`

- [ ] **Step 1: Write the failing tests**

`resources.test.js` — import `refreshLine, refreshTip` alongside the others and append:

```js
describe("refreshLine", () => {
  it("build time, the push interval and forks per minute", () => {
    expect(refreshLine({ build_s: 0.43, build_max_s: 1.2, gap_s: 1.07, forks_per_min: 140, top_forks: [] }))
      .toBe("refresh 0.4 s · every 1.1 s · 140 forks/min");
  });
  it("leaves the interval out until the server has pushed twice", () => {
    expect(refreshLine({ build_s: 0.4, build_max_s: 0.4, gap_s: null, forks_per_min: 9, top_forks: [] }))
      .toBe("refresh 0.4 s · 9 forks/min");
  });
});

describe("refreshTip", () => {
  it("the slowest build in the last minute, then the top commands", () => {
    expect(refreshTip({ build_s: 0.4, build_max_s: 2.31, gap_s: 1, forks_per_min: 52, top_forks: [["git status", 40], ["ps", 12]] }))
      .toBe("slowest 2.3 s in the last minute\ngit status ×40\nps ×12");
  });
});
```

`resourcePillRender.test.jsx` — import `refresh` from `../../store.js` with the others; add `refresh.value = null;` to `afterEach`; append inside the `describe`:

```js
  it("closes with what a refresh costs, its top commands in the tooltip", () => {
    resources.value = R;
    refresh.value = { build_s: 0.4, build_max_s: 2.3, gap_s: 1.1, forks_per_min: 140, top_forks: [["git status", 40]] };
    const html = render(<ResourcePill />);
    expect(html).toContain("refresh 0.4 s · every 1.1 s · 140 forks/min");
    expect(html).toContain("git status ×40");
  });

  it("has no refresh line before the server reports one", () => {
    resources.value = R;
    expect(render(<ResourcePill />)).not.toContain("forks/min");
  });
```

- [ ] **Step 2: Run them and see them fail**

Run: `npm install` (once), then `npx vitest run static/src/chrome/__tests__/resources.test.js static/src/chrome/__tests__/resourcePillRender.test.jsx`
Expected: `refreshLine is not a function` / `does not provide an export named 'refresh'`.

- [ ] **Step 3: Implement**

`static/src/store.js` — after the `resources` line add:

```js
export const refresh = signal(null);          // /api/state refresh: { build_s, build_max_s, gap_s, forks_per_min, top_forks } — what a dashboard refresh costs (forks.py)
```

`static/src/poll.js` — add `refresh,` to the store import (alphabetical, after `projects,`) and, in `applyState`, after `resources.value = data.resources || null;`:

```js
    refresh.value = data.refresh || null;
```

`static/src/chrome/resources.js` — append:

```js
// The popover's last line: what one dashboard refresh costs (/api/state
// `refresh`, forks.py). gap_s is the server's interval between pushes, null
// until it has pushed twice.
export function refreshLine(r) {
  const every = r.gap_s == null ? "" : ` · every ${r.gap_s.toFixed(1)} s`;
  return `refresh ${r.build_s.toFixed(1)} s${every} · ${r.forks_per_min} forks/min`;
}

// Its tooltip: the slowest refresh in the last minute, then the commands that
// forked most.
export function refreshTip(r) {
  return [
    `slowest ${r.build_max_s.toFixed(1)} s in the last minute`,
    ...r.top_forks.map(([cmd, n]) => `${cmd} ×${n}`),
  ].join("\n");
}
```

`static/src/chrome/ResourcePill.jsx` — in the header comment replace "then everything else on the machine and the machine's load / swap / free memory." with "then everything else on the machine, the machine's load / swap / free memory, and what a dashboard refresh costs."; the two store/resources imports become

```js
import { refresh, resources, windows } from "../store.js";
import { fmtCpu, fmtMem, machineLine, ownedMb, paneLines, refreshLine, refreshTip } from "./resources.js";
```

and in `ResourcePopover`, add `const rf = refresh.value;` on the line after `usePopoverDismiss(ref, open, close);`, then replace

```jsx
        <div class="res-machine">{machineLine(r.machine)}</div>
```

with

```jsx
        <div class="res-machine">{machineLine(r.machine)}</div>
        {rf && <div class="res-refresh" title={refreshTip(rf)}>{refreshLine(rf)}</div>}
```

`static/styles.css` — after the `.res-machine` rule:

```css
.res-refresh { padding: 2px 6px 0; color: var(--fg-2); }
```

- [ ] **Step 4: Run the tests, build**

Run: `npm test`
Expected: all pass.
Run: `npm run build && git status --short static/dist`
Expected: build succeeds; `static/dist/app.js` (and any hashed chunks) modified.

- [ ] **Step 5: Commit**

```sh
git add static/dist
git commit -m "ui: the resource popover closes with what a refresh costs (THI-450)" -m "Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>" -- static/src/store.js static/src/poll.js static/src/chrome/resources.js static/src/chrome/ResourcePill.jsx static/styles.css static/src/chrome/__tests__/resources.test.js static/src/chrome/__tests__/resourcePillRender.test.jsx static/dist
```

---

## Task 9: Docs, the gate, and the before/after measurement

**Files:**
- Modify: `docs/invariants.md` (end of #1, end of #11), `docs/testing.md` (after the test-isolation paragraph), `CLAUDE.md` (module table)

- [ ] **Step 1: Docs**

`docs/invariants.md` — append to #1 (after "See `update_focus_from_windows`."):

```markdown
   The window rows come from one `list-panes -a` (`tmux.list_panes`), one
   row per window off its active pane (`#{pane_active}`); `active` still
   reads `#{window_active}`.
```

Append to #11 (after its last line, "catches both."):

```markdown
    `list_panes` carries `#{window_id}` and the window option
    `#{@periscope_id}` on every pane row, so a stamp still targets the
    window id. `list_windows` always forks a fresh `list-panes` — a stamp or
    a new window must read back at once — and only `all_pane_ids` and
    session_status's pane → pid map read the shared 1 s snapshot
    (`tmux.recent_panes`).
```

`docs/testing.md` — append:

```markdown
**No test forks real tmux, ps or gh for the per-refresh readers.** conftest
seeds `tmux._panes_snapshot`, `agent_processes._snapshot_cache` and
`gh._token_cache` with unexpiring empty entries, and clears git_pr's per-path
caches (a stale entry would start a real `_bg` git refresh). A test of one of
those paths resets the seed itself (`tests/test_gh.py` overrides
`_no_gh_token` by name).
```

`CLAUDE.md` — add two rows to the server module table, after the `log.py` row:

```markdown
| `forks.py` | Audit-hook fork counter + each refresh's cost (`/api/state` `refresh`, the resource popover's last line) |
| `gh.py` | Every `gh` call: token read once an hour, telemetry and update check off |
```

Commit:

```sh
git commit -m "docs: window rows come from one list-panes; conftest seeds; forks and gh in the module table (THI-450)" -m "Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>" -- docs/invariants.md docs/testing.md CLAUDE.md
```

- [ ] **Step 2: Full gate**

Run: `uv run pytest -q` — Expected: all pass.
Run: `npm test` — Expected: all pass.
Run: `bin/check; echo "exit=$?"` — Expected: `exit=0`. Fix anything it reports in the file that introduced it and amend nothing: commit the fix as `area: … (THI-450)`.
Run: `npm run build && git status --short static/dist` — Expected: no output (dist is fresh).

- [ ] **Step 3: Measure before and after, back to back, on :17375**

"Before" is Task 2's commit (refresh block present, old fork pattern); "after" is this branch's head. Both read the same live tmux panes.

Write the measuring client to `$TMPDIR/thi450_measure.py` (not committed):

```python
"""Subscribe to the dev server's /ws/state and print the refresh block once
the last minute is all steady state."""

import asyncio
import json
import sys
import time

import websockets


async def main(seconds: float) -> None:
    async with websockets.connect("ws://127.0.0.1:17375/ws/state", max_size=None) as ws:
        end = time.monotonic() + seconds
        builds = []
        blob = {}
        while time.monotonic() < end:
            blob = json.loads(await ws.recv())
            builds.append(blob["refresh"]["build_s"])
        tail = builds[len(builds) // 2 :]
        print(
            json.dumps(
                {"panes": len(blob["windows"]), "frames": len(builds), "build_s_mean_2nd_half": round(sum(tail) / len(tail), 2), **blob["refresh"]},
                indent=1,
            )
        )


asyncio.run(main(float(sys.argv[1]) if len(sys.argv) > 1 else 120))
```

Check :17375 is free: `lsof -ti tcp:17375` prints nothing (if it prints pids, another dev server is up — stop it first, never anything on :17374).

Each server runs in the background — the `( … &)` form below, or your Bash tool's background mode with the same command; stop it by its port, never by process name (prod also runs `uv run server.py`).

Before (`--force` on the remove only because uv leaves a `.venv` in the throwaway detached worktree):

```sh
BEFORE=$(git log --format=%H -1 -F --grep="state: /api/state reports what each refresh costs (THI-450)")
git worktree add --detach "$TMPDIR/thi450-before" "$BEFORE"
(cd "$TMPDIR/thi450-before" && PERISCOPE_PORT=17375 PERISCOPE_DEV=1 uv run server.py > "$TMPDIR/thi450-before.log" 2>&1 &)
until curl -sf http://127.0.0.1:17375/api/healthz > /dev/null; do sleep 1; done
uv run --with websockets python "$TMPDIR/thi450_measure.py" 120 | tee "$TMPDIR/thi450-before.json"
pids=$(lsof -ti tcp:17375); [ -n "$pids" ] && kill $pids
git worktree remove --force "$TMPDIR/thi450-before"
```

After (from the worktree root):

```sh
until [ -z "$(lsof -ti tcp:17375)" ]; do sleep 1; done
(PERISCOPE_PORT=17375 PERISCOPE_DEV=1 uv run server.py > "$TMPDIR/thi450-after.log" 2>&1 &)
until curl -sf http://127.0.0.1:17375/api/healthz > /dev/null; do sleep 1; done
uv run --with websockets python "$TMPDIR/thi450_measure.py" 120 | tee "$TMPDIR/thi450-after.json"
pids=$(lsof -ti tcp:17375); [ -n "$pids" ] && kill $pids
```

Expected: `after.forks_per_min` well under `before.forks_per_min`; in `after.top_forks`, `tmux list-panes` and `ps` at about one per refresh (≈ 60/min at a 1 s cadence), no `tmux list-windows`, no `security`, no `git rev-list` / `git ls-files`, `git status` near (cwds × 4)/min; `build_s` and `gap_s` no worse. If `slow refresh` warnings appear in either log, keep them. Record both JSON blocks (and the pane count) in the PR description under "Before / after", next to the remaining assumptions from the spec.
