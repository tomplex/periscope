"""Tests for /api/lgtm/start."""

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    from periscope.app import app
    return TestClient(app)


def _patch_lgtm_refresh(mocker):
    """_lgtm_refresh_all is async; patch to a no-op coroutine factory."""
    async def _noop():
        return None
    for path in ("periscope.routes.lgtm._lgtm_refresh_all", "server._lgtm_refresh_all"):
        try:
            mocker.patch(path, side_effect=_noop)
            return
        except (AttributeError, ModuleNotFoundError):
            continue


def test_lgtm_start_happy_path(client, mocker, tmp_path):
    # httpx mock: AsyncClient -> AsyncContextManager -> post -> Response.
    fake_resp = mocker.MagicMock()
    fake_resp.raise_for_status = mocker.MagicMock()
    fake_resp.json = mocker.MagicMock(return_value={"slug": "myproj"})

    async def fake_post(*args, **kwargs):
        return fake_resp

    fake_client = mocker.MagicMock()
    fake_client.post = mocker.AsyncMock(return_value=fake_resp)
    fake_client.__aenter__ = mocker.AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = mocker.AsyncMock(return_value=False)

    mocker.patch("httpx.AsyncClient", return_value=fake_client)
    _patch_lgtm_refresh(mocker)

    r = client.post("/api/lgtm/start", json={"cwd": str(tmp_path)})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["slug"] == "myproj"
    assert "/project/myproj/" in body["url"]


def test_lgtm_start_rejects_bad_cwd(client, mocker):
    _patch_lgtm_refresh(mocker)
    r = client.post("/api/lgtm/start", json={"cwd": "/no/such/dir/exists/here"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert "directory" in body["error"]
