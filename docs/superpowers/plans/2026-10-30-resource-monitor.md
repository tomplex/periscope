# Resource Monitor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show memory footprint and CPU for each pane, the periscope server, its webview and everything else, in a header pill, its popover and the detail header (THI-448).

**Architecture:** A new stdlib-only `periscope/resources.py` reads every process's `proc_pid_rusage` through `ctypes` once per `build_state` tick, on the process snapshot the tick already takes (`agent_processes.process_snapshot()`), assigns each process to one group, and turns CPU-time deltas into per-core percentages. `build_state` puts a `res` on each window row and a `resources` block on the blob; the frontend renders them. The ↻ cycle hint switches from `ps` RSS to footprint.

**Tech Stack:** Python 3.13 stdlib (`ctypes` on `/usr/lib/libproc.dylib` and `/usr/lib/libSystem.dylib`), FastAPI, pytest; Preact + `@preact/signals`, vitest, Vite.

**Spec:** `docs/superpowers/specs/2026-10-30-resource-monitor-design.md` — read it first.

**Worktree:** `/Users/thibaultdody/dev/worktrees/periscope/thi-448-resource-monitor`, branch `thibaultdody/thi-448-show-memory-footprint-and-cpu-for-periscope-its-webview-and`. Run every command from there.

**Before touching tests:** read `docs/testing.md` (no leaked threads; `.venv` drift → `uv sync`).

**Commits:** one per task step marked Commit; single-line messages in the repo's `area: what (THI-448)` style, ending with the line `Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>` after a blank line.

---

## File structure

| File | Change | Responsibility |
|---|---|---|
| `periscope/resources.py` | create | rusage/sysctl bindings, group assignment, CPU accounting, `measure()` |
| `periscope/agent_processes.py` | modify | `_descendants` → `descendants` (resources reuses it) |
| `periscope/routes/state.py` | modify | call `resources.measure`, add `res` / `resources` |
| `periscope/session_status.py` | modify | `_claude_procs` drops `rss=`; `claude_proc_for` returns `mem_mb` |
| `periscope/config.py` | modify | `MEM_*_RSS_KB` → `MEM_*_MB` |
| `periscope/window_view.py` | modify | `mem_signal` on footprint, sends `trigger` / `gb` |
| `tests/conftest.py` | modify | autouse `_no_live_resources` stub |
| `tests/test_resources.py` | create | resources unit tests + one real-machine smoke test |
| `tests/test_session_status.py`, `tests/test_window_view.py`, `tests/routes/test_state.py` | modify | updated / new cases |
| `static/src/chrome/resources.js` | create | pure formatting and ordering |
| `static/src/chrome/ResourcePill.jsx` | create | header pill + popover |
| `static/src/chrome/Header.jsx`, `static/src/store.js`, `static/src/poll.js` | modify | wire the `resources` signal and the pill |
| `static/src/split/Detail.jsx` | modify | `· 6.8G mem · 58% cpu` part |
| `static/src/util.js` | modify | `memHint` reads `gb` / `trigger` |
| `static/styles.css` | modify | pill + popover styles |
| `static/src/chrome/__tests__/resources.test.js`, `.../resourcePillRender.test.jsx`, `static/src/__tests__/paneHeaderRes.test.js`, `static/src/__tests__/memHint.test.js`, `static/src/__tests__/agent.test.js` | create/modify | vitest |
| `CLAUDE.md` | modify | module table row for `resources.py` |

---

### Task 1: rusage and sysctl bindings

**Files:**
- Create: `periscope/resources.py`
- Create: `tests/test_resources.py`
- Modify: `tests/conftest.py`

- [ ] **Step 1: Write the failing smoke test**

Create `tests/test_resources.py`:

```python
"""resources: footprint and CPU per owner. Everything except the smoke tests
runs on fake snapshots and fake rusage readings."""

import os

import pytest

from periscope import resources


# ── the real system calls ────────────────────────────────────────────────


def test_read_measures_this_process():
    r = resources._read(os.getpid())
    assert r.footprint > 0
    assert r.cpu_ns > 0
    assert 0 < r.start <= resources._now()


def test_read_gone_pid_is_none():
    assert resources._read(99_999_999) is None


def test_footprint_mb_of_this_process():
    assert resources.footprint_mb(os.getpid()) > 0


def test_machine_reads_the_kernel():
    m = resources.machine()
    assert set(m) == {"load1", "swap_used_mb", "swap_total_mb", "mem_free_pct"}
    assert 0 <= m["swap_used_mb"] <= m["swap_total_mb"]
    assert 0 <= m["mem_free_pct"] <= 100


def test_responsible_pid_of_this_process_is_a_pid():
    assert resources._responsible(os.getpid()) > 0
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_resources.py -q`
Expected: FAIL — `ImportError: cannot import name 'resources'`.

- [ ] **Step 3: Write the bindings**

Create `periscope/resources.py`:

```python
"""Memory footprint and CPU per owner: each pane, the periscope server, its
webview, and everything else on the machine.

Footprint (`phys_footprint`, Activity Monitor's Memory), not RSS: RSS leaves
out compressed and swapped pages, which is where a machine deep in swap keeps
most of its memory — the webview read 276 MB resident against 2.8 GB of
footprint. Read through libproc/libSystem with ctypes: ~3 µs a process, no
subprocess.
"""

from __future__ import annotations

import ctypes
import os
from dataclasses import dataclass

_u64 = ctypes.c_uint64
_RUSAGE_INFO_V1 = 1


class _RusageInfoV1(ctypes.Structure):
    # <sys/resource.h> rusage_info_v1: the v0 fields recycle.rs binds, plus
    # the CPU time of reaped children (144 bytes).
    _fields_ = [("uuid", ctypes.c_uint8 * 16)] + [
        (name, _u64)
        for name in (
            "user_time",
            "system_time",
            "pkg_idle_wkups",
            "interrupt_wkups",
            "pageins",
            "wired_size",
            "resident_size",
            "phys_footprint",
            "proc_start_abstime",
            "proc_exit_abstime",
            "child_user_time",
            "child_system_time",
            "child_pkg_idle_wkups",
            "child_interrupt_wkups",
            "child_pageins",
            "child_elapsed_abstime",
        )
    ]


class _Timebase(ctypes.Structure):
    _fields_ = [("numer", ctypes.c_uint32), ("denom", ctypes.c_uint32)]


class _SwapUsage(ctypes.Structure):
    # <sys/sysctl.h> struct xsw_usage
    _fields_ = [
        ("total", _u64),
        ("avail", _u64),
        ("used", _u64),
        ("pagesize", ctypes.c_uint32),
        ("encrypted", ctypes.c_int32),
    ]


_libproc = ctypes.CDLL("/usr/lib/libproc.dylib")
_libproc.proc_pid_rusage.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
_libsystem = ctypes.CDLL("/usr/lib/libSystem.dylib")
_libsystem.responsibility_get_pid_responsible_for_pid.argtypes = [ctypes.c_int]
_libsystem.responsibility_get_pid_responsible_for_pid.restype = ctypes.c_int
_libsystem.mach_absolute_time.restype = _u64
_timebase = _Timebase()
_libsystem.mach_timebase_info(ctypes.byref(_timebase))
# rusage times are mach absolute units: 125/3 ns each on Apple silicon.
_NS_PER_TICK = _timebase.numer / _timebase.denom


@dataclass(frozen=True)
class Reading:
    footprint: int  # bytes
    cpu_ns: int  # own CPU time plus its reaped children's
    start: int  # ns on _now()'s clock; (pid, start) is a process's identity


def _read(pid: int) -> Reading | None:
    """One process's rusage, or None when it is gone or not the user's
    (root-owned processes refuse)."""
    info = _RusageInfoV1()
    if _libproc.proc_pid_rusage(pid, _RUSAGE_INFO_V1, ctypes.byref(info)) != 0:
        return None
    ticks = info.user_time + info.system_time + info.child_user_time + info.child_system_time
    return Reading(info.phys_footprint, int(ticks * _NS_PER_TICK), int(info.proc_start_abstime * _NS_PER_TICK))


def _responsible(pid: int) -> int:
    """The pid macOS holds responsible for `pid`: for a WebKit helper, the app
    hosting the webview. A private libSystem call, like the shell's
    `_webProcessIdentifier`."""
    return _libsystem.responsibility_get_pid_responsible_for_pid(pid)


def _now() -> int:
    """ns on the clock `proc_start_abstime` counts on."""
    return int(_libsystem.mach_absolute_time() * _NS_PER_TICK)


def footprint_mb(pid: int) -> int | None:
    reading = _read(pid)
    return None if reading is None else reading.footprint >> 20


def _sysctl(name: str, buf: ctypes.Structure | ctypes.c_int) -> None:
    size = ctypes.c_size_t(ctypes.sizeof(buf))
    _libsystem.sysctlbyname(name.encode(), ctypes.byref(buf), ctypes.byref(size), None, ctypes.c_size_t(0))


def machine() -> dict:
    """Load, swap and free memory, straight from the kernel. mem_free_pct is the
    figure `memory_pressure` prints."""
    swap = _SwapUsage()
    level = ctypes.c_int()
    _sysctl("vm.swapusage", swap)
    _sysctl("kern.memorystatus_level", level)
    return {
        "load1": round(os.getloadavg()[0], 1),
        "swap_used_mb": swap.used >> 20,
        "swap_total_mb": swap.total >> 20,
        "mem_free_pct": level.value,
    }
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_resources.py -q`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add periscope/resources.py tests/test_resources.py
git commit -m "resources: footprint, CPU time and machine figures through libproc/sysctl, no subprocess (THI-448)

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: assign every process to one group

