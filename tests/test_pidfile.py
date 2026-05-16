"""Pidfile reclaim: single-instance enforcement.

Tests sandbox $XDG_CONFIG_HOME so they don't touch real periscope state.
Subprocess invocations (ps, kill) are monkeypatched.
"""

import os
import subprocess
from pathlib import Path

from periscope.pidfile import (
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

    # Pretend 99999 IS a periscope (True on the initial check, then False
    # after the SIGTERM so the loop exits without escalation).
    is_per = mocker.patch("periscope.pidfile._pid_is_periscope")
    is_per.side_effect = [True, False]

    killed = mocker.patch("os.kill")
    _reclaim_existing_instance()
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
    mocker.patch("periscope.pidfile._pid_is_periscope", return_value=True)
    mocker.patch("time.sleep")
    # First time.time() => 0.0 (sets deadline = 3.0); subsequent calls
    # race past 3.0 so the while loop exits without finding a dead pid.
    times = iter([0.0, 0.5, 4.0, 4.0, 4.0])
    mocker.patch("time.time", side_effect=lambda: next(times))

    killed = mocker.patch("os.kill")
    _reclaim_existing_instance()
    import signal as _signal
    killed.assert_any_call(99999, _signal.SIGTERM)
    killed.assert_any_call(99999, _signal.SIGKILL)
