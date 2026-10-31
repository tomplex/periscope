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
