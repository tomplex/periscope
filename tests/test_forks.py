"""forks — the audit hook that counts every fork the server makes, and the
per-refresh cost /api/state reports."""

import logging
import subprocess
import sys
from collections import deque
from types import SimpleNamespace

import pytest

from periscope import forks


@pytest.fixture(autouse=True)
def _fresh_counters(monkeypatch):
    monkeypatch.setattr(forks, "_forks", deque())
    monkeypatch.setattr(forks, "_builds", deque())
    monkeypatch.setattr(forks, "_last_gap", None)
    monkeypatch.setattr(forks, "_slow_logged_at", None)


def _popen(*argv):
    forks._on_audit("subprocess.Popen", (argv[0], list(argv), None, None))


@pytest.mark.parametrize(
    ("argv", "key"),
    [
        (["git", "-C", "/repo", "status", "--porcelain=v2"], "git status"),
        (["git", "--no-optional-locks", "-C", "repo", "diff", "HEAD"], "git diff"),
        (["/opt/homebrew/bin/tmux", "-L", "periscope-x", "list-panes", "-a"], "tmux list-panes"),
        (["ps", "-axo", "pid=,ppid=,lstart=,state=,args="], "ps"),
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
    forks._on_audit("subprocess.Popen", ("x", [], None, None))  # no argv[0]: uncounted
    assert forks.fork_counts() == {}


def test_hook_never_raises_into_the_audited_popen(monkeypatch):
    # An exception escaping an audit hook aborts the Popen it audits.
    class Broken:
        def append(self, item):
            raise RuntimeError("boom")

    monkeypatch.setattr(forks, "_forks", Broken())
    _popen("git", "-C", "/r", "status")


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


def test_slow_builds_are_logged_at_most_once_a_minute(caplog, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(forks, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    with caplog.at_level(logging.WARNING, logger="periscope"):
        forks.record_build(3.4)
        clock[0] += 30
        forks.record_build(3.5)
        clock[0] += 31
        forks.record_build(3.6)
    assert [r.args[0] for r in caplog.records] == [3.4, 3.6]


def test_a_normal_build_logs_nothing(caplog):
    with caplog.at_level(logging.WARNING, logger="periscope"):
        forks.record_build(2.9)
    assert caplog.text == ""
