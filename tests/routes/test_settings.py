"""Tests for /api/settings."""

import pytest


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
    r = client.patch(
        "/api/settings",
        json={
            "worktree_layout_overrides": {"/foo": "sibling", "/bar": "bogus"},
        },
    )
    assert r.status_code == 400
    assert "/bar" in r.json()["detail"]


def test_patch_overrides_replaces_wholesale(client, mocker):
    update_spy = mocker.patch("periscope.routes.settings.update_settings")
    mocker.patch("periscope.routes.settings.get_settings", return_value={})
    r = client.patch(
        "/api/settings",
        json={
            "worktree_layout_overrides": {"/foo": "sibling"},
        },
    )
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


def test_patch_spawn_model_accepts_a_model_id(client, mocker):
    update_spy = mocker.patch("periscope.routes.settings.update_settings")
    mocker.patch("periscope.routes.settings.get_settings", return_value={})
    r = client.patch("/api/settings", json={"spawn_model": "opus"})
    assert r.status_code == 200
    update_spy.assert_called_once_with({"spawn_model": "opus"})


def test_patch_spawn_model_rejects_non_model_string(client, mocker):
    r = client.patch("/api/settings", json={"spawn_model": "opus && rm"})
    assert r.status_code == 400


def test_patch_spawn_model_stores_auto_and_default_literally_and_null_clears(client, mocker):
    # "default" (no override) and "auto" (chooser decides) are now different
    # launches; coercing either to unset would silently turn one into the other.
    update_spy = mocker.patch("periscope.routes.settings.update_settings")
    mocker.patch("periscope.routes.settings.get_settings", return_value={})
    assert client.patch("/api/settings", json={"spawn_model": "default"}).status_code == 200
    assert client.patch("/api/settings", json={"spawn_model": "auto"}).status_code == 200
    assert client.patch("/api/settings", json={"spawn_model": None}).status_code == 200
    assert [c.args[0] for c in update_spy.call_args_list] == [
        {"spawn_model": "default"},
        {"spawn_model": "auto"},
        {"spawn_model": None},
    ]


def test_patch_spawn_effort_accepts_levels_default_and_null(client, mocker):
    update_spy = mocker.patch("periscope.routes.settings.update_settings")
    mocker.patch("periscope.routes.settings.get_settings", return_value={})
    for v in ("high", "max", "default", None):
        assert client.patch("/api/settings", json={"spawn_effort": v}).status_code == 200
    assert [c.args[0] for c in update_spy.call_args_list] == [
        {"spawn_effort": "high"},
        {"spawn_effort": "max"},
        {"spawn_effort": "default"},
        {"spawn_effort": None},
    ]


def test_patch_spawn_effort_rejects_unknown_level(client, mocker):
    update_spy = mocker.patch("periscope.routes.settings.update_settings")
    r = client.patch("/api/settings", json={"spawn_effort": "ultracode"})
    assert r.status_code == 400
    assert "spawn_effort" in r.json()["detail"]
    update_spy.assert_not_called()


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


def test_patch_poke_at_accepts_hhmm_empty_and_null(client, mocker):
    update_spy = mocker.patch("periscope.routes.settings.update_settings")
    mocker.patch("periscope.routes.settings.get_settings", return_value={})
    assert client.patch("/api/settings", json={"poke_at": "07:30"}).status_code == 200
    assert client.patch("/api/settings", json={"poke_at": ""}).status_code == 200  # disabled
    assert client.patch("/api/settings", json={"poke_at": None}).status_code == 200  # back to the default
    assert [c.args[0] for c in update_spy.call_args_list] == [
        {"poke_at": "07:30"},
        {"poke_at": ""},
        {"poke_at": None},
    ]


def test_patch_poke_at_rejects_a_non_clock(client, mocker):
    for bad in ("8am", "25:00", "08:60", "8:00"):
        assert client.patch("/api/settings", json={"poke_at": bad}).status_code == 400, bad


def test_patch_poke_grace_min_bounds(client, mocker):
    update_spy = mocker.patch("periscope.routes.settings.update_settings")
    mocker.patch("periscope.routes.settings.get_settings", return_value={})
    assert client.patch("/api/settings", json={"poke_grace_min": 45}).status_code == 200
    assert client.patch("/api/settings", json={"poke_grace_min": 0}).status_code == 400
    assert client.patch("/api/settings", json={"poke_grace_min": 721}).status_code == 400
    update_spy.assert_called_once_with({"poke_grace_min": 45})