**Files:**
- Modify: `periscope/agent_processes.py:96,146`
- Modify: `periscope/resources.py`
- Test: `tests/test_resources.py`

- [ ] **Step 1: Make the subtree walk public**

In `periscope/agent_processes.py`, rename `_descendants` to `descendants` (definition at line 96 and the call at line 146). Add a one-line docstring to it:

```python
def descendants(root_pid: int, snapshot: dict[int, ProcessInfo]):
    """`root_pid`'s process (when in the snapshot) and every process below it."""
```

Run: `uv run pytest tests/test_agent_processes.py -q` — Expected: PASS.

- [ ] **Step 2: Write the failing tests**

Append to `tests/test_resources.py`:

```python
# ── group assignment ─────────────────────────────────────────────────────

from periscope.agent_processes import ProcessInfo

SERVER = os.getpid()


def _proc(pid, ppid, comm="/bin/zsh"):
    return ProcessInfo(pid=pid, ppid=ppid, started_at=0, comm=comm, argv=comm, state="S")


def _snapshot():
    return {
        p.pid: p
        for p in (
            _proc(1, 0, "/sbin/launchd"),
            _proc(SERVER, 1, "/usr/bin/python3"),
            _proc(900, SERVER, "/opt/homebrew/bin/tmux"),  # control-mode client
            _proc(500, 1, "/Applications/Periscope.app/Contents/MacOS/periscope-app"),
            _proc(600, 1, "/System/Library/.../com.apple.WebKit.WebContent"),
            _proc(100, 1, "/bin/zsh"),  # pane %1's root
            _proc(101, 100, "/opt/homebrew/bin/claude"),
            _proc(102, 101, "/usr/local/bin/node"),  # a tool child
            _proc(200, 1, "/bin/zsh"),  # pane %2's root
            _proc(700, 1, "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        )
    }


def test_assign_puts_every_process_in_one_group(monkeypatch):
    monkeypatch.setattr(resources, "_responsible", {600: 500}.get)
    named, per_pane = resources._assign(_snapshot(), {"%1": 100, "%2": 200})
    assert named["server"] == {SERVER, 900}
    assert named["webview"] == {500, 600}  # the WebContent by responsible pid, not by parent
    assert per_pane == {"%1": {100, 101, 102}, "%2": {200}}
    assert named["panes"] == {100, 101, 102, 200}
    assert named["elsewhere"] == {1, 700}


def test_assign_without_the_app_has_no_webview(monkeypatch):
    monkeypatch.setattr(resources, "_responsible", lambda pid: pid)
    snap = {pid: p for pid, p in _snapshot().items() if pid != 500}
    named, _ = resources._assign(snap, {"%1": 100})
    assert "webview" not in named
    assert 600 in named["elsewhere"]
```

- [ ] **Step 3: Run them to verify they fail**

Run: `uv run pytest tests/test_resources.py -k assign -q`
Expected: FAIL — `AttributeError: module 'periscope.resources' has no attribute '_assign'`.

- [ ] **Step 4: Implement `_assign`**

In `periscope/resources.py`, add `from periscope.agent_processes import ProcessInfo, descendants` to the imports (`agent_processes` is stdlib-only too), the constant below `_RUSAGE_INFO_V1`, and the function after `machine()`:

```python
# The Tauri shell's executable (src-tauri). Its WebKit helpers have launchd as
# their parent, so they're found by responsible pid instead.
_APP_NAME = "periscope-app"
```

```python
def _assign(
    snapshot: dict[int, ProcessInfo], panes: dict[str, int]
) -> tuple[dict[str, set[int]], dict[str, set[int]]]:
    """Every process in exactly one group: ({"server", "webview"?, "panes",
    "elsewhere"} → pids, pane_id → pids). Claimed in that order, so a process
    is never counted twice."""
    claimed: set[int] = set()

    def claim(pids: set[int]) -> set[int]:
        fresh = pids - claimed
        claimed.update(fresh)
        return fresh

    def subtree(root: int) -> set[int]:
        return {p.pid for p in descendants(root, snapshot)}

    named = {"server": claim(subtree(os.getpid()))}
    apps = {p.pid for p in snapshot.values() if os.path.basename(p.comm) == _APP_NAME}
    if apps:
        named["webview"] = claim(apps | {pid for pid in snapshot if _responsible(pid) in apps})
    per_pane = {pane_id: claim(subtree(root)) for pane_id, root in panes.items()}
    named["panes"] = set().union(*per_pane.values())
    named["elsewhere"] = set(snapshot) - claimed
    return named, per_pane
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_resources.py tests/test_agent_processes.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add periscope/agent_processes.py periscope/resources.py tests/test_resources.py
git commit -m "resources: every process lands in one group — server, webview by responsible pid, each pane's subtree, elsewhere (THI-448)

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: a group's CPU over the interval

**Files:**
- Modify: `periscope/resources.py`
- Test: `tests/test_resources.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_resources.py`:

```python
# ── CPU accounting (spec D4, D5) ─────────────────────────────────────────

