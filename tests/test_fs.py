"""Pure unit tests for periscope.fs.safe_read / safe_reveal.

The tmux-resolving variants (safe_read_for_pane / safe_reveal_for_pane)
are tested in tests/routes/test_fs.py against route-level fixtures.
"""
import os
from pathlib import Path

import pytest
from fastapi import HTTPException

from periscope import fs


def test_safe_read_absolute_inside_cwd(tmp_path):
    f = tmp_path / "hello.txt"
    f.write_text("hi there\n")
    resolved, contents = fs.safe_read(str(tmp_path), str(f))
    assert resolved == str(f.resolve())
    assert contents == "hi there\n"


def test_safe_read_relative_against_cwd(tmp_path):
    (tmp_path / "sub").mkdir()
    f = tmp_path / "sub" / "file.py"
    f.write_text("print('ok')\n")
    resolved, contents = fs.safe_read(str(tmp_path), "sub/file.py")
    assert resolved == str(f.resolve())
    assert contents == "print('ok')\n"


def test_safe_read_tilde_expansion(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    f = tmp_path / "rc"
    f.write_text("x\n")
    resolved, contents = fs.safe_read(str(tmp_path), "~/rc")
    assert resolved == str(f.resolve())
    assert contents == "x\n"


def test_safe_read_dotdot_escape_blocked(tmp_path, monkeypatch):
    outside_root = tmp_path / "outside"
    outside_root.mkdir()
    (outside_root / "secret").write_text("nope")
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    # Pin the safe roots to just cwd. The real policy also allows /tmp + ~, and
    # pytest's tmp_path lives under /tmp — so without this the "escape" target
    # is legitimately inside a safe root. Isolates the escape-enforcement guard
    # from the ambient root policy.
    monkeypatch.setattr(fs, "_safe_roots", lambda c: [Path(c).resolve()])
    with pytest.raises(HTTPException) as exc:
        fs.safe_read(str(cwd), "../outside/secret")
    assert exc.value.status_code == 403


def test_safe_read_missing_file(tmp_path):
    with pytest.raises(HTTPException) as exc:
        fs.safe_read(str(tmp_path), "no-such-file")
    assert exc.value.status_code == 404


def test_safe_read_oversize(tmp_path):
    f = tmp_path / "big.txt"
    f.write_text("x" * 1024)
    with pytest.raises(HTTPException) as exc:
        fs.safe_read(str(tmp_path), str(f), max_bytes=128)
    assert exc.value.status_code == 413


def test_safe_read_binary_file(tmp_path):
    f = tmp_path / "icon.png"
    f.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
    with pytest.raises(HTTPException) as exc:
        fs.safe_read(str(tmp_path), str(f))
    assert exc.value.status_code == 415


def test_safe_read_empty_path(tmp_path):
    with pytest.raises(HTTPException) as exc:
        fs.safe_read(str(tmp_path), "")
    assert exc.value.status_code == 400


def test_safe_read_prefix_confusion_guard(tmp_path, monkeypatch):
    # cwd `.../a/cwd` as the sole safe root must NOT permit `.../a/cwd-sibling`,
    # even though str(sibling).startswith(str(cwd)) is True — this is exactly
    # why _inside_any uses commonpath, not startswith. Pin roots to cwd so the
    # ambient /tmp root doesn't mask the prefix-confusion check.
    cwd = tmp_path / "a" / "cwd"
    cwd.mkdir(parents=True)
    sibling = tmp_path / "a" / "cwd-sibling"
    sibling.mkdir()
    (sibling / "secret").write_text("nope")
    monkeypatch.setattr(fs, "_safe_roots", lambda c: [Path(c).resolve()])
    with pytest.raises(HTTPException) as exc:
        fs.safe_read(str(cwd), str(sibling / "secret"))
    assert exc.value.status_code == 403


def test_safe_read_for_pane_uses_tmux_cwd(tmp_path, monkeypatch):
    f = tmp_path / "x.txt"
    f.write_text("ok\n")

    def fake_tmux(*args):
        # display-message resolves the pane's cwd
        if args[:1] == ("display-message",):
            return str(tmp_path) + "\n"
        raise AssertionError(f"unexpected tmux call: {args}")

    monkeypatch.setattr("periscope.fs.tmux", fake_tmux)
    resolved, contents = fs.safe_read_for_pane("sess:1", "x.txt")
    assert contents == "ok\n"
    assert resolved == str(f.resolve())


def test_safe_read_for_pane_404_on_empty_tmux_output(monkeypatch):
    # `tmux()` doesn't raise on non-zero exit — returns "" instead
    # (verified at periscope/tmux.py:21-25). Production 404 path is the
    # `if not out:` branch in _cwd_for_target, not the except.
    monkeypatch.setattr("periscope.fs.tmux", lambda *a: "")
    with pytest.raises(HTTPException) as exc:
        fs.safe_read_for_pane("sess:1", "x.txt")
    assert exc.value.status_code == 404


def test_safe_read_for_pane_404_when_tmux_binary_missing(monkeypatch):
    # Defensive: if tmux itself can't run (binary not found, timeout), the
    # except branch still gives 404 — that's the safety net.
    def boom(*a):
        raise FileNotFoundError("tmux")
    monkeypatch.setattr("periscope.fs.tmux", boom)
    with pytest.raises(HTTPException) as exc:
        fs.safe_read_for_pane("sess:1", "x.txt")
    assert exc.value.status_code == 404


def test_safe_reveal_for_pane_invokes_open_R(tmp_path, monkeypatch):
    f = tmp_path / "x.txt"
    f.write_text("ok\n")
    called = []

    def fake_tmux(*args):
        if args[:1] == ("display-message",):
            return str(tmp_path) + "\n"
        raise AssertionError(f"unexpected tmux call: {args}")

    def fake_run(cmd, **kw):
        called.append(cmd)
    monkeypatch.setattr("periscope.fs.tmux", fake_tmux)
    monkeypatch.setattr("periscope.fs.subprocess.run", fake_run)
    fs.safe_reveal_for_pane("sess:1", "x.txt")
    assert called == [["open", "-R", str(f.resolve())]]


def test_safe_resolve_happy(tmp_path):
    f = tmp_path / "hi.html"
    f.write_text("<h1>hi</h1>")
    resolved = fs.safe_resolve(str(tmp_path), "hi.html")
    assert str(resolved) == str(f.resolve())


def test_safe_resolve_blocks_dotdot(tmp_path, monkeypatch):
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside" / "secret.html").write_text("nope")
    (tmp_path / "cwd").mkdir()
    # Pin roots to cwd (see test_safe_read_dotdot_escape_blocked).
    monkeypatch.setattr(fs, "_safe_roots", lambda c: [Path(c).resolve()])
    with pytest.raises(HTTPException) as exc:
        fs.safe_resolve(str(tmp_path / "cwd"), "../outside/secret.html")
    assert exc.value.status_code == 403


def test_safe_resolve_missing(tmp_path):
    with pytest.raises(HTTPException) as exc:
        fs.safe_resolve(str(tmp_path), "no.html")
    assert exc.value.status_code == 404


def test_safe_resolve_doesnt_cap_size(tmp_path):
    # safe_read caps at 1MB; safe_resolve must NOT — it's used to stream
    # large assets (images, bundled JS) via FileResponse.
    big = tmp_path / "big.bin"
    big.write_bytes(b"x" * (2 * 1024 * 1024))
    resolved = fs.safe_resolve(str(tmp_path), "big.bin")
    assert str(resolved) == str(big.resolve())
