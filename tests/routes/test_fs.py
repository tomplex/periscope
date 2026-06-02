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
