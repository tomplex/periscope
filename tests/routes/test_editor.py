"""Tests for POST /api/editor/open."""


def _window(pid="@7", cwd="/repo/wt"):
    return {"pid_raw": pid, "cwd": cwd, "session": "s", "index": 0, "pane_id": "%1"}


def test_open_400_when_no_editor_configured(client, clean_state, mocker):
    """Checked before anything else — with no editor set there is nothing to
    launch regardless of which pane was clicked."""
    mocker.patch("periscope.routes.editor.get_settings", return_value={})
    r = client.post("/api/editor/open", json={"pid": "@7"})
    assert r.status_code == 400
    assert "no preferred editor" in r.json()["detail"]


def test_open_404_for_unknown_pid(client, clean_state, mocker):
    mocker.patch("periscope.routes.editor.get_settings", return_value={"editor": "Cursor"})
    mocker.patch("periscope.routes.editor.list_windows", return_value=[_window("@7")])
    r = client.post("/api/editor/open", json={"pid": "@nope"})
    assert r.status_code == 404


def test_open_400_when_pane_not_in_a_repo(client, clean_state, mocker, tmp_path):
    mocker.patch("periscope.routes.editor.get_settings", return_value={"editor": "Cursor"})
    mocker.patch(
        "periscope.routes.editor.list_windows",
        return_value=[_window(cwd=str(tmp_path))],
    )
    r = client.post("/api/editor/open", json={"pid": "@7"})
    assert r.status_code == 400
    assert "not inside a git repo" in r.json()["detail"]


def test_open_launches_the_worktree_root_not_the_cwd(client, clean_state, mocker, tmp_path):
    """The whole point of the feature: a pane sitting in a subdirectory still
    opens the repo root, so the editor gets the whole project."""
    root = tmp_path / "wt"
    (root / ".git").mkdir(parents=True)
    deep = root / "workers" / "model_train"
    deep.mkdir(parents=True)

    mocker.patch("periscope.routes.editor.get_settings", return_value={"editor": "Cursor"})
    mocker.patch(
        "periscope.routes.editor.list_windows", return_value=[_window(cwd=str(deep))],
    )
    launch = mocker.patch("periscope.routes.editor.open_in_editor")

    r = client.post("/api/editor/open", json={"pid": "@7"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["editor"] == "Cursor"
    assert body["path"] == str(root.resolve())
    launch.assert_called_once_with("Cursor", str(root.resolve()))


def test_open_500_surfaces_a_launch_failure(client, clean_state, mocker, tmp_path):
    root = tmp_path / "wt"
    (root / ".git").mkdir(parents=True)
    mocker.patch("periscope.routes.editor.get_settings", return_value={"editor": "Cursor"})
    mocker.patch(
        "periscope.routes.editor.list_windows", return_value=[_window(cwd=str(root))],
    )
    mocker.patch(
        "periscope.routes.editor.open_in_editor",
        side_effect=ValueError("app is damaged"),
    )
    r = client.post("/api/editor/open", json={"pid": "@7"})
    assert r.status_code == 500
    assert "app is damaged" in r.json()["detail"]


def test_open_422_without_a_pid(client, clean_state):
    assert client.post("/api/editor/open", json={}).status_code == 422
