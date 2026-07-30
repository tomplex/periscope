import json
import stat
from pathlib import Path

import pytest

from periscope import codex_hook_config


def test_install_is_idempotent_and_preserves_content_and_mode(tmp_path, monkeypatch):
    home = tmp_path / "codex"
    monkeypatch.setenv("CODEX_HOME", str(home))
    path = home / "hooks.json"
    path.parent.mkdir()
    original = {
        "unknown": {"keep": True},
        "hooks": {
            "SessionStart": [{
                "matcher": "other",
                "description": "keep",
                "hooks": [{"type": "command", "command": "other", "timeout": 99}],
            }]
        },
    }
    path.write_text(json.dumps(original))
    path.chmod(0o640)
    _, first = codex_hook_config.update(tmp_path, install=True)
    first_bytes = path.read_bytes()
    _, second = codex_hook_config.update(tmp_path, install=True)
    assert first == list(codex_hook_config.EVENTS)
    assert second == []
    assert path.read_bytes() == first_bytes
    data = json.loads(first_bytes)
    assert data["unknown"] == original["unknown"]
    assert data["hooks"]["SessionStart"][0] == original["hooks"]["SessionStart"][0]
    assert stat.S_IMODE(path.stat().st_mode) == 0o640


def test_uninstall_removes_only_exact_owned_groups(tmp_path, monkeypatch):
    home = tmp_path / "codex"
    monkeypatch.setenv("CODEX_HOME", str(home))
    codex_hook_config.update(tmp_path, install=True)
    path = home / "hooks.json"
    data = json.loads(path.read_text())
    data["hooks"]["Stop"].append({
        "matcher": "",
        "hooks": [{
            "type": "command",
            "command": codex_hook_config.command_for(tmp_path) + " --different",
            "timeout": 5,
        }],
    })
    path.write_text(json.dumps(data))
    _, changed = codex_hook_config.update(tmp_path, install=False)
    assert changed == list(codex_hook_config.EVENTS)
    remaining = json.loads(path.read_text())
    assert remaining["hooks"]["Stop"][0]["hooks"][0]["command"].endswith(
        "--different"
    )


def test_malformed_json_is_not_modified(tmp_path, monkeypatch):
    home = tmp_path / "codex"
    monkeypatch.setenv("CODEX_HOME", str(home))
    path = home / "hooks.json"
    path.parent.mkdir()
    path.write_text("{broken")
    before = path.read_bytes()
    with pytest.raises(json.JSONDecodeError):
        codex_hook_config.update(tmp_path, install=True)
    assert path.read_bytes() == before


def test_codex_home_empty_uses_safe_default(monkeypatch):
    monkeypatch.setenv("CODEX_HOME", "")
    assert codex_hook_config.codex_home() == Path.home() / ".codex"
