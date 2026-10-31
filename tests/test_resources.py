"""resources: footprint and CPU per owner. Everything except the smoke tests
runs on fake snapshots and fake rusage readings."""

import os

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
