import subprocess

import pytest

from periscope import editors


def _fake_apps(tmp_path, *bundles):
    """Build a fake /Applications and point detection at it."""
    d = tmp_path / "Applications"
    d.mkdir()
    for b in bundles:
        (d / b).mkdir()
    return str(d)


def test_detect_editors_finds_installed(monkeypatch, tmp_path):
    apps = _fake_apps(tmp_path, "Cursor.app", "Sublime Text.app")
    monkeypatch.setattr(editors, "_APP_DIRS", (apps,))
    assert editors.detect_editors() == ["Cursor", "Sublime Text"]


def test_detect_editors_preserves_known_order(monkeypatch, tmp_path):
    """Order is KNOWN_EDITORS order, not filesystem order — the first entry
    is what a fresh install would reasonably default to."""
    apps = _fake_apps(tmp_path, "Sublime Text.app", "Cursor.app", "Zed.app")
    monkeypatch.setattr(editors, "_APP_DIRS", (apps,))
    assert editors.detect_editors() == ["Cursor", "Zed", "Sublime Text"]


def test_detect_editors_empty_when_none_installed(monkeypatch, tmp_path):
    monkeypatch.setattr(editors, "_APP_DIRS", (_fake_apps(tmp_path),))
    assert editors.detect_editors() == []


def test_detect_editors_ignores_a_non_editor_app(monkeypatch, tmp_path):
    apps = _fake_apps(tmp_path, "Calculator.app")
    monkeypatch.setattr(editors, "_APP_DIRS", (apps,))
    assert editors.detect_editors() == []


def test_open_in_editor_rejects_undetected_app(monkeypatch, tmp_path):
    """The membership check is the security boundary: `app` comes from the
    settings block, which the client can PATCH. Anything not actually
    installed must never reach subprocess."""
    monkeypatch.setattr(editors, "_APP_DIRS", (_fake_apps(tmp_path, "Cursor.app"),))
    called = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: called.append(a))
    with pytest.raises(ValueError, match="not an available editor"):
        editors.open_in_editor("rm -rf /", "/tmp")
    assert called == []


def test_open_in_editor_argv_is_a_list_never_a_shell(monkeypatch, tmp_path):
    monkeypatch.setattr(editors, "_APP_DIRS", (_fake_apps(tmp_path, "Cursor.app"),))
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen["kwargs"] = kwargs
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    editors.open_in_editor("Cursor", "/repo/with a space")
    assert seen["argv"] == ["open", "-a", "Cursor", "/repo/with a space"]
    assert "shell" not in seen["kwargs"]      # never shell=True


def test_open_in_editor_raises_on_launch_failure(monkeypatch, tmp_path):
    """Unlike fs.safe_reveal's best-effort Finder reveal, a failed launch is
    surfaced — the user clicked expecting a window."""
    monkeypatch.setattr(editors, "_APP_DIRS", (_fake_apps(tmp_path, "Cursor.app"),))
    monkeypatch.setattr(
        subprocess, "run",
        lambda argv, **k: subprocess.CompletedProcess(argv, 1, "", "app is damaged"),
    )
    with pytest.raises(ValueError, match="app is damaged"):
        editors.open_in_editor("Cursor", "/repo")


def test_open_in_editor_failure_without_stderr_still_reports(monkeypatch, tmp_path):
    monkeypatch.setattr(editors, "_APP_DIRS", (_fake_apps(tmp_path, "Cursor.app"),))
    monkeypatch.setattr(
        subprocess, "run",
        lambda argv, **k: subprocess.CompletedProcess(argv, 3, "", ""),
    )
    with pytest.raises(ValueError, match="exit 3"):
        editors.open_in_editor("Cursor", "/repo")
