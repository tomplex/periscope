"""state.json: load/write atomicity + migration idempotency.

Tests redirect XDG_CONFIG_HOME so they don't touch ~/.config/periscope.
Symbol-level tests work via `from periscope.store import ...` because `_state_path`
reads `os.environ["XDG_CONFIG_HOME"]` at call-time (not import-time),
so the tmp_xdg_home monkeypatch takes effect when the function runs.
"""

import json
from pathlib import Path


def test_state_path_under_xdg(tmp_xdg_home: Path):
    from periscope.store import _state_path
    assert _state_path() == tmp_xdg_home / "periscope" / "state.json"


def test_load_state_returns_defaults_when_file_missing(tmp_xdg_home: Path):
    from periscope.store import _load_state
    data = _load_state()
    assert data == {"version": 1, "ui": {}, "windows": {}, "commands": []}


def test_load_state_fills_missing_defaults(tmp_xdg_home: Path):
    """An older state.json missing newer keys should get the defaults
    merged in without losing existing data."""
    from periscope.store import _load_state, _state_path
    path = _state_path()
    path.parent.mkdir(parents=True)
    path.write_text('{"version": 1, "ui": {"theme": "dark"}}')
    data = _load_state()
    assert data["version"] == 1
    assert data["ui"] == {"theme": "dark"}
    assert data["windows"] == {}
    assert data["commands"] == []


def test_load_state_renames_corrupt_file(tmp_xdg_home: Path):
    from periscope.store import _load_state, _state_path
    path = _state_path()
    path.parent.mkdir(parents=True)
    path.write_text("{not valid json")
    data = _load_state()
    assert data == {"version": 1, "ui": {}, "windows": {}, "commands": []}
    corrupts = list(path.parent.glob("state.json.corrupt-*"))
    assert len(corrupts) == 1


def test_write_state_writes_atomically(tmp_xdg_home: Path):
    from periscope.store import _write_state, _state_path
    payload = {"version": 1, "ui": {"x": 1}, "windows": {}, "commands": []}
    _write_state(payload)
    assert json.loads(_state_path().read_text()) == payload
    assert not list(_state_path().parent.glob("state.json.tmp"))


def test_write_state_creates_parent_dir(tmp_xdg_home: Path):
    from periscope.store import _write_state, _state_path
    assert not _state_path().parent.exists()
    _write_state({"version": 1, "ui": {}, "windows": {}, "commands": []})
    assert _state_path().parent.is_dir()


def test_seed_commands_fills_empty_list(tmp_xdg_home: Path, monkeypatch):
    """When _STATE.commands is empty, the seed adds the three defaults."""
    import periscope.store as store
    monkeypatch.setattr(store, "_STATE", {
        "version": 1, "ui": {}, "windows": {}, "commands": [],
    })
    store._seed_commands_if_empty()
    labels = [c["label"] for c in store._STATE["commands"]]
    assert labels == ["claude", "shell", "vim"]


def test_seed_commands_noop_when_nonempty(tmp_xdg_home: Path, monkeypatch):
    import periscope.store as store
    existing = [{"label": "custom", "exec": "my-cmd"}]
    monkeypatch.setattr(store, "_STATE", {
        "version": 1, "ui": {}, "windows": {}, "commands": existing,
    })
    store._seed_commands_if_empty()
    assert store._STATE["commands"] == existing


def test_channels_migration_v1_rewrites_claude_exec(tmp_xdg_home: Path, monkeypatch):
    import periscope.store as store
    monkeypatch.setattr(store, "_STATE", {
        "version": 1, "ui": {}, "windows": {},
        "commands": [
            {"label": "claude", "exec": "claude"},
            {"label": "shell", "exec": ""},
        ],
    })
    store._channels_migration_v1()
    assert store._STATE["commands"][0]["exec"] == (
        "claude --dangerously-load-development-channels server:periscope"
    )
    assert store._STATE["channels_migration_v1_done"] is True


def test_channels_migration_v1_is_idempotent(tmp_xdg_home: Path, monkeypatch):
    import periscope.store as store
    monkeypatch.setattr(store, "_STATE", {
        "version": 1, "ui": {}, "windows": {},
        "commands": [{"label": "claude", "exec": "claude"}],
        "channels_migration_v1_done": True,
    })
    store._channels_migration_v1()
    assert store._STATE["commands"][0]["exec"] == "claude"


# ===== Typed accessors =====================================================
# These cover the new API surface; the underlying _STATE / _STATE_LOCK /
# _write_state primitives are exercised by the migration + load/write tests
# above.


def test_set_window_fields_creates_entry_and_persists(clean_state):
    from periscope.store import set_window_fields, get_window
    set_window_fields("abc", linked_pr=1234, alias="my-pane")
    assert get_window("abc") == {"linked_pr": 1234, "alias": "my-pane"}
    assert clean_state["windows"]["abc"] == {"linked_pr": 1234, "alias": "my-pane"}


