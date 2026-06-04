"""Route tests for /api/fs/read and /api/fs/open. Exercises the
HTTP surface; the safe-path logic itself is tested in tests/test_fs.py."""
from fastapi.testclient import TestClient

from periscope.app import app


def test_fs_read_happy(tmp_path, monkeypatch):
    (tmp_path / "f.py").write_text("print('ok')\n")
    monkeypatch.setattr("periscope.fs.tmux",
                        lambda *a: str(tmp_path) + "\n")
    client = TestClient(app)
    r = client.get("/api/fs/read",
                   params={"session": "s", "index": 1, "path": "f.py"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["content"] == "print('ok')\n"
    assert body["language"] == "python"
    assert body["path"].endswith("/f.py")


def test_fs_read_blank_path(tmp_path, monkeypatch):
    monkeypatch.setattr("periscope.fs.tmux",
                        lambda *a: str(tmp_path) + "\n")
    client = TestClient(app)
    r = client.get("/api/fs/read",
                   params={"session": "s", "index": 1, "path": ""})
    assert r.status_code == 400


def test_fs_read_missing(tmp_path, monkeypatch):
    monkeypatch.setattr("periscope.fs.tmux",
                        lambda *a: str(tmp_path) + "\n")
    client = TestClient(app)
    r = client.get("/api/fs/read",
                   params={"session": "s", "index": 1, "path": "nope.txt"})
    assert r.status_code == 404


def test_fs_read_unknown_pane_empty_tmux(monkeypatch):
    # Production path: tmux returns "" when target doesn't exist; the
    # route must surface 404 (not 500).
    monkeypatch.setattr("periscope.fs.tmux", lambda *a: "")
    client = TestClient(app)
    r = client.get("/api/fs/read",
                   params={"session": "s", "index": 99, "path": "x"})
    assert r.status_code == 404


def test_fs_open_reveal_invokes_subprocess(tmp_path, monkeypatch):
    (tmp_path / "f.py").write_text("x\n")
    called = []
    monkeypatch.setattr("periscope.fs.tmux",
                        lambda *a: str(tmp_path) + "\n")
    monkeypatch.setattr("periscope.fs.subprocess.run",
                        lambda c, **kw: called.append(c))
    client = TestClient(app)
    r = client.post("/api/fs/open",
                    params={"session": "s", "index": 1,
                            "path": "f.py", "action": "reveal"})
    assert r.status_code == 200
    assert called and called[0][:2] == ["open", "-R"]


def test_fs_open_unknown_action(tmp_path, monkeypatch):
    monkeypatch.setattr("periscope.fs.tmux",
                        lambda *a: str(tmp_path) + "\n")
    client = TestClient(app)
    r = client.post("/api/fs/open",
                    params={"session": "s", "index": 1,
                            "path": "x", "action": "edit"})
    assert r.status_code == 400


# ---- /api/fs/render ----

import base64


def _pane_token(target: str) -> str:
    return base64.urlsafe_b64encode(target.encode()).decode().rstrip("=")


def test_fs_render_html(tmp_path, monkeypatch):
    f = tmp_path / "page.html"
    f.write_text("<h1>hi</h1>", encoding="utf-8")
    monkeypatch.setattr("periscope.fs.tmux", lambda *a: str(tmp_path) + "\n")
    client = TestClient(app)
    token = _pane_token("s:1")
    r = client.get(f"/api/fs/render/{token}{f}")
    assert r.status_code == 200, r.text
    assert r.text == "<h1>hi</h1>"
    # mimetypes guesses text/html — accept either form.
    assert r.headers["content-type"].startswith("text/html")


def test_fs_render_sibling_asset(tmp_path, monkeypatch):
    # Simulates the browser following a relative <img src="logo.png">
    # from /api/fs/render/<tok>/abs/path/page.html → /.../abs/path/logo.png.
    (tmp_path / "page.html").write_text("<img src='logo.png'>", encoding="utf-8")
    (tmp_path / "logo.png").write_bytes(b"\x89PNGfake")
    monkeypatch.setattr("periscope.fs.tmux", lambda *a: str(tmp_path) + "\n")
    client = TestClient(app)
    token = _pane_token("s:1")
    r = client.get(f"/api/fs/render/{token}{tmp_path}/logo.png")
    assert r.status_code == 200
    assert r.content == b"\x89PNGfake"
    assert r.headers["content-type"] == "image/png"


def test_fs_render_safe_roots_enforced(tmp_path, monkeypatch):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "leak.html").write_text("nope")
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.setattr("periscope.fs.tmux", lambda *a: str(cwd) + "\n")
    # Pin the safe roots to just cwd. The real policy also allows /tmp + ~, and
    # tmp_path lives under /tmp — pointing HOME away (below) isn't enough; the
    # /tmp root would still admit the escape. This isolates the enforcement path.
    from pathlib import Path
    monkeypatch.setattr("periscope.fs._safe_roots", lambda c: [Path(c).resolve()])
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    client = TestClient(app)
    token = _pane_token("s:1")
    r = client.get(f"/api/fs/render/{token}{outside}/leak.html")
    assert r.status_code == 403


def test_fs_render_invalid_token():
    client = TestClient(app)
    r = client.get("/api/fs/render/not-base64-at-all!/tmp/x.html")
    # urlsafe_b64decode accepts a lot of input — empty-after-decode or
    # missing-colon are the failure modes we surface.
    assert r.status_code == 400


def test_fs_render_token_without_colon():
    # base64url("nocolon") = "bm9jb2xvbg" — decodes to "nocolon" with no ":".
    bad = base64.urlsafe_b64encode(b"nocolon").decode().rstrip("=")
    client = TestClient(app)
    r = client.get(f"/api/fs/render/{bad}/tmp/x.html")
    assert r.status_code == 400


def test_fs_render_oversize(tmp_path, monkeypatch):
    # Patch the cap down so the test doesn't have to write 50MB.
    monkeypatch.setattr("periscope.routes.fs._RENDER_MAX_BYTES", 100)
    (tmp_path / "big.html").write_bytes(b"x" * 200)
    monkeypatch.setattr("periscope.fs.tmux", lambda *a: str(tmp_path) + "\n")
    client = TestClient(app)
    token = _pane_token("s:1")
    r = client.get(f"/api/fs/render/{token}{tmp_path}/big.html")
    assert r.status_code == 413
