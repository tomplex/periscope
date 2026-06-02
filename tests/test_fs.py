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


def test_safe_read_dotdot_escape_blocked(tmp_path):
    outside_root = tmp_path / "outside"
    outside_root.mkdir()
    (outside_root / "secret").write_text("nope")
    cwd = tmp_path / "cwd"
    cwd.mkdir()
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


def test_safe_read_prefix_confusion_guard(tmp_path):
    # /tmp/a/cwd as the safe root must NOT permit /tmp/a/cwd-sibling/...
    cwd = tmp_path / "a" / "cwd"
    cwd.mkdir(parents=True)
    sibling = tmp_path / "a" / "cwd-sibling"
    sibling.mkdir()
    (sibling / "secret").write_text("nope")
    with pytest.raises(HTTPException) as exc:
        fs.safe_read(str(cwd), str(sibling / "secret"))
    assert exc.value.status_code == 403
