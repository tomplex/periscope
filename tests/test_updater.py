"""Tests for periscope.updater — the self-update nag + spawn."""

import subprocess

import pytest

from periscope import config, updater


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    """updater keeps module-level cache/handle state; reset between tests."""
    monkeypatch.setattr(updater, "_checked_at", 0.0)
    monkeypatch.setattr(updater, "_behind", 0)
    monkeypatch.setattr(updater, "_proc", None)


# --- check() ---------------------------------------------------------------

def _git_stub(responses):
    """Map first-arg -> stdout. Returns None for anything unlisted."""
    def fake(*args, **kwargs):
        return responses.get(args[0])
    return fake


def test_check_counts_commits_behind(monkeypatch):
    monkeypatch.setattr(updater, "_git", _git_stub({
        "rev-parse": "origin/main", "fetch": "", "rev-list": "12",
    }))
    assert updater.check() == 12
    assert updater.summary()["behind"] == 12


def test_check_is_throttled(monkeypatch):
    calls = []

    def fake(*args, **kwargs):
        calls.append(args[0])
        return {"rev-parse": "origin/main", "fetch": "", "rev-list": "3"}.get(args[0])

    monkeypatch.setattr(updater, "_git", fake)
    assert updater.check() == 3
    calls.clear()
    # Second call inside CHECK_INTERVAL_S must not fork git at all — the fetch
    # is the expensive part and a stale checkout isn't urgent within the hour.
    assert updater.check() == 3
    assert calls == []


def test_check_force_bypasses_throttle(monkeypatch):
    monkeypatch.setattr(updater, "_git", _git_stub({
        "rev-parse": "origin/main", "fetch": "", "rev-list": "1",
    }))
    updater.check()
    calls = []

    def fake(*args, **kwargs):
        calls.append(args[0])
        return {"rev-parse": "origin/main", "fetch": "", "rev-list": "1"}.get(args[0])

    monkeypatch.setattr(updater, "_git", fake)
    updater.check(force=True)
    assert "fetch" in calls


def test_check_quiet_without_upstream(monkeypatch):
    # Detached HEAD / no tracking branch: nothing to compare against, and the
    # pill must stay silent rather than guess.
    monkeypatch.setattr(updater, "_git", _git_stub({"rev-parse": None}))
    assert updater.check() == 0


def test_check_quiet_when_fetch_fails(monkeypatch):
    # Offline or no credentials — degrade silently, don't nag with a stale count.
    monkeypatch.setattr(updater, "_git", _git_stub({
        "rev-parse": "origin/main", "fetch": None,
    }))
    assert updater.check() == 0


def test_check_survives_nonnumeric_revlist(monkeypatch):
    monkeypatch.setattr(updater, "_git", _git_stub({
        "rev-parse": "origin/main", "fetch": "", "rev-list": "",
    }))
    assert updater.check() == 0


# --- start() ---------------------------------------------------------------

def test_start_refuses_on_dev_instance(monkeypatch):
    # A dev instance runs from a worktree on a feature branch: `git pull
    # --ff-only` there would fail, or pull the WRONG branch over live work.
    monkeypatch.setattr(config, "DEV", True)
    with pytest.raises(RuntimeError, match="prod-only"):
        updater.start()


def test_start_refuses_when_already_running(monkeypatch):
    monkeypatch.setattr(config, "PORT", 8765)
    monkeypatch.setattr(config, "DEV", False)

    class LiveProc:
        pid = 999
        def poll(self): return None          # still running

    monkeypatch.setattr(updater, "_proc", LiveProc())
    with pytest.raises(RuntimeError, match="already running"):
        updater.start()


def test_start_spawns_detached(monkeypatch, tmp_path):
    """The load-bearing detail: the updater calls `launchctl bootout`, which
    tears down the launchd job. Without start_new_session the child is in that
    job's process group and gets killed by the teardown it just requested."""
    monkeypatch.setattr(config, "PORT", 8765)
    monkeypatch.setattr(config, "DEV", False)
    monkeypatch.setattr(updater, "log_path", lambda: tmp_path / "update.log")
    seen = {}

    class FakeProc:
        pid = 4242
        def poll(self): return None

    def fake_popen(argv, **kwargs):
        seen["argv"] = argv
        seen["kwargs"] = kwargs
        return FakeProc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    updater.start()

    assert seen["kwargs"]["start_new_session"] is True
    assert seen["argv"][1] == "update"
    assert seen["argv"][0].endswith("bin/periscope")
    # stdin detached too: the child outlives its parent's terminal.
    assert seen["kwargs"]["stdin"] is subprocess.DEVNULL


def test_running_false_after_exit(monkeypatch):
    class DeadProc:
        def poll(self): return 1

    monkeypatch.setattr(updater, "_proc", DeadProc())
    assert updater.running() is False


# --- status()/summary() ----------------------------------------------------

def test_summary_touches_no_disk(monkeypatch):
    """summary() rides every 3s /api/state poll; the log tail belongs to the
    on-demand probe only."""
    monkeypatch.setattr(updater, "tail", lambda *a, **k: pytest.fail("read disk"))
    assert "log" not in updater.summary()


def test_status_includes_log(monkeypatch, tmp_path):
    p = tmp_path / "update.log"
    p.write_text("working tree is dirty — commit or stash before updating\n")
    monkeypatch.setattr(updater, "log_path", lambda: p)
    assert "dirty" in updater.status()["log"][0]


def test_tail_missing_log_is_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(updater, "log_path", lambda: tmp_path / "nope.log")
    assert updater.tail() == []
