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
