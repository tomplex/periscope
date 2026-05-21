"""Tests for /api/paste-image."""


def test_paste_image_writes_file_and_pastes(client, mocker, tmp_path):
    # Redirect the temp-image dir into a tmpdir so the test doesn't litter /tmp.
    for mod_path in ("periscope.routes.paste_image", "server"):
        try:
            mod = __import__(mod_path, fromlist=["_PASTE_IMG_DIR"])
            mocker.patch.object(mod, "_PASTE_IMG_DIR", tmp_path)
            break
        except (ImportError, AttributeError):
            continue

    # Capture tmux invocations so we can assert paste-buffer was called.
    tmux_mock = mocker.MagicMock(return_value="")
    for path in ("periscope.routes.paste_image.tmux", "server.tmux"):
        try:
            mocker.patch(path, tmux_mock)
            break
        except (AttributeError, ModuleNotFoundError):
            continue

    mocker.patch("periscope.panes.note_focus")
    mocker.patch("periscope.panes.note_action")

    r = client.post(
        "/api/paste-image?session=main&index=0",
        content=b"\x89PNG\r\n\x1a\nfakebytes",
        headers={"content-type": "image/png"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["bytes"] == len(b"\x89PNG\r\n\x1a\nfakebytes")
    assert body["path"].endswith(".png")
    # tmux set-buffer + paste-buffer must have been invoked.
    cmds = [call.args[0] for call in tmux_mock.call_args_list]
    assert "set-buffer" in cmds
    assert "paste-buffer" in cmds


def test_paste_image_rejects_empty_body(client, mocker):
    # Even though body is empty, tmux must not be invoked.
    tmux_mock = mocker.MagicMock(return_value="")
    for path in ("periscope.routes.paste_image.tmux", "server.tmux"):
        try:
            mocker.patch(path, tmux_mock)
            break
        except (AttributeError, ModuleNotFoundError):
            continue

    r = client.post("/api/paste-image?session=main&index=0", content=b"")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert "empty" in body["error"]
    tmux_mock.assert_not_called()
