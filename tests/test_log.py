"""Logging setup + _bg/_task crash capture.

The logger and the background-task wrappers must surface uncaught
exceptions into the log — silent crashes are the failure mode this
module exists to prevent.
"""

import asyncio
import logging
import threading

from periscope.log import log, _bg, _task


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
    # log.exception("background thread %s crashed", name) — name is in args[1].
    assert "crashy" in spy.call_args.args


def test_task_logs_uncaught_exception(mocker):
    spy = mocker.spy(log, "error")

    async def crashes():
        raise RuntimeError("async kaboom")

    async def run():
        t = _task(crashes(), "async-crashy")
        await asyncio.sleep(0.05)
        return t

    asyncio.run(run())
    spy.assert_called_once()
    # log.error("task %s crashed", name, exc_info=exc) — name in args[1].
    assert "async-crashy" in spy.call_args.args


def test_log_path_includes_port(tmp_xdg_home, monkeypatch):
    import periscope.config
    import periscope.log
    monkeypatch.setattr(periscope.config, "PORT", 8766)
    # _log_path is the helper; the cached _LOG_PATH is set at module load
    # before our monkeypatch and isn't checked here.
    assert periscope.log._log_path().name == "periscope-8766.log"
