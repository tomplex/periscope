"""resources: footprint and CPU per group. Everything except the smoke tests
runs on fake snapshots and fake rusage readings."""

import os
import threading

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
    assert set(m) == {"load1", "ram_mb", "swap_used_mb", "swap_total_mb", "mem_free_pct"}
    assert m["ram_mb"] > 1024
    assert 0 <= m["swap_used_mb"] <= m["swap_total_mb"]
    assert 0 <= m["mem_free_pct"] <= 100


def test_responsible_pid_of_this_process_is_a_pid():
    assert resources._responsible(os.getpid()) > 0


def test_name_of_this_process_is_a_nonempty_str():
    name = resources._name(os.getpid())
    assert isinstance(name, str)
    assert name


# ── group assignment ─────────────────────────────────────────────────────

from periscope.agent_processes import ProcessInfo

SERVER = os.getpid()


def _proc(pid, ppid, comm="/bin/zsh", argv=None):
    return ProcessInfo(pid=pid, ppid=ppid, started_at=0, comm=comm, argv=argv if argv is not None else comm, state="S")


def _snapshot():
    return {
        p.pid: p
        for p in (
            _proc(1, 0, "/sbin/launchd"),
            _proc(SERVER, 1, "/usr/bin/python3"),
            _proc(900, SERVER, "/opt/homebrew/bin/tmux"),  # control-mode client
            # ps truncates comm= to 16 chars when it isn't the last column.
            _proc(
                500,
                1,
                comm="/Applications/Pe",
                argv="/Applications/Periscope.app/Contents/MacOS/periscope-app",
            ),
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
    monkeypatch.setattr(resources, "_name", lambda pid: {500: "periscope-app"}.get(pid, ""))
    named, per_pane = resources._assign(_snapshot(), {"%1": 100, "%2": 200})
    assert named["server"] == {SERVER, 900}
    assert named["webview"] == {500, 600}  # the WebContent by responsible pid, not by parent
    assert per_pane == {"%1": {100, 101, 102}, "%2": {200}}
    assert named["panes"] == {100, 101, 102, 200}
    assert named["elsewhere"] == {1, 700}


def test_assign_without_the_app_has_no_webview(monkeypatch):
    monkeypatch.setattr(resources, "_responsible", lambda pid: pid)
    monkeypatch.setattr(resources, "_name", lambda pid: "")
    snap = {pid: p for pid, p in _snapshot().items() if pid != 500}
    named, _ = resources._assign(snap, {"%1": 100})
    assert "webview" not in named
    assert 600 in named["elsewhere"]


# ── CPU accounting (spec D4, D5) ─────────────────────────────────────────

S = 1_000_000_000  # one second in ns


def _sample(at, *procs):
    """A tick's sample of (pid, start, cpu_ns, ppid) processes."""
    return resources._Sample(at, {(pid, start): ns for pid, start, ns, _ in procs}, {p[0]: p[3] for p in procs})


def _cpu(prev, cur, pids):
    return resources._cpu_pcts(prev, cur, {"g": pids})["g"]


def test_cpu_is_unknown_without_a_previous_sample():
    assert _cpu(None, _sample(S, (1, 0, S, 0)), {1}) is None


def test_cpu_steady_process():
    assert _cpu(_sample(0, (1, 0, S, 0)), _sample(S, (1, 0, S + S // 2, 0)), {1}) == 50.0


def test_cpu_counts_a_process_started_since_the_previous_sample():
    assert _cpu(_sample(S), _sample(2 * S, (2, S + S // 2, S // 5, 1)), {2}) == 20.0


def test_cpu_ignores_an_old_process_new_to_the_sample():
    # Started before the previous sample: its total isn't this interval's.
    assert _cpu(_sample(S), _sample(2 * S, (2, S // 2, 7 * S, 1)), {2}) == 0.0


def test_cpu_counts_a_reaped_child_once():
    # Child 2 had 0.3 s; it ran 0.1 s more and exited, and parent 1 reaped it:
    # the parent's total gains the child's final 0.4 s plus 0.1 s of its own.
    prev = _sample(0, (1, 0, S, 0), (2, 0, 3 * S // 10, 1))
    cur = _sample(S, (1, 0, S + 4 * S // 10 + S // 10, 0))
    assert _cpu(prev, cur, {1, 2}) == 20.0


def test_cpu_charges_a_closed_panes_shell_to_its_reaper():
    # Pane %1 closes: tmux server 50 (elsewhere) reaps its shell 100 (2 s) and
    # gains it, plus 0.1 s of its own. The shell's claude 101 (30 s) was
    # orphaned to launchd, which isn't readable: it comes off nothing.
    prev = _sample(0, (50, 0, S, 1), (100, 0, 2 * S, 50), (101, 0, 30 * S, 100))
    cur = _sample(S, (50, 0, 3 * S + S // 10, 1))
    assert _cpu(prev, cur, {1, 50}) == 10.0


def test_cpu_clamps_a_child_its_parent_never_reaped():
    # Child 2 was orphaned before it exited: parent 1 never gains its 5 s.
    prev = _sample(0, (1, 0, S, 0), (2, 0, 5 * S, 1))
    assert _cpu(prev, _sample(S, (1, 0, S + S // 10, 0)), {1}) == 0.0


def test_cpu_is_unknown_past_every_core_for_the_whole_interval():
    # Two seconds of CPU in one second on one core came from outside it.
    assert _cpu(_sample(0, (1, 0, 0, 0)), _sample(S, (1, 0, 2 * S, 0)), {1}) is None


def test_cpu_never_diffs_a_reused_pid_against_its_predecessor():
    # pid 1 exited (5 s, reaped by 9) and a new pid 1 started since.
    prev = _sample(S, (1, 0, 5 * S, 9), (9, 0, S, 0))
    cur = _sample(2 * S, (9, 0, 6 * S, 0), (1, S + S // 2, S // 10, 9))
    assert _cpu(prev, cur, {1, 9}) == 10.0


def test_cpu_is_a_share_of_every_core(monkeypatch):
    monkeypatch.setattr(resources, "_CORES", 10)
    # 2.5 cores busy for a second on a 10-core machine: a quarter of it.
    assert _cpu(_sample(0, (1, 0, 0, 0)), _sample(S, (1, 0, 5 * S // 2, 0)), {1}) == 25.0


def test_cpu_treats_a_stale_previous_sample_as_missing():
    assert _cpu(_sample(0, (1, 0, S, 0)), _sample(11 * S, (1, 0, 2 * S, 0)), {1}) is None


# ── measure ──────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _no_live_resources(monkeypatch):
    """Overrides conftest's stub of the same name: these tests install their
    own fakes, and the smoke tests read the real test process."""
    monkeypatch.setattr(resources, "_prev", None)
    monkeypatch.setattr(resources, "_last", None)
    # One core, so the CPU cases read in whole-core terms.
    monkeypatch.setattr(resources, "_CORES", 1)


MACHINE = {"load1": 1.0, "ram_mb": 16384, "swap_used_mb": 0, "swap_total_mb": 0, "mem_free_pct": 50}
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
    cpu = dict.fromkeys(_snapshot(), 0)
    monkeypatch.setattr(resources, "_responsible", {600: 500}.get)
    monkeypatch.setattr(resources, "_name", lambda pid: {500: "periscope-app"}.get(pid, ""))
    monkeypatch.setattr(resources, "machine", lambda: MACHINE)
    monkeypatch.setattr(resources, "_now", clock)
    monkeypatch.setattr(
        resources, "_read", lambda pid: resources.Reading(100 * MB, cpu[pid], 0) if pid in cpu else None
    )
    return clock, cpu


PANES = {"%1": 100, "%2": 200}


def test_measure_first_pass_has_memory_and_unknown_cpu(fake):
    m = resources.measure(_snapshot(), PANES)
    assert m.panes == {"%1": {"mem_mb": 300, "cpu_pct": None}, "%2": {"mem_mb": 100, "cpu_pct": None}}
    assert m.totals["server"] == {"mem_mb": 200, "cpu_pct": None}
    assert m.totals["webview"] == {"mem_mb": 200, "cpu_pct": None}
    assert m.totals["panes"] == {"mem_mb": 400, "cpu_pct": None}
    assert m.totals["elsewhere"] == {"mem_mb": 200, "cpu_pct": None}
    assert m.totals["machine"] == MACHINE


def test_measure_second_pass_has_cpu(fake):
    clock, cpu = fake
    resources.measure(_snapshot(), PANES)
    clock.t += S
    cpu[102] += S // 2  # the pane's tool child: half a core
    m = resources.measure(_snapshot(), PANES)
    assert m.panes["%1"]["cpu_pct"] == 50.0
    assert m.panes["%2"]["cpu_pct"] == 0.0
    assert m.totals["panes"]["cpu_pct"] == 50.0
    assert m.totals["server"]["cpu_pct"] == 0.0


def test_measure_within_half_a_second_returns_the_previous_measurement(fake):
    clock, _ = fake
    first = resources.measure(_snapshot(), PANES)
    clock.t += S // 10
    assert resources.measure(_snapshot(), PANES) is first


def test_measure_within_half_a_second_reads_nothing(fake, monkeypatch):
    clock, _ = fake
    resources.measure(_snapshot(), PANES)
    monkeypatch.setattr(resources, "_read", lambda pid: pytest.fail("read inside the throttle"))
    clock.t += S // 10
    resources.measure(_snapshot(), PANES)


def test_measure_stamps_the_sample_when_it_reads(fake, monkeypatch):
    # Assigning groups took a second: the reads, and so the sample, come after.
    clock, cpu = fake
    resources.measure(_snapshot(), PANES)
    clock.t += S

    def slow_name(pid):
        if pid == 1:
            clock.t += S
        return {500: "periscope-app"}.get(pid, "")

    monkeypatch.setattr(resources, "_name", slow_name)
    cpu[102] += S  # one core for the two seconds since the first sample
    assert resources.measure(_snapshot(), PANES).panes["%1"]["cpu_pct"] == 50.0


def test_measure_skips_unreadable_processes(fake, monkeypatch):
    monkeypatch.setattr(resources, "_read", lambda pid: None if pid == 102 else resources.Reading(100 * MB, 0, 0))
    assert resources.measure(_snapshot(), PANES).panes["%1"]["mem_mb"] == 200


def test_measure_without_the_app_sends_no_webview(fake):
    snapshot = {p: v for p, v in _snapshot().items() if p != 500}
    assert resources.measure(snapshot, PANES).totals["webview"] is None


def test_measure_is_none_when_the_snapshot_failed(fake):
    assert resources.measure(None, PANES) is None


def test_measure_from_many_threads_at_once(fake):
    out = []
    threads = [threading.Thread(target=lambda: out.append(resources.measure(_snapshot(), PANES))) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(out) == 8
    assert all(m is out[0] for m in out)  # one computed, the rest inside 0.5 s


def test_measure_leaves_no_state_when_a_pass_fails(fake, monkeypatch):
    resources.measure(_snapshot(), PANES)
    clock, _ = fake
    clock.t += S
    prev_before, last_before = resources._prev, resources._last

    def boom():
        raise RuntimeError("boom")

    monkeypatch.setattr(resources, "machine", boom)
    with pytest.raises(RuntimeError):
        resources.measure(_snapshot(), PANES)
    assert resources._prev == prev_before
    assert resources._last == last_before
