"""tmux + subprocess wrappers.

Tests don't require a live tmux. We monkeypatch subprocess.run to assert
the wrappers compose argv correctly and return what they claim to return.
"""

import subprocess

from periscope.tmux import (
    tmux, capture, deliver_input, _run, _tmux_mutate,
    _ANSI_SGR_RE, _FG_COLOR_RE,
)


def test_ansi_sgr_re_strips_color_codes():
    s = "\x1b[31mred\x1b[0m plain"
    assert _ANSI_SGR_RE.sub("", s) == "red plain"


def test_fg_color_re_matches_extended_palette():
    s = "\x1b[38;5;196mbright red\x1b[0m"
    assert _FG_COLOR_RE.search(s) is not None


def test_tmux_invokes_subprocess_with_tmux_prefix(mocker):
    mock_run = mocker.patch("subprocess.run")
    mock_run.return_value = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="hello\n", stderr="",
    )
    out = tmux("display-message", "-p", "hello")
    assert out == "hello\n"
    args, kwargs = mock_run.call_args
    assert args[0] == ["tmux", "display-message", "-p", "hello"]
    assert kwargs["capture_output"] is True
    assert kwargs["text"] is True


def test_tmux_mutate_returns_ok_on_zero_exit(mocker):
    mocker.patch(
        "subprocess.run",
        return_value=subprocess.CompletedProcess(
            args=[], returncode=0, stdout="renamed\n", stderr="",
        ),
    )
    ok, msg = _tmux_mutate("rename-window", "-t", "foo:0", "bar")
    assert ok is True
    assert msg == "renamed"


def test_tmux_mutate_surfaces_stderr_on_failure(mocker):
    mocker.patch(
        "subprocess.run",
        return_value=subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="no such window\n",
        ),
    )
    ok, msg = _tmux_mutate("rename-window", "-t", "missing:0", "bar")
    assert ok is False
    assert msg == "no such window"


def test_tmux_mutate_falls_back_to_generic_error(mocker):
    mocker.patch(
        "subprocess.run",
        return_value=subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="",
        ),
    )
    ok, msg = _tmux_mutate("bad-cmd")
    assert ok is False
    assert msg == "tmux failed"


def test_run_returns_returncode_and_stdout(mocker):
    mocker.patch(
        "subprocess.run",
        return_value=subprocess.CompletedProcess(
            args=[], returncode=0, stdout="abcdef\n", stderr="",
        ),
    )
    code, out = _run(["git", "rev-parse", "HEAD"])
    assert code == 0
    assert out == "abcdef"


def test_run_returns_minus_one_on_exception(mocker):
    mocker.patch("subprocess.run", side_effect=OSError("no such command"))
    code, out = _run(["nonexistent"])
    assert code == -1
    assert out == ""


def test_capture_calls_tmux_capture_pane_with_lines(mocker):
    mock_tmux = mocker.patch("periscope.tmux.tmux", return_value="pane body\n")
    out = capture("foo:0", lines=50)
    mock_tmux.assert_called_once_with(
        "capture-pane", "-t", "foo:0", "-p", "-e", "-S", "-50",
    )
    assert out == "pane body\n"


def test_deliver_input_uses_load_buffer_and_paste(mocker):
    mock_run = mocker.patch("subprocess.run")
    mock_run.return_value = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="", stderr="",
    )
    deliver_input("foo:0", "echo hi;\n")
    assert mock_run.call_count == 2
    first_call = mock_run.call_args_list[0]
    assert first_call.args[0][:2] == ["tmux", "load-buffer"]
    assert first_call.kwargs.get("input") == "echo hi;\n"
    second_call = mock_run.call_args_list[1]
    assert second_call.args[0][:2] == ["tmux", "paste-buffer"]
    assert "foo:0" in second_call.args[0]
