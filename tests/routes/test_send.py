"""Tests for /api/send and /api/send-bulk."""


def _patch_tmux(mocker):
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
    cmds = [c.args[0] for c in tmux_mock.call_args_list]
    assert "set-buffer" in cmds
    assert "paste-buffer" in cmds
    assert "send-keys" in cmds


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
