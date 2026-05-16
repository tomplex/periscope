"""Lifespan startup + shutdown wiring + route registration."""

from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_app_construction_imports_cleanly():
    from periscope.app import app
    assert isinstance(app, FastAPI)


def test_app_includes_state_router():
    from periscope.app import app
    paths = [r.path for r in app.routes if hasattr(r, "path")]
    assert "/api/state" in paths


def test_app_includes_prefs_router():
    from periscope.app import app
    paths = [r.path for r in app.routes if hasattr(r, "path")]
    assert "/api/prefs" in paths


def test_app_includes_ws_router():
    from periscope.app import app
    paths = [r.path for r in app.routes if hasattr(r, "path")]
    assert "/ws/pane" in paths


def test_app_mounts_static_files():
    """The catch-all `app.mount("/", StaticFiles(...))` is registered last."""
    from periscope.app import app
    # Last route is the static mount.
    static_mounts = [r for r in app.routes if r.__class__.__name__ == "Mount"]
    assert len(static_mounts) >= 1


def test_lifespan_starts_and_shuts_down_cleanly(mocker):
    """TestClient triggers lifespan startup on enter, shutdown on exit.

    Mock the heavyweight prewarms + async loops so the test is fast and
    doesn't bind a real unix socket. _lgtm_periodic_refresh and
    _mcp_listener are async functions wrapped via _task(coro, name) — they
    must return a coroutine, so we patch with a coroutine factory rather
    than return_value=None (which would make _task crash on
    asyncio.create_task(None)).
    """
    mocker.patch("periscope.app.prewarm_pr_cache")
    mocker.patch("periscope.app.cached_scraped_usage")
    mocker.patch("periscope.app.kill_orphan_usage_sessions")

    async def _noop():
        return None
    mocker.patch("periscope.app._lgtm_periodic_refresh", side_effect=_noop)
    mocker.patch("periscope.app._mcp_listener", side_effect=_noop)

    from periscope.app import app
    with TestClient(app) as client:
        r = client.get("/api/state")
        assert r.status_code == 200
