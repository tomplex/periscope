"""Tests for periscope.updater — the self-update nag + spawn."""

import subprocess
import time

import pytest

from periscope import config, updater


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    """updater keeps module-level cache/handle state; reset between tests."""
    monkeypatch.setattr(updater, "_checked_at", 0.0)
    monkeypatch.setattr(updater, "_behind", 0)
    monkeypatch.setattr(updater, "_proc", None)
    monkeypatch.setattr(updater, "_started_at", 0.0)


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


def test_check_silent_without_upstream(monkeypatch):
    # Detached HEAD / no tracking branch: nothing to compare against.
    monkeypatch.setattr(updater, "_git", _git_stub({"rev-parse": None}))
    assert updater.check() == 0
    assert updater.summary()["behind"] == 0


@pytest.mark.parametrize("broken", [
    {"rev-parse": "origin/main", "fetch": None},                    # offline
    {"rev-parse": None},                                            # no upstream
    {"rev-parse": "origin/main", "fetch": "", "rev-list": ""},      # bad count
])
def test_check_keeps_last_count_when_it_cannot_answer(monkeypatch, broken):
    """A failed probe must LEAVE THE COUNT STANDING, not reset it to 0.

    Publishing 0 would render as "up to date" — the one wrong answer. Going
    offline doesn't make the checkout less behind. Asserts through summary(),
    which is what the pill actually reads; check()'s return value alone would
    pass even if _behind were being clobbered.
    """
    monkeypatch.setattr(updater, "_git", _git_stub({
        "rev-parse": "origin/main", "fetch": "", "rev-list": "9",
    }))
    updater.check()
    assert updater.summary()["behind"] == 9

    monkeypatch.setattr(updater, "_git", _git_stub(broken))
    assert updater.check(force=True) == 9
    assert updater.summary()["behind"] == 9


# --- start() ---------------------------------------------------------------

def test_start_refuses_on_dev_instance(monkeypatch):
    # A dev instance runs from a worktree on a feature branch: `git pull
    # --ff-only` there would fail, or pull the WRONG branch over live work.
    monkeypatch.setattr(config, "DEV", True)
    with pytest.raises(RuntimeError, match="prod-only"):
        updater.start()


class LiveProc:
    pid = 999
    killed = False
    def poll(self): return None              # still running
    def kill(self): self.killed = True


def test_start_refuses_when_already_running(monkeypatch):
    monkeypatch.setattr(config, "PORT", 8765)
    monkeypatch.setattr(config, "DEV", False)
    monkeypatch.setattr(updater, "_proc", LiveProc())
    monkeypatch.setattr(updater, "_started_at", time.time())
    with pytest.raises(RuntimeError, match="already running"):
        updater.start()


def test_start_kills_a_wedged_updater(monkeypatch, tmp_path):
    """A `git pull` blocked on the network would otherwise pin running() true
    forever, 409ing every later attempt until the server restarts — on a box
    that is only ever restarted BY this feature."""
    monkeypatch.setattr(config, "PORT", 8765)
    monkeypatch.setattr(config, "DEV", False)
    monkeypatch.setattr(updater, "log_path", lambda: tmp_path / "update.log")
    wedged = LiveProc()
    monkeypatch.setattr(updater, "_proc", wedged)
    monkeypatch.setattr(updater, "_started_at", time.time() - updater.STALE_PROC_S - 1)
    assert updater.running() is False        # past the cap, so no longer "live"

    monkeypatch.setattr(subprocess, "Popen", lambda argv, **kw: LiveProc())
    updater.start()                          # must not raise
    assert wedged.killed is True


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
    monkeypatch.setattr(updater, "_started_at", time.time())
    assert updater.running() is False


def test_summary_does_not_deadlock_on_the_hot_path(monkeypatch):
    """_LOCK is a plain Lock and summary() rides every 3s /api/state poll, so
    a re-entrant acquire would hang the whole dashboard, not just this call."""
    monkeypatch.setattr(updater, "_proc", LiveProc())
    monkeypatch.setattr(updater, "_started_at", time.time())
    assert updater.summary()["running"] is True


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
