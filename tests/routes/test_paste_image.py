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

    load = mocker.patch("periscope.routes.paste_image.load_buffer")
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
    # The @path loads into a buffer, and paste-buffer pastes that buffer.
    buf, text = load.call_args.args
    assert text == f"@{body['path']} "
    assert tmux_mock.call_args.args == ("paste-buffer", "-d", "-p", "-b", buf, "-t", "main:0")


def test_paste_image_deliver_false_skips_pane_paste(client, mocker, tmp_path):
    # The transcript composer uploads with deliver=false: the file is written and
    # its @path returned, but it is NOT pasted into the pane (the composer splices
    # it into the message instead).
    for mod_path in ("periscope.routes.paste_image", "server"):
        try:
            mod = __import__(mod_path, fromlist=["_PASTE_IMG_DIR"])
            mocker.patch.object(mod, "_PASTE_IMG_DIR", tmp_path)
            break
        except (ImportError, AttributeError):
            continue

    tmux_mock = mocker.MagicMock(return_value="")
    for path in ("periscope.routes.paste_image.tmux", "server.tmux"):
        try:
            mocker.patch(path, tmux_mock)
            break
        except (AttributeError, ModuleNotFoundError):
            continue

    load = mocker.patch("periscope.routes.paste_image.load_buffer")

    r = client.post(
        "/api/paste-image?session=main&index=0&deliver=false",
        content=b"\x89PNG\r\n\x1a\nfakebytes",
        headers={"content-type": "image/png"},
    )
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert r.json()["path"].endswith(".png")
    load.assert_not_called()  # no load-buffer / paste-buffer into the pane
    tmux_mock.assert_not_called()


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
    assert r.status_code == 400
    assert "empty" in r.json()["detail"]
    tmux_mock.assert_not_called()


def test_paste_image_answers_500_and_does_not_paste_when_the_load_fails(client, mocker, tmp_path):
    # A paste that never reaches the pane must not answer ok.
    mocker.patch("periscope.routes.paste_image._PASTE_IMG_DIR", tmp_path)
    tmux_mock = mocker.patch("periscope.routes.paste_image.tmux")
    mocker.patch(
        "periscope.routes.paste_image.load_buffer", side_effect=RuntimeError("tmux load-buffer failed: no server")
    )

    r = client.post(
        "/api/paste-image?session=main&index=0",
        content=b"\x89PNG\r\n\x1a\nfakebytes",
        headers={"content-type": "image/png"},
    )
    assert r.status_code == 500
    assert r.json()["detail"] == "tmux load-buffer failed: no server"
    tmux_mock.assert_not_called()