def test_patch_repo_roots_normalizes_and_validates(client, mocker, tmp_path):
    update_spy = mocker.patch("periscope.routes.settings.update_settings")
    mocker.patch("periscope.routes.settings.get_settings", return_value={})
    a = tmp_path / "a"
    a.mkdir()
    r = client.patch("/api/settings", json={"repo_roots": [f"{a}/", "", str(a), "~"]})
    assert r.status_code == 200, r.text
    # Trailing slash stripped, blanks dropped, duplicates collapsed, `~` kept
    # as typed (expanded at read, store.repo_roots).
    update_spy.assert_called_once_with({"repo_roots": [str(a), "~"]})


def test_patch_repo_roots_rejects_a_non_directory(client, mocker, tmp_path):
    update_spy = mocker.patch("periscope.routes.settings.update_settings")
    r = client.patch("/api/settings", json={"repo_roots": [str(tmp_path / "nope")]})
    assert r.status_code == 400
    assert "not a directory" in r.json()["detail"]
    update_spy.assert_not_called()


def test_patch_repo_roots_null_clears(client, mocker):
    update_spy = mocker.patch("periscope.routes.settings.update_settings")
    mocker.patch("periscope.routes.settings.get_settings", return_value={})
    r = client.patch("/api/settings", json={"repo_roots": None})
    assert r.status_code == 200
    update_spy.assert_called_once_with({"repo_roots": None})


def test_patch_linear_team_keys_uppercases_and_dedupes(client, mocker):
    # THI-441: only these keys are read as tickets off a branch or PR title.
    update_spy = mocker.patch("periscope.routes.settings.update_settings")
    mocker.patch("periscope.routes.settings.get_settings", return_value={})
    r = client.patch("/api/settings", json={"linear_team_keys": ["fdy", " THI ", "", "FDY"]})
    assert r.status_code == 200, r.text
    update_spy.assert_called_once_with({"linear_team_keys": ["FDY", "THI"]})


def test_patch_linear_team_keys_rejects_a_non_key(client, mocker):
    update_spy = mocker.patch("periscope.routes.settings.update_settings")
    r = client.patch("/api/settings", json={"linear_team_keys": ["FDY-1"]})
    assert r.status_code == 400
    assert "team key" in r.json()["detail"]
    update_spy.assert_not_called()


def test_patch_linear_team_keys_null_clears(client, mocker):
    update_spy = mocker.patch("periscope.routes.settings.update_settings")
    mocker.patch("periscope.routes.settings.get_settings", return_value={})
    r = client.patch("/api/settings", json={"linear_team_keys": None})
    assert r.status_code == 200
    update_spy.assert_called_once_with({"linear_team_keys": None})


@pytest.mark.parametrize("days", [1, 3, 30])
def test_patch_idle_close_weekdays_takes_1_to_30(client, clean_state, days):
    assert client.patch("/api/settings", json={"idle_close_weekdays": days}).status_code == 200
    assert clean_state["settings"]["idle_close_weekdays"] == days


@pytest.mark.parametrize("days", [0, 31])
def test_patch_idle_close_weekdays_refuses_a_number_out_of_range(client, clean_state, days):
    assert client.patch("/api/settings", json={"idle_close_weekdays": days}).status_code == 400
    assert "idle_close_weekdays" not in clean_state["settings"]


# A lax int reads `true` as 1, the most aggressive value.
@pytest.mark.parametrize("days", [True, "3", 3.5])
def test_patch_idle_close_weekdays_refuses_anything_but_an_integer(client, clean_state, days):
    assert client.patch("/api/settings", json={"idle_close_weekdays": days}).status_code == 422
    assert "idle_close_weekdays" not in clean_state["settings"]


def test_patch_idle_close_weekdays_null_turns_it_off(client, clean_state):
    clean_state["settings"]["idle_close_weekdays"] = 3
    assert client.patch("/api/settings", json={"idle_close_weekdays": None}).status_code == 200
    assert "idle_close_weekdays" not in clean_state["settings"]
