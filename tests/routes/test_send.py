"""Tests for /api/send and /api/send-bulk."""

from periscope.routes import send


def _patch_tmux(mocker):
    mocker.patch("periscope.routes.send.load_buffer")
    for path in ("periscope.routes.send.tmux", "server.tmux"):
        try:
            return mocker.patch(path, return_value="")
        except (AttributeError, ModuleNotFoundError):
            continue
    return None


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
    assert send.load_buffer.call_args.args[1] == "hello world"
    cmds = [c.args[0] for c in tmux_mock.call_args_list]
    assert "paste-buffer" in cmds
    assert "send-keys" in cmds


def test_send_a_paste_tmux_cannot_load_is_500_and_sends_nothing(client, mocker):
    # Pasting and pressing Enter anyway would submit whatever the composer holds.
    tmux_mock = _patch_tmux(mocker)
    mocker.patch("periscope.routes.send.load_buffer", side_effect=RuntimeError("tmux load-buffer failed: no server"))
    r = client.post("/api/send?session=main&index=0", json={"paste": "- a list", "keys": ["Enter"]})
    assert r.status_code == 500 and "load-buffer failed" in r.json()["detail"]
    tmux_mock.assert_not_called()


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
    tmux_mock = mocker.patch.object(send, "tmux", return_value="")
    load = mocker.patch.object(send, "load_buffer")
    mocker.patch.object(send.time, "sleep")
    focus = mocker.patch.object(send, "note_focus")
    action = mocker.patch.object(send, "note_action")
    send._send_to_target("s:4", "hi", ["Enter"], address="%9")
    calls = [c.args for c in tmux_mock.call_args_list]
    assert calls[0] == ("paste-buffer", "-d", "-p", "-b", load.call_args.args[0], "-t", "%9")
    assert calls[1] == ("send-keys", "-t", "%9", "Enter")
    focus.assert_called_once_with("s:4")
    action.assert_called_once_with("s:4")
    tmux_mock.reset_mock()
    send._send_to_target("s:4", None, ["Escape"])
    assert tmux_mock.call_args_list[0].args == ("send-keys", "-t", "s:4", "Escape")
