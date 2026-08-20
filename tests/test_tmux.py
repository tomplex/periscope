"""tmux + subprocess wrappers.

Tests don't require a live tmux. We monkeypatch subprocess.run to assert
the wrappers compose argv correctly and return what they claim to return.
"""

import subprocess

from periscope import config
from periscope.tmux import (
    _ANSI_SGR_RE,
    _FG_COLOR_RE,
    _run,
    _tmux_mutate,
    capture,
    deliver_input,
    env_args,
    tmux,
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


def test_deliver_input_small_uses_send_keys_hex(mocker):
    """Common path — keystrokes and small pastes — go through a single
    `send-keys -H` subprocess. Each byte becomes a hex arg, which sidesteps
    tmux's argv parser eating standalone `;` as a command separator."""
    mock_run = mocker.patch("subprocess.run")
    mock_run.return_value = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="", stderr="",
    )
    deliver_input("foo:0", "a;b\n")
    assert mock_run.call_count == 1
    argv = mock_run.call_args_list[0].args[0]
    assert argv[:5] == ["tmux", "send-keys", "-t", "foo:0", "-H"]
    # 'a' 0x61, ';' 0x3b, 'b' 0x62, '\n' 0x0a — order preserved.
    assert argv[5:] == ["61", "3b", "62", "0a"]


def test_deliver_input_large_falls_back_to_paste_buffer(mocker):
    """Inputs above the hex-arg threshold use load-buffer + paste-buffer
    over stdin so argv length stays bounded regardless of paste size."""
    mock_run = mocker.patch("subprocess.run")
    mock_run.return_value = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="", stderr="",
    )
    big = "x" * 5000
    deliver_input("foo:0", big)
    assert mock_run.call_count == 2
    first_call = mock_run.call_args_list[0]
    assert first_call.args[0][:2] == ["tmux", "load-buffer"]
    assert first_call.kwargs.get("input") == big
    second_call = mock_run.call_args_list[1]
    assert second_call.args[0][:2] == ["tmux", "paste-buffer"]
    assert "foo:0" in second_call.args[0]


def test_env_args_empty_for_default_account():
    assert env_args("") == []
    assert env_args(None) == []
    assert env_args("", "") == []
    assert env_args(None, None) == []


def test_env_args_builds_e_flag():
    assert env_args("/Users/tom/.claude-b") == [
        "-e", "CLAUDE_CONFIG_DIR=/Users/tom/.claude-b"
    ]


def test_env_args_carries_the_wrapper_profile():
    assert env_args("", "lab") == ["-e", "CLAUDE_WRAPPER_PROFILE=lab"]


def test_env_args_carries_the_model_override():
    assert env_args("", None, "opus") == ["-e", "ANTHROPIC_MODEL=opus"]
    assert env_args("/c", "lab", "opus") == [
        "-e", "CLAUDE_CONFIG_DIR=/c", "-e", "CLAUDE_WRAPPER_PROFILE=lab",
        "-e", "ANTHROPIC_MODEL=opus",
    ]


def test_env_args_account_and_profile_are_independent():
    """The two axes compose: a lab pane on the second subscription carries both."""
    assert env_args("/Users/tom/.claude-b", "lab") == [
        "-e", "CLAUDE_CONFIG_DIR=/Users/tom/.claude-b",
        "-e", "CLAUDE_WRAPPER_PROFILE=lab",
    ]


def test_profile_env_fails_open_to_the_default():
    """An unknown id is a periscope bug; the default profile is the one that
    behaves like a hand-typed `claude`, so it is the safe landing."""
    assert config.profile_env("lab") == "lab"
    assert config.profile_env("default") == ""
    assert config.profile_env(None) == ""
    assert config.profile_env("") == ""
    assert config.profile_env("nonsense") == ""


def test_window_identity_parses_three_fields(mocker):
    """Synthetic single-window rows must carry the real stamp — a hard-coded
    pid_raw="" made every detail-pane poll rebind and flap last_seen.pane_id
    (2026-08-06, post session-keyed-identity deploy)."""
    from periscope import tmux as tmux_mod
    mocker.patch.object(tmux_mod, "tmux", return_value="abc12345\t%7\t@9\n")
    assert tmux_mod.window_identity("s:1") == ("abc12345", "%7", "@9")


def test_window_identity_empty_on_unstamped(mocker):
    from periscope import tmux as tmux_mod
    mocker.patch.object(tmux_mod, "tmux", return_value="\t%7\t@9\n")
    assert tmux_mod.window_identity("s:1") == ("", "%7", "@9")