S = 1_000_000_000  # one second in ns


def _cpu(prev_at, prev, at, cur):
    return resources._cpu_pct(resources._Sample(prev_at, prev), resources._Sample(at, cur))


def test_cpu_is_unknown_without_a_previous_sample():
    assert resources._cpu_pct(None, resources._Sample(S, {(1, 0): S})) is None


def test_cpu_steady_process():
    assert _cpu(0, {(1, 0): S}, S, {(1, 0): S + S // 2}) == 50.0


def test_cpu_counts_a_process_started_since_the_previous_sample():
    assert _cpu(S, {}, 2 * S, {(2, S + S // 2): S // 5}) == 20.0


def test_cpu_ignores_an_old_process_new_to_the_group():
    # Started before the previous sample: its total isn't this interval's.
    assert _cpu(S, {}, 2 * S, {(2, S // 2): 7 * S}) == 0.0


def test_cpu_counts_a_reaped_child_once():
    # Child 2 had 0.3 s; it ran 0.1 s more and exited, and parent 1 reaped it:
    # the parent's total gains the child's final 0.4 s plus 0.1 s of its own.
    prev = {(1, 0): S, (2, 0): 3 * S // 10}
    cur = {(1, 0): S + 4 * S // 10 + S // 10}
    assert _cpu(0, prev, S, cur) == 20.0


def test_cpu_clamps_a_child_reaped_outside_the_group():
    assert _cpu(0, {(1, 0): S, (2, 0): 5 * S}, S, {(1, 0): S + S // 10}) == 0.0


def test_cpu_never_diffs_a_reused_pid_against_its_predecessor():
    # pid 1 exited (5 s, reaped by member 9) and a new pid 1 started since.
    prev = {(1, 0): 5 * S, (9, 0): S}
    cur = {(9, 0): 6 * S, (1, S + S // 2): S // 10}
    assert _cpu(S, prev, 2 * S, cur) == 10.0


def test_cpu_treats_a_stale_previous_sample_as_missing():
    assert _cpu(0, {(1, 0): S}, 11 * S, {(1, 0): 2 * S}) is None
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_resources.py -k cpu -q`
Expected: FAIL — no attribute `_Sample`.

- [ ] **Step 3: Implement `_Sample` and `_cpu_pct`**

In `periscope/resources.py`, add below `_APP_NAME`:

```python
# A previous sample older than this (a hub that idled with no subscribers)
# would average hours into one number; it counts as missing.
_STALE_NS = 10 * 1_000_000_000
```

and after `_assign`:

```python
@dataclass(frozen=True)
class _Sample:
    at: int  # ns, _now()'s clock
    totals: dict[tuple[int, int], int]  # (pid, start) → cpu_ns, the group's members


def _cpu_pct(prev: _Sample | None, cur: _Sample) -> float | None:
    """The group's CPU per core since `prev` (100 = one core), or None with no
    usable previous sample.

    A member that vanished was reaped, and its whole CPU time reappears in its
    parent's children time, so its last total comes off. A member reaped
    outside the group never comes back, hence the clamp at 0."""
    if prev is None or cur.at - prev.at > _STALE_NS:
        return None
    delta = 0
    for ident, total in cur.totals.items():
        if ident in prev.totals:
            delta += total - prev.totals[ident]
        elif ident[1] > prev.at:  # started since the previous sample
            delta += total
    for ident, total in prev.totals.items():
        if ident not in cur.totals:
            delta -= total
    return round(max(delta, 0) / (cur.at - prev.at) * 100, 1)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_resources.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add periscope/resources.py tests/test_resources.py
git commit -m "resources: a group's CPU counts children reaped within it and never diffs a reused pid (THI-448)

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: `measure()` — one pass, under a lock, at most every 0.5 s

**Files:**
- Modify: `periscope/resources.py`
- Test: `tests/test_resources.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_resources.py`:

```python
# ── measure ──────────────────────────────────────────────────────────────

import threading


@pytest.fixture(autouse=True)
def _no_live_resources(monkeypatch):
    """Overrides conftest's stub of the same name: these tests install their
    own fakes, and the smoke tests read the real test process."""
    monkeypatch.setattr(resources, "_prev", {})
    monkeypatch.setattr(resources, "_last", None)

MACHINE = {"load1": 1.0, "swap_used_mb": 0, "swap_total_mb": 0, "mem_free_pct": 50}
MB = 1 << 20


class _Clock:
    def __init__(self):
        self.t = 10 * S

    def __call__(self):
        return self.t


@pytest.fixture
def fake(monkeypatch):
    """A machine of _snapshot()'s processes, each 100 MB, CPU set per test."""
    clock = _Clock()
    cpu = {pid: 0 for pid in _snapshot()}
    monkeypatch.setattr(resources, "process_snapshot", _snapshot)
    monkeypatch.setattr(resources, "_responsible", {600: 500}.get)
    monkeypatch.setattr(resources, "machine", lambda: MACHINE)
    monkeypatch.setattr(resources, "_now", clock)
    monkeypatch.setattr(resources, "_read", lambda pid: resources.Reading(100 * MB, cpu[pid], 0) if pid in cpu else None)
    return clock, cpu


PANES = {"%1": 100, "%2": 200}


def test_measure_first_pass_has_memory_and_unknown_cpu(fake):
    m = resources.measure(PANES)
    assert m.panes == {"%1": {"mem_mb": 300, "cpu_pct": None}, "%2": {"mem_mb": 100, "cpu_pct": None}}
    assert m.totals["server"] == {"mem_mb": 200, "cpu_pct": None}
    assert m.totals["webview"] == {"mem_mb": 200, "cpu_pct": None}
    assert m.totals["panes"] == {"mem_mb": 400, "cpu_pct": None}
    assert m.totals["elsewhere"] == {"mem_mb": 200, "cpu_pct": None}
    assert m.totals["machine"] == MACHINE


def test_measure_second_pass_has_cpu(fake):
    clock, cpu = fake
    resources.measure(PANES)
    clock.t += S
    cpu[102] += S // 2  # the pane's tool child: half a core
    m = resources.measure(PANES)
    assert m.panes["%1"]["cpu_pct"] == 50.0
    assert m.panes["%2"]["cpu_pct"] == 0.0
    assert m.totals["panes"]["cpu_pct"] == 50.0
    assert m.totals["server"]["cpu_pct"] == 0.0


def test_measure_within_half_a_second_returns_the_previous_measurement(fake):
    clock, _ = fake
    first = resources.measure(PANES)
    clock.t += S // 10
    assert resources.measure(PANES) is first


def test_measure_skips_unreadable_processes(fake, monkeypatch):
    monkeypatch.setattr(resources, "_read", lambda pid: None if pid == 102 else resources.Reading(100 * MB, 0, 0))
    assert resources.measure(PANES).panes["%1"]["mem_mb"] == 200


def test_measure_without_the_app_sends_no_webview(fake, monkeypatch):
    monkeypatch.setattr(resources, "process_snapshot", lambda: {p: v for p, v in _snapshot().items() if p != 500})
    assert resources.measure(PANES).totals["webview"] is None


def test_measure_is_none_when_the_snapshot_failed(fake, monkeypatch):
    monkeypatch.setattr(resources, "process_snapshot", lambda: None)
    assert resources.measure(PANES) is None


def test_measure_from_many_threads_at_once(fake):
    out = []
    threads = [threading.Thread(target=lambda: out.append(resources.measure(PANES))) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(out) == 8
    assert all(m is out[0] for m in out)  # one computed, the rest inside 0.5 s
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_resources.py -k measure -q`
Expected: FAIL — no attribute `measure` / `process_snapshot`.

- [ ] **Step 3: Implement `measure`**

In `periscope/resources.py`:
- add `import threading` to the imports and change the agent_processes import to `from periscope.agent_processes import ProcessInfo, descendants, process_snapshot`;
- add below `_STALE_NS`:

```python
# A delta over less than this reads noise (5 ms windows measured 0% for a
# server averaging 10%); a caller inside it gets the previous measurement.
_MIN_INTERVAL_NS = 500_000_000
```

- append at the end of the module:

```python
@dataclass(frozen=True)
class Measurement:
    panes: dict[str, dict]  # pane_id → {mem_mb, cpu_pct}
    totals: dict  # server, webview (None without the app), panes, elsewhere: {mem_mb, cpu_pct}; machine


# build_state runs from the state hub, the REST fallback poll and other tabs at
# once; the lock makes each read-diff-write of _prev whole.
_lock = threading.Lock()
_prev: dict[str, _Sample] = {}  # group key → its last sample
_last: tuple[int, Measurement] | None = None


def measure(panes: dict[str, int]) -> Measurement | None:
    """Footprint and CPU for each pane (pane_id → its root pid, tmux's
    pane_pid) and every other group. None when this tick's process snapshot
    failed: an empty tree would collapse every pane to its shell and read as
    real."""
    global _prev, _last
    snapshot = process_snapshot()
    if snapshot is None:
        return None
    with _lock:
        now = _now()
        if _last is not None and now - _last[0] < _MIN_INTERVAL_NS:
            return _last[1]
        named, per_pane = _assign(snapshot, panes)
        readings = {pid: r for pid in snapshot if (r := _read(pid)) is not None}
        prev, _prev = _prev, {}

        def group(key: str, pids: set[int]) -> dict:
            members = {(pid, readings[pid].start): readings[pid] for pid in pids if pid in readings}
            cur = _Sample(now, {ident: r.cpu_ns for ident, r in members.items()})
            _prev[key] = cur
            return {
                "mem_mb": sum(r.footprint for r in members.values()) >> 20,
                "cpu_pct": _cpu_pct(prev.get(key), cur),
            }

        totals: dict = {"webview": None} | {key: group(key, pids) for key, pids in named.items()}
        totals["machine"] = machine()
        measurement = Measurement({pane_id: group(f"pane:{pane_id}", pids) for pane_id, pids in per_pane.items()}, totals)
        _last = (now, measurement)
        return measurement
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_resources.py -q`
Expected: PASS (all of Tasks 1–4). The module-level `_no_live_resources` fixture added above resets `_prev` / `_last` per test, and in Task 5 it overrides conftest's stub of the same name.

- [ ] **Step 5: Commit**

```bash
git add periscope/resources.py tests/test_resources.py
git commit -m "resources: measure() reads every process once per tick under a lock, at most every 0.5 s (THI-448)

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: keep every other test off the real machine

**Files:**
- Modify: `tests/conftest.py` (after `_no_live_session_scan`, ~line 83)

- [ ] **Step 1: Add the autouse stub**

```python
@pytest.fixture(autouse=True)
def _no_live_resources(monkeypatch):
    """Keep resources.measure off the real machine: no `ps` snapshot, no
    rusage reads, no sysctl. Every /api/state build calls it, so without this
    any route test would read the developer's real processes.
    tests/test_resources.py overrides this fixture by name and installs its
    own fakes."""
    from periscope import resources

    monkeypatch.setattr(resources, "process_snapshot", dict)
    monkeypatch.setattr(resources, "_read", lambda pid: None)
    monkeypatch.setattr(resources, "_responsible", lambda pid: 0)
    monkeypatch.setattr(
        resources, "machine", lambda: {"load1": 0.0, "swap_used_mb": 0, "swap_total_mb": 0, "mem_free_pct": 100}
    )
    monkeypatch.setattr(resources, "_prev", {})
    monkeypatch.setattr(resources, "_last", None)
```

- [ ] **Step 2: Run the whole Python suite**

Run: `uv run pytest -q`
Expected: PASS (nothing calls `measure` yet; this proves the fixture itself is sound).

- [ ] **Step 3: Commit**

```bash
git add tests/conftest.py
git commit -m "tests: an autouse stub keeps resources off the real machine (THI-448)

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: `/api/state` carries `res` and `resources`

**Files:**
- Modify: `periscope/routes/state.py`
- Test: `tests/routes/test_state.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/routes/test_state.py`:

```python
def _patch_one_window(mocker):
    fake_windows = [
        {"session": "main", "index": 0, "name": "x", "active": True, "cwd": "/tmp",
         "pid_raw": "", "pane_id": "%7", "pane_pid": "4242", "pid": "abc123"},
    ]
    _patch(mocker, "list_windows", return_value=fake_windows)
    _patch(mocker, "update_focus_from_windows")
    _patch(mocker, "_attach_git_then_resolve_pids")
    _patch(mocker, "capture", return_value="$ ls\n")
    _patch(mocker, "parse_pane", return_value={"agent": None, "spinner": None, "state": "shell"})
    _patch(mocker, "cached_git_state", return_value={"branch": "main"})
    _patch(mocker, "cached_pr_state", return_value={"pr": None})
    _patch(mocker, "cached_claude_usage", return_value={})
    _patch(mocker, "cached_plan_usage", return_value={})
    _patch(mocker, "all_pane_ids", return_value={"%7"})
    _patch(mocker, "_channel_gc")


def test_state_carries_each_panes_resources_and_the_totals(client, mocker, clean_state):
    from periscope import resources

    _patch_one_window(mocker)
    group = {"mem_mb": 512, "cpu_pct": 12.5}
    totals = {"server": group, "webview": None, "panes": group, "elsewhere": group, "machine": {"load1": 1.0}}
    measure = mocker.patch.object(
        resources, "measure", return_value=resources.Measurement({"%7": group}, totals)
    )

    body = client.get("/api/state").json()
    measure.assert_called_once_with({"%7": 4242})
    assert body["windows"][0]["res"] == group
    assert body["resources"] == totals


def test_state_sends_no_resources_when_the_snapshot_failed(client, mocker, clean_state):
    from periscope import resources

    _patch_one_window(mocker)
    mocker.patch.object(resources, "measure", return_value=None)

    body = client.get("/api/state").json()
    assert body["windows"][0]["res"] is None
    assert body["resources"] is None
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/routes/test_state.py -k resources -q`
Expected: FAIL — `KeyError: 'res'`.

- [ ] **Step 3: Wire `measure` into `build_state`**

In `periscope/routes/state.py`, change the import line `from periscope import store, updater, usage` to `from periscope import resources, store, updater, usage`. After `annotate_cost_pressure(result)` insert:

```python
    # Memory and CPU per owner, on this tick's process snapshot. Each row's
    # pane_pid roots its pane's group; None when the snapshot failed.
    measured = resources.measure({v["pane_id"]: int(v["pane_pid"]) for v in result if v.get("pane_pid")})
    for view in result:
        view["res"] = measured.panes.get(view["pane_id"]) if measured else None
```

and in the returned dict, after `"editor": …`, add:

```python
        # server / webview / panes / elsewhere / machine (resources.measure);
        # the header pill and its popover.
        "resources": measured.totals if measured else None,
```

- [ ] **Step 4: Run the state tests**

Run: `uv run pytest tests/routes/test_state.py -q`
Expected: PASS. If an existing test's fake window has no `pane_id`, `view["pane_id"]` raises — every row from `list_windows` has one (`panes.py:427`), so give that fake one rather than loosening the code.

- [ ] **Step 5: Commit**

```bash
git add periscope/routes/state.py tests/routes/test_state.py
git commit -m "state: each row carries its pane's res and the blob the resources totals (THI-448)

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: the ↻ cycle hint on footprint

**Files:**
- Modify: `periscope/config.py:155-161`
- Modify: `periscope/session_status.py:54-56,151-185,511-528`
- Modify: `periscope/window_view.py:27,224-239`
- Test: `tests/test_session_status.py:124-196`, `tests/test_window_view.py:803-832`

- [ ] **Step 1: Update the tests first**

In `tests/test_session_status.py`, replace `_PS_SAMPLE` and `test_claude_procs_carry_rss_and_age`:

```python
_PS_SAMPLE = (
    " 9448     01-02:08:58 /Users/tom/.local/bin/claude\n"
    " 9876        22:09:53 /Users/tom/.local/share/claude/versions/2.1.161\n"
    "  329        05:33 /usr/libexec/logd\n"
    "    1     91-10:00:00 /sbin/launchd\n"
)
```

```python
def test_claude_procs_carry_age(monkeypatch):
    class _R:
        stdout = _PS_SAMPLE

    monkeypatch.setattr(ss, "_claude_procs_cache", None)
    monkeypatch.setattr(ss.subprocess, "run", lambda *a, **k: _R())
    assert ss._claude_procs() == {
        9448: 26 * 3600 + 8 * 60 + 58,
        9876: 22 * 3600 + 9 * 60 + 53,
    }
```

and the `claude_proc_for` cases:

```python
def test_claude_proc_for_returns_stats(tmp_path, monkeypatch):
    # status "shell" is untrusted for STATE, but measurement still applies —
    # a bloated claude deserves the hint regardless of what it's doing.
    _seed_procs(tmp_path, monkeypatch, _sess(300, "sid-m", "shell"), {300: 4 * 86400})
    monkeypatch.setattr(ss.resources, "footprint_mb", {300: 4400}.get)
    assert ss.claude_proc_for("sid-m") == {"pid": 300, "mem_mb": 4400, "age_s": 4 * 86400}
```

In `test_claude_proc_for_unmapped_none`, change `{302: (1, 1)}` to `{302: 1}`.

In `tests/test_window_view.py`, replace the four `mem_signal` tests (lines 803–832):

```python
def test_mem_signal_none_when_healthy():
    from periscope.window_view import mem_signal

    assert mem_signal(None) is None
    assert mem_signal({"pid": 1, "mem_mb": 600, "age_s": 3600}) is None


def test_mem_signal_warn_on_footprint():
    from periscope.window_view import mem_signal

    out = mem_signal({"pid": 1, "mem_mb": 2048, "age_s": 60})
    assert out == {"tier": "warn", "trigger": "mem", "gb": 2.0, "age_s": 60}


def test_mem_signal_warn_on_age_alone():
    from periscope.window_view import mem_signal

    out = mem_signal({"pid": 1, "mem_mb": 500, "age_s": 3 * 86400})
    assert out == {"tier": "warn", "trigger": "age", "gb": 0.5, "age_s": 3 * 86400}


def test_mem_signal_bad_overrides_warn():
    from periscope.window_view import mem_signal

    out = mem_signal({"pid": 1, "mem_mb": 4300, "age_s": 60})
    assert out["tier"] == "bad"
    assert out["gb"] == 4.2


def test_mem_signal_unreadable_footprint_trips_on_age_only():
    from periscope.window_view import mem_signal

    assert mem_signal({"pid": 1, "mem_mb": None, "age_s": 60}) is None
    out = mem_signal({"pid": 1, "mem_mb": None, "age_s": 3 * 86400})
    assert out == {"tier": "warn", "trigger": "age", "gb": None, "age_s": 3 * 86400}
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_session_status.py tests/test_window_view.py -k "claude_proc or mem_signal" -q`
Expected: FAIL.

- [ ] **Step 3: Implement**

`periscope/config.py` — replace the thresholds block:

```python
# Claude cycle-hint thresholds (rail ↻ chip): red when a pane's claude
# footprint crosses BAD, amber at WARN footprint or WARN age. Healthy claudes
# sit at 0.25–0.55GB of footprint; the leak class that motivated this shows up
# as multi-GB on multi-day processes.
MEM_BAD_MB = 4 * 1024  # 4GB
MEM_WARN_MB = 2 * 1024  # 2GB
MEM_WARN_AGE_S = 3 * 86400  # 3 days
```

`periscope/session_status.py`:
- add `from periscope import resources` to the imports;
- cache comment and type: `# (built_at, {pid: age_s}) — one ps snapshot serves both pid` / `# liveness (the key set) and the uptime cycle-hint.` and `_claude_procs_cache: tuple[float, dict[int, int]] | None = None`;
- `_claude_procs`:

```python
def _claude_procs() -> dict[int, int]:
    """{pid: age_s} of running `claude` processes, snapshotted once per poll.
    The key set guards against honoring a session file whose process has
    exited or whose pid was recycled; age feeds the cycle-hint. `comm` is the
    full executable path; claude's launcher (`.../bin/claude`) and versioned
    binary (`.../share/claude/versions/<v>`) both contain "claude". comm may
    itself contain spaces, hence maxsplit=2."""
    global _claude_procs_cache
    now = time.time()
    if _claude_procs_cache and now - _claude_procs_cache[0] < _CACHE_TTL_S:
        return _claude_procs_cache[1]
    procs: dict[int, int] = {}
    try:
        out = subprocess.run(
            ["ps", "-axo", "pid=,etime=,comm="],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout
        for line in out.splitlines():
            parts = line.split(None, 2)
            if len(parts) != 3:
                continue
            pid_s, etime, comm = parts
            if "claude" in comm.lower():
                with contextlib.suppress(ValueError):
                    procs[int(pid_s)] = parse_etime(etime)
    except (OSError, subprocess.SubprocessError):
        pass
    _claude_procs_cache = (now, procs)
    return procs
```

- `claude_proc_for`:

```python
def claude_proc_for(sid: str | None) -> dict | None:
    """Process stats for a sessionId's claude: {"pid", "mem_mb", "age_s"}, or
    None when unmapped or not a live claude. mem_mb is its footprint, None if
    it exited since the ps snapshot. Measurement, not state — applies even when
    the status is one session_state_for doesn't trust (a bloated claude
    deserves the cycle-hint regardless of what it's doing)."""
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
    return {"pid": pid, "mem_mb": resources.footprint_mb(pid), "age_s": age}
```

`periscope/window_view.py` — import line 27 becomes `from periscope.config import MEM_BAD_MB, MEM_WARN_AGE_S, MEM_WARN_MB`, and:

```python
def mem_signal(proc: dict | None) -> dict | None:
    """Cycle-hint for a claude process, from claude_proc_for's stats dict:
    "bad" at ≥MEM_BAD_MB of footprint, "warn" at ≥MEM_WARN_MB or
    ≥MEM_WARN_AGE_S, None otherwise — None renders nothing, so a healthy pane
    costs no rail space. `trigger` says which limit tripped, so the chip leads
    with it. Policy lives here (config thresholds); measurement stays in
    session_status, which can't import config."""
    if not proc:
        return None
    mb, age = proc["mem_mb"], proc["age_s"]
    if mb is not None and mb >= MEM_WARN_MB:
        trigger = "mem"
    elif age >= MEM_WARN_AGE_S:
        trigger = "age"
    else:
        return None
    tier = "bad" if mb is not None and mb >= MEM_BAD_MB else "warn"
    return {"tier": tier, "trigger": trigger, "gb": None if mb is None else round(mb / 1024, 1), "age_s": age}
```

- [ ] **Step 4: Run the suite**

Run: `uv run pytest -q` and `git grep -n "RSS_KB\|rss_kb\|rss_gb" periscope tests` (expect no output).
Expected: PASS, no leftover names.

- [ ] **Step 5: Commit**

```bash
git add periscope/config.py periscope/session_status.py periscope/window_view.py tests/test_session_status.py tests/test_window_view.py
git commit -m "cycle hint: the ↻ chip reads claude's footprint, not RSS, and says which limit tripped (THI-448)

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 8: frontend formatting and `memHint`

**Files:**
- Create: `static/src/chrome/resources.js`
- Create: `static/src/chrome/__tests__/resources.test.js`
- Modify: `static/src/util.js:105-119`
- Modify: `static/src/__tests__/memHint.test.js`, `static/src/__tests__/agent.test.js:34`

- [ ] **Step 1: Write the failing tests**

Create `static/src/chrome/__tests__/resources.test.js`:

```js
// resources.js — formatting and ordering behind <ResourcePill> and the
// detail header's mem/cpu part.
import { describe, expect, it } from "vitest";
import { fmtCpu, fmtMem, machineLine, ownedMb, paneLines } from "../resources.js";

describe("fmtMem", () => {
  it("megabytes under a gigabyte, one decimal of gigabytes above", () => {
    expect(fmtMem(512)).toBe("512M");
    expect(fmtMem(1024)).toBe("1.0G");
    expect(fmtMem(6963)).toBe("6.8G");
  });
});

describe("fmtCpu", () => {
  it("rounds per-core percent; unknown is a dash, never 0", () => {
    expect(fmtCpu(57.6)).toBe("58%");
    expect(fmtCpu(0)).toBe("0%");
    expect(fmtCpu(null)).toBe("–");
  });
});

describe("ownedMb", () => {
  const g = (mem_mb) => ({ mem_mb, cpu_pct: null });
  it("adds server, webview and panes — not elsewhere", () => {
    expect(ownedMb({ server: g(100), webview: g(2000), panes: g(10000), elsewhere: g(9999) })).toBe(12100);
  });
  it("counts no webview when there is none", () => {
    expect(ownedMb({ server: g(100), webview: null, panes: g(900), elsewhere: g(1) })).toBe(1000);
  });
});

describe("paneLines", () => {
  it("heaviest first, skipping rows without a measurement", () => {
    const a = { pid: "a", res: { mem_mb: 300, cpu_pct: 1 } };
    const b = { pid: "b", res: { mem_mb: 7000, cpu_pct: 58 } };
    const c = { pid: "c", res: null };
    expect(paneLines([a, b, c]).map((l) => l.w.pid)).toEqual(["b", "a"]);
  });
});

describe("machineLine", () => {
  it("load, swap and free memory", () => {
    expect(machineLine({ load1: 52.3, swap_used_mb: 16896, swap_total_mb: 17818, mem_free_pct: 27 }))
      .toBe("load 52.3 · swap 16.5G/17.4G · 27% free");
  });
});
```

Replace `static/src/__tests__/memHint.test.js`'s two tier cases:

```js
  it("a footprint trip leads with gigabytes", () => {
    const h = memHint({ tier: "bad", trigger: "mem", gb: 4.2, age_s: 2 * 86400 + 5 * 3600 });
    expect(h.label).toBe("4.2G");
    expect(h.cls).toBe("mem-bad");
    expect(h.title).toContain("4.2GB");
    expect(h.title).toContain("2d 5h");
  });

  it("an age trip leads with uptime days", () => {
    const h = memHint({ tier: "warn", trigger: "age", gb: 0.5, age_s: 3 * 86400 });
    expect(h.label).toBe("3d");
    expect(h.cls).toBe("mem-warn");
  });

  it("an age trip with an unreadable footprint still explains itself", () => {
    const h = memHint({ tier: "warn", trigger: "age", gb: null, age_s: 3 * 86400 });
    expect(h.title).toContain("running 3d 0h");
    expect(h.title).not.toContain("null");
  });
```

In `static/src/__tests__/agent.test.js:34`, change `mem: { rss_gb: 4, age_s: 86400 },` to `mem: { tier: "bad", trigger: "mem", gb: 4, age_s: 86400 },`.

- [ ] **Step 2: Run them to verify they fail**

Run: `npx vitest run static/src/chrome/__tests__/resources.test.js static/src/__tests__/memHint.test.js`
Expected: FAIL — module not found / label mismatch.

- [ ] **Step 3: Implement**

Create `static/src/chrome/resources.js`:

```js
// Formatting and ordering behind <ResourcePill> and the detail header.
// /api/state's `resources` groups and each row's `res` are
// { mem_mb, cpu_pct }: memory footprint, and CPU per core (100 = one core
// busy) — null until the server has two samples to diff.

export function fmtMem(mb) {
  return mb >= 1024 ? `${(mb / 1024).toFixed(1)}G` : `${mb}M`;
}

export function fmtCpu(pct) {
  return pct == null ? "–" : `${Math.round(pct)}%`;
}

// The pill's figure: what periscope and its panes hold. Elsewhere is the rest
// of the machine, shown in the popover, not owned here.
export function ownedMb(r) {
  return r.server.mem_mb + (r.webview?.mem_mb || 0) + r.panes.mem_mb;
}

// Popover pane lines, heaviest first. A row has no `res` when the server's
// snapshot failed that tick.
export function paneLines(windows) {
  return windows
    .filter((w) => w.res)
    .map((w) => ({ w, ...w.res }))
    .sort((a, b) => b.mem_mb - a.mem_mb);
}

export function machineLine(m) {
  return `load ${m.load1} · swap ${fmtMem(m.swap_used_mb)}/${fmtMem(m.swap_total_mb)} · ${m.mem_free_pct}% free`;
}
```

In `static/src/util.js`, replace the comment and body of `memHint` (lines ~105–119):

```js
// Cycle-hint chip content (rail ↻ chip + detail header). `mem` is the
// server's `w.mem` ({tier: "warn"|"bad", trigger: "mem"|"age", gb, age_s})
// or null/undefined — the server only sends it when a threshold tripped, so
// null → no chip. The label leads with whichever limit tripped. gb is null
// when claude's footprint couldn't be read.
export function memHint(mem) {
  if (!mem) return null;
  const days = Math.floor(mem.age_s / 86400);
  const hours = Math.floor((mem.age_s % 86400) / 3600);
  const up = days > 0 ? `${days}d ${hours}h` : `${hours}h`;
  const what = mem.gb == null ? `Claude has been running ${up}` : `Claude is using ${mem.gb}GB after ${up}`;
  return {
    label: mem.trigger === "mem" ? `${mem.gb}G` : `${days}d`,
    title: `${what} — consider exiting and resuming this session`,
    cls: `mem-${mem.tier}`,
  };
}
```

(Keep whatever doc-comment lines above line 105 that describe the chip if they differ; the four comment lines above are the replacement for the `rss_gb` description.)

- [ ] **Step 4: Run them to verify they pass**

Run: `npx vitest run static/src/chrome/__tests__/resources.test.js static/src/__tests__/memHint.test.js static/src/__tests__/agent.test.js`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add static/src/chrome/resources.js static/src/chrome/__tests__/resources.test.js static/src/util.js static/src/__tests__/memHint.test.js static/src/__tests__/agent.test.js
git commit -m "ui: resources formatting, and memHint labels from the server's trigger (THI-448)

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 9: the header pill and popover

**Files:**
- Modify: `static/src/store.js` (signals block, ~line 34)
- Modify: `static/src/poll.js` (store import list ending line 33; commit block ~line 66)
- Create: `static/src/chrome/ResourcePill.jsx`
- Create: `static/src/chrome/__tests__/resourcePillRender.test.jsx`
- Modify: `static/src/chrome/Header.jsx:29,138`
- Modify: `static/styles.css` (after the `.update-menu-error` rule, ~line 302)

- [ ] **Step 1: Add the signal and feed it**

`static/src/store.js`, after `export const updateInfo = …`:

```js
export const resources = signal(null);        // /api/state resources: { server, webview, panes, elsewhere, machine }, or null when the server's snapshot failed
```

`static/src/poll.js`: add `resources,` to the `from "./store.js"` import list, and after `updateInfo.value = data.update || null;`:

```js
    resources.value = data.resources || null;
```

- [ ] **Step 2: Write the failing render test**

Create `static/src/chrome/__tests__/resourcePillRender.test.jsx`:

```jsx
// Render-path test for <ResourcePill>. Same rationale/technique as
// usagePillRender.test.jsx: catches wiring errors a build alone can't.

import render from "preact-render-to-string";
import { afterEach, describe, expect, it } from "vitest";
import { resources, windows } from "../../store.js";
import { ResourcePill } from "../ResourcePill.jsx";

const g = (mem_mb, cpu_pct) => ({ mem_mb, cpu_pct });
const R = {
  server: g(176, 7),
  webview: g(2900, 40),
  panes: g(12800, 112),
  elsewhere: g(30000, 300),
  machine: { load1: 52.3, swap_used_mb: 16896, swap_total_mb: 17818, mem_free_pct: 27 },
};

afterEach(() => {
  resources.value = null;
  windows.value = [];
});

describe("<ResourcePill>", () => {
  it("renders nothing without a measurement", () => {
    expect(render(<ResourcePill />)).toBe("");
  });

  it("shows what periscope and its panes hold", () => {
    resources.value = R;
    const html = render(<ResourcePill />);
    expect(html).toContain("▣ 15.5G");  // 176 + 2900 + 12800 MB: elsewhere is not periscope's
  });

  it("lists the owners, panes heaviest first, then elsewhere and the machine", () => {
    resources.value = R;
    windows.value = [
      { pid: "a", name: "light", res: g(300, 1) },
      { pid: "b", name: "heavy", res: g(6963, 58) },
    ];
    const html = render(<ResourcePill />);
    const order = [">periscope server<", ">webview<", ">heavy<", ">light<", ">elsewhere<", "load 52.3"];
    const at = order.map((s) => html.indexOf(s));
    expect(at.every((i) => i >= 0)).toBe(true);
    expect([...at].sort((x, y) => x - y)).toEqual(at);
    expect(html).toContain("6.8G");
    expect(html).toContain("58%");
  });

  it("leaves the webview line out when there is no app", () => {
    resources.value = { ...R, webview: null };
    expect(render(<ResourcePill />)).not.toContain(">webview<");
  });
});
```

Run: `npx vitest run static/src/chrome/__tests__/resourcePillRender.test.jsx`
Expected: FAIL — cannot find `../ResourcePill.jsx`.

- [ ] **Step 3: Write the component**

Create `static/src/chrome/ResourcePill.jsx`:

```jsx
// "▣ 15.5G" pill: memory held by the periscope server, its webview and every
// pane's process tree (resources.py). Click → each owner's memory and CPU,
// panes heaviest first, then everything else on the machine and the machine's
// load / swap / free memory. Hidden when the server sent no measurement (its
// process snapshot failed that tick).
import { signal } from "@preact/signals";
import { useCallback, useEffect, useRef } from "preact/hooks";
import { useEscape } from "../hooks/useEscape.js";
import { selectPane } from "../split/alertFeed.js";
import { resources, windows } from "../store.js";
import { paneLabel } from "../util.js";
import { fmtCpu, fmtMem, machineLine, ownedMb, paneLines } from "./resources.js";

const popoverOpen = signal(false);
const CPU_TIP = "CPU per core: 100% is one core fully busy";

function Line({ label, group, onClick }) {
  return (
    <div class={`res-line${onClick ? " is-pane" : ""}`} onClick={onClick}>
      <span class="res-label">{label}</span>
      <span class="res-mem">{fmtMem(group.mem_mb)}</span>
      <span class="res-cpu" title={CPU_TIP}>{fmtCpu(group.cpu_pct)}</span>
    </div>
  );
}

export function ResourcePill() {
  const ref = useRef(null);
  const open = popoverOpen.value;
  const r = resources.value;
  const close = useCallback(() => { popoverOpen.value = false; }, []);
  useEscape(close, open);
  useEffect(() => {
    if (!open) return;
    function onDocClick(e) {
      if (ref.current && !ref.current.contains(e.target)) close();
    }
    document.addEventListener("click", onDocClick);
    return () => document.removeEventListener("click", onDocClick);
  }, [open]);
  // The pill can vanish with the popover open (a tick with no measurement);
  // close it, or Escape stays bound to an invisible popover.
  useEffect(() => {
    if (!r && open) close();
  }, [r, open]);
  if (!r) return null;

  return (
    <div class="update-dd res-dd" ref={ref}>
      <button
        type="button"
        class="res-pill"
        title="memory held by periscope's server, its webview and its panes — click for each one's memory and CPU"
        aria-haspopup="dialog"
        aria-expanded={open ? "true" : "false"}
        onClick={() => { popoverOpen.value = !open; }}
      >
        ▣ {fmtMem(ownedMb(r))}
      </button>
      <div class="tb-dd-menu res-menu" role="dialog" hidden={!open}>
        <Line label="periscope server" group={r.server} />
        {r.webview && <Line label="webview" group={r.webview} />}
        {paneLines(windows.value || []).map(({ w, mem_mb, cpu_pct }) => (
          <Line
            key={w.pid}
            label={paneLabel(w)}
            group={{ mem_mb, cpu_pct }}
            onClick={() => { selectPane(w); close(); }}
          />
        ))}
        <Line label="elsewhere" group={r.elsewhere} />
        <div class="res-machine">{machineLine(r.machine)}</div>
      </div>
    </div>
  );
}
```

- [ ] **Step 4: Run the render test**

Run: `npx vitest run static/src/chrome/__tests__/resourcePillRender.test.jsx`
Expected: PASS.

- [ ] **Step 5: Place it and style it**

`static/src/chrome/Header.jsx`: add `import { ResourcePill } from "./ResourcePill.jsx";` (alphabetical, before `SpawnAccountPicker`), and render `<ResourcePill />` directly after `<UsagePill />`.

`static/styles.css`, after the `.update-menu-error` rule:

```css
/* Resource pill (ResourcePill.jsx): neutral, it reports rather than calls to action. */
.res-pill {
  flex: 0 0 auto;
  align-self: center;
  font: inherit;
  font-size: 11px;
  padding: 2px 8px;
  border-radius: 999px;
  cursor: pointer;
  color: var(--fg-2);
  background: transparent;
  border: 1px solid var(--line);
}
.res-pill:hover { color: var(--fg-1); }
.tb-dd-menu.res-menu { left: auto; right: 0; width: max-content; min-width: 260px; gap: 0; padding: 6px; font-size: 11px; }
.res-line { display: grid; grid-template-columns: 1fr 5ch 5ch; gap: 8px; padding: 2px 6px; border-radius: var(--r-sm); }
.res-line.is-pane { cursor: pointer; }
.res-line.is-pane:hover { background: color-mix(in oklch, var(--fg-1) 8%, transparent); }
.res-label { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.res-mem, .res-cpu { text-align: right; font-variant-numeric: tabular-nums; }
.res-cpu { color: var(--fg-3); cursor: help; }
.res-machine { margin-top: 6px; padding: 4px 6px 0; border-top: 1px solid var(--line); color: var(--fg-3); }
```

- [ ] **Step 6: Run the frontend suite**

Run: `npm test`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add static/src/store.js static/src/poll.js static/src/chrome/ResourcePill.jsx static/src/chrome/__tests__/resourcePillRender.test.jsx static/src/chrome/Header.jsx static/styles.css
git commit -m "ui: a header pill with each owner's memory and CPU, panes heaviest first (THI-448)

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 10: the detail header's mem / cpu part

**Files:**
- Modify: `static/src/split/Detail.jsx:34,258-260`
- Create: `static/src/__tests__/paneHeaderRes.test.js`

- [ ] **Step 1: Write the failing test**

Create `static/src/__tests__/paneHeaderRes.test.js`:

```js
// The detail header's "6.8G mem · 58% cpu" part: the selected pane's whole
// process tree (the row's `res`).
import { h } from "preact";
import render from "preact-render-to-string";
import { describe, expect, it } from "vitest";
import { PaneHeader } from "../split/Detail.jsx";

const base = { pid: "p1", target: "dev:1", state: "shell" };

describe("PaneHeader resources", () => {
  it("labels memory and CPU", () => {
    const html = render(h(PaneHeader, { w: { ...base, res: { mem_mb: 6963, cpu_pct: 57.6 } } }));
    expect(html).toContain("6.8G mem · 58% cpu");
  });

  it("shows a dash before the server has two samples", () => {
    const html = render(h(PaneHeader, { w: { ...base, res: { mem_mb: 300, cpu_pct: null } } }));
    expect(html).toContain("300M mem · – cpu");
  });

  it("shows nothing without a measurement", () => {
    expect(render(h(PaneHeader, { w: { ...base, res: null } }))).not.toContain("header-res");
  });
});
```

Run: `npx vitest run static/src/__tests__/paneHeaderRes.test.js` — Expected: FAIL.

- [ ] **Step 2: Implement**

In `static/src/split/Detail.jsx` add `import { fmtCpu, fmtMem } from "../chrome/resources.js";` to the imports, and after the `context_pct` block (before `if (w.agent === "claude" && w.mem)`):

```jsx
  if (w.res) {
    parts.push(
      <>
        <span class="hsep">·</span>
        <span class="header-res" title="this pane's whole process tree: memory footprint, and CPU per core (100% = one core)">
          {`${fmtMem(w.res.mem_mb)} mem · ${fmtCpu(w.res.cpu_pct)} cpu`}
        </span>
      </>
    );
  }
```

- [ ] **Step 3: Run the frontend suite**

Run: `npm test`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add static/src/split/Detail.jsx static/src/__tests__/paneHeaderRes.test.js
git commit -m "ui: the detail header shows the pane's memory and CPU, labelled (THI-448)

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

### Task 11: build, gate, verify in the browser, docs

**Files:**
- Modify: `static/dist/*` (build output), `CLAUDE.md`, `docs/superpowers/specs/2026-10-30-resource-monitor-design.md`

- [ ] **Step 1: Build and commit the bundle**

```bash
npm run build
git add static/dist
git commit -m "build: static/dist with the resource pill (THI-448)

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

- [ ] **Step 2: Full gate**

Run: `bin/check && uv run pytest -q && npm test`
Expected: zero lint/type violations; all tests pass. Fix anything red in the task that introduced it, with its own commit.

- [ ] **Step 3: Verify against the dev server**

```bash
PERISCOPE_PORT=17375 PERISCOPE_DEV=1 uv run server.py   # run in the background
sleep 5; curl -s http://127.0.0.1:17375/api/state | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['resources']); print([(w['name'], w.get('res')) for w in d['windows']][:5])"
sleep 2; curl -s http://127.0.0.1:17375/api/state | python3 -c "import json,sys; print(json.load(sys.stdin)['resources']['panes'])"
```

Expected: `resources` has `server`, `webview` (the running `periscope-app`, or `null`), `panes`, `elsewhere`, `machine`; rows carry `res`; the second call has a numeric `cpu_pct`. Cross-check one pane's `mem_mb` against `top -l 1 -stats pid,mem` for its claude (the pane figure must be at least the claude's). Open http://127.0.0.1:17375/ in a browser: the pill shows next to the usage meters, the popover lists owners, clicking a pane line selects it, Escape closes it, and the detail header shows `… mem · … cpu`. Stop the dev server afterwards.

- [ ] **Step 4: Docs**

`CLAUDE.md` module table — add a row after `usage.py`'s row group (alphabetical placement is not required; put it next to `session_status.py`):

```
| `resources.py` | Memory footprint + CPU per owner (each pane, server, webview, elsewhere) through libproc on the tick's process snapshot; `measure()` feeds `/api/state` |
```

Spec — record the two places the plan differs:
- D7: `mem_signal` gets claude's footprint from `claude_proc_for` (a direct `resources.footprint_mb` read), not from the tick's measurement — the fan-out builds the row before `measure` runs.
- Server section: `resources.py` imports `agent_processes` (stdlib-only) for the snapshot and `descendants`; `res` / `resources` are `null`, not absent, when the snapshot failed.

```bash
git add CLAUDE.md docs/superpowers/specs/2026-10-30-resource-monitor-design.md
git commit -m "docs: resources.py in the module table; the spec records where the plan differs (THI-448)

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

- [ ] **Step 5: Hand off**

Do not push. Report to the user: the commits, the gate output, and what the dev-server check showed; ask before pushing the branch and opening the PR (title starts `THI-448:`).