def test_set_window_fields_merges_existing(clean_state):
    from periscope.store import set_window_fields, get_window
    set_window_fields("abc", linked_pr=1234)
    set_window_fields("abc", alias="renamed")
    out = get_window("abc")
    assert out == {"linked_pr": 1234, "alias": "renamed"}


def test_get_window_returns_copy_not_reference(clean_state):
    from periscope.store import set_window_fields, get_window
    set_window_fields("abc", alias="x")
    snapshot = get_window("abc")
    snapshot["alias"] = "MUTATED"
    assert get_window("abc")["alias"] == "x"


def test_get_window_unknown_pid_returns_empty(clean_state):
    from periscope.store import get_window
    assert get_window("nonexistent") == {}


def test_set_window_fields_bulk_skips_writes_when_nothing_changed(clean_state, mocker):
    """If the bulk update would set fields to their existing values,
    don't call _write_state."""
    from periscope.store import set_window_fields, set_window_fields_bulk
    set_window_fields("abc", completed_at=100)
    mock_write = mocker.patch("periscope.store._write_state")
    dirty = set_window_fields_bulk({"abc": {"completed_at": 100}})
    assert dirty == 0
    mock_write.assert_not_called()


def test_set_window_fields_bulk_writes_once_for_many_pids(clean_state, mocker):
    from periscope.store import set_window_fields_bulk
    mock_write = mocker.patch("periscope.store._write_state")
    dirty = set_window_fields_bulk({
        "a": {"completed_at": 1},
        "b": {"completed_at": 2},
        "c": {"acked_at": 3},
    })
    assert dirty == 3
    assert mock_write.call_count == 1


def test_delete_window_returns_false_when_absent(clean_state):
    from periscope.store import delete_window
    assert delete_window("nope") is False


def test_delete_window_removes_and_persists(clean_state):
    from periscope.store import set_window_fields, delete_window, get_window
    set_window_fields("abc", alias="x")
    assert delete_window("abc") is True
    assert get_window("abc") == {}


def test_all_windows_returns_copy(clean_state):
    from periscope.store import set_window_fields, all_windows
    set_window_fields("abc", linked_pr=1)
    set_window_fields("xyz", linked_pr=2)
    out = all_windows()
    out["abc"]["linked_pr"] = 999
    assert all_windows()["abc"]["linked_pr"] == 1


def test_update_ui_merges_and_deletes_none(clean_state):
    from periscope.store import update_ui, get_ui
    update_ui({"theme": "dark", "view": "grid"})
    assert get_ui() == {"theme": "dark", "view": "grid"}
    update_ui({"theme": None})  # deletes
    assert get_ui() == {"view": "grid"}


def test_add_command_appends(clean_state):
    from periscope.store import add_command, get_commands
    initial = len(get_commands())
    new = add_command("custom", "my-cmd")
    assert new == {"label": "custom", "exec": "my-cmd"}
    assert get_commands()[-1] == new
    assert len(get_commands()) == initial + 1


def test_update_command_returns_false_when_label_absent(clean_state):
    from periscope.store import update_command
    assert update_command("nope", exec_cmd="x") is False


def test_update_command_changes_exec_and_label(clean_state):
    from periscope.store import add_command, update_command, get_commands
    add_command("a", "x")
    assert update_command("a", exec_cmd="y", new_label="b") is True
    out = get_commands()
    assert out[-1] == {"label": "b", "exec": "y"}


def test_delete_command_removes_match(clean_state):
    from periscope.store import add_command, delete_command, get_commands
    add_command("temp", "rm")
    assert delete_command("temp") is True
    assert "temp" not in [c["label"] for c in get_commands()]


def test_delete_command_returns_false_when_absent(clean_state):
    from periscope.store import delete_command
    assert delete_command("nope") is False


def test_reorder_commands_uses_given_sequence_then_leftover(clean_state):
    from periscope.store import add_command, reorder_commands, get_commands
    add_command("a", "1")
    add_command("b", "2")
    add_command("c", "3")
    reorder_commands(["c", "a"])
    labels = [cmd["label"] for cmd in get_commands()]
    # c, a, then b (leftover) appears at the end.
    assert labels[-3:] == ["c", "a", "b"]


def test_snapshot_returns_deep_copy(clean_state):
    from periscope.store import set_window_fields, update_ui, snapshot
    set_window_fields("abc", linked_pr=1)
    update_ui({"theme": "dark"})
    snap = snapshot()
    snap["windows"]["abc"]["linked_pr"] = 999
    snap["ui"]["theme"] = "light"
    from periscope.store import get_window, get_ui
    assert get_window("abc")["linked_pr"] == 1
    assert get_ui()["theme"] == "dark"


def test_set_window_fields_none_deletes_key(clean_state):
    from periscope.store import set_window_fields, get_window
    set_window_fields("abc", notes="hello", tags=["a", "b"])
    assert get_window("abc") == {"notes": "hello", "tags": ["a", "b"]}
    set_window_fields("abc", notes=None)
    assert get_window("abc") == {"tags": ["a", "b"]}
