"""Tests for /api/settings."""


def test_get_settings_returns_block(client, mocker):
    mocker.patch(
        "periscope.routes.settings.get_settings",
        return_value={"cleanup_idle_days": 14},
    )
    # editors_available rides along so the modal populates its dropdown from a
    # single request. Derived, never persisted.
    mocker.patch("periscope.routes.settings.detect_editors", return_value=["Cursor"])
    r = client.get("/api/settings")
    assert r.status_code == 200
    assert r.json() == {
        "settings": {"cleanup_idle_days": 14},
        "editors_available": ["Cursor"],
    }


def test_patch_writes_top_level_field(client, mocker):
    update_spy = mocker.patch("periscope.routes.settings.update_settings")
    mocker.patch(
        "periscope.routes.settings.get_settings",
        return_value={"cleanup_idle_days": 30},
    )
    r = client.patch("/api/settings", json={"cleanup_idle_days": 30})
    assert r.status_code == 200
    update_spy.assert_called_once_with({"cleanup_idle_days": 30})


def test_patch_clears_with_null(client, mocker):
    update_spy = mocker.patch("periscope.routes.settings.update_settings")
    mocker.patch("periscope.routes.settings.get_settings", return_value={})
    r = client.patch("/api/settings", json={"cleanup_idle_days": None})
    assert r.status_code == 200
    update_spy.assert_called_once_with({"cleanup_idle_days": None})


def test_patch_rejects_invalid_layout(client, mocker):
    r = client.patch("/api/settings", json={"worktree_layout_default": "bogus"})
    assert r.status_code == 400


def test_patch_rejects_invalid_idle_days(client, mocker):
    r = client.patch("/api/settings", json={"cleanup_idle_days": 0})
    assert r.status_code == 400
    r = client.patch("/api/settings", json={"cleanup_idle_days": -1})
    assert r.status_code == 400


def test_patch_overrides_validates_each(client, mocker):
    r = client.patch("/api/settings", json={
        "worktree_layout_overrides": {"/foo": "sibling", "/bar": "bogus"},
    })
    assert r.status_code == 400
    assert "/bar" in r.json()["detail"]


def test_patch_overrides_replaces_wholesale(client, mocker):
    update_spy = mocker.patch("periscope.routes.settings.update_settings")
    mocker.patch("periscope.routes.settings.get_settings", return_value={})
    r = client.patch("/api/settings", json={
        "worktree_layout_overrides": {"/foo": "sibling"},
    })
    assert r.status_code == 200
    update_spy.assert_called_once_with({"worktree_layout_overrides": {"/foo": "sibling"}})


def test_patch_bg_account_accepts_known_id(client, mocker):
    update_spy = mocker.patch("periscope.routes.settings.update_settings")
    mocker.patch("periscope.routes.settings.get_settings", return_value={})
    r = client.patch("/api/settings", json={"bg_account": "b"})
    assert r.status_code == 200
    update_spy.assert_called_once_with({"bg_account": "b"})


def test_patch_bg_account_rejects_unknown_id(client, mocker):
    r = client.patch("/api/settings", json={"bg_account": "nope"})
    assert r.status_code == 400


def test_patch_bg_account_null_clears(client, mocker):
    update_spy = mocker.patch("periscope.routes.settings.update_settings")
    mocker.patch("periscope.routes.settings.get_settings", return_value={})
    r = client.patch("/api/settings", json={"bg_account": None})
    assert r.status_code == 200
    update_spy.assert_called_once_with({"bg_account": None})


def test_patch_spawn_account_accepts_known_id(client, mocker):
    update_spy = mocker.patch("periscope.routes.settings.update_settings")
    mocker.patch("periscope.routes.settings.get_settings", return_value={})
    r = client.patch("/api/settings", json={"spawn_account": "b"})
    assert r.status_code == 200
    update_spy.assert_called_once_with({"spawn_account": "b"})


def test_patch_spawn_account_rejects_unknown_id(client, mocker):
    r = client.patch("/api/settings", json={"spawn_account": "nope"})
    assert r.status_code == 400


def test_patch_spawn_account_null_clears(client, mocker):
    update_spy = mocker.patch("periscope.routes.settings.update_settings")
    mocker.patch("periscope.routes.settings.get_settings", return_value={})
    r = client.patch("/api/settings", json={"spawn_account": None})
    assert r.status_code == 200
    update_spy.assert_called_once_with({"spawn_account": None})


def test_patch_editor_accepts_a_detected_app(client, mocker):
    update_spy = mocker.patch("periscope.routes.settings.update_settings")
    mocker.patch("periscope.routes.settings.get_settings", return_value={})
    mocker.patch("periscope.routes.settings.detect_editors", return_value=["Cursor"])
    r = client.patch("/api/settings", json={"editor": "Cursor"})
    assert r.status_code == 200
    update_spy.assert_called_once_with({"editor": "Cursor"})


def test_patch_editor_rejects_an_undetected_app(client, mocker):
    """Validate at the write boundary: storing a name that can't launch would
    leave a rail button that fails every single time it is clicked."""
    mocker.patch("periscope.routes.settings.detect_editors", return_value=["Cursor"])
    r = client.patch("/api/settings", json={"editor": "Notepad"})
    assert r.status_code == 400
    assert "not an available editor" in r.json()["detail"]


def test_patch_editor_null_clears(client, mocker):
    update_spy = mocker.patch("periscope.routes.settings.update_settings")
    mocker.patch("periscope.routes.settings.get_settings", return_value={})
    mocker.patch("periscope.routes.settings.detect_editors", return_value=["Cursor"])
    r = client.patch("/api/settings", json={"editor": None})
    assert r.status_code == 200
    update_spy.assert_called_once_with({"editor": None})
