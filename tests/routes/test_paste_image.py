"""Tests for /api/paste-image."""

import subprocess

import pytest


def test_paste_image_writes_file_and_pastes(client, mocker, tmp_path):
    # Redirect the temp-image dir into a tmpdir so the test doesn't litter /tmp.
    for mod_path in ("periscope.routes.paste_image", "server"):
        try:
            mod = __import__(mod_path, fromlist=["_PASTE_IMG_DIR"])
            mocker.patch.object(mod, "_PASTE_IMG_DIR", tmp_path)
            break
        except (ImportError, AttributeError):
            continue

    paste = mocker.patch("periscope.routes.paste_image.bracketed_paste")
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
    paste.assert_called_once_with("main:0", f"@{body['path']} ")


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

    paste = mocker.patch("periscope.routes.paste_image.bracketed_paste")

    r = client.post(
        "/api/paste-image?session=main&index=0&deliver=false",
        content=b"\x89PNG\r\n\x1a\nfakebytes",
        headers={"content-type": "image/png"},
    )
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert r.json()["path"].endswith(".png")
    paste.assert_not_called()


def test_paste_image_rejects_empty_body(client, mocker):
    # Even though body is empty, tmux must not be invoked.
    paste = mocker.patch("periscope.routes.paste_image.bracketed_paste")

    r = client.post("/api/paste-image?session=main&index=0", content=b"")
    assert r.status_code == 400
    assert "empty" in r.json()["detail"]
    paste.assert_not_called()


@pytest.mark.parametrize(
    ("failure", "error"),
    [
        (subprocess.CompletedProcess([], 1, "", "can't find window: 0\n"), "can't find window: 0"),
        (subprocess.TimeoutExpired(["tmux"], 5), "timed out after 5 s"),
    ],
    ids=["exit", "timeout"],
)
@pytest.mark.parametrize("command", ["load-buffer", "paste-buffer"])
def test_paste_image_answers_500_when_the_paste_fails(client, mocker, tmp_path, command, failure, error):
    # A paste that never reaches the pane must not answer ok.
    mocker.patch("periscope.routes.paste_image._PASTE_IMG_DIR", tmp_path)
    focus = mocker.patch("periscope.routes.paste_image.note_focus")

    def run(argv, **kwargs):
        if command not in argv:
            return subprocess.CompletedProcess(argv, 0, "", "")
        if isinstance(failure, Exception):
            raise failure
        return failure

    mocker.patch("periscope.tmux.subprocess.run", side_effect=run)
    r = client.post(
        "/api/paste-image?session=main&index=0",
        content=b"\x89PNG\r\n\x1a\nfakebytes",
        headers={"content-type": "image/png"},
    )
    assert r.status_code == 500
    assert r.json()["detail"] == f"tmux {command} failed: {error}"
    focus.assert_not_called()
