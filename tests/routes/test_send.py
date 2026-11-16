"""Tests for /api/send and /api/send-bulk."""

import subprocess

import pytest

from periscope.routes import send


def _patch_tmux(mocker):
    mocker.patch("periscope.routes.send.bracketed_paste")
    return mocker.patch("periscope.routes.send._tmux_checked")


def test_send_paste_and_keys(client, mocker):
    tmux_mock = _patch_tmux(mocker)
    # Patch the time.sleep that fires between paste and Enter so the test
    # doesn't pay the 100ms delay.
    for path in ("periscope.routes.send.time.sleep", "server.time.sleep"):
        try:
            mocker.patch(path)
            break
        except (AttributeError, ModuleNotFoundError):
            continue

    r = client.post(
        "/api/send?session=main&index=0",
        json={"paste": "hello world", "keys": ["Enter"]},
    )
    assert r.status_code == 200
    assert r.json() == {"ok": True, "target": "main:0"}
    send.bracketed_paste.assert_called_once_with("main:0", "hello world")
    assert [c.args[0] for c in tmux_mock.call_args_list] == ["send-keys"]


_SEND_STEPS = ["load-buffer", "paste-buffer", "send-keys"]


@pytest.mark.parametrize(
    ("failure", "error"),
    [
        (subprocess.CompletedProcess([], 1, "", "can't find window: 0\n"), "can't find window: 0"),
        (subprocess.TimeoutExpired(["tmux"], 5), "timed out after 5 s"),
    ],
    ids=["exit", "timeout"],
)
@pytest.mark.parametrize("command", _SEND_STEPS)
def test_send_a_paste_or_key_tmux_fails_is_500_and_sends_nothing_after_it(client, mocker, command, failure, error):
    # A step that tmux refuses or times out on is 500, and nothing after it reaches the pane:
    # an Enter after a failed paste would submit whatever the composer holds.
    mocker.patch.object(send.time, "sleep")
    ran = []

    def run(argv, **kwargs):
        ran.append(next(step for step in _SEND_STEPS if step in argv))
        if command not in argv:
            return subprocess.CompletedProcess(argv, 0, "", "")
        if isinstance(failure, Exception):
            raise failure
        return failure

    mocker.patch("periscope.tmux.subprocess.run", side_effect=run)
    r = client.post("/api/send?session=main&index=0", json={"paste": "hi", "keys": ["Enter"]})
    assert r.status_code == 500
    assert r.json()["detail"] == f"tmux {command} failed: {error}"
    assert ran == _SEND_STEPS[: _SEND_STEPS.index(command) + 1]


def test_send_rejects_empty_request(client, mocker):
    _patch_tmux(mocker)
    r = client.post(
        "/api/send?session=main&index=0",
        json={"paste": "", "keys": []},
    )
    assert r.status_code == 400
    assert "no keys or paste" in r.json()["detail"]


def test_send_bulk_fans_out(client, mocker):
    tmux_mock = _patch_tmux(mocker)
    for path in ("periscope.routes.send.time.sleep", "server.time.sleep"):
        try:
            mocker.patch(path)
            break
        except (AttributeError, ModuleNotFoundError):
            continue

    r = client.post(
        "/api/send-bulk",
        json={"targets": ["main:0", "main:1"], "paste": None, "keys": ["Enter"]},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["sent"] == 2
    assert body["total"] == 2
    # send-keys was invoked twice.
    sk_calls = [c for c in tmux_mock.call_args_list if c.args and c.args[0] == "send-keys"]
    assert len(sk_calls) == 2


def test_send_bulk_rejects_empty(client, mocker):
    r = client.post("/api/send-bulk", json={"targets": [], "keys": ["Enter"]})
    assert r.status_code == 400
    assert "no targets" in r.json()["detail"]


def test_send_to_target_sends_to_the_address_and_notes_the_target(mocker):
    # pane_actions checks a pane by %id and must send to that pane, not to
    # whatever `session:index` resolves to by then; focus stays keyed by target.
    tmux_mock = mocker.patch.object(send, "_tmux_checked")
    paste = mocker.patch.object(send, "bracketed_paste")
    mocker.patch.object(send.time, "sleep")
    focus = mocker.patch.object(send, "note_focus")
    action = mocker.patch.object(send, "note_action")
    send._send_to_target("s:4", "hi", ["Enter"], address="%9")
    paste.assert_called_once_with("%9", "hi")
    assert [c.args for c in tmux_mock.call_args_list] == [("send-keys", "-t", "%9", "Enter")]
    focus.assert_called_once_with("s:4")
    action.assert_called_once_with("s:4")
    tmux_mock.reset_mock()
    send._send_to_target("s:4", None, ["Escape"])
    assert tmux_mock.call_args_list[0].args == ("send-keys", "-t", "s:4", "Escape")
